"""Fixture: deliberately slop-ridden Python test suite for slopguard's tests."""
import time
from unittest.mock import MagicMock, patch


def load_config(path):
    return {"path": path}


def test_loads():  # no-assert-test: calls the code, checks nothing
    cfg = load_config("/etc/app.conf")
    print(cfg)


def test_gateway_called():  # mock-only-test: asserts wiring, not behavior
    gateway = MagicMock()
    gateway.charge(100)
    gateway.charge.assert_called_once_with(100)


def test_mock_echo():  # mock-echo-test: verifies the mock, not the code
    client = MagicMock()
    client.fetch.return_value = "user-42-payload"
    result = client.fetch()
    assert result == "user-42-payload"


def test_always_passes():
    assert True  # tautological-assert


def test_sometimes_checks(records=None):
    records = records or []
    if len(records) > 0:  # conditional-assert: silently passes on empty input
        assert records[0] is not None


def test_exact_error_message():
    try:
        load_config(None)
    except TypeError as e:
        # brittle-exact-string: pins incidental wording
        assert str(e) == "load_config() missing 1 required positional argument: 'path' (see docs)"


def test_whole_response_pinned():
    resp = {"id": 1, "name": "a", "email": "e", "age": 3, "city": "c",
            "zip": "z", "phone": "p", "active": True, "tier": "gold"}
    # overspecified-assert: pins every field at once
    assert resp == {"id": 1, "name": "a", "email": "e", "age": 3, "city": "c",
                    "zip": "z", "phone": "p", "active": True, "tier": "gold"}


def test_internal_counter():
    svc = MagicMock()
    # private-poke-test: reads implementation internals
    assert svc._retry_count is not None


@patch("os.path.exists")
@patch("os.getcwd")
def test_wired_up(mock_cwd, mock_exists):  # excessive-mocking
    a, b, c, d = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    assert a is not b and c is not d


def test_eventually_ready():
    time.sleep(2)  # sleep-in-test
    assert load_config("x")


# parametrize-candidate: three tests identical except literals
def test_add_small():
    total = 1 + 2
    assert total == 3


def test_add_medium():
    total = 10 + 20
    assert total == 30


def test_add_large():
    total = 100 + 200
    assert total == 300
