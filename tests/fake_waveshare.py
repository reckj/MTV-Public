"""A fake Waveshare relay module: Modbus RTU frames over TCP, one slave.

Used by the tests and, via `python -m tests.fake_waveshare --port N`, for manual runs of the
daemon in real mode. Speaks FC05 (write single coil, including the all-coils address 0x00FF),
FC01 (read coils) and FC02 (read discrete inputs). Like the real module it stays silent on a bad
CRC or a foreign slave address. Knobs, each consumed by the next request: drop the connection,
swallow the request (the client times out), corrupt the response CRC, answer with an exception
response. `ignore_coil_writes` acknowledges writes without changing the coil (read-back mismatch).

Frames (slave 1):
  FC05 write coil 3 on       01 05 00 02 FF 00 + CRC   response: echo
  FC05 all coils off         01 05 00 FF 00 00 + CRC   response: echo
  FC01 read coils 1..30      01 01 00 00 00 1E + CRC   response: 01 01 04 <4 data bytes> + CRC
  FC02 read DI1              01 02 00 00 00 01 + CRC   response: 01 02 01 <data byte> + CRC
  exception                                            response: 01 <fc|0x80> <code> + CRC
Data bytes pack bits LSB first: coil/DI 1 is bit 0 of the first data byte.
"""

import argparse
import asyncio
import contextlib
import logging

from monitoni.hardware.modbus import (
    ALL_COILS,
    EXCEPTION_FLAG,
    FC_READ_COILS,
    FC_READ_DISCRETE_INPUTS,
    FC_WRITE_COIL,
    crc16,
    with_crc,
)

log = logging.getLogger(__name__)


class FakeWaveshare:
    def __init__(self, slave_address: int = 1, coils: int = 8, inputs: int = 8) -> None:
        self.slave_address = slave_address
        self.coils = [False] * coils
        self.inputs = [False] * inputs
        self.requests: list[bytes] = []  # every frame received, oldest first
        self.connections = 0
        # knobs, each one consumed by the next request
        self.drop_next = False
        self.swallow_next = False
        self.corrupt_next_crc = False
        self.exception_next: int | None = None
        self.ignore_coil_writes = False  # acknowledge FC05 but keep the coil as it is
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await asyncio.start_server(self._serve, host, port)

    async def stop(self) -> None:
        await self.drop_connections()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_connections(self) -> None:
        """Close every client connection, as a rebooted PoE switch would."""
        writers, self._writers = list(self._writers), set()
        for writer in writers:
            writer.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1.0):
                    await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        try:
            while True:
                request = await reader.readexactly(8)  # FC01/02/05 requests are all 8 bytes
                self.requests.append(request)
                if self.drop_next:
                    self.drop_next = False
                    break
                if self.swallow_next:
                    self.swallow_next = False
                    continue
                response = self.respond(request)
                if response is None:
                    continue
                if self.corrupt_next_crc:
                    self.corrupt_next_crc = False
                    response = response[:-1] + bytes((response[-1] ^ 0xFF,))
                writer.write(response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    def respond(self, request: bytes) -> bytes | None:
        """The response frame for a request, or None where a real module stays silent."""
        slave, fc = request[0], request[1]
        if crc16(request[:-2]) != request[-2] | request[-1] << 8 or slave != self.slave_address:
            return None
        if self.exception_next is not None:
            code, self.exception_next = self.exception_next, None
            return self._exception(fc, code)
        address = request[2] << 8 | request[3]
        value = request[4] << 8 | request[5]
        if fc == FC_WRITE_COIL:
            if value not in (0xFF00, 0x0000):
                return self._exception(fc, 3)
            if address != ALL_COILS and address >= len(self.coils):
                return self._exception(fc, 2)
            if not self.ignore_coil_writes:
                on = value == 0xFF00
                if address == ALL_COILS:
                    self.coils = [on] * len(self.coils)
                else:
                    self.coils[address] = on
            return request
        if fc in (FC_READ_COILS, FC_READ_DISCRETE_INPUTS):
            bits = self.coils if fc == FC_READ_COILS else self.inputs
            if value < 1 or address + value > len(bits):
                return self._exception(fc, 2)
            data = bytearray((value + 7) // 8)
            for i, bit in enumerate(bits[address:address + value]):
                if bit:
                    data[i // 8] |= 1 << (i % 8)
            return with_crc(bytes((slave, fc, len(data))) + bytes(data))
        return self._exception(fc, 1)

    def _exception(self, fc: int, code: int) -> bytes:
        return with_crc(bytes((self.slave_address, fc | EXCEPTION_FLAG, code)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="fake Waveshare Modbus module for manual runs")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--coils", type=int, default=8)
    parser.add_argument("--inputs", type=int, default=8)
    parser.add_argument("--slave", type=int, default=1)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async def run() -> None:
        fake = FakeWaveshare(args.slave, args.coils, args.inputs)
        await fake.start(port=args.port)
        log.info("fake Waveshare: %d coils, %d inputs, slave %d, listening on 127.0.0.1:%d",
                 args.coils, args.inputs, args.slave, fake.port)
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
