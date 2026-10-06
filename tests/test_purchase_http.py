"""The HTTP client against the fake Monitoni server: permission results, errors, reachability."""

import pytest

from monitoni.purchase import (
    TOKEN_HEADER,
    HttpPurchaseServer,
    NotYet,
    Permitted,
    PurchaseServerError,
)
from tests.conftest import http_purchase

PERMISSION = "/api/vending/permission"
COMPLETE = "/api/vending/complete"
CLOSE = "/api/vending/close"


@pytest.fixture
async def http(make_config, purchase_fake):
    server = http_purchase(make_config(), purchase_fake)
    await server.start()
    yield server
    await server.stop()


async def test_not_yet_then_permitted(http, purchase_fake):
    assert await http.permission() == NotYet()
    purchase_fake.pay(3)
    assert await http.permission() == Permitted(3)
    assert await http.permission() == Permitted(3)  # open until a complete consumes it
    assert http.reachable is True and http.last_ok is not None and http.last_error is None


async def test_every_request_is_a_bodyless_get_with_the_token_header(http, purchase_fake):
    await http.permission()
    await http.complete()
    await http.close()
    assert [r["path"] for r in purchase_fake.requests] == [PERMISSION, COMPLETE, CLOSE]
    assert all(r["method"] == "GET" and r["token_ok"] and r["body"] == b""
               for r in purchase_fake.requests)
    assert TOKEN_HEADER == "Monitoni-Terminal"


async def test_wrong_token_is_an_error(make_config, purchase_fake):
    server = http_purchase(make_config(), purchase_fake, token="not-the-token")
    await server.start()
    try:
        with pytest.raises(PurchaseServerError, match="HTTP 401") as info:
            await server.permission()
        assert "not-the-token" not in str(info.value)
        assert "not-the-token" not in str(server.status())
        assert server.reachable is False
    finally:
        await server.stop()


async def test_500_is_an_error_and_marks_the_server_unreachable(http, purchase_fake):
    purchase_fake.fail_next = 1
    with pytest.raises(PurchaseServerError, match="HTTP 500"):
        await http.permission()
    assert http.reachable is False and http.last_error == "HTTP 500 from /api/vending/permission"
    assert await http.permission() == NotYet()
    assert http.reachable is True and http.last_error is None


async def test_timeout_is_an_error(http, purchase_fake):
    purchase_fake.fail_mode = "timeout"
    purchase_fake.fail_next = 1
    with pytest.raises(PurchaseServerError, match="Timeout"):
        await http.permission()
    assert http.reachable is False and "Timeout" in http.last_error


async def test_connection_refused_is_an_error(http, purchase_fake):
    await purchase_fake.stop()
    with pytest.raises(PurchaseServerError, match="ConnectError"):
        await http.permission()
    assert http.reachable is False


@pytest.mark.parametrize("body,message", [
    ("not json", "without JSON"),
    ('{"Permission": true}', "without a HasPermission key"),
    ('{"HasPermission": true}', "without an integer Item"),
    ('{"HasPermission": true, "Item": "3"}', "without an integer Item"),
    ('{"HasPermission": "yes"}', "HasPermission='yes'"),
], ids=["not-json", "no-key", "no-item", "string-item", "odd-value"])
async def test_unusable_200_answers_are_errors(http, purchase_fake, body, message):
    from aiohttp import web

    async def odd_permission(request):
        return web.Response(text=body, content_type="application/json")

    purchase_fake._permission = odd_permission  # swap the handler for this test
    await purchase_fake.stop()
    await purchase_fake.start(port=purchase_fake.port)
    with pytest.raises(PurchaseServerError, match=message):
        await http.permission()
    assert http.reachable is False


async def test_reachability_callback_fires_on_changes_only(http, purchase_fake):
    seen: list[bool] = []
    http.on_reachability = seen.append
    await http.permission()
    await http.permission()
    assert seen == [True]
    purchase_fake.fail_next = 2
    for _ in range(2):
        with pytest.raises(PurchaseServerError):
            await http.permission()
    assert seen == [True, False]
    await http.permission()
    assert seen == [True, False, True]


async def test_complete_and_close_accept_201_and_report_500_as_false(http, purchase_fake):
    purchase_fake.pay(2)
    assert await http.complete() is True
    assert await http.close() is True
    assert purchase_fake.report_kinds() == ["complete", "close"]
    assert purchase_fake.permissions == []  # the complete consumed it
    purchase_fake.fail_next = 2
    assert await http.complete() is False
    assert await http.close() is False
    assert http.reachable is False and http.last_error == "HTTP 500 from /api/vending/close"


async def test_unstarted_client_raises(make_config, purchase_fake):
    server = http_purchase(make_config(), purchase_fake)
    with pytest.raises(PurchaseServerError, match="not started"):
        await server.permission()


def test_status_shape_has_no_token(make_config):
    config = make_config().purchase_server
    config.token = "secret-token"
    server = HttpPurchaseServer(config)
    assert server.status() == {"reachable": None, "last_ok": None, "last_error": None}
    assert "secret-token" not in repr(server.status())
