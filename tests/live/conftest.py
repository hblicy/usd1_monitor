import os

import pytest


def pytest_collection_modifyitems(config, items) -> None:
    if os.getenv("USD1_RUN_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(
        reason="set USD1_RUN_LIVE_TESTS=1 to run public read-only checks"
    )
    for item in items:
        if "tests/live" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)
