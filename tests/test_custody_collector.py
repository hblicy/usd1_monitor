from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.fakes import FakeRpc
from usd1_monitor.collectors.custody import (
    SOLANA_USD1_MINT,
    CustodyBalanceCollector,
    CustodyDataError,
)
from usd1_monitor.config import CustodyAddressConfig, CustodyConfig


NOW = datetime(2026, 9, 11, 4, tzinfo=UTC)
EVM_ONE = "0x" + "11" * 20
EVM_TWO = "0x" + "22" * 20
SOLANA_OWNER = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


def _address(
    *,
    chain: str,
    address: str,
    label: str,
    status: str = "trusted",
    verified_on: date | None = NOW.date(),
) -> CustodyAddressConfig:
    payload: dict[str, object] = {
        "chain": chain,
        "address": address,
        "entity": "binance_cex",
        "label": label,
        "role": "hot_wallet",
        "status": status,
    }
    if status == "trusted":
        payload.update(
            {
                "verified_on": verified_on,
                "evidence": [
                    {
                        "kind": "official",
                        "url": "https://www.binance.com/en/wallet-addresses",
                    }
                ],
            }
        )
    return CustodyAddressConfig.model_validate(payload)


def _uint256(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def _solana_accounts(
    owner: str,
    amounts: list[int],
) -> dict[str, object]:
    return {
        "value": [
            {
                "pubkey": f"account-{index}",
                "account": {
                    "data": {
                        "parsed": {
                            "info": {
                                "mint": SOLANA_USD1_MINT,
                                "owner": owner,
                                "tokenAmount": {
                                    "amount": str(amount),
                                    "decimals": 6,
                                },
                            }
                        }
                    }
                },
            }
            for index, amount in enumerate(amounts)
        ]
    }


@pytest.mark.asyncio
async def test_evm_balances_use_one_non_negative_safe_head() -> None:
    first = _address(chain="ethereum", address=EVM_ONE, label="Binance 1")
    second = _address(chain="ethereum", address=EVM_TWO, label="Binance 2")
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x64")
    rpc.result("eth_call", _uint256(12 * 10**18))
    rpc.result("eth_call", _uint256(8 * 10**18))

    result = await CustodyBalanceCollector({"ethereum": rpc}, None).collect_evm(
        "ethereum",
        [first, second],
        3,
        NOW,
        trusted_addresses=[first, second],
    )

    assert result.safe_block == 97
    assert [item.value for item in result.observations] == [12, 8]
    assert len(rpc.calls_for("eth_blockNumber")) == 1
    assert {call[1] for call in rpc.calls_for("eth_call")} == {"0x61"}
    assert rpc.calls_for("eth_call")[0][0]["data"] == (
        "0x70a08231" + "0" * 24 + EVM_ONE[2:]
    )


@pytest.mark.asyncio
async def test_evm_rejects_safe_head_before_confirmation_depth() -> None:
    owner = _address(chain="ethereum", address=EVM_ONE, label="Binance 1")
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x2")

    with pytest.raises(CustodyDataError, match="confirmed custody block"):
        await CustodyBalanceCollector({"ethereum": rpc}, None).collect_evm(
            "ethereum",
            [owner],
            3,
            NOW,
            trusted_addresses=[owner],
        )

    assert rpc.calls_for("eth_call") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [None, True, 0, "", "0x", "0x01", "0x" + "00" * 33, "0x" + "gg" * 32],
)
async def test_evm_malformed_balance_is_failure_not_zero(raw: object) -> None:
    owner = _address(chain="ethereum", address=EVM_ONE, label="Main wallet")
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0xa")
    rpc.result("eth_call", raw)

    result = await CustodyBalanceCollector({"ethereum": rpc}, None).collect_evm(
        "ethereum",
        [owner],
        0,
        NOW,
        trusted_addresses=[owner],
    )

    assert result.observations == ()
    assert result.trusted_complete is False
    assert result.trusted_balance is None
    assert result.errors[0].label == "Main wallet"
    assert "ethereum" in str(result.errors[0].error)
    assert EVM_ONE in str(result.errors[0].error)


@pytest.mark.asyncio
async def test_evm_accepts_zero_and_uint256_max_balances() -> None:
    first = _address(chain="bsc", address=EVM_ONE, label="Empty")
    second = _address(chain="bsc", address=EVM_TWO, label="Max")
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x1")
    rpc.result("eth_call", _uint256(0))
    rpc.result("eth_call", _uint256(2**256 - 1))

    result = await CustodyBalanceCollector({"bsc": rpc}, None).collect_evm(
        "bsc",
        [first, second],
        0,
        NOW,
        trusted_addresses=[first, second],
    )

    assert result.trusted_complete is True
    assert result.observations[0].value == 0
    assert result.observations[1].value > 0


@pytest.mark.asyncio
async def test_duplicate_evm_address_is_only_requested_once() -> None:
    owner = _address(chain="ethereum", address=EVM_ONE, label="Wallet")
    duplicate = owner.model_copy(
        update={"address": EVM_ONE.upper().replace("0X", "0x")}
    )
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x1")
    rpc.result("eth_call", _uint256(10**18))

    result = await CustodyBalanceCollector({"ethereum": rpc}, None).collect_evm(
        "ethereum",
        [owner, duplicate],
        0,
        NOW,
        trusted_addresses=[owner],
    )

    assert len(result.observations) == 1
    assert len(rpc.calls_for("eth_call")) == 1


@pytest.mark.asyncio
async def test_evm_address_metadata_contains_identity_and_evidence() -> None:
    owner = _address(chain="ethereum", address=EVM_ONE, label="Wallet")
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x5")
    rpc.result("eth_call", _uint256(2 * 10**18))

    result = await CustodyBalanceCollector(
        {"ethereum": rpc}, None, interval_seconds=600
    ).collect_evm(
        "ethereum",
        [owner],
        1,
        NOW,
        trusted_addresses=[owner],
    )

    observation = result.observations[0]
    assert observation.metric == "custody.address_balance"
    assert observation.scope == f"ethereum:{EVM_ONE}"
    assert observation.source == "evm_rpc"
    assert observation.unit == "USD1"
    assert observation.observed_at == NOW
    assert observation.collected_at == NOW
    assert observation.metadata == {
        "entity": "binance_cex",
        "label": "Wallet",
        "role": "hot_wallet",
        "status": "trusted",
        "configured_status": "trusted",
        "evidence_urls": ["https://www.binance.com/en/wallet-addresses"],
        "verified_on": "2026-09-11",
        "safe_block": 4,
        "max_age_seconds": 1200,
    }


@pytest.mark.asyncio
async def test_solana_owner_sums_all_usd1_token_accounts() -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Binance SOL")
    rpc = FakeRpc()
    rpc.result(
        "getTokenAccountsByOwner",
        _solana_accounts(owner.address, [4_000_000, 6_000_000]),
    )

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner], NOW, trusted_addresses=[owner]
    )

    assert result.observations[0].value == 10
    assert result.observations[0].metadata["token_accounts"] == 2
    assert result.observations[0].metadata["safe_block"] == "finalized"
    assert rpc.calls_for("getTokenAccountsByOwner") == [
        [
            owner.address,
            {"mint": SOLANA_USD1_MINT},
            {"encoding": "jsonParsed", "commitment": "finalized"},
        ]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mint", "wrong-mint"),
        ("owner", "wrong-owner"),
        ("decimals", 9),
        ("decimals", True),
        ("amount", True),
        ("amount", -1),
        ("amount", "-1"),
        ("amount", "1.0"),
    ],
)
async def test_solana_rejects_identity_or_amount_variation(
    field: str,
    value: object,
) -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Binance SOL")
    body = _solana_accounts(owner.address, [1_000_000])
    info = body["value"][0]["account"]["data"]["parsed"]["info"]  # type: ignore[index]
    if field in {"amount", "decimals"}:
        info["tokenAmount"][field] = value  # type: ignore[index]
    else:
        info[field] = value  # type: ignore[index]
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", body)

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner], NOW, trusted_addresses=[owner]
    )

    assert result.observations == ()
    assert result.trusted_complete is False
    assert result.errors[0].label == "Binance SOL"
    assert "solana" in str(result.errors[0].error)
    assert owner.address in str(result.errors[0].error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"value": None},
        {"value": {}},
        {"value": [None]},
        {"value": [{"pubkey": "account-0", "account": {}}]},
    ],
)
async def test_solana_malformed_outer_or_account_structure_fails(
    body: object,
) -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Binance SOL")
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", body)

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner], NOW, trusted_addresses=[owner]
    )

    assert result.observations == ()
    assert result.trusted_complete is False


@pytest.mark.asyncio
async def test_solana_duplicate_account_fails_instead_of_double_counting() -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Binance SOL")
    body = _solana_accounts(owner.address, [1_000_000, 2_000_000])
    body["value"][1]["pubkey"] = "account-0"  # type: ignore[index]
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", body)

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner], NOW, trusted_addresses=[owner]
    )

    assert result.observations == ()
    assert result.trusted_complete is False
    assert "duplicate" in str(result.errors[0].error)


@pytest.mark.asyncio
async def test_solana_empty_token_account_list_is_a_valid_zero_balance() -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Binance SOL")
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", {"value": []})

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner], NOW, trusted_addresses=[owner]
    )

    assert result.trusted_complete is True
    assert result.observations[0].value == 0
    assert result.observations[0].metadata["token_accounts"] == 0


@pytest.mark.asyncio
async def test_solana_accepts_zero_and_uint64_max_amounts() -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Binance SOL")
    rpc = FakeRpc()
    rpc.result(
        "getTokenAccountsByOwner",
        _solana_accounts(owner.address, [0, 2**64 - 1]),
    )

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner], NOW, trusted_addresses=[owner]
    )

    assert result.trusted_complete is True
    assert result.observations[0].metadata["raw_amount"] == str(2**64 - 1)


@pytest.mark.asyncio
async def test_solana_rejects_amount_larger_than_uint64() -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Binance SOL")
    rpc = FakeRpc()
    rpc.result(
        "getTokenAccountsByOwner",
        _solana_accounts(owner.address, [2**64]),
    )

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner], NOW, trusted_addresses=[owner]
    )

    assert result.observations == ()
    assert result.trusted_complete is False


@pytest.mark.asyncio
async def test_candidate_failure_does_not_make_trusted_set_incomplete() -> None:
    candidate = _address(
        chain="solana",
        address=SOLANA_OWNER,
        label="Candidate",
        status="candidate",
        verified_on=None,
    )
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", RuntimeError("offline"))

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [candidate], NOW, trusted_addresses=[]
    )

    assert result.trusted_complete is True
    assert result.trusted_balance == 0
    assert result.observations == ()
    assert result.errors[0].label == "Candidate"


@pytest.mark.asyncio
async def test_trusted_rpc_failure_makes_trusted_set_incomplete() -> None:
    trusted = _address(chain="solana", address=SOLANA_OWNER, label="Trusted")
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", RuntimeError("offline"))

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [trusted], NOW, trusted_addresses=[trusted]
    )

    assert result.trusted_complete is False
    assert result.trusted_balance is None
    assert result.errors[0].label == "Trusted"
    assert "offline" in str(result.errors[0].error)


@pytest.mark.asyncio
async def test_duplicate_solana_owner_is_only_requested_once() -> None:
    owner = _address(chain="solana", address=SOLANA_OWNER, label="Wallet")
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", {"value": []})

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [owner, owner], NOW, trusted_addresses=[owner]
    )

    assert len(result.observations) == 1
    assert len(rpc.calls_for("getTokenAccountsByOwner")) == 1


@pytest.mark.asyncio
async def test_expired_trusted_is_displayed_but_excluded_from_trusted_sum() -> None:
    current = _address(chain="solana", address=SOLANA_OWNER, label="Current")
    expired = _address(
        chain="solana",
        address="2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
        label="Expired",
        verified_on=date(2026, 6, 1),
    )
    config = CustodyConfig(addresses=[current, expired])
    effective = config.trusted_addresses(as_of=NOW.date())
    assert [item.label for item in effective] == ["Current"]
    rpc = FakeRpc()
    rpc.result(
        "getTokenAccountsByOwner",
        _solana_accounts(current.address, [3_000_000]),
    )
    rpc.result(
        "getTokenAccountsByOwner",
        _solana_accounts(expired.address, [9_000_000]),
    )

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        config.addresses,
        NOW,
        trusted_addresses=effective,
    )

    assert [item.value for item in result.observations] == [3, 9]
    assert [item.metadata["status"] for item in result.observations] == [
        "trusted",
        "candidate",
    ]
    assert result.observations[1].metadata["configured_status"] == "trusted"
    assert result.trusted_balance == 3


@pytest.mark.asyncio
async def test_candidate_balance_is_not_included_in_trusted_sum() -> None:
    trusted = _address(chain="solana", address=SOLANA_OWNER, label="Trusted")
    candidate = _address(
        chain="solana",
        address="2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
        label="Candidate",
        status="candidate",
        verified_on=None,
    )
    rpc = FakeRpc()
    rpc.result(
        "getTokenAccountsByOwner",
        _solana_accounts(trusted.address, [2_000_000]),
    )
    rpc.result(
        "getTokenAccountsByOwner",
        _solana_accounts(candidate.address, [8_000_000]),
    )

    result = await CustodyBalanceCollector({}, rpc).collect_solana(
        [trusted, candidate], NOW, trusted_addresses=[trusted]
    )

    assert result.trusted_complete is True
    assert result.trusted_balance == 2


@pytest.mark.asyncio
async def test_effective_trusted_address_must_be_in_collected_chain() -> None:
    ethereum = _address(chain="ethereum", address=EVM_ONE, label="ETH")
    bsc = _address(chain="bsc", address=EVM_ONE, label="BSC")
    rpc = FakeRpc()

    with pytest.raises(CustodyDataError, match="effective trusted"):
        await CustodyBalanceCollector({"ethereum": rpc}, None).collect_evm(
            "ethereum",
            [ethereum],
            0,
            NOW,
            trusted_addresses=[bsc],
        )
