"""Small helpers shared by the test modules."""

import asyncio


async def wait_until(predicate, what: str, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def wait_until_async(check, what: str, timeout: float = 2.0):
    """Like wait_until for an async check; returns the first truthy result."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await check()
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


# -- HTTP against a running Daemon ----------------------------------------------

async def command(client, daemon, **body):
    async with client.post(daemon.url + "/api/command", json=body) as resp:
        return resp.status, await resp.json()


async def status(client, daemon):
    async with client.get(daemon.url + "/api/status") as resp:
        assert resp.status == 200
        return await resp.json()


async def wait_for_state(client, daemon, state, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        current = await status(client, daemon)
        if current["state"] == state:
            return current
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"expected {state}, still in {current['state']}")
        await asyncio.sleep(0.01)


async def events(client, daemon, limit=100):
    async with client.get(daemon.url + f"/api/events?limit={limit}") as resp:
        return await resp.json()
