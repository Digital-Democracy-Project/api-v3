"""DDP jurisdiction enrollment (OPEN-318 slice 4 / OPEN-329). Mounted at /ddp/enrollment; ddp-api's
/openstates/* catch-all proxy forwards it with no ddp-api change.

GET /ddp/enrollment returns, per jurisdiction, which features it is enrolled in, read from ddp-open-states'
`jurisdictions.yaml` (the single source of truth for that). The file is mounted read-only into the container
(ddp-open-states `deploy/docker-compose.*.yml`); the path comes from DDP_JURISDICTIONS_MANIFEST (default
/app/jurisdictions.yaml). It exists so separate deployables (ddp-broker-py first) can read the list at run time
instead of each keeping a hand-edited copy. Read-only, and a trimmed view: no scrape timeouts, WAF or onboarding
detail. The file is re-read when its mtime changes, so a `git pull` of the checkout is picked up without a restart.

No `tier` (OPEN-520): the manifest is dropping it (OPEN-521, ddp-sync decides timing), and no reader of this route used
it (the broker reads only `enrollment.replica_routing`). The loader reads only name, status, archive.enabled and the
enrollment block, so a manifest with or without `tier` (or `archive.scheduled`) is served the same.

If the file is missing or not in the expected shape the answer is 503 with the reason, never an empty list: a
caller that cannot get the list must say so, not quietly serve "nothing is enrolled".

No `actions` (OPEN-544): the manifest's `actions.newest_first` (BROKER-368) is gone, since every state's bill actions
are stored oldest first, and the broker no longer reads it. A stray `actions` block in a manifest (an older copy on a
host) is ignored whatever it holds: never served, never a 503.

Nothing Voatz is served (BROKER-494; Ramon, 2026-11-13: "api-v3 should only be serving up the ddp-open-states
data. it's the responsibility of the broker to bring together data from openstates, voatz, legbot, etc."). A state's
Voatz test precinct and its private poll creator id (BROKER-482) live in its Voatz entry in AWS Secrets Manager, read
by the broker. A stray `voatz` block in a manifest (an older copy on a host) is ignored whatever it holds: never
served, never a 503.

Chamber names (OPEN-556, PLAN-state-specific-handling C7): each entry's `chambers` block (the legislature's own names
for its upper and lower chambers, and `unicameral` for a one-house legislature, Nebraska, whose one name is in `upper`
and whose `lower` is null) is served as it is, so the website and ddp-sync stop printing "House"/"Senate" everywhere.
The manifest's validator requires the block on every entry, so a manifest without it (or with a misshapen one) is not
in the expected shape: 503. Release order: the manifest first on each host, then this; a rollback goes the other way
(this first, then the manifest).
"""
import datetime
import os
import threading
from typing import Dict, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import apikey_auth

router = APIRouter(prefix="/ddp/enrollment", tags=["ddp-enrollment"])

MANIFEST_ENV = "DDP_JURISDICTIONS_MANIFEST"
DEFAULT_MANIFEST_PATH = "/app/jurisdictions.yaml"

_lock = threading.Lock()
_cache = {}  # path -> (mtime, parsed jurisdictions dict)


class Chambers(BaseModel):
    unicameral: bool = Field(..., description="true for a one-house legislature (Nebraska)")
    upper: str = Field(
        ...,
        example="Senate",
        description="The upper chamber's name; a one-house legislature's one name",
    )
    lower: Optional[str] = Field(
        ...,
        example="House of Delegates",
        description="The lower chamber's name; null for a one-house legislature",
    )


class JurisdictionEnrollment(BaseModel):
    name: str = Field(..., example="North Carolina")
    status: str = Field(
        ...,
        example="probing",
        description="probing | scraping | validating | archived | live | paused",
    )
    archive: bool = Field(..., description="archive.enabled in the manifest")
    enrollment: Dict[str, bool] = Field(
        ...,
        description=(
            "Every boolean under the manifest's `enrollment:` block, e.g. people_refresh, search_refresh, "
            "embedding, cloud_path, api_smoke_test, qa_sweep, replica_routing, legbot_replica."
        ),
    )
    chambers: Chambers = Field(
        ..., description="The legislature's own chamber names (manifest `chambers:`)"
    )


class EnrollmentResponse(BaseModel):
    manifest_mtime: datetime.datetime = Field(
        ...,
        description="When the mounted manifest file was last changed: a stale checkout shows here.",
    )
    jurisdictions: Dict[str, JurisdictionEnrollment]


def _unavailable(reason: str):
    return HTTPException(503, detail=f"jurisdiction manifest unavailable: {reason}")


def _chambers(block):
    """The manifest's `chambers` block (OPEN-556). Only the shape is checked here, as for `enrollment`; the rules
    between the fields (a one-house legislature has no lower chamber) are the manifest validator's."""
    unicameral, upper, lower = block["unicameral"], block["upper"], block["lower"]
    if not isinstance(unicameral, bool):
        raise TypeError("chambers.unicameral is not a boolean")
    if not isinstance(upper, str) or not (lower is None or isinstance(lower, str)):
        raise TypeError("chamber names must be text (lower may be null)")
    return {"unicameral": unicameral, "upper": upper, "lower": lower}


def _load(path: str):
    try:
        mtime = os.stat(path).st_mtime
    except OSError as exc:
        raise _unavailable(f"cannot read {path} ({exc.strerror or exc})")
    with _lock:
        cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return mtime, cached[1]
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise _unavailable(f"{path} is not readable YAML ({exc})")
    if not isinstance(raw, dict) or not raw:
        raise _unavailable(f"{path} has no jurisdictions")
    parsed = {}
    for code, entry in raw.items():
        try:
            enrollment = entry["enrollment"]
            if not all(isinstance(v, bool) for v in enrollment.values()):
                raise TypeError("non-boolean enrollment value")
            row = {
                "name": entry["name"],
                "status": entry["status"],
                "archive": bool(entry["archive"]["enabled"]),
                "enrollment": dict(enrollment),
                "chambers": _chambers(entry["chambers"]),
            }
            # Neither an `actions` block (OPEN-544) nor a `voatz` block (BROKER-494)
            # is read.
            parsed[str(code).lower()] = row
        # pydantic's ValidationError is a ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise _unavailable(
                f"{path}: entry '{code}' is not in the expected shape ({exc})"
            )
    with _lock:
        _cache[path] = (mtime, parsed)
    return mtime, parsed


@router.get(
    "",
    response_model=EnrollmentResponse,
    summary="Which jurisdictions are enrolled in which feature",
)
def get_enrollment(auth: str = Depends(apikey_auth)):
    mtime, jurisdictions = _load(os.environ.get(MANIFEST_ENV, DEFAULT_MANIFEST_PATH))
    return {
        "manifest_mtime": datetime.datetime.fromtimestamp(
            mtime, tz=datetime.timezone.utc
        ),
        "jurisdictions": jurisdictions,
    }
