# HTTP Retry Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve safe HTTP response status metadata when a retried request ultimately raises `HttpRequestError`.

**Architecture:** Keep the public exception types and retry flow unchanged. Build a safe cause summary inside `HttpRequestError`: include `status` and optional numeric `error_code` only for `HttpResponseError`, and retain type-only summaries for all other causes.

**Tech Stack:** Python 3.12, pytest

---

### Task 1: Preserve retry response metadata

**Files:**
- Modify: `tests/test_http.py`
- Modify: `usd1_monitor/http.py:13-22`

- [x] **Step 1: Write the failing test**

```python
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
```

- [x] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_http.py::test_retried_http_response_error_keeps_safe_status_metadata -q`

Expected: FAIL because the current string ends at `HttpResponseError`.

- [x] **Step 3: Implement the minimal safe summary**

```python
cause_summary = type(cause).__name__
if isinstance(cause, HttpResponseError):
    cause_summary += f" status={cause.status}"
    if cause.error_code is not None:
        cause_summary += f" code={cause.error_code}"
self.cause = RuntimeError(cause_summary)
super().__init__(
    f"{method} {self.url} failed after {attempts} attempt(s): {cause_summary}"
)
```

- [x] **Step 4: Run focused and related tests**

Run: `python -m pytest tests/test_http.py tests/test_rpc.py -q`

Expected: all selected tests PASS.

- [x] **Step 5: Run the full offline test suite**

Run: `python -m pytest -q -m "not live"`

Expected: all offline tests PASS.

- [ ] **Step 6: Commit the implementation**

```bash
git add tests/test_http.py usd1_monitor/http.py docs/superpowers/plans/2026-09-09-http-retry-status.md
git commit -m "修复 HTTP 重试状态诊断信息"
```
