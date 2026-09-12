"""Tests for rds_credentials.py and its RESOLVE_RDS_LIVE opt-in in api/db/__init__.py (OPEN-279)."""

import json
import os
from urllib.parse import urlparse

import pytest

from api.rds_credentials import resolve_rds_credentials

# Reuses whatever Postgres conftest.py's own TEST_DATABASE_URL already points at (the "db"
# service in .codebot/docker-compose.test.yml when run via .codebot/test.sh, or
# postgresql://v3test:v3test@localhost/v3test by default) -- this file needs its own real
# Postgres role to actually exercise the do_connect mechanism end to end, but has no reason
# to stand up a second one when the suite's existing test database already works for this.
_test_db_url = urlparse(
    os.environ.get("TEST_DATABASE_URL", "postgresql://v3test:v3test@localhost/v3test")
)
_RDS_TEST_HOST = _test_db_url.hostname or "localhost"
_RDS_TEST_PORT = str(_test_db_url.port or 5432)
_RDS_TEST_DBNAME = (_test_db_url.path or "/v3test").lstrip("/") or "v3test"
_RDS_TEST_USERNAME = _test_db_url.username or "v3test"
_RDS_TEST_PASSWORD = _test_db_url.password or "v3test"


class FakeSecretsManagerClient:
    def __init__(self, secret_string=None, error=None):
        self._secret_string = secret_string
        self._error = error
        self.get_secret_value_calls = []

    def get_secret_value(self, **kwargs):
        self.get_secret_value_calls.append(kwargs)
        if self._error:
            raise self._error
        return {"SecretString": self._secret_string}


def _real_shaped_secret(**overrides):
    # The real RDS-managed secret only ever carries these two keys (confirmed live, OPEN-260).
    secret = {"username": "openstates_admin", "password": "correct horse battery staple"}
    secret.update(overrides)
    return json.dumps(secret)


# ── resolve_rds_credentials ─────────────────────────────────────────────────────────────────


def test_missing_secret_arn_refuses_without_calling_secrets_manager(monkeypatch):
    monkeypatch.delenv("RDS_CREDENTIALS_SECRET_ARN", raising=False)
    import api.rds_credentials as mod

    monkeypatch.setattr(mod, "RDS_CREDENTIALS_SECRET_ARN", None)

    credentials, error = resolve_rds_credentials(secretsmanager_client=FakeSecretsManagerClient())

    assert credentials is None
    assert "RDS_CREDENTIALS_SECRET_ARN not set" in error


def test_successful_fetch_returns_raw_unencoded_credentials(monkeypatch):
    import api.rds_credentials as mod

    monkeypatch.setattr(mod, "RDS_CREDENTIALS_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:1:secret:rds!x")
    client = FakeSecretsManagerClient(secret_string=_real_shaped_secret())

    credentials, error = resolve_rds_credentials(secretsmanager_client=client)

    assert error == ""
    # Deliberately NOT URL-encoded -- these go directly into psycopg2 connect kwargs, not a
    # URL string, and a space here should stay a literal space, not "%20".
    assert credentials == {"username": "openstates_admin", "password": "correct horse battery staple"}


def test_password_with_url_special_characters_is_returned_raw(monkeypatch):
    """The opposite assertion from openstates-core/ddp-sync's twin tests: those percent-encode
    for a URL string, this must NOT, since it feeds a DBAPI connect-args dict directly."""
    import api.rds_credentials as mod

    monkeypatch.setattr(mod, "RDS_CREDENTIALS_SECRET_ARN", "arn:secret")
    client = FakeSecretsManagerClient(secret_string=_real_shaped_secret(password="p@ss:w/rd%25"))

    credentials, error = resolve_rds_credentials(secretsmanager_client=client)

    assert error == ""
    assert credentials["password"] == "p@ss:w/rd%25"


def test_secrets_manager_api_error_fails_loudly_not_silently(monkeypatch):
    import api.rds_credentials as mod

    monkeypatch.setattr(mod, "RDS_CREDENTIALS_SECRET_ARN", "arn:secret")
    client = FakeSecretsManagerClient(error=RuntimeError("AccessDeniedException"))

    credentials, error = resolve_rds_credentials(secretsmanager_client=client)

    assert credentials is None
    assert "AccessDeniedException" in error


def test_malformed_secret_shape_fails_cleanly_instead_of_raising(monkeypatch):
    import api.rds_credentials as mod

    monkeypatch.setattr(mod, "RDS_CREDENTIALS_SECRET_ARN", "arn:secret")
    client = FakeSecretsManagerClient(secret_string=json.dumps({"username": "x"}))

    credentials, error = resolve_rds_credentials(secretsmanager_client=client)

    assert credentials is None
    assert "unexpected shape" in error


def test_null_field_in_secret_fails_cleanly_instead_of_using_the_literal_string_none(monkeypatch):
    import api.rds_credentials as mod

    monkeypatch.setattr(mod, "RDS_CREDENTIALS_SECRET_ARN", "arn:secret")
    client = FakeSecretsManagerClient(secret_string=_real_shaped_secret(password=None))

    credentials, error = resolve_rds_credentials(secretsmanager_client=client)

    assert credentials is None
    assert "unexpected shape" in error


def test_boto3_client_construction_failure_fails_cleanly_instead_of_raising(monkeypatch):
    """pm-review lesson carried over from OPEN-260: boto3.client() itself can raise, not just
    get_secret_value() -- both must land in the same (None, error) tuple contract."""
    import api.rds_credentials as mod

    monkeypatch.setattr(mod, "RDS_CREDENTIALS_SECRET_ARN", "arn:secret")

    from unittest.mock import patch

    with patch("boto3.client", side_effect=RuntimeError("no region configured")):
        credentials, error = resolve_rds_credentials()

    assert credentials is None
    assert "no region configured" in error


# ── RESOLVE_RDS_LIVE wiring in api/db/__init__.py ───────────────────────────────────────────
#
# These import api.db fresh in a subprocess-like isolated module reload, since api.db builds
# its module-level `engine` at import time from the environment -- reusing the already-imported
# module (as conftest.py's own app fixture does) would not reflect a changed RESOLVE_RDS_LIVE.


def _reload_api_db(monkeypatch, **env):
    import importlib
    import sys

    for key in ("RESOLVE_RDS_LIVE", "RDS_HOST", "RDS_PORT", "RDS_DBNAME", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    sys.modules.pop("api.db", None)
    return importlib.import_module("api.db")


def test_resolve_rds_live_unset_uses_database_url_as_is(monkeypatch):
    db_module = _reload_api_db(monkeypatch, DATABASE_URL="postgresql://u:p@localhost/openstates")

    assert db_module.DATABASE_URL == "postgresql://u:p@localhost/openstates"
    assert db_module.RESOLVE_RDS_LIVE is False


def test_resolve_rds_live_false_string_does_not_enable_live_resolution(monkeypatch):
    """Same footgun OPEN-260 found and fixed elsewhere: a bare truthiness check on
    os.environ.get(...) would treat "false" (any non-empty string) as enabled."""
    db_module = _reload_api_db(
        monkeypatch, RESOLVE_RDS_LIVE="false", DATABASE_URL="postgresql://u:p@localhost/openstates"
    )

    assert db_module.RESOLVE_RDS_LIVE is False
    assert db_module.DATABASE_URL == "postgresql://u:p@localhost/openstates"


def test_resolve_rds_live_true_builds_placeholder_url_from_rds_env_vars(monkeypatch):
    db_module = _reload_api_db(
        monkeypatch,
        RESOLVE_RDS_LIVE="true",
        RDS_HOST="ddp-openstates.example.rds.amazonaws.com",
        RDS_PORT="5432",
        RDS_DBNAME="openstates",
    )

    assert db_module.RESOLVE_RDS_LIVE is True
    assert db_module.DATABASE_URL == "postgresql://placeholder:placeholder@ddp-openstates.example.rds.amazonaws.com:5432/openstates"


def test_new_connection_fetches_live_credentials_via_do_connect(monkeypatch):
    """The actual mechanism this ticket exists to prove: a NEW physical connection resolves
    credentials through resolve_rds_credentials() at connect time, not from DATABASE_URL's
    placeholder -- verified against a real local Postgres role, not a mock of the DB layer."""
    db_module = _reload_api_db(
        monkeypatch,
        RESOLVE_RDS_LIVE="true",
        RDS_HOST=_RDS_TEST_HOST,
        RDS_PORT=_RDS_TEST_PORT,
        RDS_DBNAME=_RDS_TEST_DBNAME,
    )

    # Patched on api.db itself, not api.rds_credentials -- api/db/__init__.py does
    # `from api.rds_credentials import resolve_rds_credentials`, which binds its own
    # independent name in api.db's namespace at import time. Patching the origin module's
    # attribute would not affect that already-bound reference.
    real_username = _RDS_TEST_USERNAME
    real_password = _RDS_TEST_PASSWORD
    monkeypatch.setattr(
        db_module,
        "resolve_rds_credentials",
        lambda secretsmanager_client=None: (
            {"username": real_username, "password": real_password},
            "",
        ),
    )

    with db_module.engine.connect() as conn:
        assert conn.execute("SELECT 1").scalar() == 1


def test_failed_credential_resolution_raises_instead_of_connecting(monkeypatch):
    """Fail loudly: a connection attempt while live resolution is broken must surface as a
    real error, not silently succeed against the placeholder or hang."""
    db_module = _reload_api_db(
        monkeypatch,
        RESOLVE_RDS_LIVE="true",
        RDS_HOST=_RDS_TEST_HOST,
        RDS_PORT=_RDS_TEST_PORT,
        RDS_DBNAME=_RDS_TEST_DBNAME,
    )

    monkeypatch.setattr(
        db_module,
        "resolve_rds_credentials",
        lambda secretsmanager_client=None: (None, "simulated Secrets Manager outage"),
    )

    with pytest.raises(Exception, match="simulated Secrets Manager outage"):
        with db_module.engine.connect():
            pass


def test_existing_connection_unaffected_by_a_later_credential_change(monkeypatch):
    """Proves the core design claim: an already-open pooled connection keeps working after
    the "current" credential changes elsewhere -- only a NEW connection picks up the change.
    Matches real Postgres rotation behavior (a live session isn't killed when the role's
    password changes), so the fix must not assume every checkout re-authenticates."""
    db_module = _reload_api_db(
        monkeypatch,
        RESOLVE_RDS_LIVE="true",
        RDS_HOST=_RDS_TEST_HOST,
        RDS_PORT=_RDS_TEST_PORT,
        RDS_DBNAME=_RDS_TEST_DBNAME,
    )

    call_count = {"n": 0}

    def fake_resolve(secretsmanager_client=None):
        call_count["n"] += 1
        return {"username": _RDS_TEST_USERNAME, "password": _RDS_TEST_PASSWORD}, ""

    monkeypatch.setattr(db_module, "resolve_rds_credentials", fake_resolve)

    # Open and hold a real connection (one do_connect call).
    conn1 = db_module.engine.connect()
    assert conn1.execute("SELECT 1").scalar() == 1
    assert call_count["n"] == 1

    # "Rotate" to a real second role with a different password (not just a wrong-credential
    # sentinel) -- proves both halves of the claim with one real rotation: the already-open
    # conn1 is untouched (this role change would break it if it tried to re-authenticate),
    # AND a genuinely new connection opened after this point picks up the new role, not a
    # cached one from conn1's own do_connect call.
    conn1.execute("DROP ROLE IF EXISTS rds_credentials_rotation_test_role")
    conn1.execute(
        "CREATE ROLE rds_credentials_rotation_test_role LOGIN PASSWORD 'rotated-password' "
        "IN ROLE v3test"
    )

    def fake_resolve_after_rotation(secretsmanager_client=None):
        call_count["n"] += 1
        return {"username": "rds_credentials_rotation_test_role", "password": "rotated-password"}, ""

    monkeypatch.setattr(db_module, "resolve_rds_credentials", fake_resolve_after_rotation)

    # The already-open connection is untouched by the "rotation" above.
    assert conn1.execute("SELECT 1").scalar() == 1

    # A genuinely NEW connection, opened after the "rotation", authenticates as the NEW role
    # -- proving do_connect actually re-invokes resolve_rds_credentials per new connection
    # rather than reusing whatever conn1 resolved at its own connect time.
    conn2 = db_module.engine.connect()
    try:
        assert conn2.execute("SELECT current_user").scalar() == "rds_credentials_rotation_test_role"
        # do_connect only fires on a NEW physical connection, never on execute() against an
        # already-open one -- exactly 2 calls total: conn1's own connect, and conn2's.
        assert call_count["n"] == 2
    finally:
        conn2.close()
        conn1.close()
