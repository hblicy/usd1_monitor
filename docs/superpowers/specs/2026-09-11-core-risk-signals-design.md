# USD1 核心风险信号增强设计

## 目标

在现有价格、流动性、合约权限、储备、供应量和官方公告监控之上，优先补齐三个直接影响 USD1 风险判断的信号：

1. Binance 及相关托管地址的 USD1 集中度与资金流；
2. 储备覆盖率数据过期时的 `UNKNOWN` 语义；
3. BitGo / 官方赎回通道状态和银行通道信息，以及不直接告警的银行通道媒体线索。

资产总状态不再把“关键风险数据不可用”显示成绿色。监控健康继续单独展示且不发送微信；只有明确的资产风险变化进入微信队列。

## 本期范围

### 纳入

- Ethereum、BNB Chain、Solana 上经过核验的 Binance 地址当前 USD1 余额。
- Binance 地址组相对完整多链供应量的集中度下限。
- Ethereum、BNB Chain 的精确组外流入流出，以及 1 小时、24 小时窗口。
- Solana 的 10 分钟余额快照差分；明确标注为“净余额变化”，不声称已确认交易对手。
- 已核验 Binance 地址组 24 小时净变化和单地址 1 小时组外净流出。
- PoR Oracle 超过 30 分钟未更新时，覆盖率和资产判断进入 `UNKNOWN`。
- BitGo Status、BitGo USD1 页面和条款、BitGo 投资者新闻、WLFI、Binance、OCC 等一手来源中的赎回、结算和银行通道变化。
- 媒体线索仅在仪表盘展示，不改变资产风险等级，不发送微信。

### 不纳入

- 自动发现或自动信任 Arkham、Etherscan、BscScan 标签。
- 使用私有交易所账户、BitGo 账户或钱包密钥进行真实赎回测试。
- Solana 交易历史和交易对手精确归因。
- 将 Fireblocks、BitGo 或未标注巨鲸余额混入 Binance 地址组。
- 本轮需求之外的大额退出滑点、铸造销毁净供应和 Dolomite/WLFI 清算距离。

## 总体架构

采用三个边界独立的能力，并复用现有状态引擎、SQLite、仪表盘和微信链路。

### `CustodyConcentrationMonitor`

职责是读取静态核验地址注册表、采集余额、计算 Binance 集中度和资金流窗口，并生成观测和资产风险规则。它不负责发现标签，也不修改供应量采集器。

- EVM 当前余额通过 USD1 `balanceOf(address)` 在确认后的安全区块读取。
- EVM 精确资金流复用现有 USD1 `Transfer` 日志，不新增 `eth_getLogs`。
- 当前生产路径没有持续执行完整区块扫描。只对命中可信地址且尚未记录时间的 Transfer 所在区块调用一次 `eth_getBlockByNumber`，把真实区块时间写入事件 metadata；转账窗口不使用程序扫到事件的时间，也不恢复逐区块交易扫描。
- Solana 通过 `getTokenAccountsByOwner` 汇总指定 owner 对 USD1 mint 的所有 Token Account 余额。
- Solana 只根据连续余额快照计算净余额变化。

### `AssetAssessment`

职责是在不改变数据库风险等级枚举的前提下，为仪表盘和状态输出生成资产判断结果。已知 YELLOW/RED 风险优先；没有已知风险但关键风险支柱不可判断时返回 `UNKNOWN`。

### `RedemptionChannelMonitor`

职责是读取 BitGo 公开状态和官方页面，维护赎回通道状态，并把媒体 RSS 结果作为独立的待核实线索。它不依赖私有 BitGo API，不尝试发起 mint 或 redeem。

三者的失败分别记录采集健康，不使供应量、市场或其他官方公告采集失败。

## 地址注册表与核验规则

### 配置结构

新增独立配置段，包含：

- 检查间隔，默认 600 秒；
- 集中度和资金流阈值；
- 地址所属链、实体、显示标签、角色；
- `trusted` 或 `candidate` 状态；
- 核验日期；
- 证据类型和证据 URL；
- 核验有效期，默认 90 天。

实体至少区分：

- `binance_cex`：Binance 热钱包、冷钱包和运营钱包；
- `binance_peg_reserve`：Binance-Peg 储备地址；
- `fireblocks_custody`：外部托管地址；
- `bitgo_issuer`：BitGo/发行端地址；
- `unlabeled_whale`：未标注巨鲸。

集中度的 Binance 分子为 `binance_cex + binance_peg_reserve`，同时在仪表盘分栏显示两者，避免把 Binance-Peg 储备误写成交易所热钱包库存。Fireblocks、BitGo 和未标注巨鲸不进入 Binance 分子。

### 可信条件

地址只有满足以下任一条件，才能配置为 `trusted`：

1. Binance、BitGo、Fireblocks 或其他所属实体的官方公开页面明确列出该地址；
2. 两个相互独立的公开标签来源均明确标注同一实体。

每个可信地址必须具有未超过 90 天的人工核验日期。证据过期、证据主机重复、地址格式错误或链内重复时，配置校验失败；地址必须重新核验或降级为 `candidate`。

运行时不抓取标签网站，也不会自动把候选地址提升为可信地址。候选地址可以显示余额和来源，但不参与阈值、资金流或资产总状态。

### 初始候选清单

实现阶段以以下地址为待核验输入，只有通过上述规则的地址进入可信集合。

Ethereum / BNB Chain EVM 候选：

```text
0xf977814e90da44bfa03b6295a0616a897441acec
0x5a52e96bacdabb82fd05763e25335261b270efcb
0x28c6c06298d514db089934071355e5743bf21d60
0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503
0x21a31ee1afc51d94c2efccaa2092ad1028285549
0xdfd5293d8e347dfe59e90efd55b2956a1343963d
0xbe0eb53f46cd790cd13851d5eff43d12404d33e8
0x8894e0a0c962cb723c1976a4421c95949be2d4e3
0xe2fc31f816a9b94326492132018c3aecc4a93ae1
0x9696f59e4d72e237be84ffd425dcad154bf96976
0x56eddb7aa87536c09ccc2793473599fd21a8b17f
0x4976a4a02f38326660d17bf34b431dc6e2eb2327
0x01c952174c24e1210d26961d456a77a39e1f0bb0
```

Solana Binance 候选：

```text
5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9
2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S
9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
3yFwqXBfZY4jBVUafQ1YEXw189y2dN3V5KQq9uzBDy1E
3gd3dqgtJ4jWfBfLYTX67DALFetjc5iS72sCgRhCkW2u
6QJzieMYfp7yr3EdrePaQoG3Ghxs2wM98xSLRu8Xh56U
```

Solana Fireblocks 候选：

```text
9Rycov3U4efJf5HiqZYGjN7qJJHEtMsj4vbmkG4xfCxk
```

未标注巨鲸候选仅用于单独展示：

```text
0xAC3E216bD55860912062a4027A03b99587B7FfC7
0x041c32c919de3e85e0D89984c2590434f6569dFA
```

Binance 2022 年公开热冷钱包页面可以作为官方所有权证据之一，但实现阶段仍需记录本次人工核验日期和当前标签来源：

`https://www.binance.com/en/square/post/97671`

## 指标与存储

不新增数据库表或迁移，复用现有通用表。

### 余额和集中度

写入 `observations`：

- `custody.address_balance`：单地址、单链 USD1 余额；
- `custody.entity_balance`：实体和链的可信地址余额合计；
- `custody.binance_verified_balance`：全部可信 Binance 地址合计；
- `custody.binance_share_lower_bound`：可信 Binance 余额 / 完整多链供应量。

集中度必须展示为“已核验地址下限”或“至少占比”，不能写成 Binance 完整持仓。分母只使用新鲜的 `supply.multichain_total`；其最大允许年龄沿用现有完整供应量 4500 秒新鲜度规则。

一次采集中的所有可信地址必须成功，并使用各链同一安全头或同一次 Solana 快照。任何可信地址失败、供应量过期或链扫描明显落后时，本轮集中度为 `UNKNOWN`，不得使用部分分子或旧分母继续判绿。

### 资金流

写入或派生：

- `custody.binance_net_change_24h`：Binance 地址组 24 小时净变化；
- `custody.address_external_outflow_1h`：单可信地址 1 小时流向地址组外的净流出；
- `custody.solana_balance_delta_1h` / `24h`：Solana 快照差分。

EVM 地址组内部转账同时产生一入一出，必须从实体资金流中抵消。EVM 组外流向使用真实 Transfer 方向。Solana 只显示余额差，不展示或推断交易对手。

EVM 区块真实时间保存在现有事件 payload metadata 中，不改变表结构。没有区块真实时间的旧事件不进入新时间窗口。Solana 和旧 EVM 数据在升级后重新积累：1 小时窗口在 1 小时后可用，24 小时窗口在 24 小时后可用；此前显示“正在积累数据”。

## 风险规则

### 集中度和资金流

- `custody.binance_share_lower_bound > 50%`：YELLOW；
- `custody.binance_share_lower_bound > 70%`：RED；
- Binance 地址组 24 小时净流入或净流出绝对值 `> 50,000,000 USD1`：YELLOW；
- 任一可信地址 1 小时内向地址组外净流出 `> 100,000,000 USD1`：YELLOW。

资金移动本身不产生 RED，避免正常归集、客户提现和赎回被当成确定性危机。连续两个 10 分钟检查回到阈值内后解除状态，避免临界点反复通知。

首次成功采集立即计算集中度并生成当前风险状态。若已超过 50% 或 70%，允许在部署后发送一次资产告警；流量规则必须等相应窗口完整后才能触发。

### 关键支柱和资产总状态

关键支柱为：

1. Binance 集中度当前快照；
2. 新鲜且可计算的储备覆盖率；
3. 最近成功检查的官方赎回通道状态。

资产总状态采用以下顺序：

1. 已知资产风险为 RED 时显示 RED；
2. 否则存在已知 YELLOW 时显示 YELLOW；
3. 否则任一关键支柱为 `UNKNOWN` 时显示 `UNKNOWN／无法判断`；
4. 三个关键支柱均可判断且没有风险时才显示 GREEN。

`UNKNOWN` 是展示和状态输出层的判断结果，不加入 `RiskLevel` 数据库枚举，不新增风险状态迁移，也不发送微信。

### PoR 覆盖率新鲜度

- PoR Oracle 的 bundle timestamp 距当前时间不超过 30 分钟时，储备值可参与覆盖率；
- 超过 30 分钟，储备金额仍可作为最后已知值展示，但覆盖率数值必须隐藏，显示“官方储备数据已过期，当前覆盖率无法判断”；
- 完整多链供应量自身过期时采用相同的覆盖率 `UNKNOWN` 结果；
- 一条新的、校验通过且处于 30 分钟窗口内的 Oracle 数据即可恢复覆盖率可用性；不发送恢复微信；
- `por.age` 继续属于监控健康，不重新变成资产风险规则。

## 官方赎回通道状态

### 一手来源

- BitGo Status 公共 Statuspage API，至少读取 Stablecoins、Settlement、API、Wallets 组件和开放事件；
- `https://www.bitgo.com/usd1/`；
- `https://www.bitgo.com/usd1-terms/`；
- BitGo 投资者新闻列表；
- WLFI FAQ、USD1 文档和官方公告；
- 已有 Binance USD1 公告；
- 已有 OCC 决定与许可页面。

默认频率：BitGo Status 每 5 分钟；BitGo USD1/条款/新闻每 60 分钟；WLFI、Binance、OCC 保留现有频率。所有这些是普通 HTTPS，不消耗链上 RPC 额度。

### 语义与等级

- 官方明确 USD1 赎回暂停、关闭、冻结或无法处理：RED；
- 官方明确 USD1 赎回延迟、限额或银行结算延迟：YELLOW；
- BitGo Status 的 Stablecoins 或 Settlement 发生故障但没有明确提到 USD1：YELLOW，文案必须写“可能影响 USD1，尚未确认”；
- USD1 条款、托管人、发行主体或银行通道发生实质变化：YELLOW；
- 官方页面和状态均无异常时只显示“未发现官方限制”，禁止写“赎回正常”或“已测试可赎回”。

通道恢复需要连续两次成功检查为正常。明确的资产风险变化和恢复进入微信；仅采集器自身失败不进入微信。

页面内容更新只对新增或发生变化的正文段落运行风险分类。现有条款中的长期免责声明和“BitGo 有权暂停”等静态文字只作为基线，不得因为其他段落更新而重复告警。

## 媒体待核实线索

使用可配置的公开 RSS 搜索源，默认按 `USD1 + BitGo + redeem/redemption/bank/settlement` 等组合查询，并继续排除 Unitas USD1。

- 媒体结果存入 `announcements`，使用独立 source；
- 仪表盘增加“待核实线索”区域，显示标题、媒体、时间和原文；
- 媒体线索不创建业务风险状态，不进入微信待发送队列；
- RSS 失败只记录监控健康；
- 官方来源后续确认同一事件时，由官方赎回规则正常告警。

## 仪表盘和通知

仪表盘新增：

- Binance 已核验余额下限与全网供应占比；
- Binance CEX、Binance-Peg、候选地址、Fireblocks/BitGo、未标注巨鲸分栏；
- 24 小时 Binance 地址组净变化；
- 最大单地址 1 小时外流；
- Solana 净余额变化及“非交易对手归因”说明；
- 官方赎回通道状态；
- 媒体待核实线索；
- 每个地址的核验来源和核验日期。

微信文案使用简明中文，不使用内部 rule ID：

- 集中度说明“已核验地址至少占全网供应量 X%”；
- 资金流说明链、地址标签、金额、窗口和是否为余额差分；
- 赎回通道说明官方原文中的直接变化和来源链接；
- 监控健康、`UNKNOWN`、媒体线索不推送。

## 错误处理

- 单地址 RPC 失败保留链、地址标签、RPC 方法和异常上下文；本轮聚合结果为 `UNKNOWN`。
- Solana 返回错误 mint、错误 decimals、格式异常或重复 Token Account 时，本轮失败，不把余额当零。
- EVM 扫描游标落后时不计算流量窗口；恢复追平后只使用真实区块时间。
- Statuspage 组件缺失、页面结构变化或正文为空时记录明确的源级错误，不解释为通道正常。
- 旧的成功值可以在详情中标成“最后已知”，但不能继续支撑绿色或覆盖率数值。
- 关键采集器失败仍只进入仪表盘健康区，不发送健康微信；资产总状态可以因关键支柱不可用而显示 UNKNOWN。

## 调用量预算

按 10 分钟采集、13 个 EVM 候选在两条链读取、7 个 Solana owner 读取的保守上限估算：

- EVM 余额：约 112,320 响应/月；
- Solana owner 余额：约 30,240 响应/月；
- 少量安全头读取及余量：约 15,000 响应/月；
- 固定新增基线约 140,000–160,000 响应/月；
- EVM 精确资金流另需为命中可信地址的唯一 Transfer 区块补取一次区块头，实际增量等于每月相关唯一区块数，部署后应从 RPC 用量和事件数核对；这部分不是固定 15,000 余量。
- 固定基线按失败重试两倍预留仍低于 350,000 响应/月；区块时间补全应单独计入实测预算。

EVM 资金流复用已有 Transfer 日志，只补取相关区块头，不重复拉取日志或恢复逐区块交易扫描。普通 HTTPS 不计入 Dwellir。按 Dwellir Developer 每月 25M responses 计算，固定基线约占 0.6%，双倍预留约占 1.4%；相关区块头按实测数量另计。

## 测试与验收

### 配置与核验

- EVM/Solana 地址格式；
- 链内重复地址；
- 实体和角色枚举；
- 官方单来源或两个独立来源；
- 重复证据主机和超过 90 天的核验；
- candidate 不进入聚合。

### 采集和窗口

- EVM 安全头 `balanceOf`；
- Solana owner 的多 Token Account 汇总、错误 mint/decimals；
- 地址组内部转账抵消；
- 组外流入、流出和单地址流出；
- 真实区块时间；
- 1 小时、24 小时边界及历史不足；
- 扫描落后、部分失败和过期供应量均返回 UNKNOWN。

### 风险规则

- 集中度 50%/70% 边界；
- 5000 万/24h 和 1 亿/1h 边界；
- 连续两轮恢复；
- 流量不单独产生 RED；
- 已知 YELLOW/RED 优先于 UNKNOWN；
- 三个关键支柱任一未知时禁止资产总状态 GREEN。

### 覆盖率

- PoR 29:59 可用、30:00 起 UNKNOWN；
- 供应量过期；
- 最后已知储备值仍可查看但覆盖率隐藏；
- 新鲜数据恢复后不发送健康恢复微信。

### 赎回和媒体

- USD1 明确暂停、延迟、限额；
- BitGo 通用 Stablecoins/Settlement 故障；
- 静态条款基线不误报；
- 只分析新增或变化段落；
- 连续两次恢复；
- 媒体线索只进入仪表盘和公告存储；
- Unitas 排除；
- 采集失败不误报资产风险。

### 整体回归

- 仪表盘 API 和静态页面；
- 微信资产告警与健康静默边界；
- `check`、`run`、`status`；
- 配置示例和 Linux systemd 部署；
- 完整 pytest、Python 编译、依赖检查和前端 JavaScript 语法检查。

## 上线与兼容性

- 不迁移现有 SQLite；新观测和公告使用现有表。
- 首次成功采集立即显示集中度；若下限已越过阈值，发送一次资产告警。
- 资金流在窗口积累完成前不可用，不回填缺少真实区块时间的旧记录。
- 升级配置时必须显式加入已核验地址；示例配置不把候选地址伪装成可信默认值。
- 新功能使用 `codex/core-risk-signals` 分支，避免继续与尚未合并的 PR #12 继续混在同一分支。该分支当前建立在 PR #12 之后；创建新 PR 前应先确认 #12 已合并，或明确以 #12 为依赖基线。
