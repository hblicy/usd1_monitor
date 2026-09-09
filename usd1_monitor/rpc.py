from __future__ import annotations

from typing import Protocol

from usd1_monitor.http import HttpRequestError, sanitize_url


class JsonPoster(Protocol):
    async def post_json(self, url: str, payload: dict) -> object: ...


class RpcError(RuntimeError):
    pass


class RpcResponseError(RpcError):
    """The endpoint responded and explicitly rejected the JSON-RPC call."""


class RpcChainMismatchError(RpcError):
    """The configured endpoint serves a different chain."""


class JsonRpcClient:
    def __init__(
        self,
        urls: list[str],
        http: JsonPoster,
        *,
        expected_chain_id: int | None = None,
    ) -> None:
        if not urls:
            raise ValueError("at least one RPC URL is required")
        self._urls = tuple(urls)
        self._http = http
        self._request_id = 0
        self._expected_chain_id = expected_chain_id
        self._active_url: str | None = None

    async def call(self, method: str, params: list) -> object:
        failures: list[str] = []
        response_errors: list[RpcResponseError] = []
        saw_non_response_error = False
        for url in self._urls:
            try:
                if (
                    self._expected_chain_id is not None
                    and url != self._active_url
                    and method != "eth_chainId"
                ):
                    chain_id = await self._request(url, "eth_chainId", [])
                    try:
                        parsed_chain_id = int(str(chain_id), 16)
                    except (TypeError, ValueError) as exc:
                        raise RpcError("RPC chain id is not valid hex") from exc
                    if parsed_chain_id != self._expected_chain_id:
                        raise RpcChainMismatchError(
                            "RPC chain id mismatch: "
                            f"expected {self._expected_chain_id}, got {parsed_chain_id}"
                        )
                result = await self._request(url, method, params)
                self._active_url = url
                return result
            except RpcChainMismatchError:
                if self._active_url == url:
                    self._active_url = None
                raise
            except Exception as exc:
                if self._active_url == url:
                    self._active_url = None
                if isinstance(exc, RpcResponseError):
                    response_errors.append(exc)
                else:
                    saw_non_response_error = True
                if isinstance(exc, (RpcError, HttpRequestError)):
                    summary = str(exc)
                else:
                    summary = type(exc).__name__
                failures.append(f"{sanitize_url(url)}: {summary}")
        if response_errors and not saw_non_response_error:
            raise response_errors[-1]
        raise RpcError(
            f"all RPC endpoints failed for {method}: {' | '.join(failures)}"
        )

    async def _request(self, url: str, method: str, params: list) -> object:
        self._request_id += 1
        body = await self._http.post_json(
            url,
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            },
        )
        if not isinstance(body, dict):
            raise RpcError(f"RPC {method} response is not an object")
        if "error" in body:
            error = body["error"]
            detail = (
                f"code={error.get('code')}"
                if isinstance(error, dict)
                else "malformed error"
            )
            raise RpcResponseError(f"RPC {method} failed: {detail}")
        if "result" not in body:
            raise RpcError(f"RPC {method} response has no result")
        return body["result"]
