"""Turn observed input into strategy steps.

The hard part is not capturing clicks, it is deciding what a click *meant*.  A
raw stream of coordinates would replay badly at a different resolution and
would be impossible to edit, so the recorder reconstructs intent:

* a hotbar key followed by a click in the game becomes ``Place``;
* a click near an existing placement selects that tower, and the next
  upgrade/sell/ability input becomes a step referencing it by id;
* idle gaps become ``Wait`` steps rather than being silently dropped.

The tower-selection rule is proximity-based and therefore a *heuristic*.  Two
towers placed close together can resolve to the wrong one, so every inference
is counted in :class:`RecordingStats` and surfaced in the UI; the editor shows
which tower each step bound to so the user can correct it.

Pure: takes :class:`RawEvent` values, produces steps.  No Win32, no timing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import ActionBinding, Keybinds
from ..input.coords import NormPoint
from ..input.events import RawEvent, RawKind
from ..input.keys import MODIFIER_KEYS, KeyParseError, normalize_key_name
from .steps import (
    Ability,
    Click,
    Drag,
    DragPoint,
    KeyPress,
    Place,
    Scroll,
    Sell,
    SkipWave,
    Step,
    Strategy,
    Upgrade,
    Wait,
    WaitForWave,
)

# OS key auto-repeat starts within 1 s even at its slowest setting, then
# repeats every ~30-500 ms; a gap longer than this is a fresh press.
KEY_REPEAT_GAP_MS = 1500

@dataclass
class RecorderOptions:
    """Tunables for the raw-input-to-steps conversion."""

    insert_waits: bool = True
    min_wait_ms: int = 400
    max_wait_ms: int = 120_000
    quantize_wait_ms: int = 100
    # Normalized click distance that still counts as "clicking that tower".
    # ~4% of the client width is roughly a tower's footprint at 1080p.
    select_radius: float = 0.04
    # A hotbar key that is not followed by a click soon after was a mis-press.
    pending_slot_timeout_ms: int = 5_000
    coalesce_upgrades: bool = True
    insert_wave_gates: bool = False
    # TinyTask behaviour: anything that is not a recognised tower action is kept
    # as a plain Click / Key press / Drag / Scroll instead of being dropped.
    keep_everything: bool = False
    # A right/middle-button press that moves further than this (normalized
    # distance) is a drag, such as turning the camera; less is a click.
    drag_threshold: float = 0.01
    # Consecutive wheel turns closer together than this become one Scroll.
    scroll_merge_ms: int = 300


@dataclass
class RecordingStats:
    """What the recorder had to guess, so the UI can say so plainly."""

    events_seen: int = 0
    clicks_outside_window: int = 0
    unmatched_clicks: int = 0
    unmatched_actions: int = 0
    plain_steps: int = 0
    skipped_shortcuts: int = 0
    inferred_tower_refs: int = 0
    dropped_slot_presses: int = 0
    key_repeats: int = 0
    ignored_keys: list[str] = field(default_factory=list)

    def warnings(self) -> list[str]:
        notes: list[str] = []
        if self.unmatched_clicks:
            notes.append(
                f"{self.unmatched_clicks} click(s) did not match a placed tower and were "
                "ignored -- if those were upgrades, place the tower first or widen the "
                "selection radius in Settings."
            )
        if self.unmatched_actions:
            notes.append(
                f"{self.unmatched_actions} upgrade/sell/ability input(s) had no tower "
                "selected and were dropped."
            )
        if self.dropped_slot_presses:
            notes.append(
                f"{self.dropped_slot_presses} hotbar press(es) were never followed by a "
                "click and were dropped."
            )
        if self.clicks_outside_window:
            notes.append(
                f"{self.clicks_outside_window} click(s) landed outside the Roblox window "
                "and were ignored."
            )
        if self.inferred_tower_refs:
            notes.append(
                f"{self.inferred_tower_refs} step(s) were matched to a tower by position. "
                "Check the tower each one targets before replaying."
            )
        return notes


class Recorder:
    """Accumulates raw events and produces a :class:`Strategy`."""

    def __init__(
        self,
        keybinds: Keybinds,
        options: RecorderOptions | None = None,
        wave_provider: Callable[[], int | None] | None = None,
        excluded_keys: set[str] | None = None,
        aspect_ratio: float = 0.0,
    ) -> None:
        self.keybinds = keybinds
        self.options = options or RecorderOptions()
        self.wave_provider = wave_provider
        # Keys never recorded: the app's own unmodified hotkeys (F6/F7/F8). A
        # replay that pressed "stop recording" would be absurd.
        self.excluded_keys = {k.lower() for k in (excluded_keys or set())}
        self.aspect_ratio = aspect_ratio
        self.stats = RecordingStats()
        self.steps: list[Step] = []
        self._held_modifiers: set[str] = set()
        # Keys currently down -> when their last key-down arrived. A held key
        # repeats its key-down about 30 times a second (OS auto-repeat); only
        # the first press is a new action. A "repeat" long after the last one
        # means the key-up was missed, so it counts as a new press.
        self._keys_down: dict[str, int] = {}
        self._open_keys: dict[str, tuple[KeyPress, int]] = {}
        # A click that selected a tower, kept back until we know whether an
        # upgrade/sell/ability follows (which clicks the tower itself on replay).
        self._pending_click: tuple[int, NormPoint] | None = None
        # A right/middle press in progress: (button, t_down, position, path).
        self._press: tuple[str, int, NormPoint, list[DragPoint]] | None = None

        self._placements: dict[str, NormPoint] = {}
        self._selected: str | None = None
        self._pending_slot: tuple[int, int] | None = None  # (slot, t_ms)
        self._last_event_ms: int | None = None
        self._last_wave: int | None = None
        self._next_tower = 1
        self._slot_lookup = self._build_slot_lookup()

    def _build_slot_lookup(self) -> dict[str, int]:
        lookup: dict[str, int] = {}
        for index, key in enumerate(self.keybinds.slots, start=1):
            if key:
                lookup[key.lower()] = index
        return lookup

    # -- feeding --------------------------------------------------------------
    def feed(self, event: RawEvent) -> None:
        self.stats.events_seen += 1
        if event.kind is RawKind.KEY_DOWN:
            self._on_key(event)
        elif event.kind is RawKind.KEY_UP:
            self._on_key_up(event)
        elif event.kind is RawKind.MOUSE_DOWN and event.button == "left":
            self._on_click(event)
        elif event.kind is RawKind.MOUSE_DOWN and self.options.keep_everything:
            self._on_press(event)
        elif event.kind is RawKind.MOUSE_MOVE and self._press is not None:
            self._on_drag_move(event)
        elif event.kind is RawKind.MOUSE_UP and self._press is not None:
            self._on_release(event)
        elif event.kind is RawKind.SCROLL and self.options.keep_everything:
            self._on_scroll(event)

    def feed_all(self, events: list[RawEvent]) -> None:
        for event in events:
            self.feed(event)

    # -- key handling ---------------------------------------------------------
    def _on_key(self, event: RawEvent) -> None:
        key = (event.key or "").lower()
        if not key:
            return
        last_down = self._keys_down.get(key)
        self._keys_down[key] = event.t_ms
        if last_down is not None and event.t_ms - last_down <= KEY_REPEAT_GAP_MS:
            self.stats.key_repeats += 1
            return
        if key in MODIFIER_KEYS:
            self._held_modifiers.add(key)
            return
        if self._held_modifiers & {"ctrl", "alt", "win", "cmd"} or key in self.excluded_keys:
            # A keyboard shortcut -- most likely this app's own hotkey.
            self.stats.skipped_shortcuts += 1
            return

        slot = self._slot_lookup.get(key)
        if slot is not None:
            if self._pending_slot is not None:
                self._drop_pending_slot()
            self._pending_slot = (slot, event.t_ms)
            return

        for binding, handler in (
            (self.keybinds.upgrade, self._emit_upgrade),
            (self.keybinds.sell, self._emit_sell),
            (self.keybinds.ability, self._emit_ability),
        ):
            if binding.mode == "key" and binding.key.lower() == key:
                handler(event.t_ms)
                return

        skip = self.keybinds.skip_wave
        if skip.mode == "key" and skip.key.lower() == key:
            self._emit_skip(event.t_ms)
            return

        if self.options.keep_everything and self._recordable(key):
            self._emit_key(key, event.t_ms)
            return
        if key not in self.stats.ignored_keys:
            self.stats.ignored_keys.append(key)

    def _on_key_up(self, event: RawEvent) -> None:
        key = (event.key or "").lower()
        self._keys_down.pop(key, None)
        self._held_modifiers.discard(key)
        opened = self._open_keys.pop(key, None)
        if opened is not None:
            step, pressed_at = opened
            step.hold_ms = max(step.hold_ms, event.t_ms - pressed_at)

    @staticmethod
    def _recordable(key: str) -> bool:
        """Only keys the replay can actually press."""
        try:
            normalize_key_name(key)
        except KeyParseError:
            return False
        return True

    # -- click handling -------------------------------------------------------
    def _on_click(self, event: RawEvent) -> None:
        if not event.in_client or event.pos is None:
            self.stats.clicks_outside_window += 1
            return
        position = event.pos
        # A previous tower selection that no action consumed was a plain click.
        self._flush_pending_click()

        # A pending hotbar press wins over everything else: the user explicitly
        # selected a tower and then clicked, which is a placement wherever it
        # landed. Checking menu buttons first would misread a placement made
        # near a calibrated Upgrade button as an upgrade.
        if self._pending_slot is not None:
            slot, pressed_at = self._pending_slot
            self._pending_slot = None
            if event.t_ms - pressed_at <= self.options.pending_slot_timeout_ms:
                self._emit_place(slot, position, event.t_ms)
                return
            self._pending_slot = (slot, pressed_at)
            self._drop_pending_slot()

        # Otherwise, a click on a calibrated menu button is that action rather
        # than a selection.
        for binding, handler in (
            (self.keybinds.upgrade, self._emit_upgrade),
            (self.keybinds.sell, self._emit_sell),
            (self.keybinds.ability, self._emit_ability),
            (self.keybinds.skip_wave, self._emit_skip_binding),
        ):
            if self._matches_menu_point(binding, position):
                handler(event.t_ms)
                return

        tower_id = self._nearest_tower(position)
        if tower_id is None:
            # Clicking empty ground deselects in-game, so it must deselect here
            # too -- otherwise a later upgrade key would be attributed to a
            # tower the user had already clicked away from.
            self._selected = None
            self.stats.unmatched_clicks += 1
            if self.options.keep_everything:
                self._emit_click(position, "left", event.t_ms)
            return
        self._selected = tower_id
        self.stats.inferred_tower_refs += 1
        if self.options.keep_everything:
            self._pending_click = (event.t_ms, position)

    # -- right/middle presses: clicks or drags --------------------------------
    def _on_press(self, event: RawEvent) -> None:
        if not event.in_client or event.pos is None or event.button is None:
            self.stats.clicks_outside_window += 1
            return
        self._flush_pending_click()
        self._finish_press(None)
        self._press = (event.button, event.t_ms, event.pos, [DragPoint(0, event.pos)])

    def _on_drag_move(self, event: RawEvent) -> None:
        assert self._press is not None
        if event.pos is None:
            return
        _, started, _, path = self._press
        path.append(DragPoint(max(0, event.t_ms - started), event.pos))

    def _on_release(self, event: RawEvent) -> None:
        self._finish_press(event)

    def _finish_press(self, release: RawEvent | None) -> None:
        """Turn a completed right/middle press into a Drag or a Click."""
        if self._press is None:
            return
        button, started, start, path = self._press
        self._press = None
        if release is not None and release.pos is not None:
            path.append(DragPoint(max(0, release.t_ms - started), release.pos))
        moved = max(point.position.distance_to(start) for point in path)
        if moved < self.options.drag_threshold:
            self._emit_click(start, button, started)
            return
        self._gap_before(started)
        self.steps.append(Drag(button=button, path=_thin_path(path)))
        self.stats.plain_steps += 1
        self._last_event_ms = release.t_ms if release is not None else started

    def _on_scroll(self, event: RawEvent) -> None:
        if not event.in_client or event.pos is None or event.delta == 0:
            return
        self._flush_pending_click()
        last = self.steps[-1] if self.steps else None
        recent = (
            self._last_event_ms is not None
            and event.t_ms - self._last_event_ms <= self.options.scroll_merge_ms
        )
        if isinstance(last, Scroll) and recent and (last.clicks > 0) == (event.delta > 0):
            last.clicks += event.delta
            self._last_event_ms = event.t_ms
            return
        self._gap_before(event.t_ms)
        self.steps.append(Scroll(position=event.pos, clicks=event.delta))
        self.stats.plain_steps += 1

    def _matches_menu_point(self, binding: ActionBinding, position: NormPoint) -> bool:
        return (
            binding.mode == "click"
            and binding.point is not None
            and binding.point.distance_to(position) <= self.options.select_radius
        )

    def _nearest_tower(self, position: NormPoint) -> str | None:
        best: tuple[float, str] | None = None
        for tower_id, point in self._placements.items():
            distance = point.distance_to(position)
            if distance <= self.options.select_radius and (best is None or distance < best[0]):
                best = (distance, tower_id)
        return best[1] if best else None

    # -- step emission --------------------------------------------------------
    def _gap_before(self, t_ms: int) -> None:
        """Insert the timing that precedes a step.

        With wave reading on, a step that happens in a new wave is preceded by
        "wait for wave N" and *no* fixed wait: on replay it should happen when
        that wave arrives, however long the game took to get there. Within a
        wave, fixed waits keep the original rhythm.
        """
        gated = False
        if self.options.insert_wave_gates and self.wave_provider is not None:
            wave = self.wave_provider()
            if wave is not None and (self._last_wave is None or wave > self._last_wave):
                if self._last_wave is not None:
                    self.steps.append(WaitForWave(wave=wave))
                    gated = True
                self._last_wave = wave

        previous, self._last_event_ms = self._last_event_ms, t_ms
        if gated or not self.options.insert_waits or previous is None:
            return
        gap = t_ms - previous
        if gap < self.options.min_wait_ms:
            return
        gap = min(gap, self.options.max_wait_ms)
        quantum = max(1, self.options.quantize_wait_ms)
        self.steps.append(Wait(ms=int(round(gap / quantum)) * quantum))

    def _emit_click(self, position: NormPoint, button: str, t_ms: int) -> None:
        self._gap_before(t_ms)
        self.steps.append(Click(position=position, button=button))
        self.stats.plain_steps += 1

    def _emit_key(self, key: str, t_ms: int) -> None:
        self._flush_pending_click()
        self._gap_before(t_ms)
        step = KeyPress(key=key)
        self.steps.append(step)
        self._open_keys[key] = (step, t_ms)
        self.stats.plain_steps += 1

    def _flush_pending_click(self) -> None:
        """Emit a held-back tower-selection click as a plain click."""
        pending, self._pending_click = self._pending_click, None
        if pending is not None:
            t_ms, position = pending
            self._emit_click(position, "left", t_ms)

    def _drop_pending_slot(self) -> None:
        """A hotbar press that no placement followed."""
        pending, self._pending_slot = self._pending_slot, None
        if pending is None:
            return
        self.stats.dropped_slot_presses += 1
        if self.options.keep_everything:
            slot, pressed_at = pending
            key = self.keybinds.slot_key(slot)
            if key:
                self._emit_key(key, pressed_at)

    def _emit_place(self, slot: int, position: NormPoint, t_ms: int) -> None:
        self._flush_pending_click()
        self._gap_before(t_ms)
        tower_id = f"T{self._next_tower}"
        self._next_tower += 1
        self._placements[tower_id] = position
        self._selected = tower_id
        self.steps.append(Place(slot=slot, position=position, tower_id=tower_id))

    def _require_selection(self, action: str) -> str | None:
        """The tower a tower-action applies to, or ``None`` if nothing is selected."""
        if self._selected is None:
            self.stats.unmatched_actions += 1
            return None
        del action
        # The action's replay clicks the tower itself, so the selection click
        # is not a separate step.
        self._pending_click = None
        return self._selected

    def _emit_upgrade(self, t_ms: int) -> None:
        tower_id = self._require_selection("upgrade")
        if tower_id is None:
            self._keep_unmatched_action(self.keybinds.upgrade, t_ms)
            return
        if self.options.coalesce_upgrades and self.steps:
            last = self.steps[-1]
            if isinstance(last, Upgrade) and last.tower_id == tower_id:
                last.times += 1
                self._last_event_ms = t_ms
                return
        self._gap_before(t_ms)
        self.steps.append(Upgrade(tower_id=tower_id))

    def _emit_sell(self, t_ms: int) -> None:
        tower_id = self._require_selection("sell")
        if tower_id is None:
            self._keep_unmatched_action(self.keybinds.sell, t_ms)
            return
        self._gap_before(t_ms)
        self.steps.append(Sell(tower_id=tower_id))
        self._placements.pop(tower_id, None)
        self._selected = None

    def _emit_ability(self, t_ms: int) -> None:
        tower_id = self._require_selection("ability")
        if tower_id is None:
            self._keep_unmatched_action(self.keybinds.ability, t_ms)
            return
        self._gap_before(t_ms)
        self.steps.append(Ability(tower_id=tower_id))

    def _keep_unmatched_action(self, binding: ActionBinding, t_ms: int) -> None:
        """An action key pressed with no tower selected still happened."""
        if self.options.keep_everything and binding.mode == "key" and binding.key:
            self._emit_key(binding.key.lower(), t_ms)

    def _emit_skip(self, t_ms: int) -> None:
        self._flush_pending_click()
        if self.steps and isinstance(self.steps[-1], SkipWave):
            self.steps[-1].times += 1
            self._last_event_ms = t_ms
            return
        self._gap_before(t_ms)
        self.steps.append(SkipWave())

    def _emit_skip_binding(self, t_ms: int) -> None:
        self._emit_skip(t_ms)

    # -- output ----------------------------------------------------------------
    def finish(self, name: str = "Recorded strategy") -> Strategy:
        """Produce the strategy.  A trailing Wait is dropped -- it is just the
        time between the last action and the user pressing stop."""
        self._finish_press(None)
        self._flush_pending_click()
        if self._pending_slot is not None:
            self._drop_pending_slot()
        steps = list(self.steps)
        while steps and isinstance(steps[-1], Wait):
            steps.pop()
        return Strategy(name=name, steps=steps, aspect_ratio=self.aspect_ratio)


def _thin_path(path: list[DragPoint], min_gap_ms: int = 15) -> list[DragPoint]:
    """Drop drag samples closer together than ``min_gap_ms``.

    The pointer reports far more often than replay needs; this keeps a long
    camera turn to a readable size without changing its shape or duration.
    """
    if len(path) <= 2:
        return path
    kept = [path[0]]
    for point in path[1:-1]:
        if point.t_ms - kept[-1].t_ms >= min_gap_ms:
            kept.append(point)
    kept.append(path[-1])
    return kept
