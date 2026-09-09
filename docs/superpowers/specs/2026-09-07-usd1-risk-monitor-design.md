# USD1 公开数据风险监控器设计

日期：2026-09-07
状态：用户已确认
目标目录：`07-web3-bot/DEFI/usd1_monitor`

## 1. 目标

开发一个在 Linux 服务器常驻运行的 USD1 风险监控器。首版只读取公开数据，不使用交易所账户 API Key，不下单、不兑换、不执行任何资产操作。系统持续采集 Binance 盘口、Ethereum/BNB Chain 链上状态、USD1 储备预言机、全链供应量辅助数据和官方公告，在风险状态发生变化时通过企业微信通知。

系统必须同时监控自身盲区。数据源不可用不得被解释为“没有风险”；关键来源持续不可用时必须单独告警。

## 2. 首版范围

### 2.1 包含

- Binance `USD1USDT`、`USD1USDC` 公开交易状态和订单簿。
- Ethereum、BNB Chain 上的 USD1 token、proxy、admin、owner、暂停、冻结、mint/burn 和实现代码变化。
- Ethereum 上的 USD1 PoR Oracle 储备值及更新时间。
- Ethereum、BNB Chain 的 `totalSupply`。
- DefiLlama 的 World Liberty Financial USD 全链供应量，作为辅助估算值。
- Binance、BitGo、World Liberty Financial、OCC 官方页面的 USD1 相关新增或修改内容。
- SQLite 历史数据、持久化风险状态、企业微信告警和恢复通知。
- CLI、systemd 服务、日志轮转、示例配置和部署文档。

### 2.2 不包含

- 真实下单、兑换、充值或提现测试。
- Binance 账户、保证金、Earn、个人限额或私有充提状态。
- TRON、Solana、Aptos、Tempo 及桥接链的独立读取和 CCIP 完整对账。
- 自动识别全部交易所热钱包，或自动计算 Binance 持仓集中度。
- 特朗普家族、WLFI 成员及其他人物的社交媒体或非官方新闻。
- DeFi 借贷、抵押和清算传导。
- Web 仪表盘和在线修改配置。
- 自动给出交易指令或执行减仓。

未包含的项目必须在状态输出中标为 `NOT_MONITORED`，不得显示为正常。

## 3. 事实基线与动态配置

- USD1 在 Ethereum 与 BNB Chain 的 token 地址默认为 `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d`。
- PoR Oracle 地址默认为 `0x691b74146cdba162449012aa32d3cbf5df77d4c4`。
- 地址来自 World Liberty Financial 的公开 PoR 实现，但仍放入 YAML 配置，方便官方迁移后更新。
- DefiLlama 资产不得只按符号 `USD1` 匹配。启动时应同时校验符号和名称 `World Liberty Financial USD`，排除 Unitas 的同名资产；匹配不唯一时停止该采集器并产生配置/数据源告警。
- 鉴证月份、活动截止日、托管人、审计机构和监管状态都属于动态事实，不得写死为长期规则。

官方参考：

- <https://github.com/worldliberty/cre-por-dashboard>
- <https://github.com/worldliberty/usd1-smart-contracts>
- <https://docs.worldlibertyfinancial.com/usd1-token/proof-of-reserves>
- <https://www.bitgo.com/usd1/attestations/>
- <https://www.binance.com/en/support/announcement/>
- <https://www.occ.gov/topics/charters-and-licensing/interpretations-and-decisions/index-interpretations-and-decisions.html>

## 4. 架构

采用 Python 3.11+ 单进程 `asyncio` 架构。各采集器独立调度，共享标准化观测模型、SQLite 存储、风险引擎和通知器。

```text
公开数据源
  ├─ Binance 市场数据
  ├─ Ethereum / BNB Chain RPC
  ├─ PoR Oracle
  ├─ DefiLlama
  └─ 官方公告页 / 鉴证 PDF
          ↓
独立 Collector → 标准化 Observation / Event
          ↓
        SQLite
          ↓
规则状态机 → 聚合器 → 企业微信
```

建议模块：

```text
usd1_monitor/
  __main__.py
  cli.py
  config.py
  models.py
  storage.py
  scheduler.py
  collectors/
    market.py
    evm.py
    reserves.py
    supply.py
    announcements.py
  engine/
    rules.py
    state.py
    aggregate.py
  notifications/
    wechat.py
  tests/
  deploy/usd1-monitor.service
```

Collector 只负责取数、校验和标准化；规则引擎不发网络请求；通知器不重新计算风险。模块间通过明确的数据对象交互，便于用固定样本测试。

## 5. 配置

### 5.1 `.env`

只保存敏感或部署相关值：

- `WECHAT_WEBHOOK`
- `ETH_RPC_URLS`：可选，逗号分隔的自有/备用 RPC。
- `BSC_RPC_URLS`：可选，逗号分隔的自有/备用 RPC。

未提供 RPC 时使用配置中的公开 RPC 列表。Webhook 未配置时允许 `check` 和 `status` 运行；`run` 启动时必须明确记录“通知被禁用”，不得假装已具备告警能力。

### 5.2 `config.yaml`

保存：

- token、PoR Oracle、proxy/admin 相关地址。
- RPC 公共备用列表和确认深度。
- 采集周期、HTTP 超时、重试次数。
- 所有红黄绿阈值及恢复持续时间。
- `watched_addresses`，包含自定义标签和链。
- 公告来源、关键词、排除词和鉴证宽限期。
- SQLite 保留天数和日志设置。

启动时严格校验 URL、链名、地址格式、正数周期、阈值顺序和必需字段。配置无效时进程非零退出并记录具体字段路径。

## 6. 数据采集

### 6.1 Binance 市场

每 60 秒通过官方公开 Spot REST API 获取：

- `exchangeInfo`：确认两个 symbol 是否存在且为 `TRADING`。
- 深度 `limit=1000`：计算 best bid/ask、中间价、价差、10bps/30bps bid 深度。
- 模拟卖出 100 万、500 万、2000 万 USD1，记录可成交数量、VWAP 和末档价格。

卖出模拟只遍历 bids，不产生订单。若前 1000 档不足以填满目标量，保存 `fully_fillable=false`，不得把截断数据当完整深度。

### 6.2 Ethereum 与 BNB Chain

每个采集周期读取安全头部，并从持久化 cursor 扫描至 `latest - confirmation_depth`。默认确认深度为 Ethereum 3、BNB Chain 10，可配置。每次启动和 RPC 切换时向前重叠扫描 20 个区块；事件唯一键为 `chain + tx_hash + log_index`。

采集内容：

- 标准 `Transfer`；以零地址识别 mint/burn，不只依赖自定义事件。
- `Mint`、`Burn`、`Freeze`、`Unfreeze`、`Paused`、`Unpaused`、`OwnershipTransferred` 及可验证链上 ABI 中的等价事件。
- `totalSupply()`、`owner()`、`paused()`（存在时）。
- EIP-1967 implementation/admin 存储槽。
- implementation 地址的 runtime bytecode hash。
- 发往 token proxy、proxy admin 的交易；记录管理员/owner 发出的已知和未知方法 selector。

如果管理员调用未知 selector 且同一交易产生非普通管理员余额转移，系统保存原始 input、日志摘要和链接并升级风险，避免依赖公开源码未包含的 `drain/reallocate` 名称。

### 6.3 储备

每 5 分钟读取 Ethereum PoR Oracle：

- `latestBundle()`
- `latestBundleTimestamp()`
- `bundleDecimals()`

bundle 按官方定义解码为时间戳和储备值。链上 timestamp 与 bundle 内 timestamp 必须一致；不一致视为数据校验失败，不进入正常指标。

### 6.4 供应量

- Ethereum 与 BNB Chain `totalSupply` 每小时读取。
- DefiLlama 全链供应量每小时读取并验证资产身份。
- `estimated_collateralization = oracle_reserves / defillama_global_supply`。

此覆盖率必须始终标记为 `ESTIMATED`。首版不读取全部链，因此不得用 ETH+BSC 供应量计算或展示“真实全链覆盖率”。

### 6.5 官方信息

采集频率：Binance 每 15 分钟；BitGo/WLFI/OCC 每 6 小时。

- 只允许预配置官方域名。
- 保存稳定条目标识、标题、URL、发布时间、正文 hash 和首次发现时间。
- 新条目或正文 hash 改变才进入通知流程。
- 关键词同时要求 USD1/WLFI/World Liberty/BitGo 实体命中，排除 Unitas。
- 页面结构无法解析不得等同“没有新公告”；连续失败进入数据源健康规则。
- 新鉴证 PDF 保存 URL、月份、文件 hash，并提取流通代币、赎回资产总额、资产类别、托管人和审计机构。关键字段提取失败时告警并保留 PDF 链接供人工查看。
- 报告月份 `M` 的逾期线为 `M` 月末后 45 天。该规则等价于给下月正常出报窗口再留约 14 天，不依赖某个历史月份的发布日期。

## 7. 风险规则

每条规则独立维护 `GREEN/YELLOW/RED/UNKNOWN/NOT_MONITORED`。总体状态取已监控规则中的最高等级；`UNKNOWN` 通过“监控盲区”单独展示和通知，不被绿色覆盖。

### 7.1 市场价格

- 黄灯：任一主交易对中间价 `< 0.997` 连续 15 分钟。
- 红灯：任一主交易对中间价 `< 0.995` 连续 5 分钟。
- 红灯保持：价格 `< 0.99` 连续 1 小时；该条件用于告警正文强化，不降低已有红灯。
- 恢复：两个交易对均 `>= 0.998` 连续 15 分钟。

持续时间按有效观测计算。采集中断期间不累计“价格持续低于阈值”的时间。

### 7.2 盘口退出容量

- 红灯：100 万 USD1 在前 1000 档无法完全成交，连续 2 次观测成立。
- 红灯：100 万 USD1 模拟卖出的末档价格 `< 0.995`，连续 2 次观测成立。
- 恢复：可完全成交且末档价格 `>= 0.997` 连续 15 分钟。
- 500 万和 2000 万结果只记录并附在市场告警中，首版不单独改变总体状态。

### 7.3 交易对状态

- 红灯：任一主交易对不存在或状态不是 `TRADING`，连续 2 次有效查询成立。
- 恢复：两个交易对均恢复 `TRADING`，连续 2 次查询成立。

### 7.4 合约权限

- 红灯：proxy implementation/admin 改变、implementation code hash 改变、合约进入 pause。
- 红灯：`watched_addresses` 中的地址被冻结。
- 红灯：管理员未知调用伴随从非管理员地址转出的 token `Transfer`。
- 黄灯：owner 变化、普通地址被冻结、管理员未知调用但未观察到资产移动。
- 恢复：只对 pause/unpause 和 freeze/unfreeze 这类可逆状态发送恢复；升级、owner 变化和历史资产移动属于事实事件，不自动恢复。

### 7.5 mint/burn 与供应量

- 黄灯：单笔 mint 或 burn `>= 10,000,000 USD1`。
- 黄灯：ETH+BSC 合计供应量 24 小时净下降 `>= 2%`。
- 组合红灯：24 小时净下降 `>= 2%` 且市场价格规则已为黄灯或红灯。
- 事件型黄灯在发送后保留历史，但不无限维持总体黄灯；聚合窗口结束且无其他风险时恢复。

### 7.6 PoR 与估算覆盖率

- 黄灯：Oracle 距当前时间超过 1 小时未更新。
- 红灯：Oracle 超过 2 小时未更新。
- 黄灯：储备值单次相对变化超过 0.5%。
- 黄灯：估算覆盖率 `< 100%` 连续 2 个小时点，或单次变化超过 0.5 个百分点。
- 组合红灯：估算覆盖率 `< 99%` 连续 2 个小时点，并且市场价格规则已为黄灯或红灯。
- 恢复：Oracle 恢复更新且连续 2 次读取有效；估算覆盖率 `>= 100%` 连续 2 个小时点。

DefiLlama 是辅助数据源，估算覆盖率不足 100% 不单独触发红灯。

### 7.7 鉴证和官方公告

- 黄灯：应出月份超过月末 45 天仍未发布。
- 黄灯：新报告的托管人、审计机构或资产类别发生变化，或关键字段无法解析。
- 黄灯：官方发布与 USD1 下架、限制、暂停、储备、托管、牌照、调查相关的新公告。
- 公告文本不单独触发红灯。交易对实际停止、PoR 异常或链上事实由对应 A 层规则触发红灯。

### 7.8 监控盲区

- 单次失败：只记带上下文日志。
- 连续失败 3 次：黄色数据源告警。
- Binance 盘口或 PoR 连续 15 分钟没有成功观测：红色关键监控盲区。
- RPC、DefiLlama、公告源恢复：发送恢复通知。

业务风险和监控健康状态分开存储、分开展示，避免把网络故障解释成脱锚。

## 8. 状态机与通知

状态持久化规则：

- `GREEN → YELLOW/RED`：发送一次。
- `YELLOW → RED`：立即发送升级通知。
- 同等级持续：不重复发送。
- `RED → YELLOW` 或风险解除：满足规则恢复窗口后发送降级/恢复通知。
- 进程重启后从 SQLite 恢复状态和首次触发时间，不重复发送旧告警。
- 不可逆事实事件按唯一事件键只通知一次。

同一采集周期内由同一根因引起的多个规则合并成一条消息，内容包括：总体等级、触发规则、当前值、阈值、首次触发时间、数据时间、来源链接和监控盲区。不同根因不强行合并。

企业微信返回 HTTP 200 仍需检查业务 `errcode`。发送失败记录响应上下文，并有限重试；通知失败不得修改为“已成功发送”。

## 9. 存储

SQLite 至少包含：

- `observations`：指标、来源、链/市场、数值、单位、数据时间、采集时间、质量标签。
- `chain_events`：链、区块、交易 hash、log index、事件、地址、解析字段、原始摘要。
- `risk_states`：规则、当前等级、首次触发、最近变化；计时窗口与连续读数由持久化 `observations` 历史重建。
- `alert_deliveries`：告警键、payload hash、发送时间、结果和错误摘要。
- `announcements`：来源、稳定 ID、标题、URL、发布时间、正文 hash、首次发现。
- `collector_health`：连续失败数、最近成功、最近错误和恢复时间。
- `scan_cursors`：每条链最近安全扫描区块。

市场和普通观测默认保留 180 天；链上事件、公告、风险状态历史和告警发送记录长期保留。时间统一以 UTC 存储，CLI、日志和企业微信默认显示 `Asia/Shanghai`。

SQLite 使用事务写入观测与相应状态变化，避免进程在中途退出时出现“已推进 cursor 但事件未保存”。

## 10. 错误处理与日志

- 所有请求设置连接/读取超时、有限次数重试和指数退避。
- RPC 按顺序故障切换，并记录实际使用的端点；日志不得输出 URL 中可能存在的 token。
- 预期的 HTTP、RPC、解析和配置错误使用可识别错误类型。
- 非预期异常保留调用链，由 scheduler 标记对应 collector 失败；不得用宽泛异常吞掉整个循环。
- 关键日志包含 collector、source、chain/symbol、block/timestamp、attempt 和 error category。
- 使用按大小轮转日志；systemd 捕获标准输出/错误作为第二条排障路径。

## 11. CLI 与部署

- `python -m usd1_monitor check`：执行一次公开数据检查，输出每个 collector 的成功/失败和数据时间，不发送告警。
- `python -m usd1_monitor run`：启动常驻监控。
- `python -m usd1_monitor status`：从 SQLite 输出总体状态、每条规则、最后成功采集和未监控范围。

提供 systemd unit：异常退出自动重启，使用专用工作目录和环境文件。正常启动发送一次简短通知，包含版本、启用 collector 和明确的 `NOT_MONITORED` 项；后续只在风险变化、恢复或监控盲区时通知。

## 12. 测试策略

### 12.1 单元测试

- 每个阈值的边界值、持续时间、升级、降级和恢复。
- 盘口深度、VWAP、末档价格、未完全成交。
- PoR bundle 解码、decimals、时间戳不一致、心跳过期。
- 零地址 mint/burn、冻结、暂停、owner/proxy/code hash 变化。
- DefiLlama 同名资产排除。
- 公告新增、正文修改、无关关键词、Unitas 排除。
- 鉴证到期日和 PDF 字段解析。
- 告警去重、合并、发送失败和重启恢复。

### 12.2 集成测试

- 使用固定 HTTP/RPC 样本贯穿 collector → SQLite → rule → notification fake。
- SQLite 事务、cursor 回放、20 区块重叠扫描和事件幂等。
- RPC/HTTP 超时、重试、主备切换、连续失败和恢复。
- 实时冒烟测试只读取公开端点，默认不运行且绝不发送真实 webhook。

测试不得依赖实时价格来断言固定数值。所有风险规则使用确定性样本。

## 13. 对原《方案.md》的修改建议

1. 把“2026-07 及以后未发布”改为写作时快照，并注明当前 BitGo 页面已显示 2026-07 发布；程序按动态到期规则判断。
2. 将“当前约 11+ 条链”改为引用官方 PoR 的动态链配置，不在长期方案正文固定数量。
3. 区分“Oracle 储备值”“官方全链覆盖率”“首版 DefiLlama 估算覆盖率”，禁止混用。
4. 把 `drain/reallocate` 从确定事件名改为“已知 selector + 未知管理员调用 + 实现变化 + 资产移动”的通用检测，除非实施时从已验证链上 ABI 确认具体事件。
5. 将“币安充提状态”“集中度”“真实兑换测试”标成需要私有 API、可靠地址标签或资产操作的第二阶段能力，首版不得声称已覆盖。
6. 把“政治新闻可升级执行”保留为人工风险框架，不进入首版自动状态机。
7. 所有活动截止日期保留为动态公告事实，不作为静态程序常量。
8. 增加监控系统自身健康、数据时效、解析失败、RPC 分叉/重组和通知失败规则。
9. 增加 `NOT_MONITORED` 状态，使未覆盖风险与绿灯明确区分。

## 14. 验收标准

- Linux 上可通过三个 CLI 命令完成单次检查、常驻运行和状态查询。
- 无交易所 API Key、无钱包私钥、无交易权限即可运行全部首版 collector。
- 固定样本能够稳定复现价格、盘口、PoR、链上权限、供应和公告规则。
- 所有状态转换、告警去重、恢复通知和重启恢复均有自动化测试。
- 任一 collector 故障不会阻断其他 collector，且按规则形成监控盲区告警。
- 配置或关键数据解析错误可被调用方和运维明确感知，不静默降级。
- 输出明确区分事实值、估算值、未知值和未监控项。
- 不执行任何资产操作，不需要 Binance 私有 API Key。
