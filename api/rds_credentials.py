"""Resolves the live RDS connection credential from Secrets Manager at connection time.

OPEN-279: follow-up to OPEN-260 (ddp-sync), which fixed this exact bug class for Fargate
task launches. RDS's own "manage master credentials in Secrets Manager" feature rotates
the master password every 7 days (confirmed via the console: rotation enabled, 7-day
schedule -- infra/rds/rds.tf's `manage_master_user_password = true`). This app previously
read a single static DATABASE_URL from the environment at container start (rendered by
deploy/rotate-database-url.sh, a manual one-off script) -- a cached value goes stale on
that 7-day cadence regardless of how recently the container started, and required a
manual script rerun plus a container restart every time it did.

Unlike ddp-sync's short-lived Fargate tasks (where "resolve at call time" naturally means
"once per task launch," so nothing is ever cached across a rotation boundary), this is a
long-running server process with a pooled SQLAlchemy engine. Resolving on every request
would work but would waste a Secrets Manager call per request for no benefit -- the right
unit of reuse for a connection pool is the physical database connection, not the request.
This module is wired into the engine's `do_connect` event instead (see api/db/__init__.py),
which fires once per NEW physical connection the pool opens: already-open connections keep
using whatever credential they connected with (Postgres doesn't kill a live session when
the password changes elsewhere), so a rotation is picked up the next time the pool needs to
open a fresh connection -- never later than one `pool_recycle` interval.

RDS's managed secret is JSON with `username`/`password` fields only (confirmed directly
against the real secret in OPEN-260 -- no host/port/dbname fields exist in it). Deliberately
does NOT URL-encode these values, unlike OPEN-260's ddp-sync twin: that module builds a
full `postgresql://...` URL string, where special characters must be percent-encoded to
avoid corrupting the URL grammar. This module instead returns raw values for direct
assignment into a DBAPI connect-args dict (psycopg2's `user`/`password` kwargs), which take
the credential as-is -- encoding it here would corrupt a password containing a literal `%`
or other reserved-in-a-URL character that is perfectly valid as a raw connect argument.
"""

from __future__ import annotations

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

RDS_CREDENTIALS_SECRET_ARN = os.environ.get("RDS_CREDENTIALS_SECRET_ARN")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def resolve_rds_credentials(secretsmanager_client=None) -> tuple[dict | None, str]:
    """Fetch the current RDS username/password from Secrets Manager.

    Returns (credentials, error): on success `error` is "" and `credentials` is
    {"username": ..., "password": ...}; on any failure `credentials` is None and `error`
    describes what went wrong. Deliberately never falls back to a cached/stale value -- a
    failure here should be loud and visible (surfaced as a normal connection failure to
    whatever triggered it), not silently absorbed into "proceed anyway with whatever we had."

    `secretsmanager_client` is injectable for tests; production callers should leave it
    unset and get a real boto3 client.
    """
    if not RDS_CREDENTIALS_SECRET_ARN:
        return None, "RDS_CREDENTIALS_SECRET_ARN not set -- refusing to guess which secret to read"

    try:
        client = secretsmanager_client or boto3.client("secretsmanager", region_name=AWS_REGION)
        response = client.get_secret_value(SecretId=RDS_CREDENTIALS_SECRET_ARN)
    except Exception as e:  # noqa: BLE001 -- any boto3/network failure is equally "can't proceed"
        # boto3.client() itself can raise (region/credential-provider/botocore config
        # problems), not just get_secret_value() -- both must land in this same (None, error)
        # contract, matching OPEN-260's own hard-won lesson about this exact failure mode.
        logger.error("rds_credentials: fetch failed: %s", e)
        return None, f"could not fetch RDS credential from Secrets Manager: {e}"

    try:
        secret = json.loads(response["SecretString"])
        username = secret["username"]
        password = secret["password"]
        # A JSON null for either would otherwise pass this check via `KeyError` never firing,
        # then get used as-is by psycopg2 -- reject explicitly instead of producing a
        # confusing downstream connection error with no context.
        if not all([username, password]):
            raise ValueError("username or password is null or empty")
    except (KeyError, ValueError, TypeError) as e:
        logger.error("rds_credentials: unexpected secret shape: %s", e)
        return None, f"RDS credential secret has an unexpected shape: {e}"

    return {"username": str(username), "password": str(password)}, ""
