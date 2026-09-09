import traceback

import aiohttp
import pytest

from usd1_monitor.config import HttpConfig
from usd1_monitor.http import (
    AsyncHttpClient,
    HttpBodyTooLargeError,
    HttpRequestError,
    HttpResponseError,
    sanitize_url,
)


def test_sanitize_url_removes_query_and_fragment() -> None:
    assert (
        sanitize_url("https://rpc.example/v1?key=secret#fragment")
        == "https://rpc.example"
    )


def test_http_error_never_renders_query_values() -> None:
    error = HttpRequestError(
        "GET",
        "https://rpc.example/v1?key=secret",
        3,
        TimeoutError("late"),
    )

    assert error.url == "https://rpc.example"
    assert "secret" not in str(error)
    assert "3 attempt(s)" in str(error)


def test_http_error_redacts_userinfo_path_token_and_cause_url() -> None:
    secret_url = "https://user:password@rpc.example/v2/secret-token"
    cause = TimeoutError(f"request failed for {secret_url}")

    error = HttpRequestError("POST", secret_url, 2, cause)

    rendered = str(error)
    assert rendered == (
        "POST https://rpc.example failed after 2 attempt(s): TimeoutError"
    )
    assert "password" not in rendered
    assert "secret-token" not in rendered


def test_retried_http_response_error_keeps_safe_status_metadata() -> None:
    secret_url = "https://rpc.example/v2/secret-token"
    error = HttpRequestError(
        "POST",
        secret_url,
        3,
        HttpResponseError("POST", secret_url, 429, 429),
    )

    assert str(error) == (
        "POST https://rpc.example failed after 3 attempt(s): "
        "HttpResponseError status=429 code=429"
    )
    assert "secret-token" not in str(error)


@pytest.mark.asyncio
async def test_full_http_traceback_does_not_include_secret_url() -> None:
    secret_url = "https://user:password@rpc.example/v2/secret-token?key=query-secret"

    class FailingContext:
        async def __aenter__(self):
            raise aiohttp.ClientConnectionError(
                f"connection failed for {secret_url}"
            )

        async def __aexit__(self, *args):
            return None

    class FailingSession:
        closed = False

        def get(self, url, **kwargs):
            return FailingContext()

    client = AsyncHttpClient(HttpConfig(timeout_seconds=1, retries=0))
    client._session = FailingSession()

    rendered = ""
    with pytest.raises(HttpRequestError):
        try:
            await client.get_bytes(secret_url)
        except HttpRequestError:
            rendered = traceback.format_exc()
            raise

    assert "password" not in rendered
    assert "secret-token" not in rendered
    assert "query-secret" not in rendered


@pytest.mark.asyncio
async def test_json_http_error_exposes_only_status_and_safe_error_code() -> None:
    class Content:
        async def iter_chunked(self, size):
            yield b'{"code": -1121, "msg": "Invalid symbol secret-value"}'

    class ErrorResponse:
        status = 400
        content_length = None
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class ErrorSession:
        closed = False

        def request(self, method, url, **kwargs):
            return ErrorResponse()

    client = AsyncHttpClient(HttpConfig(timeout_seconds=1, retries=2))
    client._session = ErrorSession()

    with pytest.raises(HttpResponseError) as raised:
        await client.get_json(
            "https://api.binance.com/api/v3/exchangeInfo",
            {"symbol": "UNKNOWN"},
        )

    assert raised.value.status == 400
    assert raised.value.error_code == -1121
    assert "secret-value" not in str(raised.value)


@pytest.mark.asyncio
async def test_json_requests_do_not_follow_redirects() -> None:
    class Content:
        async def iter_chunked(self, size):
            yield b"{}"

    class RedirectResponse:
        status = 302
        content_length = None
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        closed = False
        kwargs = None

        def request(self, method, url, **kwargs):
            self.kwargs = kwargs
            return RedirectResponse()

    session = Session()
    client = AsyncHttpClient(HttpConfig(timeout_seconds=1, retries=0))
    client._session = session

    with pytest.raises(HttpResponseError) as raised:
        await client.get_json("https://api.binance.com/source")

    assert raised.value.status == 302
    assert session.kwargs["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("getter", ["get_text", "get_bytes"])
async def test_text_and_bytes_requests_reject_cross_origin_redirects(
    getter: str,
) -> None:
    class Content:
        async def iter_chunked(self, size):
            yield b"redirect body"

    class RedirectResponse:
        status = 302
        content_length = None
        content = Content()
        headers = {"Location": "https://outside.example/source"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self):
            return None

    class Session:
        closed = False
        kwargs = None

        def get(self, url, **kwargs):
            self.kwargs = kwargs
            return RedirectResponse()

    session = Session()
    client = AsyncHttpClient(HttpConfig(timeout_seconds=1, retries=0))
    client._session = session

    with pytest.raises(HttpResponseError) as raised:
        await getattr(client, getter)("https://example.com/source")

    assert raised.value.status == 302
    assert session.kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_text_request_follows_one_same_origin_redirect() -> None:
    class Content:
        def __init__(self, body: bytes) -> None:
            self.body = body

        async def iter_chunked(self, size):
            yield self.body

    class Response:
        content_length = None

        def __init__(self, status: int, body: bytes, location: str | None = None):
            self.status = status
            self.content = Content(body)
            self.headers = {} if location is None else {"Location": location}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        closed = False

        def __init__(self) -> None:
            self.responses = [
                Response(307, b"redirect", "/usd1-token/what-is-usd1"),
                Response(200, b"official markdown"),
            ]
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            return self.responses.pop(0)

    session = Session()
    client = AsyncHttpClient(HttpConfig(timeout_seconds=1, retries=0))
    client._session = session

    body = await client.get_text(
        "https://docs.worldlibertyfinancial.com/usd1-token"
    )

    assert body == "official markdown"
    assert session.urls == [
        "https://docs.worldlibertyfinancial.com/usd1-token",
        "https://docs.worldlibertyfinancial.com/usd1-token/what-is-usd1",
    ]


@pytest.mark.asyncio
async def test_http_client_rejects_body_over_configured_limit() -> None:
    class Content:
        async def iter_chunked(self, size):
            yield b"1234"
            yield b"5"

    class Response:
        status = 200
        content_length = None
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self):
            return None

    class Session:
        closed = False

        def get(self, url, **kwargs):
            return Response()

    client = AsyncHttpClient(
        HttpConfig(timeout_seconds=1, retries=0, max_response_bytes=4)
    )
    client._session = Session()

    with pytest.raises(HttpBodyTooLargeError, match="exceeds 4 bytes"):
        await client.get_bytes("https://example.com/file.pdf")
