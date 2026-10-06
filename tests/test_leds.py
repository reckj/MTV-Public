"""LED patterns, the ArtNet sender against the fake receiver, the mock, runtime.json."""

import asyncio
import logging
import math

import pytest

from monitoni.leds import (
    BREATH_FLOOR,
    OFF,
    PATTERNS,
    ArtnetLeds,
    Layout,
    MockLeds,
    artdmx_packet,
    mix,
    scale,
)
from tests.fake_artnet import FakeArtnet, parse_zones
from tests.helpers import wait_until

IDLE, SELECTED, UNLOCKED = (60, 40, 20), (233, 162, 59), (233, 162, 59)
OPEN, ALARM, FAULT = (239, 228, 210), (239, 90, 106), (120, 30, 30)
ZONES = parse_zones("10x12")


def zone_colours(frame, zones=ZONES):
    """One colour per zone (the frame is uniform inside a zone) plus the pixels outside them."""
    for start, end in zones:
        assert len({frame[i] for i in range(start, end + 1)}) == 1, f"zone {start}-{end} mixed"
    outside = {frame[i] for i in range(len(frame)) if not any(a <= i <= b for a, b in zones)}
    return [frame[start] for start, _ in zones], outside


@pytest.fixture
def layout(make_config):
    return Layout(make_config())


# -- the pattern table ------------------------------------------------------------------

@pytest.mark.parametrize("name,t,level,expected", [
    ("off", 0.0, None, [OFF] * 10),
    ("idle", 0.0, None, [IDLE] * 10),
    ("selected", 0.0, 3, [IDLE] * 2 + [SELECTED] + [IDLE] * 7),
    ("unlocked", 0.0, 3, [OFF] * 2 + [UNLOCKED] + [OFF] * 7),
    ("unlocked", 0.75, 3, [OFF] * 2 + [scale(UNLOCKED, BREATH_FLOOR)] + [OFF] * 7),
    ("open", 0.0, 10, [OFF] * 9 + [OPEN]),
    ("alarm", 0.0, None, [ALARM] * 10),
    ("alarm", 0.25, None, [OFF] * 10),
    ("alarm", 0.5, None, [ALARM] * 10),
    ("fault", 0.0, None, [scale(FAULT, 0.5)] * 10),
    ("thanks", 0.0, 4, [OFF] * 3 + [OPEN] + [OFF] * 6),
    ("thanks", 0.5, 4,
     [mix(OFF, IDLE, 0.5)] * 3 + [mix(OPEN, IDLE, 0.5)] + [mix(OFF, IDLE, 0.5)] * 6),
    ("thanks", 1.0, 4, [IDLE] * 10),
    ("thanks", 5.0, 4, [IDLE] * 10),
], ids=lambda v: str(v) if not isinstance(v, list) else "")
def test_pattern_renders_the_expected_zone_colours(layout, name, t, level, expected):
    frame = PATTERNS[name].render(layout, t, level)
    assert len(frame) == 300
    colours, outside = zone_colours(frame)
    assert colours == expected
    assert outside == {OFF}  # pixels past the last zone stay dark


def test_pattern_table_shape():
    assert set(PATTERNS) == {"off", "idle", "selected", "unlocked", "open", "alarm", "fault",
                             "thanks"}
    assert {n for n, p in PATTERNS.items() if p.animated} == {"unlocked", "alarm", "thanks"}
    assert PATTERNS["thanks"].ends_after_s == 1.0 and PATTERNS["thanks"].then == "idle"


def test_breathing_is_smooth_and_never_dark(layout):
    values = [PATTERNS["unlocked"].render(layout, t / 100, 1)[0] for t in range(150)]
    assert values[0] == UNLOCKED and min(v[0] for v in values) == round(233 * BREATH_FLOOR)
    assert values[150 - 1] != OFF and all(v != OFF for v in values)
    assert math.isclose(values[75][0], 233 * BREATH_FLOOR, abs_tol=1.5)


# -- the mock -----------------------------------------------------------------------------

def test_mock_records_calls_and_renders_frames(make_config):
    leds = MockLeds(make_config())
    assert leds.status() == {"enabled": True, "reachable": True, "since": None, "pattern": "off",
                             "level": None, "brightness": 0.6}
    leds.set_pattern("selected", 3)
    leds.fill((10, 20, 30))
    leds.light_level(2, (1, 2, 3))
    leds.off()
    assert [c[1:] for c in leds.calls] == [
        ("set_pattern", "selected", 3), ("fill", (10, 20, 30)), ("light_level", 2, (1, 2, 3)),
        ("set_pattern", "off", None)]
    assert all(isinstance(c[0], float) for c in leds.calls)
    selected, fill, light, off = leds.frames
    dim_idle, amber = scale(IDLE, 0.6), scale(SELECTED, 0.6)
    assert zone_colours(selected)[0] == [dim_idle] * 2 + [amber] + [dim_idle] * 7
    assert set(fill) == {(6, 12, 18)}
    assert zone_colours(light)[0] == [OFF, (1, 1, 2)] + [OFF] * 8
    assert set(off) == {OFF} and leds.frame is off
    with pytest.raises(ValueError, match="unknown LED pattern 'disco'"):
        leds.set_pattern("disco")
    leds.set_brightness(1.0)
    assert leds.status()["brightness"] == 1.0 and set(leds.frame) == {OFF}


# -- the ArtNet sender against the fake -----------------------------------------------------

@pytest.fixture
async def fake_artnet():
    fake = FakeArtnet(ZONES)
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def make_leds(make_config, fake_artnet):
    started = []

    async def _make(pixel_count: int = 120, health_poll_s: float = 10.0,
                    universe: int = 0) -> ArtnetLeds:
        config = make_config()
        wled = config.hardware.wled
        wled.ip_address, wled.port, wled.pixel_count = "127.0.0.1", fake_artnet.port, pixel_count
        wled.health_poll_s, wled.universe = health_poll_s, universe
        leds = ArtnetLeds(config, health_url=f"http://127.0.0.1:{fake_artnet.port}/json/info")
        await leds.start()
        started.append(leds)
        return leds

    yield _make
    for leds in started:
        await leds.stop()


async def test_steady_pattern_is_sent_once_and_animated_ones_at_fps(make_leds, fake_artnet):
    leds = await make_leds()
    await wait_until(lambda: fake_artnet.frames_received == 1, "the off frame")  # start sends off
    leds.set_pattern("idle")
    await asyncio.sleep(0.3)
    assert fake_artnet.frames_received == 2  # one frame on change, no keepalive yet (1 s)
    assert fake_artnet.zone_colours() == [scale(IDLE, 0.6)] * 10
    before = fake_artnet.frames_received
    leds.set_pattern("alarm")
    await asyncio.sleep(0.3)
    frames = fake_artnet.frames_received - before
    assert 6 <= frames <= 12, frames  # 30 fps for 0.3 s, give or take scheduling
    seen = set()
    for _ in range(20):
        seen.add(fake_artnet.zone_colour(1))
        await asyncio.sleep(0.03)
    assert seen == {scale(ALARM, 0.6), OFF}  # it flashes
    leds.set_pattern("off")
    await asyncio.sleep(0.1)
    before = fake_artnet.frames_received
    await asyncio.sleep(0.3)
    assert fake_artnet.frames_received == before  # nothing animates, nothing is sent


async def test_keepalive_resends_a_steady_frame(make_leds, fake_artnet, monkeypatch):
    monkeypatch.setattr(ArtnetLeds, "keepalive_s", 0.05)
    leds = await make_leds()
    leds.set_pattern("fault")
    await asyncio.sleep(0.3)
    assert fake_artnet.frames_received >= 4
    assert fake_artnet.zone_colours() == [scale(scale(FAULT, 0.5), 0.6)] * 10


async def test_thanks_fades_to_idle_and_an_idle_request_lets_it_finish(make_leds, fake_artnet):
    leds = await make_leds()
    leds.set_pattern("thanks", 3)
    leds.set_pattern("idle")  # what Feedback does a few ms later: the fade keeps going
    assert leds.status()["pattern"] == "thanks" and leds.status()["level"] == 3
    await asyncio.sleep(0.1)
    mid = fake_artnet.zone_colour(3)
    assert mid != scale(OPEN, 0.6) and mid != scale(IDLE, 0.6)
    await wait_until(lambda: leds.status()["pattern"] == "idle", "the fade to end", timeout=2.0)
    assert leds.status()["level"] is None
    await wait_until(lambda: fake_artnet.zone_colours() == [scale(IDLE, 0.6)] * 10, "idle frame")
    leds.set_pattern("thanks", 3)
    leds.set_pattern("selected", 2)  # anything else replaces the fade at once
    assert leds.status()["pattern"] == "selected"


@pytest.mark.parametrize("pixel_count,universes", [(120, 1), (300, 2)])
async def test_universe_split_at_170_pixels(make_leds, fake_artnet, pixel_count, universes):
    leds = await make_leds(pixel_count=pixel_count)
    leds.set_brightness(1.0)
    leds.fill((1, 2, 3))
    await wait_until(lambda: len(fake_artnet.frames) == universes and
                     fake_artnet.pixel(pixel_count - 1) == (1, 2, 3), "the fill frame")
    assert sorted(fake_artnet.frames) == list(range(universes))
    assert len(fake_artnet.frames[0]) == min(pixel_count, 170) * 3
    if universes == 2:
        assert len(fake_artnet.frames[1]) == (pixel_count - 170) * 3
        assert fake_artnet.pixel(169) == (1, 2, 3) and fake_artnet.pixel(170) == (1, 2, 3)
    assert all(fake_artnet.pixel(i) == (1, 2, 3) for i in range(pixel_count))


async def test_brightness_scales_every_frame(make_leds, fake_artnet):
    leds = await make_leds()
    leds.set_pattern("selected", 1)
    await wait_until(lambda: fake_artnet.zone_colour(1) == scale(SELECTED, 0.6), "60 %")
    assert fake_artnet.zone_colour(1) == (140, 97, 35)
    leds.set_brightness(0.25)  # a steady pattern is resent at once
    await wait_until(lambda: fake_artnet.zone_colour(1) == scale(SELECTED, 0.25), "25 %")
    assert fake_artnet.zone_colour(2) == scale(IDLE, 0.25)
    leds.set_brightness(7)
    assert leds.status()["brightness"] == 1.0


async def test_runtime_json_overrides_the_configured_brightness(make_config, tmp_path):
    from monitoni.runtime import Runtime

    config = make_config()
    (tmp_path / "runtime.json").write_text('{"brightness": 0.1}')
    Runtime.load(tmp_path / "runtime.json", config).apply(config)
    leds = MockLeds(config)
    assert leds.status()["brightness"] == 0.1
    leds.set_pattern("idle")
    assert set(zone_colours(leds.frame)[0]) == {scale(IDLE, 0.1)}


async def test_health_poll_reports_reachability_changes(make_leds, fake_artnet):
    leds = await make_leds(health_poll_s=0.05)
    seen: list[bool] = []
    leds.on_reachability = seen.append
    await wait_until(lambda: leds.reachable is True, "reachable")
    assert seen == [True] or seen == []  # the first poll may have run before the hook was set
    assert leds.since is None  # fine since the start
    fake_artnet.http_up = False
    await wait_until(lambda: leds.reachable is False, "unreachable")
    assert seen[-1] is False
    assert leds.since is not None and leds.status()["since"] == leds.since
    went_bad = leds.since
    fake_artnet.http_up = True
    await wait_until(lambda: leds.reachable is True, "reachable again")
    assert seen[-2:] == [False, True]
    assert leds.status()["reachable"] is True
    assert leds.since is not None and leds.since >= went_bad  # the flip back is stamped too


async def test_unreachable_controller_is_status_only(make_config):
    config = make_config()
    config.hardware.wled.ip_address, config.hardware.wled.port = "127.0.0.1", 1  # nobody listens
    config.hardware.wled.health_poll_s = 0.05
    leds = ArtnetLeds(config, health_url="http://127.0.0.1:1/json/info")
    await leds.start()
    try:
        leds.set_pattern("alarm")
        await wait_until(lambda: leds.reachable is False, "unreachable")
        await asyncio.sleep(0.1)
        assert leds.frames_sent > 2 and leds.status()["pattern"] == "alarm"  # still sending
    finally:
        await leds.stop()


# -- the packets themselves ----------------------------------------------------------------

def test_artdmx_packet_matches_the_spec():
    data = bytes(range(256)) + bytes(134)  # 130 pixels = 390 channels
    packet = artdmx_packet(1, 7, data)
    assert len(packet) == 18 + 390
    assert packet[:8] == b"Art-Net\0"
    assert packet[8:10] == b"\x00\x50"  # OpDmx 0x5000, low byte first
    assert packet[10:12] == b"\x00\x0e"  # protocol version 14, high byte first
    assert packet[12] == 7 and packet[13] == 0  # sequence, physical
    assert packet[14:16] == b"\x01\x00"  # universe 1, low byte first
    assert packet[16:18] == b"\x01\x86"  # length 390, high byte first
    assert packet[18:] == data
    odd = artdmx_packet(0x7FFF, 255, b"\x01\x02\x03")
    assert odd[14:16] == b"\xff\x7f" and odd[16:18] == b"\x00\x04"  # 15-bit universe, even length
    assert odd[18:] == b"\x01\x02\x03\x00"
    for sequence, channels in ((0, 6), (256, 6), (1, 0), (1, 513)):
        with pytest.raises(ValueError):
            artdmx_packet(0, sequence, bytes(channels))


async def test_universe_1_with_130_pixels_on_the_wire(make_leds, fake_artnet):
    leds = await make_leds(pixel_count=130, universe=1)
    leds.set_brightness(1.0)
    leds.fill((9, 8, 7))
    await wait_until(lambda: fake_artnet.frames.get(1, b"")[-3:] == bytes((9, 8, 7)), "the frame")
    assert list(fake_artnet.frames) == [1] and len(fake_artnet.frames[1]) == 390
    assert fake_artnet.frames[1] == bytes((9, 8, 7)) * 130


class FailingSocket:
    """Stands in for the UDP socket while the network is down."""

    def __init__(self) -> None:
        self.attempts = 0

    def sendto(self, packet, target):
        self.attempts += 1
        raise OSError(51, "Network is unreachable")

    def close(self) -> None:
        pass


async def test_send_failures_are_logged_once_each_way(make_leds, fake_artnet, monkeypatch, caplog):
    leds = await make_leds()
    leds.set_pattern("idle")
    await wait_until(lambda: fake_artnet.frames_received >= 2, "frames before the outage")
    real_socket, failing = leds._socket, FailingSocket()
    with caplog.at_level(logging.INFO, logger="monitoni.leds"):
        monkeypatch.setattr(leds, "_socket", failing)
        leds.set_pattern("alarm")  # 30 frames a second, every one of them failing
        await asyncio.sleep(0.3)
        assert failing.attempts >= 6
        monkeypatch.setattr(leds, "_socket", real_socket)
        await wait_until(lambda: fake_artnet.frames_received >= 4, "frames after the outage")
    messages = [r.getMessage() for r in caplog.records if "ArtNet to" in r.getMessage()]
    assert len(messages) == 2
    assert messages[0].endswith("failing: [Errno 51] Network is unreachable")
    assert messages[1].endswith("works again")
    assert leds.status()["pattern"] == "alarm"  # nothing else changed


async def test_a_clean_stop_sends_one_dark_frame(make_leds, fake_artnet):
    leds = await make_leds()
    leds.set_pattern("idle")
    await wait_until(lambda: fake_artnet.zone_colours() == [scale(IDLE, 0.6)] * 10, "idle")
    sent = fake_artnet.frames_received
    await leds.stop()
    await wait_until(lambda: fake_artnet.frames_received == sent + 1, "the dark frame")
    assert fake_artnet.zone_colours() == [OFF] * 10
    await asyncio.sleep(0.1)
    assert fake_artnet.frames_received == sent + 1  # and nothing after it
    await leds.stop()  # idempotent
