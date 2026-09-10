from usd1_monitor.supply_assets import (
    APTOS_METADATA,
    BRIDGED_EVM_SPECS,
    EVM_CHAIN_IDS,
    NATIVE_EVM_SPECS,
    SOLANA_MINT,
    TRON_TOKEN,
)


def test_official_multichain_asset_inventory_is_exact() -> None:
    assert {item.scope for item in NATIVE_EVM_SPECS} == {
        "ethereum",
        "bsc",
        "tempo",
    }
    assert {item.scope for item in BRIDGED_EVM_SPECS} == {
        "plume",
        "ab",
        "monad",
        "mantle",
        "morph",
    }
    assert EVM_CHAIN_IDS == {
        "ethereum": 1,
        "bsc": 56,
        "tempo": 4217,
        "plume": 98866,
        "ab": 36888,
        "monad": 143,
        "mantle": 5000,
        "morph": 2818,
    }
    assert TRON_TOKEN == "TPFqcBAaaUMCSVRCqPaQ9QnzKhmuoLR6Rc"
    assert SOLANA_MINT == "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
    assert APTOS_METADATA.endswith("e855437d2")
