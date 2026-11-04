import os

import pytest


# Real-model tests draw on the User's Max limits and real-GitHub tests need gh signed in, so they run only when asked.
OPT_IN = {
    "real_model": ("JARVIS_REAL_MODEL_TESTS", "talks to the real model"),
    "real_github": ("JARVIS_REAL_GITHUB_TESTS", "reads the Org on GitHub through gh"),
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for marker, (variable, what) in OPT_IN.items():
        if os.environ.get(variable) == "1":
            continue
        skip = pytest.mark.skip(reason=f"{what}; set {variable}=1 to run")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)
