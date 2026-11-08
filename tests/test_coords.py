"""Coordinate conversion, including the DPI and multi-monitor cases.

These are the claims that cannot be verified by running the app on this
machine, so they are pinned down here instead.
"""

from __future__ import annotations

import pytest

from tds_macro.input.coords import (
    ABS_MAX,
    NormPoint,
    Rect,
    VirtualScreen,
    absolute_to_screen,
    client_rect_from_window,
    norm_to_absolute,
    norm_to_screen,
    round_trip_error,
    screen_to_absolute,
    screen_to_norm,
)

# Realistic desktop layouts, including the awkward ones.
LAYOUTS = {
    "single 1080p": VirtualScreen(0, 0, 1920, 1080),
    "single 4k": VirtualScreen(0, 0, 3840, 2160),
    "second monitor to the left": VirtualScreen(-1920, 0, 3840, 1080),
    "second monitor above": VirtualScreen(0, -1440, 2560, 2520),
    "mixed dpi, left and higher": VirtualScreen(-2560, -400, 4480, 1840),
    "three wide": VirtualScreen(-1920, 0, 5760, 1080),
    "portrait secondary": VirtualScreen(-1080, -300, 3000, 1920),
}


@pytest.mark.parametrize("name", list(LAYOUTS))
def test_screen_to_absolute_round_trips_exactly_on_every_layout(name: str) -> None:
    """Windows converts absolute coords back with (abs * width) >> 16.

    Every pixel must survive that round trip, or clicks land one pixel off --
    which only shows up at a monitor edge, where it spills onto the next
    display.
    """
    screen = LAYOUTS[name]
    assert round_trip_error(screen) == 0
    xs = [
        screen.left,
        screen.left + 1,
        screen.left + screen.width // 2,
        screen.right - 2,
        screen.right - 1,
    ]
    ys = [screen.top, screen.top + 1, screen.top + screen.height // 2, screen.bottom - 1]
    for x in xs:
        for y in ys:
            ax, ay = screen_to_absolute(x, y, screen)
            assert 0 <= ax <= ABS_MAX and 0 <= ay <= ABS_MAX
            assert absolute_to_screen(ax, ay, screen) == (x, y), f"{name} at ({x},{y})"


@pytest.mark.parametrize("name", list(LAYOUTS))
def test_every_pixel_of_a_scanline_round_trips(name: str) -> None:
    screen = LAYOUTS[name]
    y = screen.top
    for x in range(screen.left, screen.right, max(1, screen.width // 500)):
        ax, ay = screen_to_absolute(x, y, screen)
        assert absolute_to_screen(ax, ay, screen)[0] == x


def test_negative_virtual_screen_origin_is_handled() -> None:
    """A monitor left of primary gives a negative origin; a naive conversion
    would produce negative absolute coordinates and clamp everything to 0."""
    screen = VirtualScreen(-1920, 0, 3840, 1080)
    left_edge = screen_to_absolute(-1920, 0, screen)
    primary_origin = screen_to_absolute(0, 0, screen)
    # Inside the first pixel band, not clamped to 0 and not negative.
    assert 0 < left_edge[0] < 65536 // screen.width + 1
    assert primary_origin[0] == pytest.approx(ABS_MAX // 2, abs=20)
    assert absolute_to_screen(*primary_origin, screen) == (0, 0)


def test_absolute_coordinates_stay_in_range_at_the_far_edge() -> None:
    screen = VirtualScreen(0, 0, 1920, 1080)
    ax, ay = screen_to_absolute(1919, 1079, screen)
    assert ax <= ABS_MAX and ay <= ABS_MAX
    assert absolute_to_screen(ax, ay, screen) == (1919, 1079)


def test_round_trip_error_reports_the_limit_for_an_absurd_desktop() -> None:
    """Beyond 65536 px wide the mapping cannot be lossless; say so rather than
    pretending otherwise."""
    assert round_trip_error(VirtualScreen(0, 0, 70_000, 1080)) >= 1
    assert round_trip_error(VirtualScreen(0, 0, 65_536, 1080)) == 0


# --- normalized client space ---------------------------------------------------
@pytest.mark.parametrize(
    "client",
    [
        Rect(0, 0, 1280, 720),
        Rect(100, 50, 1380, 770),
        Rect(-1920, 0, -640, 720),  # window on a monitor left of primary
        Rect(0, -1440, 2560, 0),  # window on a monitor above primary
        Rect(8, 31, 1288, 751),  # windowed with a title bar offset
    ],
)
def test_norm_to_screen_round_trips_within_a_pixel(client: Rect) -> None:
    for nx, ny in [(0.0, 0.0), (0.25, 0.5), (0.5, 0.5), (0.999, 0.999), (1.0, 1.0)]:
        point = NormPoint(nx, ny)
        sx, sy = norm_to_screen(point, client)
        assert client.contains(sx, sy)
        back = screen_to_norm(sx, sy, client)
        assert abs(back.x - nx) <= 1.0 / client.width
        assert abs(back.y - ny) <= 1.0 / client.height
        # Exact inverse on the same rectangle.
        assert norm_to_screen(back, client) == (sx, sy)


def test_a_macro_recorded_small_replays_at_a_larger_size() -> None:
    """The whole point of normalized storage: same relative spot, both sizes."""
    recorded_at = Rect(0, 0, 1280, 720)
    replayed_at = Rect(0, 0, 2560, 1440)
    click = screen_to_norm(320, 540, recorded_at)  # 25% across, 75% down
    assert norm_to_screen(click, replayed_at) == (640, 1080)


def test_a_macro_replays_correctly_after_the_window_moves_to_another_monitor() -> None:
    recorded_at = Rect(0, 0, 1600, 900)
    moved_to = Rect(-1920, -200, -320, 700)  # same size, monitor up and to the left
    point = screen_to_norm(400, 450, recorded_at)
    assert norm_to_screen(point, moved_to) == (-1520, 250)


def test_dpi_scaling_does_not_shift_the_target() -> None:
    """At 150% scaling the client area is larger in physical pixels; because we
    store a fraction, the same normalized point still hits the same UI element."""
    at_100 = Rect(0, 0, 1280, 720)
    at_150 = Rect(0, 0, 1920, 1080)
    point = NormPoint(0.5, 0.25)
    assert norm_to_screen(point, at_100) == (640, 180)
    assert norm_to_screen(point, at_150) == (960, 270)


def test_full_pipeline_lands_inside_the_client_area() -> None:
    client = Rect(100, 50, 1380, 770)
    screen = VirtualScreen(-1920, -200, 4480, 1640)
    for nx, ny in [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]:
        ax, ay = norm_to_absolute(NormPoint(nx, ny), client, screen)
        assert client.contains(*absolute_to_screen(ax, ay, screen))


def test_out_of_range_points_are_clamped_not_wrapped() -> None:
    client = Rect(0, 0, 1000, 1000)
    screen = VirtualScreen(0, 0, 1920, 1080)
    ax, ay = norm_to_absolute(NormPoint(1.5, -0.5), client, screen)
    x, y = absolute_to_screen(ax, ay, screen)
    assert client.contains(x, y)


# --- degenerate input -----------------------------------------------------------
def test_minimized_window_is_rejected_rather_than_clicking_at_the_origin() -> None:
    """A minimized window reports a client origin of about (-32000, -32000).
    Treating that as a real position would fire clicks into nowhere."""
    with pytest.raises(ValueError, match="minimized"):
        client_rect_from_window(Rect(-32000, -32000, -31000, -31000), (1280, 720), (-32000, -32000))


def test_zero_size_client_area_is_rejected() -> None:
    with pytest.raises(ValueError, match="no size"):
        client_rect_from_window(Rect(0, 0, 100, 100), (0, 0), (0, 0))
    with pytest.raises(ValueError, match="no area"):
        norm_to_screen(NormPoint(0.5, 0.5), Rect(0, 0, 0, 0))
    with pytest.raises(ValueError, match="no area"):
        screen_to_absolute(0, 0, VirtualScreen(0, 0, 0, 0))


def test_client_rect_from_window_builds_screen_space_geometry() -> None:
    rect = client_rect_from_window(Rect(100, 100, 1420, 880), (1280, 720), (108, 139))
    assert rect.as_tuple() == (108, 139, 1388, 859)
    assert (rect.width, rect.height) == (1280, 720)


def test_norm_point_helpers() -> None:
    assert NormPoint(1.2, -0.3).clamped().as_tuple() == (1.0, 0.0)
    assert not NormPoint(1.2, 0.5).is_inside()
    assert NormPoint(0.5, 0.5).distance_to(NormPoint(0.5, 0.6)) == pytest.approx(0.1)
    assert NormPoint.from_dict(NormPoint(0.1234567, 0.5).to_dict()).x == pytest.approx(0.123457)
