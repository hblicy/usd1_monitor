import traceback

import pytest

from tests.fakes import FakeHttp
from usd1_monitor.rpc import JsonRpcClient, RpcError


@pytest.mark.asyncio
async def test_rpc_falls_back_and_returns_result() -> None:
    fake_http = FakeHttp()
    fake_http.queue_error(
        "https://rpc-one.example", TimeoutError("slow"), method="POST"
    )
    fake_http.queue_json(
        "https://rpc-two.example",
        {"jsonrpc": "2.0", "id": 2, "result": "0x10"},
        method="POST",
    )
    rpc = JsonRpcClient(
        ["https://rpc-one.example", "https://rpc-two.example"], fake_http
    )

    assert await rpc.call("eth_blockNumber", []) == "0x10"


@pytest.mark.asyncio
async def test_rpc_error_object_is_not_a_result() -> None:
    fake_http = FakeHttp()
    fake_http.queue_json(
        "https://rpc-one.example",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "bad block"},
        },
        method="POST",
    )
    rpc = JsonRpcClient(["https://rpc-one.example"], fake_http)

    with pytest.raises(RpcError, match="-32000"):
        await rpc.call("eth_getLogs", [])


@pytest.mark.asyncio
async def test_rpc_failure_summary_redacts_endpoint_queries() -> None:
    fake_http = FakeHttp()
    url = "https://rpc-one.example/path?api_key=secret"
    fake_http.queue_error(url, TimeoutError("slow"), method="POST")

    with pytest.raises(RpcError) as raised:
        await JsonRpcClient([url], fake_http).call("eth_blockNumber", [])

    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_rpc_rejects_endpoint_with_wrong_chain_id() -> None:
    fake_http = FakeHttp()
    url = "https://rpc-one.example"
    fake_http.queue_json(
        url, {"jsonrpc": "2.0", "id": 1, "result": "0x38"}, method="POST"
    )

    rpc = JsonRpcClient([url], fake_http, expected_chain_id=1)

    with pytest.raises(RpcError, match="chain id"):
        await rpc.call("eth_blockNumber", [])
    assert fake_http.calls[0][2]["method"] == "eth_chainId"


@pytest.mark.asyncio
async def test_rpc_chain_mismatch_is_not_hidden_by_fallback() -> None:
    fake_http = FakeHttp()
    fake_http.queue_json(
        "https://wrong.example",
        {"jsonrpc": "2.0", "id": 1, "result": "0x38"},
        method="POST",
    )
    fake_http.queue_json(
        "https://right.example",
        {"jsonrpc": "2.0", "id": 2, "result": "0x1"},
        method="POST",
    )
    rpc = JsonRpcClient(
        ["https://wrong.example", "https://right.example"],
        fake_http,
        expected_chain_id=1,
    )

    with pytest.raises(RpcError, match="chain id mismatch"):
        await rpc.call("eth_blockNumber", [])

    assert [call[1] for call in fake_http.calls] == ["https://wrong.example"]


@pytest.mark.asyncio
async def test_rpc_revalidates_chain_after_endpoint_switch() -> None:
    primary = "https://primary.example"
    secondary = "https://secondary.example"
    fake_http = FakeHttp()
    fake_http.queue_json(primary, {"result": "0x1"}, method="POST")
    fake_http.queue_json(primary, {"result": "0x10"}, method="POST")
    fake_http.queue_error(primary, TimeoutError("offline"), method="POST")
    fake_http.queue_json(secondary, {"result": "0x1"}, method="POST")
    fake_http.queue_json(secondary, {"result": "0x11"}, method="POST")
    fake_http.queue_json(primary, {"result": "0x1"}, method="POST")
    fake_http.queue_json(primary, {"result": "0x12"}, method="POST")
    rpc = JsonRpcClient(
        [primary, secondary], fake_http, expected_chain_id=1
    )

    assert await rpc.call("eth_blockNumber", []) == "0x10"
    assert await rpc.call("eth_blockNumber", []) == "0x11"
    assert await rpc.call("eth_blockNumber", []) == "0x12"

    primary_methods = [
        call[2]["method"] for call in fake_http.calls if call[1] == primary
    ]
    assert primary_methods == [
        "eth_chainId",
        "eth_blockNumber",
        "eth_blockNumber",
        "eth_chainId",
        "eth_blockNumber",
    ]


@pytest.mark.asyncio
async def test_rpc_response_error_does_not_expose_remote_message() -> None:
    fake_http = FakeHttp()
    secret = "upstream-secret-token"
    fake_http.queue_json(
        "https://rpc.example",
        {"error": {"code": -32000, "message": secret}},
        method="POST",
    )
    rpc = JsonRpcClient(["https://rpc.example"], fake_http)

    rendered = ""
    with pytest.raises(RpcError):
        try:
            await rpc.call("eth_call", [])
        except RpcError:
            rendered = traceback.format_exc()
            raise

    assert "-32000" in rendered
    assert secret not in rendered
