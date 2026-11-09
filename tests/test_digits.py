"""Digit recognition, driven by a synthetic bitmap font.

Rendering our own font means the test proves the pipeline (threshold ->
segment -> normalize -> match) rather than proving anything about TDS, which is
the only part that can be verified off Windows.
"""

from __future__ import annotations

import numpy as np
import pytest

from tds_macro.vision.digits import (
    DigitClassifier,
    binarize,
    crop_glyph,
    otsu_threshold,
    read_number,
    resize_nearest,
    segment_glyphs,
    to_gray,
)

# A 3x5 bitmap font: enough shape variety that a broken classifier fails.
FONT: dict[str, list[str]] = {
    "0": ["###", "# #", "# #", "# #", "###"],
    "1": ["  #", "  #", "  #", "  #", "  #"],
    "2": ["###", "  #", "###", "#  ", "###"],
    "3": ["###", "  #", "###", "  #", "###"],
    "4": ["# #", "# #", "###", "  #", "  #"],
    "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"],
    "7": ["###", "  #", "  #", "  #", "  #"],
    "8": ["###", "# #", "###", "# #", "###"],
    "9": ["###", "# #", "###", "  #", "###"],
    "$": [" # ", "###", "## ", " ##", "###"],
    ",": ["   ", "   ", "   ", " # ", "#  "],
}

SCALE = 4
GAP = 3
PAD = 4


def glyph_mask(char: str, scale: int = SCALE) -> np.ndarray:
    rows = FONT[char]
    grid = np.array([[c == "#" for c in row] for row in rows], dtype=bool)
    return np.repeat(np.repeat(grid, scale, axis=0), scale, axis=1)


def render(text: str, scale: int = SCALE, gap: int = GAP, bright: int = 235,
           dark: int = 20, noise: float = 0.0, seed: int = 0) -> np.ndarray:
    """Render text as a BGRA image, the way mss hands a capture back."""
    glyphs = [glyph_mask(c, scale) for c in text]
    height = max(g.shape[0] for g in glyphs) + PAD * 2
    width = sum(g.shape[1] for g in glyphs) + gap * (len(glyphs) - 1) + PAD * 2
    canvas = np.zeros((height, width), dtype=np.float32) + dark
    x = PAD
    for glyph in glyphs:
        region = canvas[PAD : PAD + glyph.shape[0], x : x + glyph.shape[1]]
        region[glyph] = bright
        x += glyph.shape[1] + gap
    if noise:
        canvas += np.random.default_rng(seed).normal(0, noise, canvas.shape)
    canvas = canvas.clip(0, 255).astype(np.uint8)
    return np.dstack([canvas, canvas, canvas, np.full_like(canvas, 255)])


def trained_classifier(scale: int = SCALE) -> DigitClassifier:
    """Mimic the calibration wizard: label one example of each glyph."""
    classifier = DigitClassifier()
    for char in FONT:
        classifier.add(char, glyph_mask(char, scale))
    return classifier


@pytest.fixture
def classifier() -> DigitClassifier:
    return trained_classifier()


@pytest.mark.parametrize("text", ["0", "7", "12", "99", "1250", "100000", "4815162342"])
def test_reads_plain_numbers(classifier: DigitClassifier, text: str) -> None:
    result = read_number(render(text), classifier)
    assert result.value == int(text), result.reason
    assert result.confidence > 0.9


def test_reads_currency_with_separators(classifier: DigitClassifier) -> None:
    result = read_number(render("$1,250"), classifier)
    assert result.value == 1250
    assert result.text == "$1,250"


def test_every_digit_is_distinguishable(classifier: DigitClassifier) -> None:
    """Catches a classifier that collapses similar shapes like 6/8 or 3/9."""
    for digit in "0123456789":
        assert read_number(render(digit), classifier).value == int(digit)


def test_survives_sensor_noise(classifier: DigitClassifier) -> None:
    assert read_number(render("342", noise=18.0), classifier).value == 342


def test_reads_dark_text_on_a_light_background(classifier: DigitClassifier) -> None:
    """Polarity is detected from the border, so no theme setting is needed."""
    assert read_number(render("88", bright=15, dark=240), classifier).value == 88


def test_reads_at_a_different_rendering_scale(classifier: DigitClassifier) -> None:
    """Templates normalize to a fixed glyph size, so a larger HUD still reads."""
    assert read_number(render("57", scale=SCALE * 2), classifier).value == 57


def test_uncalibrated_classifier_says_so_instead_of_guessing() -> None:
    result = read_number(render("12"), DigitClassifier())
    assert result.value is None
    assert "not calibrated" in result.reason


def test_unknown_glyph_is_refused_rather_than_misread() -> None:
    """A wrong wave number would make the engine act early; no answer is safer."""
    weird = np.zeros((20, 12), dtype=bool)
    weird[5:15, 2:4] = True
    weird[5:7, 2:10] = True
    classifier = DigitClassifier(min_confidence=0.95)
    for char in "0123456789":
        classifier.add(char, glyph_mask(char))
    image = np.dstack([weird.astype(np.uint8) * 255] * 3)
    result = read_number(image, classifier)
    assert result.value is None
    assert "matched only" in result.reason


def test_blank_region_reports_no_text(classifier: DigitClassifier) -> None:
    blank = np.full((30, 60, 4), 20, dtype=np.uint8)
    result = read_number(blank, classifier)
    assert result.value is None
    assert "no text" in result.reason


def test_region_with_no_digits_reports_so(classifier: DigitClassifier) -> None:
    result = read_number(render("$"), classifier)
    assert result.value is None
    assert "no digits" in result.reason


def test_empty_capture_is_handled(classifier: DigitClassifier) -> None:
    assert read_number(np.zeros((0, 0, 4), dtype=np.uint8), classifier).value is None


# --- pipeline pieces -----------------------------------------------------------
def test_segmentation_finds_one_box_per_character() -> None:
    mask = binarize(to_gray(render("1234")))
    assert len(segment_glyphs(mask)) == 4


def test_segmentation_bridges_small_holes_but_not_character_gaps() -> None:
    mask = binarize(to_gray(render("11", gap=3)))
    assert len(segment_glyphs(mask, max_gap=2)) == 2


def test_otsu_separates_text_from_background() -> None:
    gray = to_gray(render("8"))
    threshold = otsu_threshold(gray)
    assert 20 < threshold < 235


def test_otsu_on_a_flat_image_does_not_divide_by_zero() -> None:
    assert otsu_threshold(np.full((10, 10), 50.0)) == 50.0


def test_resize_is_shape_stable_for_any_input() -> None:
    assert resize_nearest(np.ones((3, 2), dtype=bool)).shape == (16, 12)
    assert resize_nearest(np.zeros((0, 0), dtype=bool)).shape == (16, 12)


def test_crop_glyph_trims_blank_rows() -> None:
    mask = np.zeros((20, 10), dtype=bool)
    mask[8:12, 2:6] = True
    cropped = crop_glyph(mask, segment_glyphs(mask)[0])
    assert cropped.shape[0] == 4


def test_classifier_serializes_for_storage(classifier: DigitClassifier) -> None:
    restored = DigitClassifier.from_dict(classifier.to_dict())
    assert restored.labels == classifier.labels
    assert read_number(render("909"), restored).value == 909


def test_multiple_examples_per_label_are_supported() -> None:
    classifier = DigitClassifier()
    for char in "0123456789":
        classifier.add(char, glyph_mask(char, SCALE))
        classifier.add(char, glyph_mask(char, SCALE * 2))
    assert len(classifier.templates["5"]) == 2
    assert read_number(render("505"), classifier).value == 505


def test_speckles_are_not_segmented_as_glyphs() -> None:
    """A single stray bright pixel used to become a fourth 'glyph' in '342'."""
    mask = np.zeros((20, 40), dtype=bool)
    mask[2:18, 4:12] = True  # a real glyph
    mask[9, 20] = True  # one speck of noise
    assert len(segment_glyphs(mask)) == 1


def test_thin_glyphs_survive_the_speckle_filter() -> None:
    mask = np.zeros((20, 40), dtype=bool)
    mask[2:18, 4:12] = True  # wide glyph
    mask[2:18, 20:22] = True  # a "1"-like bar
    assert len(segment_glyphs(mask)) == 2


def test_aspect_ratio_is_preserved_so_one_is_not_confused_with_zero() -> None:
    """Stretching each glyph to fill the box made a '1' look like a '0'."""
    from tds_macro.vision.digits import fit_glyph

    bar = np.zeros((20, 3), dtype=bool)
    bar[:, :] = True
    block = np.ones((20, 14), dtype=bool)
    assert fit_glyph(bar).sum() < fit_glyph(block).sum() / 2


def test_templates_match_regardless_of_how_they_were_cropped() -> None:
    padded = np.zeros((30, 24), dtype=bool)
    padded[6:26, 6:18] = glyph_mask("8", SCALE)[:20, :12]
    classifier = DigitClassifier()
    classifier.add("8", padded)
    tight = glyph_mask("8", SCALE)[:20, :12]
    label, score = classifier.classify(tight)
    assert label == "8" and score > 0.9


# --- calibration by typed readout ------------------------------------------------
def test_learning_from_typed_text_teaches_every_glyph() -> None:
    from tds_macro.vision.digits import learn_from_text, missing_digits

    classifier = DigitClassifier()
    outcome = learn_from_text(render("$1,250"), "$1,250", classifier)
    assert outcome.ok and outcome.added == 6
    assert read_number(render("521"), classifier).value == 521
    assert missing_digits(classifier) == ["3", "4", "6", "7", "8", "9"]


def test_learning_tolerates_spaces_in_what_the_user_typed() -> None:
    from tds_macro.vision.digits import learn_from_text

    classifier = DigitClassifier()
    assert learn_from_text(render("42"), " 4 2 ", classifier).ok


def test_count_mismatch_is_refused_rather_than_guessed() -> None:
    """Pairing the wrong characters onto glyphs would poison every later read."""
    from tds_macro.vision.digits import learn_from_text

    classifier = DigitClassifier()
    outcome = learn_from_text(render("123"), "12", classifier)
    assert not outcome.ok
    assert "found 3 character(s)" in outcome.error
    assert classifier.templates == {}


def test_empty_input_and_empty_region_are_reported() -> None:
    from tds_macro.vision.digits import learn_from_text

    classifier = DigitClassifier()
    assert "type what the readout" in learn_from_text(render("1"), "  ", classifier).error
    blank = np.full((30, 60, 4), 20, dtype=np.uint8)
    assert "no characters were found" in learn_from_text(blank, "1", classifier).error


def test_learning_accumulates_across_several_captures() -> None:
    from tds_macro.vision.digits import learn_from_text, missing_digits

    classifier = DigitClassifier()
    learn_from_text(render("1234"), "1234", classifier)
    learn_from_text(render("5678"), "5678", classifier)
    learn_from_text(render("90"), "90", classifier)
    assert missing_digits(classifier) == []
    assert read_number(render("9081726354"), classifier).value == 9081726354


def test_glyph_previews_match_the_character_count() -> None:
    from tds_macro.vision.digits import glyph_previews

    assert len(glyph_previews(render("$1,250"))) == 6


def test_a_current_over_total_readout_reads_the_current_value() -> None:
    """'12/40' was read as 1240, which satisfied every WaitForWave at once."""
    from tds_macro.vision.digits import parse_readout

    slash = np.zeros((20, 12), dtype=bool)
    for row in range(20):
        slash[row, max(0, 11 - row * 12 // 20)] = True
    classifier = trained_classifier()
    classifier.add("/", slash)
    assert parse_readout("12/40") == "12"
    assert parse_readout("$1,250") == "1250"
    assert parse_readout("/40") == ""


# --- calibration format 1 -> 2 ------------------------------------------------------------
def _format1_vector(glyph: np.ndarray) -> np.ndarray:
    """How format 1 stored a template: no softening."""
    from tds_macro.vision.digits import fit_glyph

    vector = fit_glyph(glyph).astype(np.float32).reshape(-1)
    vector -= vector.mean()
    return vector / float(np.linalg.norm(vector))


def test_templates_saved_by_format_1_still_read_after_the_update(tmp_path) -> None:
    import json

    from tds_macro.vision.calibration import CALIBRATION_FILE, Calibration
    from tds_macro.vision.capture import NormBox
    from tds_macro.vision.digits import normalize_template

    old = Calibration(client_size=(100, 40))
    for region in (old.wave, old.cash):
        region.box = NormBox(0.1, 0.1, 0.5, 0.5)
        region.classifier = DigitClassifier()
        for char in "0123456789$,":
            region.classifier.templates[char] = [_format1_vector(glyph_mask(char))]
    old.save(tmp_path)
    payload = json.loads((tmp_path / CALIBRATION_FILE).read_text(encoding="utf-8"))
    payload["version"] = 1
    (tmp_path / CALIBRATION_FILE).write_text(json.dumps(payload), encoding="utf-8")

    loaded = Calibration.load(tmp_path)
    assert loaded.version == 2
    for char in "0123456789$,":
        stored = loaded.cash.classifier.templates[char][0]
        assert np.allclose(stored, normalize_template(glyph_mask(char)), atol=1e-5), char
    # The thin "1" is the glyph the old templates lost first.
    for text in ("111", "$1,110", "7104"):
        result = read_number(render(text), loaded.cash.classifier)
        assert result.text == text and result.ok, (text, result)

