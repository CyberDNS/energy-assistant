"""ioBroker client: optional HTTP Basic auth for simple-api."""

import pytest

from energy_assistant.plugins._iobroker.client import IoBrokerClient
from energy_assistant.plugins._iobroker.pool import IoBrokerConnectionPool


def test_no_credentials_no_auth() -> None:
    assert IoBrokerClient("h")._client._auth is None


def test_basic_auth_set() -> None:
    c = IoBrokerClient("h", username="u", password="p")
    assert c._client._auth is not None


def test_partial_credentials_rejected() -> None:
    with pytest.raises(ValueError):
        IoBrokerClient("h", username="u")


def test_pool_separates_by_credentials() -> None:
    pool = IoBrokerConnectionPool()
    assert pool.get("h") is not pool.get("h", username="u", password="p")
    assert pool.get("h", username="u", password="p") is pool.get("h", username="u", password="p")


def test_token_and_credentials_rejected() -> None:
    with pytest.raises(ValueError):
        IoBrokerClient("h", api_token="t", username="u", password="p")
