"""The HTTP purchase client against the fake purchase server: results, errors, reachability."""

import pytest

from monitoni.purchase import HttpPurchaseServer, Invalid, NotYet, Paid, PurchaseServerError
from tests.conftest import http_purchase

CHECK = "/api/purchase/check"
COMPLETE = "/api/purchase/complete"


@pytest.fixture
async def http(make_config, purchase_fake):
    server = http_purchase(make_config(), purchase_fake)
    await server.start()
    yield server
    await server.stop()


async def test_not_yet_paid_and_invalid(http, purchase_fake):
    assert await http.check(3) == NotYet()
    purchase_fake.pay(3, purchase_id="p-1")
    assert await http.check(3) == Paid("p-1")
    assert await http.check(3) == NotYet()  # a paid purchase is reported once (fake's guess)
    purchase_fake.mark_invalid(3)
    assert await http.check(3) == Invalid()
    assert purchase_fake.requests[0] == (CHECK, {"machine_id": "VM001", "level": 3})
    assert http.reachable is True and http.last_ok is not None and http.last_error is None


async def test_500_is_an_error_and_marks_the_server_unreachable(http, purchase_fake):
    purchase_fake.fail_next = 1
    with pytest.raises(PurchaseServerError, match="HTTP 500"):
        await http.check(1)
    assert http.reachable is False and http.last_error == "HTTP 500 from /api/purchase/check"
    assert await http.check(1) == NotYet()
    assert http.reachable is True and http.last_error is None


async def test_timeout_is_an_error(http, purchase_fake):
    purchase_fake.fail_mode = "timeout"
    purchase_fake.fail_next = 1
    with pytest.raises(PurchaseServerError, match="Timeout"):
        await http.check(1)
    assert http.reachable is False and "Timeout" in http.last_error


async def test_connection_refused_is_an_error(http, purchase_fake):
    await purchase_fake.stop()
    with pytest.raises(PurchaseServerError, match="ConnectError"):
        await http.check(1)
    assert http.reachable is False


async def test_reachability_callback_fires_on_changes_only(http, purchase_fake):
    seen: list[bool] = []
    http.on_reachability = seen.append
    await http.check(1)
    await http.check(1)
    assert seen == [True]
    purchase_fake.fail_next = 2
    for _ in range(2):
        with pytest.raises(PurchaseServerError):
            await http.check(1)
    assert seen == [True, False]
    await http.check(1)
    assert seen == [True, False, True]


async def test_complete_success_and_failure(http, purchase_fake):
    assert await http.complete("p-1", 3, True) is True
    assert purchase_fake.completions == [
        {"purchase_id": "p-1", "machine_id": "VM001", "level": 3, "success": True}]
    assert purchase_fake.requests[-1] == (COMPLETE, purchase_fake.completions[0])
    purchase_fake.fail_next = 1
    assert await http.complete("p-2", 4, False) is False
    assert http.reachable is False and http.last_error == "HTTP 500 from /api/purchase/complete"


async def test_malformed_valid_answer_is_an_error(http, purchase_fake):
    purchase_fake.paid[("VM001", 2)] = ""  # valid: true without a usable id
    with pytest.raises(PurchaseServerError, match="without a purchase_id"):
        await http.check(2)


async def test_unstarted_client_raises(make_config, purchase_fake):
    server = http_purchase(make_config(), purchase_fake)
    with pytest.raises(PurchaseServerError, match="not started"):
        await server.check(1)


def test_status_shape():
    server = HttpPurchaseServer.__new__(HttpPurchaseServer)
    server.reachable, server.last_ok, server.last_error = None, None, None
    assert server.status() == {"reachable": None, "last_ok": None, "last_error": None}
