# USD1 Official Information Alert Noise Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop historical and semantically unrelated official announcements from flooding WeChat while retaining current, directly USD1-related adverse-event alerts.

**Architecture:** Keep collection and durable announcement storage unchanged. Tighten the pure text classifier, gate only `NEW` announcement events at the information monitor boundary, and let the state engine persist selected expiration transitions without enqueueing notifications.

**Tech Stack:** Python 3.11+, asyncio, SQLite/aiosqlite, pytest.

---

### Task 1: Scope official risk text to the affected entity

**Files:**
- Modify: `usd1_monitor/engine/information_rules.py`
- Test: `tests/test_information_rules.py`

- [x] Add failing tests proving generic WLFI listing/region terms and USD1 promotion boilerplate are GREEN, while a sentence that directly suspends USD1 withdrawals remains YELLOW and is returned as evidence.
- [x] Run `python -m pytest tests/test_information_rules.py -q` and confirm the new tests fail for the current whole-document substring matcher.
- [x] Split text into clauses and retain only adverse action stems in the generic classifier.
- [x] Re-run `python -m pytest tests/test_information_rules.py -q` and confirm it passes.

### Task 2: Baseline historical announcements without hiding current changes

**Files:**
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_information_integration.py`

- [x] Add failing integration tests for an old published `NEW` item, the first undated source snapshot, a recent published adverse item, a later undated adverse item, and an adverse `CHANGED` item.
- [x] Run the five focused tests and confirm the historical cases currently enqueue YELLOW states.
- [x] Gate only `NEW` evaluations: published items older than 24 hours are stored silently; undated items are stored silently until that source has a recorded successful poll. Leave `CHANGED` behavior unchanged.
- [x] Include the matched clause in event evidence and re-run the focused tests.

### Task 3: Expire information events without recovery notifications

**Files:**
- Modify: `usd1_monitor/engine/state.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_evm_integration.py`
- Test: `tests/test_market_rules.py`

- [x] Add a failing composite-monitor test with one expired `event.information.*` state and one expired non-information event; assert both become GREEN but only the non-information recovery is queued.
- [x] Run the focused test and confirm both recovery alerts are currently queued.
- [x] Add an optional state-engine enqueue flag with its current behavior as the default, then split maintenance expiration into silent information transitions and normal other transitions.
- [x] Re-run the focused test plus existing state/expiry tests.

### Task 4: Verify and document one-time queue cleanup

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-09-09-information-alert-noise.md`

- [x] Add a deployment note that first stops the old process, backs up `data/monitor.db`, previews matching pending rows, and deletes only undelivered `official:*` and `expiry:event.information.*` rows in one SQLite transaction.
- [x] Run `python -m pytest tests/test_information_rules.py tests/test_information_integration.py tests/test_evm_integration.py tests/test_market_rules.py tests/test_wechat.py -q`.
- [x] Run the complete offline test suite, `python -m compileall -q usd1_monitor tests`, `python -m pip check`, and `git diff --check`.
- [x] Inspect `git diff --stat`, `git diff`, and `git status --short`; ensure no unrelated files changed.
- [x] Mark this plan complete and commit with a Chinese commit message.
