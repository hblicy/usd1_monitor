# RPC 吞吐限流设计

## 问题

权限调用扫描对每个区块执行一次 `eth_getBlockByNumber`、一次 `eth_getStorageAt` 和两次 `eth_call`。按 Alchemy 当前计费权重，每个区块消耗 92 CU；默认两条链各扫描 60 个区块，仅此阶段就会在短时间内消耗 11040 CU。现有每链 8 路并发和独立重试会超过免费账户 300 CUPS 的账户级限制，产生 HTTP 429。

## 目标与范围

- ETH、BSC 和重试请求共同遵守一个进程级 RPC 吞吐预算。
- 保留当前扫描区间、重叠区块、确认深度、风险判断和失败传播行为。
- 不改变市场、公告、储备与通知 HTTP 请求。
- 不加入供应商切换、RPC 批处理或扫描算法重构。

## 配置

在 `http` 下增加 `rpc_throughput_cups`，默认值为 `270`，必须大于零。默认值在 Alchemy 免费账户 300 CUPS 下保留 10% 余量；用户使用更高吞吐套餐时可以显式调高。

```yaml
http:
  timeout_seconds: 10
  retries: 2
  max_response_bytes: 20000000
  rpc_throughput_cups: 270
```

## 实现

`AsyncHttpClient` 持有一个共享的异步令牌桶。令牌桶容量为配置速率的 10 倍，对应 Alchemy 的 10 秒滚动窗口；初始桶为满。只有载荷符合 JSON-RPC 且 `method` 为程序使用的已知 EVM 方法时才消耗令牌，普通 HTTP 请求不受影响。

每一次实际 HTTP 尝试都在发出请求前取得令牌，因此首次请求和 HTTP 层重试都会计入预算。方法权重采用当前程序用到的 Alchemy Throughput CU：

- `eth_chainId`: 5
- `eth_blockNumber`: 10
- `eth_getLogs`: 60
- `eth_call`: 26
- `eth_getBlockByNumber`、`eth_getStorageAt`、`eth_getCode`、`eth_getTransactionReceipt`: 20

若载荷不是已知 RPC 方法，则不启用该限流器，避免误限流普通 POST 接口。ETH 与 BSC 复用同一个 `AsyncHttpClient`，因此天然共享令牌桶。

## 错误处理

现有 429/5xx 重试次数和指数退避保持不变。限流等待受上层 45 秒组件超时控制；若账户同时被其他进程消耗或配置高于实际套餐，最终错误仍按当前机制携带 `status=429` 返回，不吞掉失败。

## 测试

- 配置默认值、正数校验和示例配置解析。
- 方法 CU 权重映射。
- 令牌不足时按缺口等待，令牌补充不超过 10 秒容量。
- 两个并发请求共享同一令牌桶。
- HTTP 429 重试前再次取得令牌。
- 普通 JSON POST 不消耗 RPC 令牌。
- 运行完整离线测试集。
