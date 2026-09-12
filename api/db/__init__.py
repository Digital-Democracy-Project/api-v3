import os
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from api.rds_credentials import resolve_rds_credentials

# OPEN-279: opt-in live credential resolution, for the EC2/RDS deployment where RDS's
# managed-secret rotation (every 7 days) would otherwise make a credential baked into
# DATABASE_URL at container start go stale between restarts. `=true`-only, not bare
# truthiness -- matching openstates-core's own RESOLVE_RDS_LIVE precedent (OPEN-260), fixed
# there after a real bug where "false"/"0" were being treated as enabled. Off by default:
# the Mac's local dev deployment uses a fixed local Postgres password with no rotation to
# chase, and DATABASE_URL there is a normal, complete connection string.
RESOLVE_RDS_LIVE = os.environ.get("RESOLVE_RDS_LIVE") == "true"

if RESOLVE_RDS_LIVE:
    # The RDS-managed secret carries only username/password (OPEN-260) -- host/port/dbname
    # come from their own env vars, matching ddp-sync's own RDS_HOST/RDS_PORT/RDS_DBNAME
    # precedent, since those three don't rotate and aren't part of the secret at all. The
    # placeholder user:pass below is never actually used to connect -- do_connect (below)
    # always overwrites it before any real connection attempt -- it exists only so
    # create_engine's URL parsing has a complete, valid URL to work with at import time.
    RDS_HOST = os.environ["RDS_HOST"]
    RDS_PORT = os.environ.get("RDS_PORT", "5432")
    RDS_DBNAME = os.environ["RDS_DBNAME"]
    DATABASE_URL = f"postgresql://placeholder:placeholder@{RDS_HOST}:{RDS_PORT}/{RDS_DBNAME}"
else:
    DATABASE_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://")
"""
From https://docs.sqlalchemy.org/en/14/core/pooling.html
Default pool/overflow size is 5/10, timeout 30 seconds

max_overflow=15 - the number of connections to allow in connection pool “overflow”,
    that is connections that can be opened above and beyond the pool_size setting,
    which defaults to five. this is only used with QueuePool.

pool_size=10 - the number of connections to keep open inside the connection pool.
    This used with QueuePool as well as SingletonThreadPool.
    With QueuePool, a pool_size setting of 0 indicates no limit; to disable pooling,
    set poolclass to NullPool instead.

pool_timeout=30 - number of seconds to wait before giving up on getting a connection from the pool.
    This is only used with QueuePool. This can be a float but is subject to the limitations of
    Python time functions which may not be reliable in the tens of milliseconds.

pool_recycle=28800 - this setting causes the pool to recycle connections after the given number
    of seconds has passed. It defaults to -1, or no timeout. For example, setting to 3600 means
    connections will be recycled after one hour. Note that MySQL in particular will disconnect
    automatically if no activity is detected on a connection for eight hours
    (although this is configurable with the MySQLDB connection itself and the server configuration as well).
"""
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=7,
    pool_timeout=45,
    pool_recycle=7200,
    connect_args={"application_name": "os_api_v3"},
)

if RESOLVE_RDS_LIVE:
    # Fires once per NEW physical connection the pool opens -- not once per checkout, and
    # not once per request. Already-open pooled connections are unaffected by a credential
    # change elsewhere (Postgres doesn't kill a live session when the password rotates).
    # pm-review correction: pool_recycle (7200s/2h) doesn't proactively reconnect an idle
    # connection on a timer -- SQLAlchemy checks a connection's age against pool_recycle at
    # its next *checkout* and replaces it then if it's aged out. So a rotation is picked up
    # the next time a connection is both checked out AND due for recycling (or the pool
    # otherwise needs a genuinely new connection, e.g. after an error) -- bounded by
    # pool_recycle, but not on a standalone background schedule.
    @event.listens_for(engine, "do_connect")
    def _inject_live_rds_credentials(dialect, conn_rec, cargs, cparams):
        credentials, error = resolve_rds_credentials()
        if error:
            # Fail loudly: raising here surfaces as a normal connection failure to whatever
            # caller triggered this new connection, rather than silently falling back to
            # DATABASE_URL's placeholder credential (which was never valid) or some other
            # cached value.
            raise RuntimeError(f"OPEN-279: could not resolve live RDS credentials: {error}")
        cparams["user"] = credentials["username"]
        cparams["password"] = credentials["password"]

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
