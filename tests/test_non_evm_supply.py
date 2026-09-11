from datetime import UTC, datetime

import pytest

from tests.fakes import FakeHttp, FakeRpc
from usd1_monitor.collectors.non_evm_supply import (
    AptosSupplyCollector,
    SolanaSupplyCollector,
    TronSupplyCollector,
)
from usd1_monitor.supply_assets import (
    APTOS_METADATA,
    APTOS_POOL,
    SOLANA_MINT,
    SOLANA_POOL_TOKEN_ACCOUNT,
    TRON_TOKEN,
)


NOW = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


def solana_mint_response(amount: str, decimals: int) -> dict:
    return {
        "value": {
            "data": {
                "parsed": {
                    "info": {"supply": amount, "decimals": decimals}
                }
            }
        }
    }


def solana_pool_response(amount: str, decimals: int) -> dict:
    return {
        "value": {
            "data": {
                "parsed": {
                    "info": {
                        "mint": SOLANA_MINT,
                        "tokenAmount": {
                            "amount": amount,
                            "decimals": decimals,
                        },
                    }
                }
            }
        }
    }


def aptos_response(*, supply: str, balance: str) -> dict:
    return {
        "data": {
            "fungible_asset_metadata": [
                {
                    "asset_type": APTOS_METADATA,
                    "decimals": 6,
                    "supply_v2": supply,
                }
            ],
            "current_fungible_asset_balances": [
                {
                    "asset_type": APTOS_METADATA,
                    "owner_address": APTOS_POOL,
                    "amount": balance,
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_tron_total_supply_uses_constant_contract() -> None:
    http = FakeHttp()
    url = "https://tron.example/wallet/triggerconstantcontract"
    http.queue_json(
        url,
        {
            "result": {"result": True},
            "constant_result": [f"{4_200_000 * 10**18:064x}"],
        },
        method="POST",
    )

    batch = await TronSupplyCollector(
        http, ["https://tron.example"]
    ).collect(NOW)

    assert batch.snapshots[0].supply == 4_200_000
    payload = http.calls[0][2]
    assert payload["function_selector"] == "totalSupply()"
    assert payload["contract_address"] == TRON_TOKEN
    assert payload["visible"] is True


@pytest.mark.asyncio
async def test_tron_missing_constant_result_marks_native_component_failed(
) -> None:
    http = FakeHttp()
    url = "https://tron.example/wallet/triggerconstantcontract"
    http.queue_json(url, {"result": {"result": True}}, method="POST")

    batch = await TronSupplyCollector(
        http, ["https://tron.example"]
    ).collect(NOW)

    assert [item[0] for item in batch.errors] == ["native_tron"]


@pytest.mark.asyncio
async def test_tron_uses_second_endpoint_after_network_error() -> None:
    first = "https://tron-one.example/wallet/triggerconstantcontract"
    second = "https://tron-two.example/wallet/triggerconstantcontract"
    http = FakeHttp()
    http.queue_error(first, TimeoutError("timeout"), method="POST")
    http.queue_json(
        second,
        {
            "result": {"result": True},
            "constant_result": [f"{4_200_000 * 10**18:064x}"],
        },
        method="POST",
    )

    batch = await TronSupplyCollector(
        http,
        ["https://tron-one.example", "https://tron-two.example"],
    ).collect(NOW)

    assert not batch.errors


@pytest.mark.asyncio
async def test_solana_reads_mint_and_pool_with_get_account_info() -> None:
    rpc = FakeRpc()
    rpc.result("getAccountInfo", solana_mint_response("4200000000000", 6))
    rpc.result("getAccountInfo", solana_pool_response("1250000000000", 6))

    batch = await SolanaSupplyCollector(rpc).collect(NOW)

    assert [item.supply for item in batch.snapshots] == [
        4_200_000,
        1_250_000,
    ]
    assert rpc.calls_for("getAccountInfo") == [
        [SOLANA_MINT, {"encoding": "jsonParsed", "commitment": "finalized"}],
        [
            SOLANA_POOL_TOKEN_ACCOUNT,
            {"encoding": "jsonParsed", "commitment": "finalized"},
        ],
    ]


@pytest.mark.asyncio
async def test_solana_wrong_decimals_only_fails_affected_component() -> None:
    rpc = FakeRpc()
    rpc.result("getAccountInfo", solana_mint_response("4200000000000", 9))
    rpc.result("getAccountInfo", solana_pool_response("1250000000000", 6))

    batch = await SolanaSupplyCollector(rpc).collect(NOW)

    assert [item[0] for item in batch.errors] == ["native_solana"]
    assert [
        item.observation.metadata["component_id"]
        for item in batch.snapshots
    ] == ["locked_solana"]


@pytest.mark.asyncio
async def test_solana_wrong_pool_mint_fails_locked_component() -> None:
    rpc = FakeRpc()
    rpc.result("getAccountInfo", solana_mint_response("4200000000000", 6))
    pool = solana_pool_response("1250000000000", 6)
    pool["value"]["data"]["parsed"]["info"]["mint"] = "wrong"
    rpc.result("getAccountInfo", pool)

    batch = await SolanaSupplyCollector(rpc).collect(NOW)

    assert [item[0] for item in batch.errors] == ["locked_solana"]


@pytest.mark.asyncio
async def test_aptos_reads_supply_and_pool_in_one_graphql_response() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    http.queue_json(
        url,
        aptos_response(
            supply="4200000000000", balance="1250000000000"
        ),
        method="POST",
    )

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert [item.supply for item in batch.snapshots] == [
        4_200_000,
        1_250_000,
    ]
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_aptos_accepts_integer_supply_v2() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(
        supply="4200000000000", balance="1250000000000"
    )
    body["data"]["fungible_asset_metadata"][0]["supply_v2"] = 16211958179163
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert batch.errors == ()
    assert batch.snapshots[0].supply == pytest.approx(16_211_958.179163)


@pytest.mark.asyncio
async def test_aptos_rejects_boolean_supply_v2() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(supply="1", balance="0")
    body["data"]["fungible_asset_metadata"][0]["supply_v2"] = True
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert [item[0] for item in batch.errors] == ["native_aptos"]


@pytest.mark.asyncio
async def test_aptos_duplicate_metadata_preserves_valid_pool_balance() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(
        supply="4200000000000", balance="1250000000000"
    )
    body["data"]["fungible_asset_metadata"] *= 2
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert [item[0] for item in batch.errors] == ["native_aptos"]
    assert [
        item.observation.metadata["component_id"]
        for item in batch.snapshots
    ] == ["locked_aptos"]


@pytest.mark.asyncio
async def test_aptos_null_supply_preserves_valid_pool_balance() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(
        supply="4200000000000", balance="1250000000000"
    )
    body["data"]["fungible_asset_metadata"][0]["supply_v2"] = None
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert [item[0] for item in batch.errors] == ["native_aptos"]
    assert len(batch.snapshots) == 1


@pytest.mark.asyncio
async def test_aptos_negative_pool_amount_fails_locked_component() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(
        supply="4200000000000", balance="1250000000000"
    )
    body["data"]["current_fungible_asset_balances"][0]["amount"] = "-1"
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert [item[0] for item in batch.errors] == ["locked_aptos"]
    assert len(batch.snapshots) == 1
