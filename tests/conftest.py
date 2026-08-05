"""Shared fixtures for the Portal test suite."""
import os
import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_registry_dir(tmp_path_factory):
    """Point PORTAL_DATA_DIR at a temp dir for the whole test session.

    The registry is machine-global and persistent; tests must never
    touch the real one.
    """
    registry_dir = tmp_path_factory.mktemp("portal-registry")
    os.environ["PORTAL_DATA_DIR"] = str(registry_dir)
    yield
    os.environ.pop("PORTAL_DATA_DIR", None)
