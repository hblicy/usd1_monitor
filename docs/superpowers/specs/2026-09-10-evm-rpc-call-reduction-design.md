# EVM 权限监控降频设计

## 目标

将 Ethereum 与 BNB Chain 的链上监控从逐区块完整交易分析，调整为每 10 分钟一次的合约权限、关键运行状态和关键事件检查。保留对 USD1 合约控制权及发行风险最直接的信号，同时把本项目的链上 RPC 调用量控制在约 10 万至 15 万次/月，远低于 500 万次/月硬上限。

## 保留的监控

每条链每 10 分钟读取一次确认区块上的当前状态：

- EIP-1967 implementation 地址；
- implementation 代码哈希；
- EIP-1967 admin 地址；
- token owner 地址；
- ProxyAdmin owner 地址（admin 合约实现 `owner()` 时）；
- paused 状态；
- 配置了 `watched_addresses` 时，对这些地址读取 frozen 状态。

同时按游标连续读取 USD1 合约日志，保留：

- implementation、admin、owner 变化；
- pause、unpause；
- freeze、unfreeze；
- mint、burn，其中单次达到现有阈值的大额铸造或销毁继续告警。

日志检查保留重叠区块对账，从而纠正确认窗口附近的链重组事件。

风险等级沿用现有规则：implementation、代码哈希或 admin 地址变化为 RED，token owner 变化为 YELLOW，paused 为 RED，普通地址冻结为 YELLOW、受监控地址冻结为 RED，大额 mint/burn 为 YELLOW。新增的 ProxyAdmin owner 变化按 RED 处理，因为该身份可控制代理升级。

## 停止的监控

运行时不再启用 `PrivilegedCallCollector`，因此停止：

- 下载每个区块的完整交易列表；
- 对每个区块读取历史 admin、token owner 和 admin owner；
- 逐笔识别已知高权限函数调用；
- 识别没有对应状态变化或已知事件的未知高权限调用；
- 读取候选交易的交易回执和 Safe 内层调用。

这意味着无事件、未留下最终状态变化，并且在两个 10 分钟检查点之间发生后又恢复的临时权限操作可能不会被发现。该边界是满足成本约束的明确取舍，不得在实现或文案中表述为逐笔完整链上审计。

## 扫描策略

1. 两条链的 `interval_seconds` 统一调整为 600。
2. 每轮先读取 `eth_blockNumber` 并计算确认后的 safe head。
3. 日志扫描从持久化游标减去重叠区块开始，到 safe head 为止。
4. 单轮最多推进 2,000 个区块，确保 BSC 在约 0.45 秒出块时仍能追上每 10 分钟约 1,333 个新区块。
5. `eth_getLogs` 每次最多查询 500 个区块，避免单次响应过大；一轮正常 BSC 约 3 次日志请求，Ethereum 约 1 次。日志解码覆盖标准 `Upgraded` 和 `AdminChanged` 事件。
6. 日志完成后在本轮 processed head 读取权限与运行状态快照，包括 token owner 和 ProxyAdmin owner。
7. 原子完成事件对账、观察值写入、风险状态更新和游标推进。任一 RPC 或解析失败时不推进游标。

## 重组与错误处理

- 保留确认深度和 20 个重叠区块，不使用逐块完整交易来检测重组。
- 对重叠范围重新获取 canonical 日志；数据库中已不存在的旧日志事件由现有事件对账删除并触发对应恢复处理。
- 当前状态始终从本轮确认区块重新读取，避免沿用内存推导状态。
- RPC 限流、超时、不支持历史读取或响应格式异常继续使本轮失败，并记录健康状态与完整错误上下文。
- 不添加静默降级；日志或状态检查失败时不得把链标记为采集成功。

## 调用量预算

正常每 10 分钟一轮时：

- BNB Chain：约 10 次 RPC/轮，约 43,200 次/月；
- Ethereum：约 8 次 RPC/轮，约 34,560 次/月；
- 现有 PoR：约 34,560 次/月；
- 两条链供应量：约 5,760 次/月。

合计约 118,080 次/月。即使按所有请求均发生两次重试的极端上界估算，也低于 36 万次/月。每增加一个受监控冻结地址，约增加 4,320 次/月。该预算不包含非 RPC 的 Binance 市场和官方网页请求。

## 配置和兼容性

更新默认示例配置：

- Ethereum 和 BNB Chain 的 `interval_seconds` 设为 600；
- `scan_batch_blocks` 设为 2,000；
- 日志查询块跨度设为 500，并由链配置显式承载。

现有数据库表和数据继续使用，不执行迁移。旧游标可继续推进。环境变量中的 RPC URL 格式不变。

启动通知中的“Ethereum 链上合约、BNB Chain 链上合约”改为“Ethereum 合约权限、BNB Chain 合约权限”，避免让用户误以为仍在逐笔分析全部链上交易。

## 文件范围

- `usd1_monitor/config.py`：允许新的扫描批量上限并增加日志块跨度配置。
- `config.example.yaml`：写入 10 分钟周期、2,000 区块批量和 500 区块日志跨度。
- `deploy/config.production.example.yaml`：同步生产部署示例中的周期和扫描参数。
- `usd1_monitor/collectors/evm.py`：使用链配置的日志跨度；保留日志扫描与状态快照，运行时不再依赖逐区块特权调用扫描。
- `usd1_monitor/evm_abi.py`：解析标准 `Upgraded` 和 `AdminChanged` 事件。
- `usd1_monitor/scheduler.py`：持久化、比较并评估 ProxyAdmin owner 快照。
- `usd1_monitor/cli.py`：不再向生产 monitor 注入 `PrivilegedCallCollector`。
- `usd1_monitor/notifications/wechat.py`：只调整启动覆盖名称。
- `README.md`：说明已有部署需要手动更新周期和扫描参数。
- `tests/test_config.py`、`tests/test_evm_abi.py`、`tests/test_evm_scanner.py`、`tests/test_evm_snapshot.py`、`tests/test_evm_integration.py`、`tests/test_cli.py`、`tests/test_wechat.py`：覆盖配置、调用数量、事件与状态行为及启动文案。

不修改数据库结构、价格规则、PoR、供应量、公告采集和通知发送机制。不在本次修改中增加其他链或网页仪表盘。

## 测试与验收

测试必须先失败再实现，并覆盖：

1. BSC 每 10 分钟约 1,333 个新区块时，单轮游标能够前进到 safe head，而不是持续落后。
2. 2,000 个区块的扫描按 500 个区块拆成 4 次 `eth_getLogs`。
3. 生产 builder 不创建或注入 `PrivilegedCallCollector`，因此不调用完整区块、逐块历史权限和 Trace/Debug 方法。
4. implementation、代码哈希、admin、owner、paused 和 watched frozen 状态变化继续产生原有风险级别。
5. ProxyAdmin owner 首次读取建立基线，后续变化触发 RED；无法读取 owner 的非 Ownable admin 保持 `None`，不产生伪告警。
6. 权限、暂停、冻结、mint 和 burn 日志继续被识别；大额 mint/burn 阈值保持不变。
7. 重叠日志对账继续移除重组后的孤儿事件。
8. RPC 失败不推进游标，并通过现有健康检查暴露。
9. 启动通知明确显示“合约权限”。
10. 完整测试套件通过，且测试中的 RPC 方法计数符合月预算模型。
