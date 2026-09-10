# USD1 完整多链供应量核对设计

日期：2026-09-10
状态：已确认

## 1. 目标

在现有 USD1 监控程序中加入完整多链供应量核对，覆盖 WLFI 官方仪表盘当前列出的 11 条链和 5 个 CCIP 锁仓池，并将完整原生供应量用于储备覆盖率计算。

本功能必须满足以下约束：

- 每小时采集一次，不扫描历史区块。
- 不调用 `eth_getLogs`、`trace_*` 或 `debug_*`。
- 任一必需数据源失败时，本轮不生成聚合值，也不更新供应量风险状态。
- 不使用上一轮数据填补本轮缺失值。
- 微信只在状态变化时通知，并合并同一轮的数据源故障。
- 保留 DefiLlama 数据作为辅助对照，但不再将其作为储备覆盖率的主要供应量分母。

## 2. 官方数据边界

链、合约地址、桥池地址和精度以 WLFI 官方 `cre-por-dashboard` 仓库提交 `109a68ae0a6ca5a769bee69e590f24ae57e7943e` 为基准：

- [USD1 链与合约配置](https://github.com/worldliberty/cre-por-dashboard/blob/109a68ae0a6ca5a769bee69e590f24ae57e7943e/lib/contracts/usd1-token.ts)
- [官方供应量聚合逻辑](https://github.com/worldliberty/cre-por-dashboard/blob/109a68ae0a6ca5a769bee69e590f24ae57e7943e/hooks/use-usd1-supply.ts)
- [官方储备证明说明](https://docs.worldlibertyfinancial.com/usd1-token/proof-of-reserves)

### 2.1 原生供应量

完整总供应量只累计以下 6 条原生链，避免把桥接代币重复计入：

| 链 | 资产或合约 | 精度 |
| --- | --- | ---: |
| Ethereum | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | 18 |
| BNB Chain | `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d` | 18 |
| Tron | `TPFqcBAaaUMCSVRCqPaQ9QnzKhmuoLR6Rc` | 18 |
| Solana | `USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB` | 6 |
| Aptos | `0x05fabd1b12e39967a3c24e91b7b8f67719a6dacee74f3c8b9fb7d93e855437d2` | 6 |
| Tempo | `0x20C000000000000000000000111111111E910F0f` | 6 |

### 2.2 桥接发行量

以下 5 条目标链使用同一个桥接合约地址 `0x111111d2bf19e43C34263401e0CAd979eD1cdb61`：

| 链 | 精度 |
| --- | ---: |
| Plume | 18 |
| AB Core | 18 |
| Monad | 6 |
| Mantle | 18 |
| Morph | 18 |

桥接发行量只用于桥池核对，不计入完整总供应量。

### 2.3 CCIP 锁仓量

| 源链 | 桥池账户或合约 |
| --- | --- |
| Ethereum | `0x36a72eD0096B414521C45E3ddC9ed657d1D9c141` |
| BNB Chain | `0xCe3f7378aE409e1CE0dD6fFA70ab683326b73f04` |
| Solana | `8c2WaLy3aW9rnFaq8cCZUYnQNSVa9oX2eUun4buZqhmf` |
| Aptos | `0x1eb155d08acc900954b6ccee01659b390399ae81ad4c582b73d41374c475caf6` |
| Tempo | `0x891F30e80B0809800BbaB14633F9eCe8Fc210024` |

Solana 使用实际池代币账户，而不是 CCIP 状态账户。

## 3. 方案选择

采用在现有 Python 程序内扩展采集器的方案。

未采用直接运行官方 TypeScript 仪表盘代码，因为它会引入第二套运行时和多套 SDK，增加部署与故障处理成本。未采用第三方聚合 API 作为主数据源，因为聚合 API 不能可靠提供全部桥池余额，也无法独立识别桥接超发。

## 4. 架构与组件

### 4.1 协议读取器

新增按协议隔离的轻量读取器，统一输出带有数据源、作用域、数值、精度、采集时间和来源地址的供应量组件：

- EVM 读取器：处理 Ethereum、BNB Chain、Tempo、Plume、AB Core、Monad、Mantle、Morph。使用 `eth_blockNumber` 和固定区块标签上的 `eth_call` 读取 `totalSupply()` 或 `balanceOf()`。同一链同一轮复用区块号。
- Tron 读取器：通过 Tron HTTP API 调用 USD1 合约的 `totalSupply()` 常量方法。
- Solana 读取器：按照官方实现使用 `getAccountInfo` 的 `jsonParsed` 编码，分别读取 mint 的 `supply` 和池代币账户的 `tokenAmount.amount`。不使用公共 RPC 经常禁用的索引方法。
- Aptos 读取器：按照官方实现通过 Aptos Indexer 查询 fungible asset 的 `supply_v2` 和指定桥池账户余额。

读取器只负责获取并验证单项数据，不负责计算风险。

### 4.2 多链编排器

`MultichainSupplySource` 并发执行所有读取任务，并将并发上限固定为 4，避免短时间突发请求。它返回：

- 本轮成功的组件读数；
- 按具体链标识的错误；
- 仅在所有必需组件成功时生成的完整快照。

完整快照包含：

- `native_total`：6 条原生链供应量之和；
- `bridged_total`：5 条桥接链发行量之和；
- `locked_total`：5 个源链桥池余额之和；
- `issuance_delta`：`bridged_total - locked_total`。

### 4.3 现有监控器集成

复用 `ReserveSupplyMonitor`、现有 SQLite observations 表、健康状态、状态机和通知发送流程，不增加数据库迁移。

新增观察指标：

- `supply.native`：6 条原生链的单链读数；
- `supply.bridged`：5 条桥接链的单链读数；
- `bridge.locked`：5 个桥池的单项读数；
- `supply.multichain_total`：完整原生供应量；
- `supply.bridged_total`：桥接发行量总和；
- `bridge.locked_total`：桥池锁仓量总和；
- `bridge.issuance_delta`：带正负号的桥接差额。

DefiLlama 继续写入既有 `supply.global`，仅用于状态输出和人工对照。

程序重启后，以最近一次 `supply.multichain_total` 判断下次供应量任务是否到期。上一次采集不完整时没有该指标，因此重启后会立即重试，而不会被 DefiLlama 的成功读数推迟。

## 5. 配置与兼容性

在现有 `supply` 配置下增加多链 RPC 配置。旧配置文件不写新增字段时仍可加载，并使用官方公共端点默认值。

新增链的默认端点如下：

| 链或协议 | 默认端点 |
| --- | --- |
| Tron | `https://api.trongrid.io`、`https://api.tronstack.io` |
| Solana | `https://solana-rpc.publicnode.com`、`https://api.mainnet-beta.solana.com` |
| Aptos Indexer | `https://api.mainnet.aptoslabs.com/v1/graphql` |
| Tempo | `https://rpc.presto.tempo.xyz` |
| Plume | `https://rpc.plume.org` |
| AB Core | `https://rpc.core.ab.org` |
| Monad | `https://rpc.monad.xyz` |
| Mantle | `https://rpc.mantle.xyz` |
| Morph | `https://rpc.morphl2.io` |

Ethereum 和 BNB Chain 继续复用 `chains.ethereum.rpc_urls`、`chains.bsc.rpc_urls` 以及现有的 `ETH_RPC_URLS`、`BSC_RPC_URLS` 环境变量。其余链允许在配置中提供多个 HTTPS 端点，并支持以下环境变量覆盖：

- `TRON_RPC_URLS`
- `SOLANA_RPC_URLS`
- `APTOS_INDEXER_URLS`
- `TEMPO_RPC_URLS`
- `PLUME_RPC_URLS`
- `AB_RPC_URLS`
- `MONAD_RPC_URLS`
- `MANTLE_RPC_URLS`
- `MORPH_RPC_URLS`

环境变量继续使用项目现有的逗号分隔格式。密钥型 RPC URL 不写入示例配置或日志。

合约地址和精度由代码中的官方配置常量提供，不开放普通配置覆盖，防止错误地址产生看似正常但实际无效的汇总结果。官方地址变化需要明确更新常量、来源提交和对应测试。

## 6. 数据流与事务边界

每个小时周期按以下顺序执行：

1. 编排器并发采集 16 个逻辑组件，其中同链 EVM 请求复用区块号。
2. 验证类型、精度、非负性和响应身份。
3. 保存本轮成功的单项观察，供排障和健康恢复使用。
4. 如果任何必需组件失败，停止本轮聚合，不读取历史值补齐，不更新覆盖率或桥接风险。
5. 如果全部成功，在同一 SQLite 事务中写入四个聚合指标并计算风险状态。
6. 完成事务后更新各数据源健康状态并发送待处理通知。

原生供应量必须大于 0；桥接供应量和桥池余额允许为 0。失败绝不转换为 0。

聚合值、覆盖率观察和对应风险状态必须在同一个数据库事务中提交。事务失败时不得留下半成品汇总。

## 7. 储备覆盖率

储备覆盖率调整为：

`储备覆盖率 = 最新有效 PoR 储备金额 / 本轮 supply.multichain_total × 100%`

只有当前完整多链快照成功且 PoR 数据未过期时才新增覆盖率观察。PoR 单独刷新时不重复使用同一份供应量生成新的覆盖率读数，防止一份供应量在一小时内被计作两次连续异常。

沿用现有覆盖率规则：

- 连续 2 次低于 100%，或相邻结果变化超过 0.5 个百分点：YELLOW；
- 连续 2 次低于 99%，并且市场价格同时异常：RED；
- 连续 2 次恢复到至少 100% 才恢复 GREEN。

现有 24 小时原生供应量下降规则改为基于 `supply.multichain_total`，不再只累计 Ethereum 和 BNB Chain。单链因正常跨链导致的供应量迁移不单独判定为供应量下降风险。

## 8. 桥接差额风险规则

差额定义为 `跨链发行量 - 跨链锁仓量`。规则只使用同一轮完整快照。

### 8.1 潜在超发

差额为正时，以锁仓量为比例分母：

- 差额同时大于 100,000 USD1 和锁仓量的 0.1%，连续 2 次达到条件：YELLOW；
- 差额同时大于 1,000,000 USD1 和锁仓量的 1%，连续 2 次达到条件：RED；
- 锁仓量为 0、桥接发行量大于 0，连续 2 次：RED。

RED 必须连续两次达到 RED 阈值。一次 YELLOW 后紧接一次 RED，先保持或进入 YELLOW，下一次仍达到 RED 阈值才升级。

### 8.2 锁仓多于发行

差额为负时，以桥接发行量为比例分母：

- 差额绝对值同时大于 1,000,000 USD1 和发行量的 1%，连续 2 次：YELLOW；
- 不升级为 RED；
- 发行量为 0 时，绝对差额大于 1,000,000 USD1，连续 2 次：YELLOW。

两个连续样本必须方向相同才能触发。方向变化会重新累计，避免把两种不同现象拼成连续异常。

### 8.3 恢复

已处于异常状态时，必须连续 2 次回到对应阈值以内才恢复 GREEN。只要本轮数据不完整，就保持原风险状态但不累计异常或恢复次数。

使用单个风险规则 `supply.bridge_reconciliation` 保存总体桥接核对状态，证据中记录方向、锁仓量、发行量、绝对差额和差额比例。

## 9. 健康状态与微信降噪

每个链或协议数据源保留独立健康计数，以便定位故障；总供应量健康状态仅用于内部状态和命令行输出，不额外生成重复微信消息。

通知规则：

- 同一轮多个供应量数据源失败时，合并为一条“供应量数据异常”；
- 连续失败 3 次才通知；
- 恢复时只发送一条合并恢复通知；
- 相同风险等级不重复推送；
- 风险消息只显示简单中文字段：发生了什么、发行量、锁仓量、相差多少、涉及方向、发现时间和建议；
- 不向用户显示内部规则 ID、异常堆栈或一长串 RPC 地址；详细错误保留在日志和 `check` 输出中。

示例：

```text
🟡 USD1 跨链核对异常

发生了什么：跨链发行量比桥池锁仓量多 35 万 USD1
跨链发行量：12.50 亿 USD1
桥池锁仓量：12.4965 亿 USD1
连续发现：2 次
发现时间：2026-09-10 12:00:00（北京时间）

建议：关注后续一小时结果；如果差额继续扩大，检查官方桥接状态。
```

## 10. 调用量

实现只读取当前状态。通过同链复用区块号和固定精度，预计每轮少于 30 个 RPC 或 HTTP 响应，每小时一次约为每月 15,000 至 19,000 次响应。

该估算不包括现有价格、PoR、权限检查和官方公告调用，但新增多链供应量核对相对于 25,000,000 次/月套餐占用极低，也不会使用按高倍计费的 trace/debug 方法。

## 11. 错误处理

- 预期的数据格式、精度、身份和非负性错误转换为带链名的数据采集错误。
- 网络超时、HTTP/RPC 错误保留原始异常上下文并由现有重试和多端点切换处理。
- 非预期异常不吞掉，保留日志调用链并使本轮 `check` 返回失败。
- 单项错误不得阻止其他并发项完成和保存，但会阻止本轮聚合与风险更新。
- 不为失败数据设置默认值，不静默降级为 DefiLlama 汇总。

## 12. 测试与验收

### 12.1 单元测试

- EVM、Tron、Solana、Aptos 读取器的正常响应、异常响应、精度解析、零值和无效值。
- 11 条链、5 个桥池的地址、精度和原生/桥接/锁仓分类。
- EVM 同链复用区块号，且没有 `eth_getLogs`、trace/debug 调用。
- 所有桥接阈值边界、连续两次触发、连续两次恢复、正负方向、方向切换和零分母。

### 12.2 集成测试

- 完整批次正确写入所有单项与聚合观察。
- 任意一个必需组件失败时，不写聚合观察、不更新覆盖率和桥接风险。
- 成功单项仍可落库，失败项健康计数正确。
- 储备覆盖率使用 `supply.multichain_total`，且同一供应量快照不会被 PoR 刷新重复计数。
- 24 小时供应量下降使用 6 条原生链总量。
- 多链故障和恢复各只生成一条合并微信消息。
- 旧配置不增加新字段也能加载；环境变量可覆盖新增端点。

### 12.3 验收

- 全部自动化测试通过。
- `python -m usd1_monitor --config config.yaml check` 输出 6 条原生链、5 条桥接链、5 个桥池、三个聚合总量和有符号差额。
- 启动信息将“完整多链供应量核对”列为正在监控，不再列入暂未覆盖。
- 实际运行一轮时没有调用历史日志、trace 或 debug 方法。

## 13. 非目标

本次不开发网页仪表盘、主动赎回测试、私有交易所账户接入、Binance 钱包集中度、社交媒体情绪或 DeFi 清算监控；也不修改现有 EVM 权限监控频率和实现。
