"""The one place where states become light and sound.

Not safety-relevant: every call into the LEDs or the audio is guarded, an exception is logged
and swallowed, the flow never sees it. No retries. The mapping (notes/feedback.md):

  entering                   LEDs                            audio
  idle                       idle                            (a running loop stops)
  sleep                      off
  checking_purchase          selected, the selected level
  door_unlocked              unlocked, the server's level    success once
  door_opened                open, that level
  door_alarm, door_forced    alarm, all zones                alarm, looped while in the state
  completing                 thanks, that level (fades to idle)
  out_of_order               fault                           error once, except for maintenance
"""

import logging
from collections.abc import Callable

from monitoni.audio import Audio
from monitoni.flow import REASON_MAINTENANCE, Flow, State
from monitoni.leds import Leds

log = logging.getLogger(__name__)

LED_PATTERNS: dict[State, str] = {
    State.IDLE: "idle",
    State.SLEEP: "off",
    State.CHECKING_PURCHASE: "selected",
    State.DOOR_UNLOCKED: "unlocked",
    State.DOOR_OPENED: "open",
    State.DOOR_ALARM: "alarm",
    State.DOOR_FORCED: "alarm",
    State.COMPLETING: "thanks",
    State.OUT_OF_ORDER: "fault",
}
LEVEL_PATTERNS = frozenset({"selected", "unlocked", "open", "thanks"})  # concern one level

SOUNDS: dict[State, tuple[str, bool]] = {  # state -> (sound, looped)
    State.DOOR_UNLOCKED: ("success", False),
    State.DOOR_ALARM: ("alarm", True),
    State.DOOR_FORCED: ("alarm", True),
    State.OUT_OF_ORDER: ("error", False),  # a fault (hardware, database); not for maintenance
}


class Feedback:
    def __init__(self, flow: Flow, leds: Leds, audio: Audio) -> None:
        self.flow = flow
        self.leds = leds
        self.audio = audio
        self._looping = False
        flow.on_transition.append(self.on_transition)

    async def on_transition(self, old: State | None, new: State) -> None:
        pattern = LED_PATTERNS[new]
        level = self.flow.selected_level if pattern in LEVEL_PATTERNS else None
        self._guard(self.leds.set_pattern, pattern, level)

        if self._looping:  # the state that started the loop is over
            self._guard(self.audio.stop_playing)
            self._looping = False
        sound = SOUNDS.get(new)
        if new is State.OUT_OF_ORDER and self.flow.reason == REASON_MAINTENANCE:
            sound = None  # maintenance is someone's choice, not a fault
        if sound is not None:
            name, loop = sound
            self._guard(self.audio.play, name, loop)
            self._looping = loop

    @staticmethod
    def _guard(call: Callable, *args) -> None:
        try:
            call(*args)
        except Exception:
            log.exception("feedback: %s%r failed", call.__name__, args)
