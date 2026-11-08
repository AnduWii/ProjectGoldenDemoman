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
from .steps import (
    Ability,
    Place,
    Sell,
    SkipWave,
    Step,
    Strategy,
    Upgrade,
    Wait,
    WaitForWave,
)


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


@dataclass
class RecordingStats:
    """What the recorder had to guess, so the UI can say so plainly."""

    events_seen: int = 0
    clicks_outside_window: int = 0
    unmatched_clicks: int = 0
    unmatched_actions: int = 0
    inferred_tower_refs: int = 0
    dropped_slot_presses: int = 0
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
    ) -> None:
        self.keybinds = keybinds
        self.options = options or RecorderOptions()
        self.wave_provider = wave_provider
        self.stats = RecordingStats()
        self.steps: list[Step] = []

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
        elif event.kind is RawKind.MOUSE_DOWN and event.button == "left":
            self._on_click(event)
        # Key-up and mouse-up carry no extra intent; holding a key is not a
        # distinct action in TDS, so recording the press alone keeps the step
        # list readable.

    def feed_all(self, events: list[RawEvent]) -> None:
        for event in events:
            self.feed(event)

    # -- key handling ---------------------------------------------------------
    def _on_key(self, event: RawEvent) -> None:
        key = (event.key or "").lower()
        if not key:
            return

        slot = self._slot_lookup.get(key)
        if slot is not None:
            if self._pending_slot is not None:
                self.stats.dropped_slot_presses += 1
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

        if key not in self.stats.ignored_keys:
            self.stats.ignored_keys.append(key)

    # -- click handling -------------------------------------------------------
    def _on_click(self, event: RawEvent) -> None:
        if not event.in_client or event.pos is None:
            self.stats.clicks_outside_window += 1
            return
        position = event.pos

        # A click on a calibrated menu button is that action, not a selection.
        for binding, handler in (
            (self.keybinds.upgrade, self._emit_upgrade),
            (self.keybinds.sell, self._emit_sell),
            (self.keybinds.ability, self._emit_ability),
            (self.keybinds.skip_wave, self._emit_skip_binding),
        ):
            if self._matches_menu_point(binding, position):
                handler(event.t_ms)
                return

        if self._pending_slot is not None:
            slot, pressed_at = self._pending_slot
            self._pending_slot = None
            if event.t_ms - pressed_at <= self.options.pending_slot_timeout_ms:
                self._emit_place(slot, position, event.t_ms)
                return
            self.stats.dropped_slot_presses += 1

        tower_id = self._nearest_tower(position)
        if tower_id is None:
            # Clicking empty ground deselects in-game, so it must deselect here
            # too -- otherwise a later upgrade key would be attributed to a
            # tower the user had already clicked away from.
            self._selected = None
            self.stats.unmatched_clicks += 1
            return
        self._selected = tower_id
        self.stats.inferred_tower_refs += 1

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
        """Insert a Wait (and optionally a wave gate) for the idle time."""
        if self.options.insert_wave_gates and self.wave_provider is not None:
            wave = self.wave_provider()
            if wave is not None and (self._last_wave is None or wave > self._last_wave):
                if self._last_wave is not None:
                    self.steps.append(WaitForWave(wave=wave))
                self._last_wave = wave

        if not self.options.insert_waits or self._last_event_ms is None:
            self._last_event_ms = t_ms
            return
        gap = t_ms - self._last_event_ms
        self._last_event_ms = t_ms
        if gap < self.options.min_wait_ms:
            return
        gap = min(gap, self.options.max_wait_ms)
        quantum = max(1, self.options.quantize_wait_ms)
        self.steps.append(Wait(ms=int(round(gap / quantum)) * quantum))

    def _emit_place(self, slot: int, position: NormPoint, t_ms: int) -> None:
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
        return self._selected

    def _emit_upgrade(self, t_ms: int) -> None:
        tower_id = self._require_selection("upgrade")
        if tower_id is None:
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
            return
        self._gap_before(t_ms)
        self.steps.append(Sell(tower_id=tower_id))
        self._placements.pop(tower_id, None)
        self._selected = None

    def _emit_ability(self, t_ms: int) -> None:
        tower_id = self._require_selection("ability")
        if tower_id is None:
            return
        self._gap_before(t_ms)
        self.steps.append(Ability(tower_id=tower_id))

    def _emit_skip(self, t_ms: int) -> None:
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
        steps = list(self.steps)
        while steps and isinstance(steps[-1], Wait):
            steps.pop()
        if self._pending_slot is not None:
            self.stats.dropped_slot_presses += 1
        return Strategy(name=name, steps=steps)
