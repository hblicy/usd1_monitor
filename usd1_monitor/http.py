from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from usd1_monitor.config import HttpConfig


class HttpRequestError(RuntimeError):
    def __init__(self, method: str, url: str, attempts: int, cause: Exception) -> None:
        self.method = method
        self.url = sanitize_url(url)
        self.attempts = attempts
        cause_summary = type(cause).__name__
        if isinstance(cause, HttpResponseError):
            cause_summary += f" status={cause.status}"
            if cause.error_code is not None:
                cause_summary += f" code={cause.error_code}"
        self.cause = RuntimeError(cause_summary)
        super().__init__(
            f"{method} {self.url} failed after {attempts} attempt(s): "
            f"{cause_summary}"
        )


class HttpResponseError(RuntimeError):
    def __init__(
        self,
        method: str,
        url: str,
        status: int,
        error_code: int | None = None,
    ) -> None:
        self.method = method
        self.url = sanitize_url(url)
        self.status = status
        self.error_code = error_code
        detail = f" status={status}"
        if error_code is not None:
            detail += f" code={error_code}"
        super().__init__(f"{method} {self.url} failed:{detail}")


class HttpBodyTooLargeError(RuntimeError):
    def __init__(self, url: str, maximum: int) -> None:
        self.url = sanitize_url(url)
        self.maximum = maximum
        super().__init__(
            f"response from {self.url} exceeds {maximum} bytes"
        )


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if parts.port is not None:
        hostname = f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme, hostname, "", "", ""))


def _same_origin(left: str, right: str) -> bool:
    try:
        left_parts = urlsplit(left)
        right_parts = urlsplit(right)
        left_port = left_parts.port or (
            443 if left_parts.scheme.casefold() == "https" else 80
        )
        right_port = right_parts.port or (
            443 if right_parts.scheme.casefold() == "https" else 80
        )
    except ValueError:
        return False
    return (
        left_parts.scheme.casefold() == right_parts.scheme.casefold()
        and left_parts.hostname == right_parts.hostname
        and left_port == right_port
        and right_parts.username is None
        and right_parts.password is None
    )


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
                if self._tokens >= cost - 1e-9:
                    self._tokens = max(0.0, self._tokens - cost)
                    return
                delay = (cost - self._tokens) / self._rate
            await self._sleep(delay)


class AsyncHttpClient:
    def __init__(self, config: HttpConfig) -> None:
        self._timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)
        self._retries = config.retries
        self._max_response_bytes = config.max_response_bytes
        self._rpc_throughput_limiter = _RpcThroughputLimiter(
            config.rpc_throughput_cups
        )
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AsyncHttpClient":
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def get_json(
        self, url: str, params: dict[str, object] | None = None
    ) -> object:
        return await self._request_json("GET", url, params=params)

    async def post_json(self, url: str, payload: dict[str, Any]) -> object:
        return await self._request_json(
            "POST",
            url,
            rpc_cost=_rpc_throughput_cost(payload),
            json=payload,
        )

    async def get_text(self, url: str) -> str:
        body = await self._request_bytes(url)
        return body.decode("utf-8", errors="replace")

    async def get_bytes(self, url: str) -> bytes:
        return await self._request_bytes(url)

    async def _request_bytes(self, url: str) -> bytes:
        await self.open()
        assert self._session is not None
        last_error: Exception | None = None
        attempts = self._retries + 1
        for attempt in range(1, attempts + 1):
            current_url = url
            redirects = 0
            try:
                while True:
                    async with self._session.get(
                        current_url, allow_redirects=False
                    ) as response:
                        if 300 <= response.status < 400:
                            location = response.headers.get("Location")
                            redirect_url = (
                                urljoin(current_url, location)
                                if isinstance(location, str)
                                else None
                            )
                            if (
                                redirect_url is None
                                or redirects >= 3
                                or not _same_origin(current_url, redirect_url)
                            ):
                                raise HttpResponseError(
                                    "GET", url, response.status
                                )
                            current_url = redirect_url
                            redirects += 1
                            continue
                        if response.status >= 300:
                            raise HttpResponseError("GET", url, response.status)
                        return await self._read_limited(response, current_url)
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
        raise HttpRequestError("GET", url, attempts, last_error) from None

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

    async def _read_limited(self, response: object, url: str) -> bytes:
        content_length = getattr(response, "content_length", None)
        if (
            isinstance(content_length, int)
            and content_length > self._max_response_bytes
        ):
            raise HttpBodyTooLargeError(url, self._max_response_bytes)
        content = getattr(response, "content")
        chunks: list[bytes] = []
        total = 0
        async for chunk in content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > self._max_response_bytes:
                raise HttpBodyTooLargeError(url, self._max_response_bytes)
            chunks.append(chunk)
        return b"".join(chunks)
