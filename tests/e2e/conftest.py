# tests/e2e/conftest.py
from __future__ import annotations

import pytest

# golden_utils is a plain imported module, not a test file, so pytest won't
# assert-rewrite it on its own. Must run before golden_utils is first imported;
# conftest.py loads before test modules, so top-of-conftest is correct.
pytest.register_assert_rewrite("tests.e2e.golden_utils")

import os  # noqa: E402
from typing import Iterator  # noqa: E402

# Register DataHub's --update-golden-files flag + autouse golden-flags fixture for this dir.
from datahub.testing.pytest_hooks import (  # noqa: E402, F401
    load_golden_flags,
    pytest_addoption,
)


@pytest.fixture
def dh_graph() -> Iterator["DataHubGraph"]:  # noqa: F821
    """Live DataHubGraph client for tests/e2e, built from
    DATAHUB_GMS_URL/DATAHUB_GMS_TOKEN. Only invoked by tests that already
    skip themselves when DATAHUB_GMS_URL is unset."""
    from datahub.ingestion.graph.client import DataHubGraph
    from datahub.ingestion.graph.config import DatahubClientConfig

    config = DatahubClientConfig(
        server=os.environ["DATAHUB_GMS_URL"], token=os.environ.get("DATAHUB_GMS_TOKEN")
    )
    graph = DataHubGraph(config)
    try:
        yield graph
    finally:
        graph.close()
