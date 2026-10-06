"""Every row of the state -> light/sound mapping through the real Flow, with the mocks."""

import asyncio
import logging

import pytest

from monitoni.audio import MockAudio
from monitoni.eventlog import EventLog
from monitoni.feedback import Feedback
from monitoni.flow import REASON_HARDWARE, REASON_MAINTENANCE, Event, Flow, State
from monitoni.hardware.mock import MockHardware
from monitoni.leds import MockLeds
from monitoni.outbox import Outbox
from monitoni.purchase import MockPurchaseServer
from monitoni.runtime import Runtime
from tests.helpers import wait_until


class BrokenLeds(MockLeds):
    def set_pattern(self, pattern: str, level: int | None = None) -> None:
        super().set_pattern(pattern, level)
        raise RuntimeError("strip exploded")


@pytest.fixture
async def make_flow(make_config):
    started = []

    async def _make(maintenance: bool = False, leds_cls=MockLeds, **timings: float):
        config = make_config(**timings)
        events = EventLog(config.database.path)
        await events.start()
        hardware = MockHardware(config.vending.levels)
        await hardware.start()
        purchase = MockPurchaseServer()
        holder: dict = {}
        outbox = Outbox(config.database.path, purchase, events,
                        config.purchase_server.outbox_backoff_s,
                        lambda: holder["flow"].state.value)
        await outbox.start()
        flow = holder["flow"] = Flow(config, hardware, purchase, outbox, events,
                                     runtime=Runtime(out_of_order=maintenance))
        leds, audio = leds_cls(config), MockAudio()
        feedback = Feedback(flow, leds, audio)
        await flow.start()
        started.append((flow, outbox, events))
        return flow, leds, audio, feedback

    yield _make
    for flow, outbox, events in started:
        await flow.stop()
        await outbox.stop()
        await events.stop()


def led_calls(leds: MockLeds) -> list[tuple]:
    return [c[1:] for c in leds.calls]


def audio_calls(audio: MockAudio) -> list[tuple]:
    return [c[1:] for c in audio.calls]


async def wait_for(flow: Flow, state: State) -> None:
    await wait_until(lambda: flow.state is state, state.value)


async def test_a_vend_walks_every_customer_row(make_flow):
    flow, leds, audio, _ = await make_flow(door_alarm_delay_s=0.05)
    assert led_calls(leds) == [("set_pattern", "idle", None)]  # start: (None, idle)
    assert audio_calls(audio) == [] and leds.status()["pattern"] == "idle"

    await flow.dispatch(Event.SELECT_LEVEL, level=3)
    assert led_calls(leds)[-1] == ("set_pattern", "selected", 3) and audio_calls(audio) == []

    flow.purchase.simulate_payment(3, item=5)  # the server's level wins
    await wait_for(flow, State.DOOR_UNLOCKED)
    assert led_calls(leds)[-1] == ("set_pattern", "unlocked", 5)
    assert audio_calls(audio) == [("play", "success", False)]
    assert audio.status()["playing"] == "success"

    await flow.dispatch(Event.DOOR_OPENED)
    assert led_calls(leds)[-1] == ("set_pattern", "open", 5) and len(audio.calls) == 1

    await wait_for(flow, State.DOOR_ALARM)
    assert led_calls(leds)[-1] == ("set_pattern", "alarm", None)
    assert audio_calls(audio)[-1] == ("play", "alarm", True)
    assert audio.status()["playing"] == "alarm"

    await flow.dispatch(Event.DOOR_CLOSED)  # completing, then idle on its own
    await wait_for(flow, State.IDLE)
    assert led_calls(leds)[-2:] == [("set_pattern", "thanks", 5), ("set_pattern", "idle", None)]
    assert audio_calls(audio)[-1] == ("stop_playing",)  # the loop ended with door_alarm
    assert audio.status()["playing"] is None
    assert len(audio.calls) == 3  # success, alarm, stop: nothing else made a sound


async def test_sleep_is_dark_and_silent(make_flow):
    flow, leds, audio, _ = await make_flow(sleep_timeout_s=0.05)
    await wait_for(flow, State.SLEEP)
    assert led_calls(leds)[-1] == ("set_pattern", "off", None) and audio.calls == []
    await flow.dispatch(Event.TOUCH)
    assert led_calls(leds)[-1] == ("set_pattern", "idle", None) and audio.calls == []


async def test_forced_door_flashes_and_loops_until_closed(make_flow):
    flow, leds, audio, _ = await make_flow()
    await flow.dispatch(Event.DOOR_OPENED)
    assert flow.state is State.DOOR_FORCED
    assert led_calls(leds)[-1] == ("set_pattern", "alarm", None)
    assert audio_calls(audio) == [("play", "alarm", True)]
    await flow.dispatch(Event.DOOR_CLOSED)
    assert led_calls(leds)[-1] == ("set_pattern", "idle", None)
    assert audio_calls(audio) == [("play", "alarm", True), ("stop_playing",)]


async def test_hardware_fault_is_red_with_one_error_sound(make_flow):
    flow, leds, audio, _ = await make_flow()
    await flow.dispatch(Event.HARDWARE_FAULT, error="relay_core: gone")
    assert flow.reason == REASON_HARDWARE
    assert led_calls(leds)[-1] == ("set_pattern", "fault", None)
    assert audio_calls(audio) == [("play", "error", False)]
    await flow.dispatch(Event.HARDWARE_OK)
    assert led_calls(leds)[-1] == ("set_pattern", "idle", None) and len(audio.calls) == 1


async def test_fault_during_an_alarm_stops_the_loop(make_flow):
    flow, leds, audio, _ = await make_flow()
    await flow.dispatch(Event.DOOR_OPENED)  # forced: alarm loop
    await flow.dispatch(Event.HARDWARE_FAULT, error="x")
    assert audio_calls(audio) == [("play", "alarm", True), ("stop_playing",),
                                  ("play", "error", False)]


async def test_a_lost_report_is_red_with_the_error_sound(make_flow, monkeypatch):
    flow, leds, audio, _ = await make_flow()

    async def refuse(kind, level):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(flow.outbox, "enqueue", refuse)
    await flow.dispatch(Event.SELECT_LEVEL, level=2)
    flow.purchase.simulate_payment(2)
    await wait_for(flow, State.DOOR_UNLOCKED)
    await flow.dispatch(Event.DOOR_OPENED)
    await flow.dispatch(Event.DOOR_CLOSED)
    await wait_for(flow, State.OUT_OF_ORDER)
    assert flow.reason == "database"
    assert led_calls(leds)[-1] == ("set_pattern", "fault", None)
    assert audio_calls(audio) == [("play", "success", False), ("play", "error", False)]


async def test_maintenance_is_red_but_silent(make_flow):
    flow, leds, audio, _ = await make_flow(maintenance=True)
    assert flow.state is State.OUT_OF_ORDER and flow.reason == REASON_MAINTENANCE
    assert led_calls(leds) == [("set_pattern", "fault", None)]
    assert audio.calls == []


async def test_a_failed_entry_hook_still_reports_out_of_order(make_flow):
    flow, leds, audio, _ = await make_flow()

    async def broken_unlock(level):
        raise RuntimeError("no relay")

    flow.hardware.unlock_door = broken_unlock
    await flow.dispatch(Event.SELECT_LEVEL, level=2)
    await flow.dispatch(Event.PURCHASE_VALID, item=2)
    assert flow.state is State.OUT_OF_ORDER
    assert led_calls(leds)[-1] == ("set_pattern", "fault", None)
    assert audio_calls(audio) == [("play", "error", False)]


async def test_a_broken_strip_never_stops_a_transition(make_flow, caplog):
    with caplog.at_level(logging.ERROR, logger="monitoni.feedback"):
        flow, leds, audio, _ = await make_flow(leds_cls=BrokenLeds)
        await flow.dispatch(Event.SELECT_LEVEL, level=4)
        flow.purchase.simulate_payment(4)
        await wait_for(flow, State.DOOR_UNLOCKED)
        await flow.dispatch(Event.DOOR_OPENED)
        await flow.dispatch(Event.DOOR_CLOSED)
        await wait_for(flow, State.IDLE)
    assert [c[1] for c in leds.calls] == ["set_pattern"] * 6  # every state was still announced
    assert audio_calls(audio) == [("play", "success", False)]  # the sound side kept working
    failures = [r for r in caplog.records if "feedback: set_pattern" in r.getMessage()]
    assert len(failures) == 6 and all(r.exc_info for r in failures)
    assert not [r for r in await flow.events.recent(50) if r["kind"] == "rejected"]


async def test_a_broken_subscriber_is_logged_by_the_flow(make_flow, caplog):
    flow, leds, audio, _ = await make_flow()

    async def bad(old, new):
        raise KeyError("boom")

    flow.on_transition.append(bad)
    with caplog.at_level(logging.ERROR, logger="monitoni.flow"):
        await flow.dispatch(Event.SELECT_LEVEL, level=1)
    assert flow.state is State.CHECKING_PURCHASE
    assert "on_transition subscriber failed for idle -> checking_purchase" in caplog.text
    await asyncio.sleep(0)
