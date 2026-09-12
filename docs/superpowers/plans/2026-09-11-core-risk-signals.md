# USD1 核心风险信号增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加已核验 Binance 地址集中度与资金流、PoR 覆盖率过期 UNKNOWN、BitGo/官方赎回通道状态，并让资产总状态只有在三个关键支柱均可判断时才显示 GREEN。

**Architecture:** 新增独立的托管集中度与赎回通道采集器，观测继续写入现有 `observations` 和 `announcements`，业务告警继续走现有 `risk_states` 与 `StateEngine`。新增纯函数 `AssetAssessment` 在输出层合并已知风险和三个关键支柱的可用性，不修改 `RiskLevel` 或 SQLite schema；EVM 资金流复用现有 Transfer 日志，只为命中可信地址且尚无时间戳的区块补取一次区块头。

**Tech Stack:** Python 3.12、asyncio、aiohttp、aiosqlite、Pydantic v2、BeautifulSoup、原生 JavaScript、pytest。

---

## 文件结构

- Create `usd1_monitor/collectors/custody.py`：EVM/Solana 地址余额与相关 Transfer 区块时间采集。
- Create `usd1_monitor/engine/custody_rules.py`：集中度、实体净变化、单地址外流与两轮恢复规则。
- Create `usd1_monitor/engine/asset_assessment.py`：关键支柱可用性与资产总状态纯函数。
- Create `usd1_monitor/collectors/redemption.py`：BitGo Status、官方页面和 RSS 线索采集。
- Create `usd1_monitor/engine/redemption_rules.py`：赎回/结算/银行通道文本分类与恢复规则。
- Modify `usd1_monitor/config.py`：地址证据、托管集中度和赎回通道配置。
- Modify `usd1_monitor/storage.py`：相关 Transfer 查询、区块时间补写和最近公告读取。
- Modify `usd1_monitor/scheduler.py`：两个新监控组件及现有 PoR 覆盖率新鲜度。
- Modify `usd1_monitor/cli.py`：构建组件、状态输出和启动文案。
- Modify `usd1_monitor/dashboard_data.py`：关键支柱、集中度、资金流、赎回状态和媒体线索快照。
- Modify `usd1_monitor/dashboard_static/index.html`、`dashboard.js`、`dashboard.css`：新增风险卡片和证据展示。
- Modify `usd1_monitor/notifications/wechat.py`：新增资产风险的简明中文文案。
- Modify `config.example.yaml`、`deploy/config.production.example.yaml`、`README.md`：配置与运行说明。
- Create/Modify 对应 `tests/test_*.py` 和 `tests/fixtures/*`：边界、失败、通知及端到端回归。

### Task 1: 增加地址证据与新监控配置

**Files:**
- Modify: `tests/test_config.py`
- Modify: `usd1_monitor/config.py`
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`

- [ ] **Step 1: 写入可信地址、候选地址和过期证据测试**

在 `tests/test_config.py` 增加以下测试和固定日期注入；模型以传入的 `verified_on` 与 `verification_max_age_days` 校验，不在测试中依赖本地时区：

```python
from datetime import date, timedelta

from usd1_monitor.config import CustodyAddressConfig, CustodyConfig


def evidence(url: str, kind: str = "official") -> dict[str, str]:
    return {"kind": kind, "url": url}


def test_trusted_custody_address_accepts_one_official_source() -> None:
    item = CustodyAddressConfig.model_validate(
        {
            "chain": "ethereum",
            "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
            "entity": "binance_cex",
            "label": "Binance 8",
            "role": "hot_wallet",
            "status": "trusted",
            "verified_on": date.today().isoformat(),
            "evidence": [evidence("https://www.binance.com/en/square/post/97671")],
        }
    )

    assert item.status == "trusted"


def test_trusted_custody_address_rejects_one_nonofficial_source() -> None:
    with pytest.raises(ValueError, match="official source or two independent"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "bsc",
                "address": "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",
                "entity": "binance_cex",
                "label": "Binance Hot Wallet",
                "role": "hot_wallet",
                "status": "trusted",
                "verified_on": date.today().isoformat(),
                "evidence": [evidence("https://bscscan.com/address/0x8894e0a0c962cb723c1976a4421c95949be2d4e3", "label")],
            }
        )


def test_official_evidence_must_belong_to_configured_entity() -> None:
    with pytest.raises(ValueError, match="official evidence host does not match entity"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "ethereum",
                "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
                "entity": "binance_cex",
                "label": "Binance 8",
                "role": "hot_wallet",
                "status": "trusted",
                "verified_on": date.today().isoformat(),
                "evidence": [evidence("https://example.com/address-list")],
            }
        )


def test_trusted_custody_address_rejects_expired_verification() -> None:
    with pytest.raises(ValueError, match="verification is older than 90 days"):
        CustodyConfig.model_validate(
            {
                "verification_max_age_days": 90,
                "addresses": [
                    {
                        "chain": "solana",
                        "address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
                        "entity": "binance_cex",
                        "label": "Binance 2",
                        "role": "hot_wallet",
                        "status": "trusted",
                        "verified_on": (date.today() - timedelta(days=91)).isoformat(),
                        "evidence": [evidence("https://www.binance.com/en/wallet-addresses")],
                    }
                ],
            }
        )
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_config.py -q`

Expected: FAIL，提示 `CustodyAddressConfig` 尚不存在。

- [ ] **Step 3: 实现严格配置模型**

在 `usd1_monitor/config.py` 引入 `date`、`Literal`，并增加以下模型；`candidate` 允许单一标签来源但永不进入告警，`trusted` 必须满足证据规则：

```python
class AddressEvidenceConfig(StrictModel):
    kind: Literal["official", "label"]
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("address evidence must be a valid HTTPS URL")
        return value


class CustodyAddressConfig(StrictModel):
    chain: Literal["ethereum", "bsc", "solana"]
    address: str
    entity: Literal[
        "binance_cex",
        "binance_peg_reserve",
        "fireblocks_custody",
        "bitgo_issuer",
        "unlabeled_whale",
    ]
    label: str = Field(min_length=1)
    role: Literal["hot_wallet", "cold_wallet", "reserve", "custody", "issuer", "whale"]
    status: Literal["trusted", "candidate"] = "candidate"
    verified_on: date | None = None
    evidence: list[AddressEvidenceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity_and_evidence(self) -> "CustodyAddressConfig":
        if self.chain in {"ethereum", "bsc"}:
            if re.fullmatch(r"0x[0-9a-fA-F]{40}", self.address) is None:
                raise ValueError("EVM custody address must be a 20-byte hex address")
        elif re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", self.address) is None:
            raise ValueError("Solana custody address must be base58")
        hosts = [
            str(urlsplit(item.url).hostname).casefold().removeprefix("www.")
            for item in self.evidence
        ]
        if len(hosts) != len(set(hosts)):
            raise ValueError("address evidence hosts must be independent")
        if self.status == "trusted":
            if self.verified_on is None:
                raise ValueError("trusted address requires verified_on")
            owner_hosts = {
                "binance_cex": {"binance.com"},
                "binance_peg_reserve": {"binance.com"},
                "fireblocks_custody": {"fireblocks.com"},
                "bitgo_issuer": {"bitgo.com"},
                "unlabeled_whale": set(),
            }[self.entity]
            official_hosts = [
                str(urlsplit(item.url).hostname)
                .casefold()
                .removeprefix("www.")
                for item in self.evidence
                if item.kind == "official"
            ]
            official = any(host in owner_hosts for host in official_hosts)
            if official_hosts and not official:
                raise ValueError(
                    "official evidence host does not match entity"
                )
            if not official and len(self.evidence) < 2:
                raise ValueError("trusted address requires one official source or two independent labels")
        return self


class CustodyConfig(StrictModel):
    interval_seconds: int = Field(default=600, ge=60)
    verification_max_age_days: int = Field(default=90, ge=1)
    yellow_share: float = Field(default=0.50, gt=0, lt=1)
    red_share: float = Field(default=0.70, gt=0, lt=1)
    entity_flow_24h: float = Field(default=50_000_000, gt=0)
    address_outflow_1h: float = Field(default=100_000_000, gt=0)
    recovery_checks: int = Field(default=2, ge=1)
    addresses: list[CustodyAddressConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_thresholds_and_duplicates(self) -> "CustodyConfig":
        if self.red_share <= self.yellow_share:
            raise ValueError("custody red_share must exceed yellow_share")
        keys = [(item.chain, item.address.casefold()) for item in self.addresses]
        if len(keys) != len(set(keys)):
            raise ValueError("custody addresses must be unique within each chain")
        for item in self.addresses:
            if (
                item.status == "trusted"
                and item.verified_on is not None
                and (date.today() - item.verified_on).days
                > self.verification_max_age_days
            ):
                raise ValueError(
                    "address verification is older than "
                    f"{self.verification_max_age_days} days"
                )
        return self


class RedemptionConfig(StrictModel):
    status_url: str = "https://status.bitgo.com/api/v2/summary.json"
    status_interval_seconds: int = Field(default=300, ge=60)
    page_interval_seconds: int = Field(default=3600, ge=300)
    recovery_checks: int = Field(default=2, ge=1)
    official_page_urls: list[str] = Field(
        default_factory=lambda: [
            "https://www.bitgo.com/usd1/",
            "https://www.bitgo.com/usd1-terms/",
            "https://investors.bitgo.com/news/default.aspx",
            "https://docs.worldlibertyfinancial.com/resources/faq",
        ]
    )
    media_rss_urls: list[str] = Field(default_factory=list)

    @field_validator("status_url")
    @classmethod
    def validate_status_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname != "status.bitgo.com":
            raise ValueError("redemption status_url must use status.bitgo.com")
        return value

    @field_validator("official_page_urls")
    @classmethod
    def validate_official_pages(cls, values: list[str]) -> list[str]:
        allowed = {
            "www.bitgo.com",
            "bitgo.com",
            "investors.bitgo.com",
            "docs.worldlibertyfinancial.com",
        }
        if not values:
            raise ValueError("redemption official_page_urls must not be empty")
        if any(urlsplit(value).scheme != "https" or urlsplit(value).hostname not in allowed for value in values):
            raise ValueError("redemption official page is outside allowlist")
        return values
```

将 `custody: CustodyConfig`、`redemption: RedemptionConfig` 加入 `AppConfig`。`CustodyConfig` 用自身 `verification_max_age_days` 对所有 trusted 地址再次检查，不能把固定 90 天写死在外层配置行为中。

- [ ] **Step 4: 补齐示例配置并验证所有地址均显式标记**

在两个示例 YAML 增加 `custody` 和 `redemption`。把设计文档中的 13 个 EVM 地址分别配置到 Ethereum 与 BNB Chain，把 6 个 Solana Binance 地址、1 个 Fireblocks 地址和 2 个未标注巨鲸全部列出。只有 Binance 官方页面当前明确列出的记录可写 `trusted` 并附 `official` 证据；其余记录必须写：

```yaml
custody:
  interval_seconds: 600
  verification_max_age_days: 90
  yellow_share: 0.50
  red_share: 0.70
  entity_flow_24h: 50000000
  address_outflow_1h: 100000000
  recovery_checks: 2
  addresses:
    - chain: solana
      address: 5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9
      entity: binance_cex
      label: Binance 2 / SOL hot
      role: hot_wallet
      status: candidate
      evidence:
        - kind: label
          url: https://solscan.io/account/5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9
redemption:
  status_url: https://status.bitgo.com/api/v2/summary.json
  status_interval_seconds: 300
  page_interval_seconds: 3600
  recovery_checks: 2
  official_page_urls:
    - https://www.bitgo.com/usd1/
    - https://www.bitgo.com/usd1-terms/
    - https://investors.bitgo.com/news/default.aspx
    - https://docs.worldlibertyfinancial.com/resources/faq
  media_rss_urls: []
```

示例配置使用以下精确清单；EVM 表中的每个地址分别生成 `ethereum` 和 `bsc` 两条记录，`verified_on: 2026-09-11`，证据均为 Binance 官方透明度页面。`0x47ac...` 的 entity/role 为 `binance_peg_reserve/reserve`，其余为 `binance_cex`，`0xbe0e...` 为 `cold_wallet`，其余为 `hot_wallet`：

```text
0xf977814e90da44bfa03b6295a0616a897441acec trusted
0x5a52e96bacdabb82fd05763e25335261b270efcb trusted
0x28c6c06298d514db089934071355e5743bf21d60 trusted
0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503 trusted
0x21a31ee1afc51d94c2efccaa2092ad1028285549 trusted
0xdfd5293d8e347dfe59e90efd55b2956a1343963d trusted
0xbe0eb53f46cd790cd13851d5eff43d12404d33e8 trusted
0x8894e0a0c962cb723c1976a4421c95949be2d4e3 trusted
0xe2fc31f816a9b94326492132018c3aecc4a93ae1 trusted
0x9696f59e4d72e237be84ffd425dcad154bf96976 trusted
0x56eddb7aa87536c09ccc2793473599fd21a8b17f trusted
0x4976a4a02f38326660d17bf34b431dc6e2eb2327 trusted
0x01c952174c24e1210d26961d456a77a39e1f0bb0 trusted
```

Solana 的 6 个 Binance owner 和 1 个 Fireblocks owner 先保持 candidate，只使用各自 Solscan 链接作为 label 证据：

```text
5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9 binance_cex
2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S binance_cex
9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM binance_cex
3yFwqXBfZY4jBVUafQ1YEXw189y2dN3V5KQq9uzBDy1E binance_cex
3gd3dqgtJ4jWfBfLYTX67DALFetjc5iS72sCgRhCkW2u binance_cex
6QJzieMYfp7yr3EdrePaQoG3Ghxs2wM98xSLRu8Xh56U binance_cex
9Rycov3U4efJf5HiqZYGjN7qJJHEtMsj4vbmkG4xfCxk fireblocks_custody
```

两个未标注巨鲸分别在 Ethereum 与 BNB Chain 各生成 candidate 记录，entity/role 为 `unlabeled_whale/whale`：

```text
0xAC3E216bD55860912062a4027A03b99587B7FfC7
0x041c32c919de3e85e0D89984c2590434f6569dFA
```

Run: `python -m pytest tests/test_config.py tests/test_examples.py -q`

Expected: PASS；示例配置可加载，所有 trusted 地址核验未过期，candidate 不要求第二证据。

- [ ] **Step 5: 提交配置模型**

```bash
git add usd1_monitor/config.py config.example.yaml deploy/config.production.example.yaml tests/test_config.py tests/test_examples.py
git commit -m "增加核心风险信号配置"
```

### Task 2: 实现托管集中度与资金流纯规则

**Files:**
- Create: `tests/test_custody_rules.py`
- Create: `usd1_monitor/engine/custody_rules.py`

- [ ] **Step 1: 写入阈值、内部转账抵消和两轮恢复测试**

```python
from datetime import UTC, datetime, timedelta

from usd1_monitor.engine.custody_rules import (
    CustodyFlow,
    Transfer,
    evaluate_concentration,
    evaluate_entity_flow,
    evaluate_address_outflow,
    summarize_transfers,
)
from usd1_monitor.models import RiskLevel

NOW = datetime(2026, 9, 11, 4, tzinfo=UTC)
GROUP = frozenset({"0xaaa", "0xbbb"})


def test_internal_transfer_is_excluded_from_entity_flow() -> None:
    transfers = [
        Transfer("0xaaa", "0xbbb", 70_000_000, NOW - timedelta(minutes=5)),
        Transfer("0xccc", "0xaaa", 60_000_000, NOW - timedelta(hours=2)),
    ]

    result = summarize_transfers(transfers, GROUP, NOW)

    assert result.entity_net_24h == 60_000_000
    assert result.address_outflow_1h == {"0xaaa": 0, "0xbbb": 0}


def test_share_boundaries_and_flow_never_raise_red() -> None:
    assert evaluate_concentration(0.50, RiskLevel.GREEN, 0).level is RiskLevel.GREEN
    assert evaluate_concentration(0.500001, RiskLevel.GREEN, 0).level is RiskLevel.YELLOW
    assert evaluate_concentration(0.700001, RiskLevel.GREEN, 0).level is RiskLevel.RED
    assert evaluate_entity_flow(50_000_001, RiskLevel.GREEN, 0).level is RiskLevel.YELLOW
    assert evaluate_address_outflow({"0xaaa": 100_000_001}, RiskLevel.GREEN, 0).level is RiskLevel.YELLOW


def test_warning_recovers_only_after_two_clear_checks() -> None:
    first = evaluate_concentration(0.10, RiskLevel.YELLOW, 0)
    second = evaluate_concentration(0.10, first.level, first.clear_checks)

    assert first.level is RiskLevel.YELLOW and first.clear_checks == 1
    assert second.level is RiskLevel.GREEN and second.clear_checks == 0
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_custody_rules.py -q`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现纯函数**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta

from usd1_monitor.models import RiskLevel


@dataclass(frozen=True)
class Transfer:
    sender: str
    receiver: str
    amount: float
    observed_at: datetime


@dataclass(frozen=True)
class CustodyFlow:
    entity_net_24h: float
    address_outflow_1h: dict[str, float]


@dataclass(frozen=True)
class RuleDecision:
    level: RiskLevel
    clear_checks: int


def summarize_transfers(transfers: list[Transfer], group: frozenset[str], now: datetime) -> CustodyFlow:
    normalized = frozenset(item.casefold() for item in group)
    entity_net = 0.0
    outflow = {item: 0.0 for item in normalized}
    for item in transfers:
        sender = item.sender.casefold()
        receiver = item.receiver.casefold()
        sender_inside = sender in normalized
        receiver_inside = receiver in normalized
        if item.observed_at >= now - timedelta(hours=24) and sender_inside != receiver_inside:
            entity_net += -item.amount if sender_inside else item.amount
        if item.observed_at >= now - timedelta(hours=1) and sender_inside and not receiver_inside:
            outflow[sender] += item.amount
    return CustodyFlow(entity_net, outflow)


def _with_recovery(
    current: RiskLevel,
    previous: RiskLevel,
    clear_checks: int,
    *,
    recovery_checks: int = 2,
) -> RuleDecision:
    if current is not RiskLevel.GREEN:
        return RuleDecision(current, 0)
    if previous is RiskLevel.GREEN:
        return RuleDecision(RiskLevel.GREEN, 0)
    next_clear = clear_checks + 1
    if next_clear < recovery_checks:
        return RuleDecision(previous, next_clear)
    return RuleDecision(RiskLevel.GREEN, 0)


def evaluate_concentration(share: float, previous: RiskLevel, clear_checks: int, *, yellow: float = 0.50, red: float = 0.70, recovery_checks: int = 2) -> RuleDecision:
    current = RiskLevel.RED if share > red else RiskLevel.YELLOW if share > yellow else RiskLevel.GREEN
    return _with_recovery(current, previous, clear_checks, recovery_checks=recovery_checks)


def evaluate_entity_flow(value: float, previous: RiskLevel, clear_checks: int, *, threshold: float = 50_000_000, recovery_checks: int = 2) -> RuleDecision:
    current = RiskLevel.YELLOW if abs(value) > threshold else RiskLevel.GREEN
    return _with_recovery(current, previous, clear_checks, recovery_checks=recovery_checks)


def evaluate_address_outflow(values: dict[str, float], previous: RiskLevel, clear_checks: int, *, threshold: float = 100_000_000, recovery_checks: int = 2) -> RuleDecision:
    current = RiskLevel.YELLOW if max(values.values(), default=0) > threshold else RiskLevel.GREEN
    return _with_recovery(current, previous, clear_checks, recovery_checks=recovery_checks)
```

- [ ] **Step 4: 运行规则测试并提交**

Run: `python -m pytest tests/test_custody_rules.py -q`

Expected: PASS。

```bash
git add usd1_monitor/engine/custody_rules.py tests/test_custody_rules.py
git commit -m "实现托管集中度风险规则"
```

### Task 3: 实现 EVM 与 Solana 可信地址余额采集

**Files:**
- Create: `tests/test_custody_collector.py`
- Create: `usd1_monitor/collectors/custody.py`

- [ ] **Step 1: 写入 EVM 同一安全头和 Solana 多 Token Account 汇总测试**

```python
from datetime import date

from tests.fakes import FakeRpc
from usd1_monitor.collectors.custody import SOLANA_USD1_MINT
from usd1_monitor.config import CustodyAddressConfig


def trusted_evm(address: str) -> CustodyAddressConfig:
    return CustodyAddressConfig.model_validate(
        {
            "chain": "ethereum",
            "address": address,
            "entity": "binance_cex",
            "label": address,
            "role": "hot_wallet",
            "status": "trusted",
            "verified_on": date.today(),
            "evidence": [{"kind": "official", "url": "https://www.binance.com/en/square/post/97671"}],
        }
    )


def trusted_solana() -> CustodyAddressConfig:
    return CustodyAddressConfig.model_validate(
        {
            "chain": "solana",
            "address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
            "entity": "binance_cex",
            "label": "Binance 2",
            "role": "hot_wallet",
            "status": "trusted",
            "verified_on": date.today(),
            "evidence": [{"kind": "official", "url": "https://www.binance.com/en/wallet-addresses"}],
        }
    )


def solana_accounts(amounts: list[int]) -> dict[str, object]:
    return {
        "value": [
            {
                "pubkey": f"account-{index}",
                "account": {
                    "data": {
                        "parsed": {
                            "info": {
                                "mint": SOLANA_USD1_MINT,
                                "owner": trusted_solana().address,
                                "tokenAmount": {"amount": str(amount), "decimals": 6},
                            }
                        }
                    }
                },
            }
            for index, amount in enumerate(amounts)
        ]
    }


@pytest.mark.asyncio
async def test_evm_balances_use_one_safe_head() -> None:
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", hex(100))
    rpc.result("eth_call", hex(12 * 10**18))
    rpc.result("eth_call", hex(8 * 10**18))
    collector = CustodyBalanceCollector({"ethereum": rpc}, None)

    result = await collector.collect_evm(
        "ethereum", [trusted_evm("0x" + "11" * 20), trusted_evm("0x" + "22" * 20)], 3, NOW
    )

    assert result.safe_block == 97
    assert [item.value for item in result.observations] == [12, 8]
    assert {call[1][1] for call in rpc.calls if call[0] == "eth_call"} == {hex(97)}


@pytest.mark.asyncio
async def test_solana_owner_sums_only_usd1_accounts() -> None:
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", solana_accounts([4_000_000, 6_000_000]))
    collector = CustodyBalanceCollector({}, rpc)

    result = await collector.collect_solana([trusted_solana()], NOW)

    assert result.observations[0].value == 10
    assert result.observations[0].metadata["token_accounts"] == 2
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_custody_collector.py -q`

Expected: FAIL，`CustodyBalanceCollector` 尚不存在。

- [ ] **Step 3: 实现严格采集器**

在 `usd1_monitor/collectors/custody.py` 定义 `CustodyCollection`、`CustodyDataError` 和 `CustodyBalanceCollector`。EVM calldata 必须由地址严格编码，Solana 必须校验 mint、owner、decimals=6 和无损整数：

```python
BALANCE_OF_SELECTOR = "0x70a08231"
SOLANA_USD1_MINT = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"


def balance_of_data(address: str) -> str:
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None:
        raise CustodyDataError("invalid EVM custody address")
    return BALANCE_OF_SELECTOR + address[2:].lower().rjust(64, "0")


async def _evm_balance(rpc: RpcClient, token: str, address: str, block: int) -> float:
    raw = await rpc.call(
        "eth_call",
        [{"to": token, "data": balance_of_data(address)}, hex(block)],
    )
    if not isinstance(raw, str) or re.fullmatch(r"0x[0-9a-fA-F]+", raw) is None:
        raise CustodyDataError("balanceOf result is malformed")
    return int(raw, 16) / 10**18
```

`collect_evm` 先读取一次 `eth_blockNumber` 并减 confirmation depth，再用该 block tag 读取全部 trusted 和 candidate 地址；任一 trusted 地址失败时在结果中记录失败并禁止可信聚合，candidate 失败不影响可信聚合。`collect_solana` 对每个 owner 调用：

```python
await rpc.call(
    "getTokenAccountsByOwner",
    [
        owner,
        {"mint": SOLANA_USD1_MINT},
        {"encoding": "jsonParsed", "commitment": "finalized"},
    ],
)
```

每个地址生成 `custody.address_balance`，metadata 至少包含 `entity`、`label`、`role`、`status`、`evidence_urls`、`verified_on`、`safe_block`。聚合集中度观测另写 `max_age_seconds = interval_seconds * 2`，供输出层判断关键支柱是否仍新鲜。不可把缺失或异常响应解释为 0。

- [ ] **Step 4: 补齐失败与身份校验测试**

增加参数化测试，直接修改一份合法响应并断言采集失败：

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mint", "wrong-mint"),
        ("owner", "wrong-owner"),
        ("decimals", 9),
        ("amount", True),
    ],
)
async def test_solana_rejects_invalid_account_identity(field: str, value: object) -> None:
    owner = trusted_solana()
    body = solana_accounts([1_000_000])
    info = body["value"][0]["account"]["data"]["parsed"]["info"]
    if field in {"amount", "decimals"}:
        info["tokenAmount"][field] = value
    else:
        info[field] = value
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", body)

    result = await CustodyBalanceCollector({}, rpc).collect_solana([owner], NOW)

    assert result.trusted_complete is False
    assert result.errors[0].label == "Binance 2"


@pytest.mark.asyncio
async def test_candidate_failure_does_not_make_trusted_set_incomplete() -> None:
    candidate = trusted_solana().model_copy(update={"status": "candidate"})
    rpc = FakeRpc()
    rpc.result("getTokenAccountsByOwner", RuntimeError("offline"))

    result = await CustodyBalanceCollector({}, rpc).collect_solana([candidate], NOW)

    assert result.trusted_complete is True
    assert len(result.errors) == 1
```

Run: `python -m pytest tests/test_custody_collector.py -q`

Expected: PASS。

- [ ] **Step 5: 提交采集器**

```bash
git add usd1_monitor/collectors/custody.py tests/test_custody_collector.py
git commit -m "采集托管地址余额"
```

### Task 4: 为相关 Transfer 补充真实区块时间并查询窗口

**Files:**
- Modify: `tests/test_storage.py`
- Modify: `tests/test_custody_collector.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/collectors/custody.py`

- [ ] **Step 1: 写入只选相关地址且一次补写一个区块的测试**

```python
from usd1_monitor.models import ChainEvent


def transfer_event(
    block: int,
    sender: str,
    receiver: str,
    *,
    log_index: int = 0,
) -> ChainEvent:
    return ChainEvent(
        chain="ethereum",
        block_number=block,
        tx_hash=f"0x{block:064x}",
        log_index=log_index,
        event_type="Transfer",
        payload={
            "from_address": sender,
            "to_address": receiver,
            "amount": 1.0,
        },
        observed_at=NOW,
    )


@pytest.mark.asyncio
async def test_unstamped_transfer_blocks_only_include_trusted_group(storage) -> None:
    await storage.insert_chain_events_and_cursor(
        "ethereum",
        [transfer_event(90, "0xaaa", "0xbbb"), transfer_event(91, "0xccc", "0xddd")],
        91,
    )

    blocks = await storage.unstamped_transfer_blocks("ethereum", {"0xaaa"}, min_block=90)

    assert blocks == [90]


@pytest.mark.asyncio
async def test_timestamp_enrichment_fetches_each_block_once(storage) -> None:
    rpc = FakeRpc()
    rpc.result("eth_getBlockByNumber", {"number": hex(90), "timestamp": hex(int(NOW.timestamp()))})
    await storage.insert_chain_events_and_cursor(
        "ethereum",
        [transfer_event(90, "0xaaa", "0xbbb", log_index=0), transfer_event(90, "0xaaa", "0xccc", log_index=1)],
        90,
    )

    await enrich_transfer_timestamps(
        "ethereum", rpc, storage, {"0xaaa"}, min_block=90
    )

    assert [call[0] for call in rpc.calls] == ["eth_getBlockByNumber"]
    events = await storage.custody_transfers_since("ethereum", NOW - timedelta(hours=1), {"0xaaa"})
    assert len(events) == 2
    assert all(item.payload["block_time"] == NOW.isoformat() for item in events)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_storage.py tests/test_custody_collector.py -q`

Expected: FAIL，新查询和补写函数尚不存在。

- [ ] **Step 3: 实现限定查询与原子补写**

在 `Storage` 增加：

```python
async def unstamped_transfer_blocks(self, chain: str, addresses: set[str], *, min_block: int) -> list[int]:
    if not addresses:
        return []
    values = tuple(item.casefold() for item in addresses)
    placeholders = ",".join("?" for _ in values)
    cursor = await self.connection.execute(
        f"""
        SELECT DISTINCT block_number
        FROM chain_events
        WHERE chain = ? AND event_type = 'Transfer' AND block_number >= ?
          AND json_extract(payload_json, '$.block_time') IS NULL
          AND (
            lower(json_extract(payload_json, '$.from_address')) IN ({placeholders})
            OR lower(json_extract(payload_json, '$.to_address')) IN ({placeholders})
          )
        ORDER BY block_number
        """,
        (chain, min_block, *values, *values),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [int(row["block_number"]) for row in rows]


async def custody_transfers_since(self, chain: str, since: datetime, addresses: set[str]) -> list[ChainEvent]:
    if not addresses:
        return []
    values = tuple(item.casefold() for item in addresses)
    placeholders = ",".join("?" for _ in values)
    cursor = await self.connection.execute(
        f"""
        SELECT chain, block_number, tx_hash, log_index, event_type, payload_json, observed_at
        FROM chain_events
        WHERE chain = ? AND event_type = 'Transfer'
          AND json_extract(payload_json, '$.block_time') >= ?
          AND (
            lower(json_extract(payload_json, '$.from_address')) IN ({placeholders})
            OR lower(json_extract(payload_json, '$.to_address')) IN ({placeholders})
          )
        ORDER BY block_number, log_index, id
        """,
        (chain, since.isoformat(), *values, *values),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [self._chain_event_from_row(row) for row in rows]
```

另加 `set_transfer_block_time(chain, block_number, block_time)`，在 `write_lock` 内逐行解析 JSON、加入 `block_time` 后更新同区块 Transfer 行并提交。不得用 SQL 字符串拼接地址值。

- [ ] **Step 4: 实现按相关区块缓存的时间戳补全**

`enrich_transfer_timestamps` 只读取 `unstamped_transfer_blocks(chain, addresses, min_block=flow_start_block)` 返回的去重区块，调用 `eth_getBlockByNumber(block, False)`，严格校验返回 number 与 timestamp，再调用原子补写。失败保留 chain、block、RPC method 上下文并使本轮资金流 UNKNOWN，不删除未补全事件。首次上线不回填 marker 之前的历史区块。

Run: `python -m pytest tests/test_storage.py tests/test_custody_collector.py -q`

Expected: PASS。

- [ ] **Step 5: 提交存储与时间戳补全**

```bash
git add usd1_monitor/storage.py usd1_monitor/collectors/custody.py tests/test_storage.py tests/test_custody_collector.py
git commit -m "补全托管资金流区块时间"
```

### Task 5: 调度托管集中度并持久化观测和风险

**Files:**
- Create: `tests/test_custody_integration.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/cli.py`

- [ ] **Step 1: 写入首次集中度、窗口积累和部分失败集成测试**

```python
@pytest.mark.asyncio
async def test_custody_monitor_persists_share_and_initial_alert(storage) -> None:
    await storage.insert_observation(observation("supply.multichain_total", "global", 100, NOW))
    monitor = CustodyConcentrationMonitor(FakeCustodySource([trusted_balance(71)]), storage, CustodyConfig())

    result = await monitor.check_once(deliver=False, now=NOW)

    share = await storage.latest_observation("custody.binance_share_lower_bound", "global")
    state = await storage.get_risk_state("custody.binance_concentration")
    assert result.success
    assert share is not None and share.value == pytest.approx(0.71)
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_custody_monitor_does_not_publish_partial_share(storage) -> None:
    await storage.insert_observation(observation("supply.multichain_total", "global", 100, NOW))
    monitor = CustodyConcentrationMonitor(FakeCustodySource([], trusted_complete=False), storage, CustodyConfig())

    result = await monitor.check_once(deliver=False, now=NOW)

    assert not result.success
    assert await storage.latest_observation("custody.binance_share_lower_bound", "global") is None
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_custody_integration.py -q`

Expected: FAIL，`CustodyConcentrationMonitor` 尚不存在。

- [ ] **Step 3: 实现监控事务边界**

`CustodyConcentrationMonitor.check_once` 必须：采集所有地址；确认 `supply.multichain_total` 不超过 4500 秒；写入单地址与实体余额；可信集合完整时写 `custody.binance_verified_balance` 和 `custody.binance_share_lower_bound`。第一次 EVM 可信采集完成时为每条链写 `custody.flow_start_block`，value 为该链 safe block；以后只补全该 block 起的相关 Transfer。资金流要求当前 scan cursor 已追到本轮 safe block，并且 `custody.flow_coverage_start` 距当前分别达到 1h/24h；历史不足时写“正在积累数据”观测但不评估流量。把 clear count 放入规则 evidence 并从当前风险状态对应最近观测恢复。集中度、24h 流量和 1h 外流使用三个稳定 rule id：

```python
RuleEvaluation("custody.binance_concentration", concentration_level, evidence, "custody:concentration")
RuleEvaluation("custody.binance_flow_24h", flow_level, flow_evidence, "custody:flow:24h")
RuleEvaluation("custody.address_outflow_1h", outflow_level, outflow_evidence, "custody:flow:1h")
```

观测 scope 使用 `global`、`{chain}:{address.casefold()}` 和 `{entity}:{chain}`，避免同地址跨链覆盖。Solana 只写 `custody.solana_balance_delta_1h/24h`，metadata 固定包含 `counterparty_attribution: false`。

- [ ] **Step 4: 接入主调度器和 CLI 构建**

给 `Usd1Monitor` 增加可选 `custody` 参数，在 `check_once` 和 `_component_checks` 中注册 `custody`；`build_market_monitor` 复用现有 `rpc_by_chain` 和 multichain Solana RPC 创建采集器。组件失败写 `health.scheduler_custody`，不阻断其他组件，不发送健康微信。

Run: `python -m pytest tests/test_custody_integration.py tests/test_cli.py -q`

Expected: PASS；成功时 `check` 输出 `custody trusted_balance=3000000000 share_lower_bound=0.71`，失败时输出以 `custody:` 开头的可识别错误。

- [ ] **Step 5: 提交托管监控集成**

```bash
git add usd1_monitor/scheduler.py usd1_monitor/cli.py tests/test_custody_integration.py tests/test_cli.py
git commit -m "接入托管集中度监控"
```

### Task 6: 将 PoR 30 分钟过期转为关键支柱 UNKNOWN

**Files:**
- Create: `tests/test_asset_assessment.py`
- Create: `usd1_monitor/engine/asset_assessment.py`
- Modify: `tests/test_reserve_supply_integration.py`
- Modify: `usd1_monitor/scheduler.py`

- [ ] **Step 1: 写入资产总状态优先级和 PoR 边界测试**

```python
from usd1_monitor.engine.asset_assessment import Pillar, assess_asset
from usd1_monitor.models import CoverageState, RiskLevel


def test_asset_assessment_priority_is_red_yellow_unknown_green() -> None:
    ready = [Pillar("concentration", True), Pillar("coverage", True), Pillar("redemption", True)]
    missing = [Pillar("concentration", True), Pillar("coverage", False, "por_stale"), Pillar("redemption", True)]

    assert assess_asset(RiskLevel.RED, missing).level is RiskLevel.RED
    assert assess_asset(RiskLevel.YELLOW, missing).level is RiskLevel.YELLOW
    assert assess_asset(RiskLevel.GREEN, missing).level is CoverageState.UNKNOWN
    assert assess_asset(RiskLevel.GREEN, ready).level is RiskLevel.GREEN


@pytest.mark.asyncio
@pytest.mark.parametrize(("age_seconds", "expected_count"), [(1799, 1), (1800, 0)])
async def test_coverage_is_available_before_30_minutes(
    storage, age_seconds: int, expected_count: int
) -> None:
    observed_at = NOW - timedelta(seconds=age_seconds)
    por = FakePorCollector()
    por.queue_snapshot(por_snapshot_for(4_200_000_000, observed_at))
    supply = FakeSupplyCollector()
    supply.queue_batch(
        multichain_batch(
            native_total=4_100_000_000,
            bridged_total=1_000_000,
            locked_total=1_000_000,
            complete=True,
        )
    )
    monitor = ReserveSupplyMonitor(
        por,
        supply,
        storage,
        FakeNotifier(),
        por_config=PorConfig(coverage_max_age_seconds=1800),
    )

    await monitor.check_once(deliver=False, now=NOW)

    rows = await storage.latest_observations(
        "supply.estimated_collateralization", limit=2
    )
    assert len(rows) == expected_count
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_asset_assessment.py tests/test_reserve_supply_integration.py -q`

Expected: FAIL，资产评估模块和 30 分钟覆盖率边界尚不存在。

- [ ] **Step 3: 实现纯资产评估函数**

```python
from dataclasses import dataclass

from usd1_monitor.models import CoverageState, RiskLevel


@dataclass(frozen=True)
class Pillar:
    name: str
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class AssetAssessment:
    level: RiskLevel | CoverageState
    missing_pillars: tuple[Pillar, ...]


def assess_asset(known_level: RiskLevel, pillars: list[Pillar]) -> AssetAssessment:
    missing = tuple(item for item in pillars if not item.available)
    if known_level is RiskLevel.RED:
        return AssetAssessment(RiskLevel.RED, missing)
    if known_level is RiskLevel.YELLOW:
        return AssetAssessment(RiskLevel.YELLOW, missing)
    if missing:
        return AssetAssessment(CoverageState.UNKNOWN, missing)
    return AssetAssessment(RiskLevel.GREEN, ())
```

- [ ] **Step 4: 将覆盖率新鲜度改为 1800 秒且保留健康规则**

在 `PorConfig` 增加 `coverage_max_age_seconds: int = Field(default=1800, gt=0)`；`ReserveSupplyMonitor._coverage_update` 对 `por.reserves.observed_at` 使用该值，对 `supply.multichain_total` 仍使用 4500 秒。`evaluate_por_age` 的 3600/7200 健康阈值和 `por.age` 状态不变，且仍由 StateEngine 静默持久化。

Run: `python -m pytest tests/test_asset_assessment.py tests/test_reserve_rules.py tests/test_reserve_supply_integration.py tests/test_wechat.py -q`

Expected: PASS；30:00 起覆盖率不可计算，储备最后已知金额仍存在，不产生微信。

- [ ] **Step 5: 提交 PoR 与资产评估内核**

```bash
git add usd1_monitor/config.py usd1_monitor/engine/asset_assessment.py usd1_monitor/scheduler.py tests/test_asset_assessment.py tests/test_reserve_supply_integration.py
git commit -m "增加资产未知状态判断"
```

### Task 7: 实现官方赎回通道分类规则

**Files:**
- Create: `tests/test_redemption_rules.py`
- Create: `usd1_monitor/engine/redemption_rules.py`

- [ ] **Step 1: 写入明确暂停、延迟、通用故障、否定语句和两轮恢复测试**

```python
@pytest.mark.parametrize(
    ("text", "usd1_specific", "expected"),
    [
        ("USD1 redemptions are suspended", True, RiskLevel.RED),
        ("USD1 redemption settlement is delayed", True, RiskLevel.YELLOW),
        ("Stablecoins settlement service disruption", False, RiskLevel.YELLOW),
        ("USD1 redemptions are not suspended", True, RiskLevel.GREEN),
        ("BitGo may suspend redemptions under these terms", True, RiskLevel.GREEN),
    ],
)
def test_redemption_classification(text, usd1_specific, expected) -> None:
    assert classify_redemption(text, usd1_specific=usd1_specific).level is expected


def test_redemption_recovery_requires_two_successes() -> None:
    first = apply_recovery(RiskLevel.RED, RiskLevel.GREEN, 0, required=2)
    second = apply_recovery(first.level, RiskLevel.GREEN, first.clear_checks, required=2)
    assert first.level is RiskLevel.RED and first.clear_checks == 1
    assert second.level is RiskLevel.GREEN and second.clear_checks == 0
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_redemption_rules.py -q`

Expected: FAIL，分类模块尚不存在。

- [ ] **Step 3: 实现上下文受限的分类器**

`classify_redemption` 先按句号和列表项分段，只分析当前新增/变化段落。RED 词组必须与 USD1 和 redeem/redemption 同句；YELLOW 的 delay/limit/bank/settlement 也必须有 USD1 上下文。`usd1_specific=False` 时只有 Statuspage 的 Stablecoins/Settlement active incident 可返回 YELLOW。沿用 `information_rules.py` 的前置否定判断，并额外排除 `may/can reserve the right to suspend` 这类静态权利条款。

返回：

```python
@dataclass(frozen=True)
class RedemptionClassification:
    level: RiskLevel
    summary: str
    matched_text: str | None
    confirmed_usd1: bool


@dataclass(frozen=True)
class RecoveryDecision:
    level: RiskLevel
    clear_checks: int
```

Run: `python -m pytest tests/test_redemption_rules.py -q`

Expected: PASS。

- [ ] **Step 4: 提交赎回规则**

```bash
git add usd1_monitor/engine/redemption_rules.py tests/test_redemption_rules.py
git commit -m "实现官方赎回通道规则"
```

### Task 8: 采集 BitGo Status、官方页面和媒体 RSS

**Files:**
- Create: `tests/fixtures/bitgo_status_normal.json`
- Create: `tests/fixtures/bitgo_status_incident.json`
- Create: `tests/fixtures/redemption_media.xml`
- Create: `tests/test_redemption_collector.py`
- Create: `usd1_monitor/collectors/redemption.py`

- [ ] **Step 1: 写入状态组件、页面正文和媒体排除测试**

```python
@pytest.mark.asyncio
async def test_status_collector_keeps_only_relevant_components() -> None:
    http = FakeHttp()
    http.queue_json(STATUS_URL, fixture_json("bitgo_status_incident.json"))

    snapshot = await BitGoStatusCollector(http, STATUS_URL).collect(NOW)

    assert snapshot.level is RiskLevel.YELLOW
    assert snapshot.confirmed_usd1 is False
    assert snapshot.summary == "BitGo Stablecoins 服务异常，可能影响 USD1，尚未确认"


def test_media_rss_excludes_unitas_and_marks_leads_unverified() -> None:
    items = parse_media_rss(fixture_text("redemption_media.xml"), NOW)

    assert [item.source for item in items] == ["media_redemption"]
    assert all(item.metadata["verified"] is False for item in items)
    assert all("Unitas" not in item.title for item in items)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_redemption_collector.py -q`

Expected: FAIL，采集器尚不存在。

- [ ] **Step 3: 实现严格 Statuspage 解析**

`BitGoStatusCollector` 调用 summary endpoint，要求 `components` 和 `incidents` 为列表；只接受名称匹配 Stablecoins、Settlement、API、Wallets 的组件。缺少 Stablecoins 或 Settlement、响应为空、状态值未知均抛 `RedemptionDataError`，不能解释成正常。开放 incident 若明确含 USD1，调用 `classify_redemption(incident_text, usd1_specific=True)`；否则 Stablecoins/Settlement 非 operational 返回未确认 YELLOW。

正常快照必须生成数值观测：

```python
Observation(
    metric="redemption.channel_status",
    source="bitgo_status",
    scope="global",
    value=float(classification.level),
    unit="risk_level",
    observed_at=collected_at,
    collected_at=collected_at,
    metadata={
        "summary": classification.summary,
        "confirmed_usd1": classification.confirmed_usd1,
        "source_url": status_url,
        "max_age_seconds": 900,
    },
)
```

- [ ] **Step 4: 实现官方页面差异和 RSS 线索解析**

官方页面采集复用 `normalize_text`、`_detail_body_content` 的同等边界，但在新模块公开专用 `extract_sections`。稳定 ID 使用 URL path，metadata 保存 `body_sections`。监控层在 upsert 前读取前一版 metadata，以规范化字符串集合差分，只分类新增段落；首次采集只建基线，不因长期条款告警。

RSS 使用 `xml.etree.ElementTree`，只接受 HTTPS link；标题或摘要必须同时命中 USD1/World Liberty 和 redeem/redemption/bank/settlement 之一，并排除 Unitas。每条结果生成完整对象：

```python
Announcement(
    source="media_redemption",
    stable_id=hashlib.sha256(link.encode("utf-8")).hexdigest(),
    title=title,
    url=link,
    published_at=published_at,
    body_hash=content_hash(title, summary),
    first_seen_at=collected_at,
    metadata={"verified": False, "lead_only": True, "summary": summary},
)
```

Run: `python -m pytest tests/test_redemption_collector.py -q`

Expected: PASS，页面结构错误和 RSS XML 错误均返回明确异常。

- [ ] **Step 5: 提交赎回采集器**

```bash
git add usd1_monitor/collectors/redemption.py tests/test_redemption_collector.py tests/fixtures/bitgo_status_normal.json tests/fixtures/bitgo_status_incident.json tests/fixtures/redemption_media.xml
git commit -m "采集官方赎回通道状态"
```

### Task 9: 调度赎回状态并限制媒体线索影响范围

**Files:**
- Create: `tests/test_redemption_integration.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/cli.py`

- [ ] **Step 1: 写入基线静默、明确风险、通用故障和媒体不告警测试**

```python
@pytest.mark.asyncio
async def test_redemption_baseline_is_clear_without_alert(storage) -> None:
    monitor = redemption_monitor(storage, status=clear_status(), pages=[terms_with_static_suspend_right()])
    await monitor.check_once(deliver=False, now=NOW)
    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.GREEN
    assert await storage.count_alert_deliveries() == 0


@pytest.mark.asyncio
async def test_explicit_usd1_suspension_is_red_and_alertable(storage) -> None:
    await seed_redemption_baseline(storage)
    monitor = redemption_monitor(storage, status=clear_status(), pages=[page_with_new_section("USD1 redemptions are suspended")])
    await monitor.check_once(deliver=False, now=NOW)
    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED
    assert await storage.count_alert_deliveries() == 1


@pytest.mark.asyncio
async def test_media_lead_is_stored_without_risk_or_alert(storage) -> None:
    monitor = redemption_monitor(storage, status=clear_status(), media=[unverified_lead()])
    await monitor.check_once(deliver=False, now=NOW)
    assert await storage.latest_announcement("media_redemption") is not None
    assert await storage.get_risk_state("event.information.media_redemption") is None
    assert await storage.count_alert_deliveries() == 0
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_redemption_integration.py -q`

Expected: FAIL，`RedemptionChannelMonitor` 尚不存在。

- [ ] **Step 3: 增加上一版公告读取并实现事务处理**

在 `Storage` 增加 `get_announcement(source, stable_id)`，返回 upsert 前版本。`RedemptionChannelMonitor` 对 status、pages、media 分别调度；status 每 300 秒，pages/media 每 3600 秒。每个官方来源维护 `redemption.source_status` 观测，scope 为稳定 source key；页面未变化时沿用该来源最近一次有效分类，页面变化时只用变化段落更新该来源状态。来自现有 `binance`、`wlfi`、`occ` 公告的新增正式内容也写各自 source status，不能由通用公告过期逻辑自动解除。聚合取所有未过期 source status 的最高等级，写 `redemption.channel_status`；明确风险统一写 `redemption.channel`，evidence 包含中文 summary、matched_text、confirmed_usd1、source_url 和 data_time。聚合连续两次为 GREEN 才把 `redemption.channel` 恢复为 GREEN，clear count 存在聚合观测 metadata 中。

媒体只调用 `upsert_announcement_uncommitted`，不创建 `RuleEvaluation`。官方页面首轮 `NEW` 只基线化并写该来源 GREEN；后续 `CHANGED` 只分类差异段落。明确暂停来源保持 RED，直到同一来源内容明确恢复，或另一条经匹配的官方恢复公告解除对应 cause；不能仅因 BitGo Status 为 operational 就覆盖掉仍有效的 USD1 暂停公告。

聚合前执行明确的新鲜度检查：BitGo Status 最近成功时间不得超过 `status_interval_seconds * 3`，每个配置的官方页面最近成功时间不得超过 `page_interval_seconds * 2`。任一必需来源过期时不刷新 `redemption.channel_status`，让其 metadata 中的 `max_age_seconds=status_interval_seconds * 3` 自然过期为关键支柱 UNKNOWN；来源失败仅更新健康状态，不生成赎回风险或恢复。

- [ ] **Step 4: 接入主调度与 CLI**

给 `Usd1Monitor` 增加可选 `redemption`，注册独立 component 和 interval。`build_market_monitor` 使用现有 `AsyncHttpClient` 创建 status、page 和 RSS source。BitGo/WLFI/Binance/OCC 既有公告功能保留；新组件只负责赎回语义，不重复发送通用公告事件。

Run: `python -m pytest tests/test_redemption_integration.py tests/test_information_integration.py tests/test_cli.py -q`

Expected: PASS；普通官方公告行为不变，赎回风险只告警一次，媒体不告警。

- [ ] **Step 5: 提交赎回监控集成**

```bash
git add usd1_monitor/storage.py usd1_monitor/scheduler.py usd1_monitor/cli.py tests/test_redemption_integration.py tests/test_information_integration.py tests/test_cli.py
git commit -m "接入官方赎回通道监控"
```

### Task 10: 在状态输出与仪表盘应用三支柱资产判断

**Files:**
- Modify: `tests/test_status_output.py`
- Modify: `tests/test_dashboard_data.py`
- Modify: `usd1_monitor/cli.py`
- Modify: `usd1_monitor/dashboard_data.py`

- [ ] **Step 1: 写入三支柱缺失、已知风险优先和最后已知值测试**

```python
@pytest.mark.asyncio
async def test_dashboard_business_is_unknown_when_por_is_stale(storage) -> None:
    await seed_green_business_states(storage, NOW)
    await seed_pillar(storage, "custody.binance_share_lower_bound", 0.40, NOW)
    await seed_pillar(storage, "redemption.channel_status", 0, NOW)
    await seed_pillar(storage, "por.reserves", 4_200_000_000, NOW - timedelta(minutes=30))
    await seed_pillar(storage, "supply.multichain_total", 4_100_000_000, NOW)

    snapshot = await dashboard_snapshot(storage, NOW)

    assert snapshot["business"]["level"] == "UNKNOWN"
    assert snapshot["business"]["missing_pillars"][0]["name"] == "coverage"
    assert snapshot["metrics"]["reserves"]["value"] == 4_200_000_000
    assert snapshot["metrics"]["estimated_collateralization"] is None


@pytest.mark.asyncio
async def test_known_yellow_precedes_missing_pillar(storage) -> None:
    await storage.set_risk_state("market.price", RiskLevel.YELLOW, NOW, NOW)
    snapshot = await dashboard_snapshot(storage, NOW)
    assert snapshot["business"]["level"] == "YELLOW"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_dashboard_data.py tests/test_status_output.py -q`

Expected: FAIL，当前输出只使用 `business_overall`。

- [ ] **Step 3: 实现统一支柱读取与输出**

在 `dashboard_data.py` 增加 `_critical_pillars(now)`：

```python
concentration_ready = is_fresh_with_metadata(concentration, now)
coverage_ready = (
    reserves is not None
    and supply is not None
    and now - reserves.observed_at < timedelta(seconds=1800)
    and now - supply.observed_at < timedelta(seconds=4500)
)
redemption_ready = is_fresh_with_metadata(redemption, now)
return [
    Pillar("concentration", concentration_ready, None if concentration_ready else "custody_unavailable"),
    Pillar("coverage", coverage_ready, None if coverage_ready else "coverage_unavailable"),
    Pillar("redemption", redemption_ready, None if redemption_ready else "redemption_unavailable"),
]
```

`is_fresh_with_metadata` 要求观测存在、`metadata.max_age_seconds` 为正整数且 `now - observed_at < max_age_seconds`；集中度默认 1200 秒，BitGo Status 默认 900 秒。reason 只在 unavailable 时保留。`snapshot()` 先计算 `known_level = business_overall(states, now=now, event_active_seconds=self.event_active_seconds)`，再调用 `assess_asset(known_level, pillars)` 覆盖 business level并返回 `missing_pillars`。CLI `_print_status` 使用相同纯函数和相同新鲜度常量；把常量及 `pillar_from_observations` 放入 `asset_assessment.py`，避免两处规则漂移。

Run: `python -m pytest tests/test_dashboard_data.py tests/test_status_output.py -q`

Expected: PASS。

- [ ] **Step 4: 提交统一资产判断**

```bash
git add usd1_monitor/engine/asset_assessment.py usd1_monitor/dashboard_data.py usd1_monitor/cli.py tests/test_dashboard_data.py tests/test_status_output.py
git commit -m "应用三支柱资产状态判断"
```

### Task 11: 展示集中度、资金流、赎回状态和媒体线索

**Files:**
- Modify: `tests/test_dashboard_data.py`
- Modify: `tests/test_dashboard_assets.py`
- Modify: `usd1_monitor/dashboard_data.py`
- Modify: `usd1_monitor/dashboard_static/index.html`
- Modify: `usd1_monitor/dashboard_static/dashboard.js`
- Modify: `usd1_monitor/dashboard_static/dashboard.css`

- [ ] **Step 1: 写入 API 字段与前端安全渲染测试**

```python
@pytest.mark.asyncio
async def test_dashboard_exposes_verified_lower_bound_and_unverified_leads(storage) -> None:
    await seed_custody_dashboard_observations(storage)
    await storage.upsert_announcement(unverified_media_lead())
    snapshot = await dashboard_snapshot(storage, NOW)

    assert snapshot["custody"]["binance_share_lower_bound"] == pytest.approx(0.68)
    assert snapshot["custody"]["label"] == "已核验地址至少占比"
    assert snapshot["custody"]["solana"]["counterparty_attribution"] is False
    assert snapshot["redemption"]["summary"] == "未发现官方限制"
    assert snapshot["recent"]["unverified_leads"][0]["verified"] is False


def test_dashboard_assets_render_new_risk_sections_without_inner_html() -> None:
    javascript = (ASSET_ROOT / "dashboard.js").read_text(encoding="utf-8")
    assert "renderCustody" in javascript
    assert "renderRedemption" in javascript
    assert "renderUnverifiedLeads" in javascript
    assert "非交易对手归因" in javascript
    assert ".innerHTML" not in javascript
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_dashboard_data.py tests/test_dashboard_assets.py -q`

Expected: FAIL，新字段和渲染函数尚不存在。

- [ ] **Step 3: 扩展只读快照**

`DashboardRepository` 从 observations 聚合：可信 Binance 总额、CEX、Peg、候选、Fireblocks/BitGo、巨鲸；24h 实体净变化；最大 1h 外流；Solana 1h/24h 净余额变化；每个地址的 label/status/evidence_urls/verified_on。过期值标 `available: false` 并保留 `last_known`，不能参与 GREEN。

`_recent_announcements` 保持官方公告；新增 `_unverified_leads`，SQL 固定 `source='media_redemption'`，返回最多 10 条并经过 `safe_external_url` 和 `sanitize_dashboard_text`。

- [ ] **Step 4: 实现前端卡片与 UNKNOWN 文案**

只用 `textContent` 和 `safeLink` 构造节点。资产 UNKNOWN 文案为“关键风险数据暂不可用，当前无法完整判断 USD1 风险”。覆盖率不可用时显示“官方储备数据已过期，当前覆盖率无法判断”；储备金额卡仍显示最后已知值并标“最后已知”。集中度标题固定“已核验 Binance 地址至少占比”。Solana 卡固定附“净余额变化，非交易对手归因”。官方清晰状态只写“未发现官方限制”。

Run: `python -m pytest tests/test_dashboard_data.py tests/test_dashboard_assets.py -q`

Expected: PASS。

Run: `node --check usd1_monitor/dashboard_static/dashboard.js`

Expected: exit 0。

- [ ] **Step 5: 提交仪表盘**

```bash
git add usd1_monitor/dashboard_data.py usd1_monitor/dashboard_static/index.html usd1_monitor/dashboard_static/dashboard.js usd1_monitor/dashboard_static/dashboard.css tests/test_dashboard_data.py tests/test_dashboard_assets.py
git commit -m "展示核心资产风险信号"
```

### Task 12: 增加简明微信资产告警并保持健康静默

**Files:**
- Modify: `tests/test_wechat.py`
- Modify: `usd1_monitor/notifications/wechat.py`

- [ ] **Step 1: 写入集中度、资金流和赎回告警文案测试**

```python
def test_custody_concentration_message_is_plain_and_explains_lower_bound() -> None:
    content = format_transitions(
        [transition("custody.binance_concentration", RiskLevel.GREEN, RiskLevel.RED, evidence={"share": 0.71, "verified_balance": 3_000_000_000, "source_urls": ["https://www.binance.com/en/square/post/97671"]})],
        overall_level=RiskLevel.RED,
        timezone_name="Asia/Shanghai",
    )
    assert "已核验 Binance 地址至少占全网供应量 71%" in content
    assert "rule_id" not in content and "**" not in content


def test_generic_bitgo_incident_says_not_confirmed() -> None:
    content = format_transitions(
        [transition("redemption.channel", RiskLevel.GREEN, RiskLevel.YELLOW, evidence={"summary": "BitGo Stablecoins 服务异常，可能影响 USD1，尚未确认"})],
        overall_level=RiskLevel.YELLOW,
        timezone_name="Asia/Shanghai",
    )
    assert "可能影响 USD1，尚未确认" in content


@pytest.mark.asyncio
async def test_unknown_health_and_media_never_enqueue_wechat(storage) -> None:
    await StateEngine(storage).apply([RuleEvaluation("health.redemption", RiskLevel.RED)], NOW)
    assert await storage.count_alert_deliveries() == 0
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_wechat.py -q`

Expected: FAIL，新规则尚无可读文案。

- [ ] **Step 3: 实现三类专用文案**

在 `_human_summary` 中增加 `custody.binance_concentration`、`custody.binance_flow_24h`、`custody.address_outflow_1h`、`redemption.channel` 分支。集中度显示至少占比和已核验余额；资金流显示链、标签、金额、1h/24h，Solana 必须写“余额变化”；赎回显示 `evidence["summary"]` 和官方来源。恢复文案分别为“集中度已回到预警线内”“大额资金变化已结束”“官方赎回限制已解除”。

不得修改 `is_monitoring_health_rule` 的静默边界；`UNKNOWN` 没有 RuleEvaluation，媒体没有风险状态，因此二者不能进入微信队列。

Run: `python -m pytest tests/test_wechat.py tests/test_custody_integration.py tests/test_redemption_integration.py -q`

Expected: PASS。

- [ ] **Step 4: 提交通知文案**

```bash
git add usd1_monitor/notifications/wechat.py tests/test_wechat.py tests/test_custody_integration.py tests/test_redemption_integration.py
git commit -m "增加核心资产风险通知"
```

### Task 13: 更新运行说明与启动覆盖范围

**Files:**
- Modify: `tests/test_status_output.py`
- Modify: `tests/test_examples.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `README.md`

- [ ] **Step 1: 写入启动文案和状态输出断言**

```python
def test_startup_message_lists_new_asset_monitors() -> None:
    message = format_startup_message(
        ["价格", "已核验 Binance 地址集中度与资金流", "储备覆盖率", "官方赎回通道"],
        [],
    )
    assert "已核验 Binance 地址集中度与资金流" in message
    assert "官方赎回通道" in message


@pytest.mark.asyncio
async def test_status_prints_unknown_pillars(capsys, storage) -> None:
    await _print_status(storage)
    output = capsys.readouterr().out
    assert "business_overall: UNKNOWN" in output
    assert "missing_pillar: concentration" in output
    assert "missing_pillar: coverage" in output
    assert "missing_pillar: redemption" in output
```

- [ ] **Step 2: 更新 README 和启动范围**

README 明确：trusted 核验标准和 90 天有效期；candidate 仅展示；Binance 数字是下限；Solana 是净余额差；PoR 超过 30 分钟覆盖率 UNKNOWN；“未发现官方限制”不等于主动赎回成功；媒体线索不告警；新增 RPC 响应基线约 14–16 万/月，另加“命中可信 EVM 地址的唯一 Transfer 区块数”，每个相关区块只补取一次时间戳。

启动文案把“已核验 Binance 地址集中度与资金流”“储备覆盖率”“官方赎回通道”列入正在监控，不把媒体线索描述为风险监控。

- [ ] **Step 3: 运行文档与输出测试并提交**

Run: `python -m pytest tests/test_status_output.py tests/test_examples.py tests/test_wechat.py -q`

Expected: PASS。

```bash
git add README.md usd1_monitor/scheduler.py tests/test_status_output.py tests/test_examples.py
git commit -m "说明核心风险监控边界"
```

### Task 14: 完整验证与范围审查

**Files:**
- Verify only

- [ ] **Step 1: 运行新增功能的直接相关测试**

Run: `python -m pytest tests/test_config.py tests/test_custody_rules.py tests/test_custody_collector.py tests/test_custody_integration.py tests/test_asset_assessment.py tests/test_reserve_supply_integration.py tests/test_redemption_rules.py tests/test_redemption_collector.py tests/test_redemption_integration.py tests/test_dashboard_data.py tests/test_dashboard_assets.py tests/test_wechat.py tests/test_status_output.py tests/test_cli.py -q`

Expected: PASS。

- [ ] **Step 2: 运行完整测试集**

Run: `python -m pytest -q`

Expected: 所有测试通过，仅保留项目已有 skip。

- [ ] **Step 3: 检查编译、依赖和 JavaScript**

Run: `python -m compileall -q usd1_monitor tests`

Expected: exit 0。

Run: `python -m pip check`

Expected: `No broken requirements found.`

Run: `node --check usd1_monitor/dashboard_static/dashboard.js`

Expected: exit 0。

- [ ] **Step 4: 检查差异质量与配置安全**

Run: `git diff --check`

Expected: 无输出。

Run: `rg -n "WECHAT_WEBHOOK|RPC_URL.*https://[^$]|api[_-]?key|token=" config.example.yaml deploy README.md usd1_monitor tests`

Expected: 不出现真实 webhook、RPC 密钥、API key 或 token；示例仅使用无凭据公共 URL 或环境变量名。

- [ ] **Step 5: 检查最终范围**

Run: `git status --short`

Expected: 无未提交文件。

Run: `git diff 923cec3..HEAD --stat`

Expected: 只包含本计划列出的核心风险配置、采集器、规则、调度、存储、仪表盘、通知、文档和测试；不包含数据库迁移、依赖升级、目录重构、逐区块交易扫描或其他风险功能。
