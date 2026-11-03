import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Real-model tests draw on the User's Max limits, so they run only when asked for.
    if os.environ.get("JARVIS_REAL_MODEL_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="talks to the real model; set JARVIS_REAL_MODEL_TESTS=1 to run")
    for item in items:
        if "real_model" in item.keywords:
            item.add_marker(skip)
