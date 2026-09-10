from dataclasses import dataclass


USD1_EVM = "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d"
USD1_BRIDGED = "0x111111d2bf19e43C34263401e0CAd979eD1cdb61"
USD1_TEMPO = "0x20C000000000000000000000111111111E910F0f"
TRON_TOKEN = "TPFqcBAaaUMCSVRCqPaQ9QnzKhmuoLR6Rc"
SOLANA_MINT = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
SOLANA_POOL_TOKEN_ACCOUNT = "8c2WaLy3aW9rnFaq8cCZUYnQNSVa9oX2eUun4buZqhmf"
APTOS_METADATA = (
    "0x05fabd1b12e39967a3c24e91b7b8f67719a6dacee74f3c8b9fb7d93e855437d2"
)
APTOS_POOL = (
    "0x1eb155d08acc900954b6ccee01659b390399ae81ad4c582b73d41374c475caf6"
)

EVM_CHAIN_IDS = {
    "ethereum": 1,
    "bsc": 56,
    "tempo": 4217,
    "plume": 98866,
    "ab": 36888,
    "monad": 143,
    "mantle": 5000,
    "morph": 2818,
}


@dataclass(frozen=True)
class EvmSupplySpec:
    component_id: str
    metric: str
    scope: str
    token_address: str
    decimals: int
    explorer_url: str
    holder_address: str | None = None


NATIVE_ETHEREUM = EvmSupplySpec(
    "native_ethereum",
    "supply.native",
    "ethereum",
    USD1_EVM,
    18,
    "https://etherscan.io",
)
NATIVE_BSC = EvmSupplySpec(
    "native_bsc",
    "supply.native",
    "bsc",
    USD1_EVM,
    18,
    "https://bscscan.com",
)
NATIVE_TEMPO = EvmSupplySpec(
    "native_tempo",
    "supply.native",
    "tempo",
    USD1_TEMPO,
    6,
    "https://explore.tempo.xyz",
)
NATIVE_EVM_SPECS = (NATIVE_ETHEREUM, NATIVE_BSC, NATIVE_TEMPO)

BRIDGED_EVM_SPECS = tuple(
    EvmSupplySpec(
        f"bridged_{scope}",
        "supply.bridged",
        scope,
        USD1_BRIDGED,
        decimals,
        explorer,
    )
    for scope, decimals, explorer in (
        ("plume", 18, "https://explorer.plume.org"),
        ("ab", 18, "https://explorer.core.ab.org"),
        ("monad", 6, "https://monadscan.com"),
        ("mantle", 18, "https://mantlescan.xyz"),
        ("morph", 18, "https://explorer.morphl2.io"),
    )
)

LOCKED_ETHEREUM = EvmSupplySpec(
    "locked_ethereum",
    "bridge.locked",
    "ethereum",
    USD1_EVM,
    18,
    "https://etherscan.io",
    "0x36a72eD0096B414521C45E3ddC9ed657d1D9c141",
)
LOCKED_BSC = EvmSupplySpec(
    "locked_bsc",
    "bridge.locked",
    "bsc",
    USD1_EVM,
    18,
    "https://bscscan.com",
    "0xCe3f7378aE409e1CE0dD6fFA70ab683326b73f04",
)
LOCKED_TEMPO = EvmSupplySpec(
    "locked_tempo",
    "bridge.locked",
    "tempo",
    USD1_TEMPO,
    6,
    "https://explore.tempo.xyz",
    "0x891F30e80B0809800BbaB14633F9eCe8Fc210024",
)
LOCKED_EVM_SPECS = (LOCKED_ETHEREUM, LOCKED_BSC, LOCKED_TEMPO)
