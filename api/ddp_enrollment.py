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

BROKER-368: two per-state facts the broker used to keep in its own code are served too, as the manifest states them:
`actions` (`newest_first`: OpenStates lists the state's bill actions newest first) and `voatz` (`test_precinct`: the
Voatz test precinct, null when unknown). The Voatz poll creator id is NOT served (BROKER-482): it is private, lives in
AWS Secrets Manager and the broker reads it there; a stray `creator_id` key in a manifest is dropped, never served.
An entry without the block (a manifest
older than BROKER-368) is served without the key, never with a guessed value, so the broker can tell "the manifest does
not say yet" from "the manifest says no"; a block in the wrong shape is a 503 like any other.
"""
import datetime
import os
import threading
from typing import Dict, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictStr

from .auth import apikey_auth

router = APIRouter(prefix="/ddp/enrollment", tags=["ddp-enrollment"])

MANIFEST_ENV = "DDP_JURISDICTIONS_MANIFEST"
DEFAULT_MANIFEST_PATH = "/app/jurisdictions.yaml"

_lock = threading.Lock()
_cache = {}  # path -> (mtime, parsed jurisdictions dict)


class Actions(BaseModel):
    newest_first: StrictBool = Field(
        ...,
        description="OpenStates lists this state's bill actions newest first (us, nc).",
    )


class Voatz(BaseModel):
    # No creator_id (BROKER-482): it is private. Unknown keys are ignored (pydantic's default), so a stray one in a
    # manifest is never served; a test pins that.
    test_precinct: Optional[StrictStr] = Field(
        ..., description="The Voatz test precinct; null when unknown (no test poll)."
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
    actions: Optional[Actions] = Field(
        None,
        description="The manifest's `actions` block (BROKER-368); absent when the manifest has none.",
    )
    voatz: Optional[Voatz] = Field(
        None,
        description="The manifest's `voatz` block (BROKER-368); absent when the manifest has none.",
    )


class EnrollmentResponse(BaseModel):
    manifest_mtime: datetime.datetime = Field(
        ...,
        description="When the mounted manifest file was last changed: a stale checkout shows here.",
    )
    jurisdictions: Dict[str, JurisdictionEnrollment]


def _unavailable(reason: str):
    return HTTPException(503, detail=f"jurisdiction manifest unavailable: {reason}")


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
            }
            # BROKER-368: served only when the manifest has them; checked here, so
            # a wrong shape is a 503 like any other, not a 500
            if "actions" in entry:
                row["actions"] = Actions(**entry["actions"]).dict()
            if "voatz" in entry:
                row["voatz"] = Voatz(**entry["voatz"]).dict()
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
    response_model_exclude_unset=True,  # an entry without actions/voatz is served without the key (BROKER-368)
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
