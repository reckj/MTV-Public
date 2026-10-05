"""Modbus RTU frames over a plain TCP socket (Waveshare "transparent mode"): no MBAP, no pymodbus.

One ModbusTcpModule per physical module. A failed or unanswered frame closes the socket and
raises HardwareError; nothing is ever retried. `reconnect_loop` reconnects the transport only.

Timeouts use `asyncio.timeout()`, never `asyncio.wait_for()`: on Python 3.11 the latter can swallow
a task cancellation when the awaited future completes in the same loop iteration, which left a
cancelled poll task running and `stop()` waiting for it forever.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import NoReturn, TypeVar

from monitoni.hardware.base import HardwareError

log = logging.getLogger(__name__)

RECONNECT_BACKOFF: Sequence[float] = (2, 5, 10, 30)  # seconds between attempts while disconnected
MONITOR_INTERVAL_S = 1.0  # how often a connected module is checked for a close by the peer
ALL_COILS = 0x00FF  # Waveshare: FC05 to this address switches every relay on the module

FC_READ_COILS = 0x01
FC_READ_DISCRETE_INPUTS = 0x02
FC_WRITE_COIL = 0x05
EXCEPTION_FLAG = 0x80
EXCEPTION_NAMES = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
                   4: "slave device failure"}

T = TypeVar("T")


# -- frames --------------------------------------------------------------------

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(payload: bytes) -> bytes:
    """Append the CRC, low byte first, as Modbus RTU wants it."""
    crc = crc16(payload)
    return payload + bytes((crc & 0xFF, crc >> 8))


def write_coil_frame(slave: int, address: int, on: bool) -> bytes:
    """FC05 write single coil. `address` is the 0-based coil address, or ALL_COILS."""
    return with_crc(bytes((slave, FC_WRITE_COIL, address >> 8, address & 0xFF,
                           0xFF if on else 0x00, 0x00)))


def read_coils_frame(slave: int, start: int, count: int) -> bytes:
    """FC01 read coils, `start` 0-based."""
    return _read_bits_frame(slave, FC_READ_COILS, start, count)


def read_discrete_inputs_frame(slave: int, start: int, count: int) -> bytes:
    """FC02 read discrete inputs, `start` 0-based."""
    return _read_bits_frame(slave, FC_READ_DISCRETE_INPUTS, start, count)


def _read_bits_frame(slave: int, fc: int, start: int, count: int) -> bytes:
    return with_crc(bytes((slave, fc, start >> 8, start & 0xFF, count >> 8, count & 0xFF)))


def check_response(response: bytes, slave: int, fc: int) -> None:
    """Length, CRC, slave address, exception response and function code. Raises ValueError."""
    if len(response) < 5:
        raise ValueError(f"response too short: {response.hex(' ')}")
    if crc16(response[:-2]) != response[-2] | response[-1] << 8:
        raise ValueError(f"bad CRC in response {response.hex(' ')}")
    if response[0] != slave:
        raise ValueError(f"response from slave {response[0]}, expected {slave}")
    if response[1] == fc | EXCEPTION_FLAG:
        code = response[2]
        raise ValueError(f"exception response code {code} ({EXCEPTION_NAMES.get(code, 'unknown')})")
    if response[1] != fc:
        raise ValueError(f"function code {response[1]:#04x} in response, expected {fc:#04x}")


def parse_write_coil_response(response: bytes, request: bytes) -> None:
    """FC05 echoes the request byte for byte; anything else is an error."""
    check_response(response, request[0], FC_WRITE_COIL)
    if response != request:
        raise ValueError(f"write coil response {response.hex(' ')} does not echo "
                         f"request {request.hex(' ')}")


def parse_read_bits_response(response: bytes, slave: int, fc: int, count: int) -> list[bool]:
    """FC01/FC02: [slave][fc][byte count][bits, LSB first][crc lo][crc hi]."""
    check_response(response, slave, fc)
    byte_count = response[2]
    if byte_count != (count + 7) // 8 or len(response) != 3 + byte_count + 2:
        raise ValueError(f"expected {(count + 7) // 8} data bytes for {count} bits, "
                         f"got {response.hex(' ')}")
    data = response[3:3 + byte_count]
    return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


# -- one module ----------------------------------------------------------------

class ModbusTcpModule:
    """A Waveshare module in transparent mode: one TCP connection, one frame at a time."""

    def __init__(self, name: str, host: str, port: int, slave_address: int, timeout: float,
                 max_channels: int, *, on_lost: Callable[[str, str], None] | None = None,
                 backoff: Sequence[float] | None = None,
                 monitor_interval: float | None = None) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.slave_address = slave_address
        self.timeout = timeout
        self.max_channels = max_channels
        self.last_error: str | None = None
        self._on_lost = on_lost  # called once each time an open connection is lost
        self._backoff = tuple(RECONNECT_BACKOFF if backoff is None else backoff)
        self._monitor_interval = (MONITOR_INTERVAL_S if monitor_interval is None
                                  else monitor_interval)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return (self._writer is not None and not self._writer.is_closing()
                and not self._reader.at_eof())

    def status(self) -> dict:
        return {"connected": self.connected, "host": self.host, "port": self.port,
                "last_error": self.last_error}

    async def connect(self) -> None:
        """Open the TCP connection. Raises HardwareError if the module is unreachable."""
        try:
            async with asyncio.timeout(self.timeout):
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        except (OSError, TimeoutError) as exc:
            self._reader = self._writer = None
            self.last_error = f"connect failed: {exc or type(exc).__name__}"
            raise HardwareError(f"{self.name} {self.host}:{self.port}: {self.last_error}") from None
        self.last_error = None
        log.info("%s connected to %s:%d", self.name, self.host, self.port)

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):  # incl. TimeoutError: never wait on a dead peer
                async with asyncio.timeout(1.0):
                    await writer.wait_closed()

    async def reconnect_loop(self) -> None:
        """Notice a connection the module closed and reconnect with backoff. Replays nothing."""
        attempt = 0
        while True:
            if self._writer is not None:
                if self.connected:
                    attempt = 0
                    await asyncio.sleep(self._monitor_interval)
                    continue
                with contextlib.suppress(HardwareError):
                    await self._fail("connection closed by the module")
            await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            try:
                await self.connect()
            except HardwareError as exc:
                attempt += 1
                log.warning("%s; next attempt in %ss", exc,
                            self._backoff[min(attempt, len(self._backoff) - 1)])

    # -- commands (each exactly one frame on the wire) -----------------------

    async def write_coil(self, channel: int, on: bool) -> None:
        """Switch one relay. `channel` is 1-based, as printed on the module."""
        if not 1 <= channel <= self.max_channels:
            raise ValueError(f"{self.name}: channel {channel} outside 1..{self.max_channels}")
        request = write_coil_frame(self.slave_address, channel - 1, on)
        await self._request(request, FC_WRITE_COIL,
                            lambda raw: parse_write_coil_response(raw, request))

    async def write_all_coils(self, on: bool) -> None:
        request = write_coil_frame(self.slave_address, ALL_COILS, on)
        await self._request(request, FC_WRITE_COIL,
                            lambda raw: parse_write_coil_response(raw, request))

    async def read_coils(self, start: int, count: int) -> list[bool]:
        """Relay states, `start` 0-based (channel 1 is address 0)."""
        request = read_coils_frame(self.slave_address, start, count)
        return await self._request(
            request, FC_READ_COILS,
            lambda raw: parse_read_bits_response(raw, self.slave_address, FC_READ_COILS, count))

    async def read_discrete_inputs(self, start: int, count: int) -> list[bool]:
        """Digital input states, `start` 0-based (DI1 is address 0)."""
        request = read_discrete_inputs_frame(self.slave_address, start, count)
        return await self._request(
            request, FC_READ_DISCRETE_INPUTS,
            lambda raw: parse_read_bits_response(raw, self.slave_address,
                                                 FC_READ_DISCRETE_INPUTS, count))

    async def _request(self, request: bytes, fc: int, parse: Callable[[bytes], T]) -> T:
        """Send one frame, read and parse its response. Any problem closes the socket and raises."""
        async with self._lock:
            if not self.connected:
                raise HardwareError(f"{self.name}: not connected")
            try:
                self._writer.write(request)
                await self._writer.drain()
                async with asyncio.timeout(self.timeout):
                    raw = await self._read_response(fc)
                return parse(raw)
            except TimeoutError:
                await self._fail(f"no response within {self.timeout}s to {request.hex(' ')}")
            except asyncio.IncompleteReadError:
                await self._fail("connection closed while waiting for a response")
            except OSError as exc:
                await self._fail(f"socket error: {exc}")
            except ValueError as exc:
                await self._fail(f"bad response to {request.hex(' ')}: {exc}")

    async def _read_response(self, fc: int) -> bytes:
        head = await self._reader.readexactly(3)
        if head[1] & EXCEPTION_FLAG:
            return head + await self._reader.readexactly(2)  # byte 3 was the code; CRC follows
        if fc == FC_WRITE_COIL:
            return head + await self._reader.readexactly(5)  # echo of the 8-byte request
        return head + await self._reader.readexactly(head[2] + 2)  # byte count, data, CRC

    async def _fail(self, error: str) -> NoReturn:
        self.last_error = error
        log.error("%s: %s", self.name, error)
        was_open = self._writer is not None
        await self.close()
        if was_open and self._on_lost is not None:
            self._on_lost(self.name, error)
        raise HardwareError(f"{self.name}: {error}")
