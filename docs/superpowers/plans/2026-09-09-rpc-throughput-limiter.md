# RPC Throughput Limiter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Ethereum and BSC JSON-RPC requests from exceeding the configured shared CUPS budget while preserving the existing scan behavior.

**Architecture:** Add a configurable CUPS value to `HttpConfig`. The single `AsyncHttpClient` shared by both chains owns one asynchronous 10-second token bucket and charges every actual HTTP attempt according to the JSON-RPC method; ordinary HTTP requests bypass the limiter.

**Tech Stack:** Python 3.12, asyncio, Pydantic, pytest

---

### Task 1: Add the RPC throughput configuration

**Files:**
- Modify: `tests/test_config.py`
- Modify: `usd1_monitor/config.py:33-36`
- Modify: `config.example.yaml:23-26`
- Modify: `deploy/config.production.example.yaml:18-21`

- [x] **Step 1: Write failing configuration tests**

Add the default assertion to `test_load_config_reads_market_defaults`:

```python
assert config.http.rpc_throughput_cups == 270
```

Add this validation test:

```python
def test_load_config_rejects_non_positive_rpc_throughput(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: monitor.db\nhttp:\n  rpc_throughput_cups: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="greater than 0"):
        load_config(path, environ={})
```

- [x] **Step 2: Run the tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py::test_load_config_reads_market_defaults tests/test_config.py::test_load_config_rejects_non_positive_rpc_throughput -q`

Expected: the default assertion fails because the field is absent.

- [x] **Step 3: Add the field and example values**

Add to `HttpConfig`:

```python
rpc_throughput_cups: float = Field(default=270, gt=0)
```

Add under `http` in both example YAML files:

```yaml
  rpc_throughput_cups: 270
```

- [x] **Step 4: Run configuration and example tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_examples.py -q`

Expected: all selected tests PASS.

### Task 2: Implement the shared weighted token bucket

**Files:**
- Modify: `tests/test_http.py`
- Modify: `usd1_monitor/http.py:3-114`

- [x] **Step 1: Write failing method-cost and token-bucket tests**

Import `asyncio`, `_RpcThroughputLimiter`, and `_rpc_throughput_cost`, then add:

```python
@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("eth_chainId", 5),
        ("eth_blockNumber", 10),
        ("eth_getLogs", 60),
        ("eth_call", 26),
        ("eth_getBlockByNumber", 20),
        ("eth_getStorageAt", 20),
        ("eth_getCode", 20),
        ("eth_getTransactionReceipt", 20),
    ],
)
def test_rpc_throughput_cost_uses_known_method_weights(
    method: str, expected: int
) -> None:
    assert _rpc_throughput_cost(
        {"jsonrpc": "2.0", "method": method}
    ) == expected


def test_rpc_throughput_cost_ignores_non_rpc_payload() -> None:
    assert _rpc_throughput_cost({"method": "eth_call"}) == 0


@pytest.mark.asyncio
async def test_rpc_throughput_limiter_waits_for_shared_token_deficit() -> None:
    now = 0.0
    waits: list[float] = []

    def clock() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    limiter = _RpcThroughputLimiter(
        10, window_seconds=10, clock=clock, sleep=sleep
    )
    await asyncio.gather(limiter.acquire(80), limiter.acquire(80))

    assert waits == pytest.approx([6.0])


@pytest.mark.asyncio
async def test_rpc_throughput_limiter_caps_refill_at_window_capacity() -> None:
    now = 0.0
    waits: list[float] = []

    def clock() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    limiter = _RpcThroughputLimiter(
        10, window_seconds=10, clock=clock, sleep=sleep
    )
    await limiter.acquire(100)
    now = 100.0
    await limiter.acquire(100)
    await limiter.acquire(1)

    assert waits == pytest.approx([0.1])
```

- [x] **Step 2: Run the new tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_http.py -q`

Expected: collection fails because the limiter and cost function do not exist.

- [x] **Step 3: Implement the weights and token bucket**

Add imports:

```python
import time
from collections.abc import Awaitable, Callable
```

Add the known weights, cost function, and limiter before `AsyncHttpClient`:

```python
_RPC_THROUGHPUT_CUPS = {
    "eth_chainId": 5,
    "eth_blockNumber": 10,
    "eth_getLogs": 60,
    "eth_call": 26,
    "eth_getBlockByNumber": 20,
    "eth_getStorageAt": 20,
    "eth_getCode": 20,
    "eth_getTransactionReceipt": 20,
}


def _rpc_throughput_cost(payload: dict[str, Any]) -> int:
    if payload.get("jsonrpc") != "2.0":
        return 0
    method = payload.get("method")
    return _RPC_THROUGHPUT_CUPS.get(method, 0) if isinstance(method, str) else 0


class _RpcThroughputLimiter:
    def __init__(
        self,
        cups: float,
        *,
        window_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate = float(cups)
        self._capacity = max(
            self._rate * window_seconds,
            float(max(_RPC_THROUGHPUT_CUPS.values())),
        )
        self._tokens = self._capacity
        self._updated_at = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def acquire(self, cost: int) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._rate,
                )
                self._updated_at = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                delay = (cost - self._tokens) / self._rate
            await self._sleep(delay)
```

Initialize one limiter in `AsyncHttpClient.__init__`:

```python
self._rpc_throughput_limiter = _RpcThroughputLimiter(
    config.rpc_throughput_cups
)
```

- [x] **Step 4: Run HTTP tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_http.py -q`

Expected: all HTTP tests PASS.

### Task 3: Charge each JSON-RPC HTTP attempt

**Files:**
- Modify: `tests/test_http.py`
- Modify: `usd1_monitor/http.py:113-207`

- [x] **Step 1: Write failing retry and bypass tests**

Add:

```python
@pytest.mark.asyncio
async def test_rpc_retries_each_acquire_throughput_tokens(monkeypatch) -> None:
    class Content:
        async def iter_chunked(self, size):
            yield b"{}"

    class ErrorResponse:
        status = 429
        content_length = None
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        closed = False

        def request(self, method, url, **kwargs):
            return ErrorResponse()

    class RecordingLimiter:
        def __init__(self) -> None:
            self.costs: list[int] = []

        async def acquire(self, cost: int) -> None:
            self.costs.append(cost)

    async def no_sleep(delay: float) -> None:
        return None

    client = AsyncHttpClient(HttpConfig(timeout_seconds=1, retries=2))
    client._session = Session()
    limiter = RecordingLimiter()
    client._rpc_throughput_limiter = limiter
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    with pytest.raises(HttpRequestError):
        await client.post_json(
            "https://rpc.example/v2/secret",
            {"jsonrpc": "2.0", "method": "eth_call", "params": []},
        )

    assert limiter.costs == [26, 26, 26]


@pytest.mark.asyncio
async def test_non_rpc_json_post_bypasses_throughput_limiter() -> None:
    class Content:
        async def iter_chunked(self, size):
            yield b"{}"

    class SuccessResponse:
        status = 200
        content_length = None
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        closed = False

        def request(self, method, url, **kwargs):
            return SuccessResponse()

    class RecordingLimiter:
        def __init__(self) -> None:
            self.costs: list[int] = []

        async def acquire(self, cost: int) -> None:
            self.costs.append(cost)

    client = AsyncHttpClient(HttpConfig(timeout_seconds=1, retries=0))
    client._session = Session()
    limiter = RecordingLimiter()
    client._rpc_throughput_limiter = limiter

    assert await client.post_json(
        "https://notify.example/hook",
        {"method": "eth_call", "message": "notification"},
    ) == {}
    assert limiter.costs == []
```

- [x] **Step 2: Run the two new tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_http.py -q`

Expected: the recorder receives no costs because `_request_json` does not yet acquire tokens.

- [x] **Step 3: Pass the RPC cost into the request loop**

Change `post_json` and `_request_json` as follows:

```python
async def post_json(self, url: str, payload: dict[str, Any]) -> object:
    return await self._request_json(
        "POST",
        url,
        rpc_cost=_rpc_throughput_cost(payload),
        json=payload,
    )

async def _request_json(
    self,
    method: str,
    url: str,
    *,
    rpc_cost: int = 0,
    **kwargs: object,
) -> object:
    await self.open()
    assert self._session is not None
    last_error: Exception | None = None
    attempts = self._retries + 1
    for attempt in range(1, attempts + 1):
        if rpc_cost:
            await self._rpc_throughput_limiter.acquire(rpc_cost)
        try:
            async with self._session.request(
                method, url, allow_redirects=False, **kwargs
            ) as response:
                raw_body = await self._read_limited(response, url)
                if response.status >= 300:
                    try:
                        payload = json.loads(raw_body)
                    except (UnicodeDecodeError, ValueError):
                        payload = None
                    error_code = (
                        payload.get("code")
                        if isinstance(payload, dict)
                        and isinstance(payload.get("code"), int)
                        else None
                    )
                    raise HttpResponseError(
                        method, url, response.status, error_code
                    )
                return json.loads(raw_body)
        except HttpResponseError as exc:
            if exc.status < 500 and exc.status != 429:
                raise
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(2 ** (attempt - 1))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise HttpRequestError(method, url, attempts, last_error) from None
```

- [x] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_http.py tests/test_rpc.py tests/test_config.py tests/test_examples.py -q`

Expected: all selected tests PASS.

- [x] **Step 5: Run the full offline suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q -m "not live"`

Expected: all offline tests PASS.

- [x] **Step 6: Commit and update the existing PR**

```bash
git add tests/test_http.py tests/test_config.py usd1_monitor/http.py usd1_monitor/config.py config.example.yaml deploy/config.production.example.yaml docs/superpowers/plans/2026-09-09-rpc-throughput-limiter.md
git commit -m "修复 RPC 并发触发限流"
git push
```
