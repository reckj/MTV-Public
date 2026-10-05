"""Modbus RTU over TCP: frames, parsers, and the module's no-retry behaviour against the fake."""

import asyncio

import pytest

from monitoni.hardware.base import HardwareError
from monitoni.hardware.modbus import (
    ALL_COILS,
    ModbusTcpModule,
    crc16,
    parse_read_bits_response,
    parse_write_coil_response,
    read_coils_frame,
    read_discrete_inputs_frame,
    with_crc,
    write_coil_frame,
)
from tests.fake_waveshare import FakeWaveshare
from tests.helpers import wait_until


def hx(text: str) -> bytes:
    return bytes.fromhex(text)


# -- frames -------------------------------------------------------------------

def test_crc16_known_vectors():
    assert crc16(hx("01050000FF00")) == 0x3A8C  # on the wire: 8C 3A
    assert with_crc(hx("01050000FF00")) == hx("01050000FF008C3A")
    assert with_crc(hx("010300000001")) == hx("010300000001840A")  # the classic example


def test_write_coil_frames():
    assert write_coil_frame(1, 0, True) == hx("01050000FF008C3A")
    assert write_coil_frame(1, 2, False) == with_crc(hx("010500020000"))
    assert write_coil_frame(1, ALL_COILS, False) == with_crc(hx("010500FF0000"))
    assert write_coil_frame(2, ALL_COILS, True) == with_crc(hx("020500FFFF00"))


def test_read_frames():
    assert read_coils_frame(1, 0, 30) == with_crc(hx("01010000001E"))
    assert read_discrete_inputs_frame(1, 0, 1) == with_crc(hx("010200000001"))
    assert read_discrete_inputs_frame(1, 3, 2) == with_crc(hx("010200030002"))


def test_parse_write_coil_echo():
    request = write_coil_frame(1, 4, True)
    parse_write_coil_response(request, request)
    with pytest.raises(ValueError, match="does not echo"):
        parse_write_coil_response(write_coil_frame(1, 4, False), request)


def test_parse_read_bits():
    response = with_crc(bytes((1, 1, 2, 0b00000101, 0b00000010)))
    bits = parse_read_bits_response(response, 1, 1, 10)
    assert bits == [True, False, True, False, False, False, False, False, False, True]
    assert parse_read_bits_response(with_crc(bytes((1, 2, 1, 0x01))), 1, 2, 1) == [True]


@pytest.mark.parametrize("response,message", [
    (hx("0101"), "too short"),
    (hx("01010101") + hx("0000"), "bad CRC"),
    (with_crc(hx("02010101")), "from slave 2"),
    (with_crc(hx("018102")), "exception response code 2"),
    (with_crc(hx("01020101")), "function code 0x02"),
    (with_crc(hx("0101020101")), "expected 1 data bytes"),
], ids=["short", "crc", "slave", "exception", "fc", "byte-count"])
def test_parse_rejections(response, message):
    with pytest.raises(ValueError, match=message):
        parse_read_bits_response(response, 1, 1, 1)


# -- the module against the fake -----------------------------------------------

@pytest.fixture
async def fake():
    fake = FakeWaveshare(coils=8)
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def module(fake):
    module = ModbusTcpModule("test", "127.0.0.1", fake.port, 1, timeout=0.3, max_channels=8,
                             backoff=(0.05,), monitor_interval=0.02)
    await module.connect()
    yield module
    await module.close()


async def test_write_then_read_back(module, fake):
    await module.write_coil(3, True)
    assert fake.coils[2] is True
    assert await module.read_coils(0, 8) == [False, False, True, False, False, False, False, False]
    assert len(fake.requests) == 2
    assert fake.requests[0] == hx("01050002FF00") + fake.requests[0][6:]


async def test_write_all_coils(module, fake):
    fake.coils = [True] * 8
    await module.write_all_coils(False)
    assert fake.coils == [False] * 8
    assert fake.requests[-1][:6] == hx("010500FF0000")


async def test_read_discrete_inputs(module, fake):
    fake.inputs[0] = True
    assert await module.read_discrete_inputs(0, 1) == [True]
    fake.inputs[0] = False
    assert await module.read_discrete_inputs(0, 1) == [False]


async def test_channel_range_is_a_programming_error(module, fake):
    with pytest.raises(ValueError, match="outside 1..8"):
        await module.write_coil(9, True)
    assert fake.requests == []


async def test_timeout_closes_socket_and_never_retries(module, fake):
    fake.swallow_next = True
    with pytest.raises(HardwareError, match="no response within 0.3s"):
        await module.write_coil(1, True)
    assert not module.connected and "no response" in module.last_error
    assert len(fake.requests) == 1  # exactly one frame went out
    with pytest.raises(HardwareError, match="not connected"):
        await module.write_coil(1, True)
    assert len(fake.requests) == 1  # and nothing after the failure


async def test_bad_crc_closes_socket(module, fake):
    fake.corrupt_next_crc = True
    with pytest.raises(HardwareError, match="bad CRC"):
        await module.read_coils(0, 8)
    assert not module.connected


async def test_exception_response_closes_socket(module, fake):
    fake.exception_next = 2
    with pytest.raises(HardwareError, match="illegal data address"):
        await module.write_coil(1, True)
    assert not module.connected


async def test_peer_close_during_request(module, fake):
    fake.drop_next = True
    with pytest.raises(HardwareError, match="closed"):
        await module.write_coil(1, True)
    assert not module.connected


async def test_connect_failure_is_a_hardware_error():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    module = ModbusTcpModule("test", "127.0.0.1", port, 1, timeout=0.3, max_channels=8)
    with pytest.raises(HardwareError, match="connect failed"):
        await module.connect()
    assert not module.connected and module.last_error.startswith("connect failed")


async def test_on_lost_is_called_once_per_connection(fake):
    lost = []
    module = ModbusTcpModule("levels", "127.0.0.1", fake.port, 1, timeout=0.3, max_channels=8,
                             on_lost=lambda name, error: lost.append((name, error)))
    await module.connect()
    fake.swallow_next = True
    with pytest.raises(HardwareError):
        await module.write_coil(1, True)
    with pytest.raises(HardwareError):
        await module.write_coil(1, True)
    assert len(lost) == 1 and lost[0][0] == "levels" and "no response" in lost[0][1]


async def test_reconnect_loop_reconnects_after_failure(module, fake):
    loop_task = asyncio.create_task(module.reconnect_loop())
    try:
        fake.drop_next = True
        with pytest.raises(HardwareError):
            await module.write_coil(1, True)
        assert not module.connected
        await wait_until(lambda: module.connected, "reconnect")
        await wait_until(lambda: fake.connections == 2, "server side of the reconnect")
        await module.write_coil(1, True)  # works again, nothing was replayed
        assert fake.coils[0] is True
    finally:
        loop_task.cancel()


async def test_reconnect_loop_notices_a_connection_closed_by_the_module(fake):
    lost = []
    module = ModbusTcpModule("core", "127.0.0.1", fake.port, 1, timeout=0.3, max_channels=8,
                             backoff=(0.05,), monitor_interval=0.02,
                             on_lost=lambda name, error: lost.append(error))
    await module.connect()
    await wait_until(lambda: fake.connections == 1, "server side of the connection")
    loop_task = asyncio.create_task(module.reconnect_loop())
    try:
        await fake.drop_connections()
        await wait_until(lambda: lost, "loss noticed")
        assert lost == ["connection closed by the module"]
        await wait_until(lambda: module.connected, "reconnect")
        await wait_until(lambda: fake.connections == 2, "server side of the reconnect")
    finally:
        loop_task.cancel()
        await module.close()


async def test_reconnect_loop_keeps_trying_while_the_module_is_down():
    fake = FakeWaveshare(coils=8)
    await fake.start()
    port = fake.port
    await fake.stop()
    module = ModbusTcpModule("core", "127.0.0.1", port, 1, timeout=0.3, max_channels=8,
                             backoff=(0.05,))
    loop_task = asyncio.create_task(module.reconnect_loop())
    try:
        await asyncio.sleep(0.2)
        assert not module.connected and module.last_error.startswith("connect failed")
        await fake.start(port=port)
        await wait_until(lambda: module.connected, "connect once the module is back")
    finally:
        loop_task.cancel()
        await module.close()
        await fake.stop()
