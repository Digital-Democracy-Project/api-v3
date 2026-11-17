"""OPEN-329: GET /ddp/enrollment serves the mounted jurisdictions.yaml. The route reads a file, so these tests
only need a temp manifest (the session fixtures in conftest still create the test database)."""
import os
import time

import pytest
import yaml

from api import ddp_enrollment

URL = "/ddp/enrollment"
TWO_HOUSES = {"unicameral": False, "upper": "Senate", "lower": "House of Representatives"}


def _entry(status="live", tier="secondary", archive=True, scheduled=None, **enrollment):
    """One manifest entry. tier=None leaves `tier` out (the shape after OPEN-521); `scheduled` is set only if given."""
    base = {"people_refresh": True, "search_refresh": False, "replica_routing": True}
    base.update(enrollment)
    entry = {
        "name": "Somewhere",
        "status": status,
        "scrape": {"timeout_s": 21600},  # not served
        "archive": {"enabled": archive, "timeout_s": 14400},
        "enrollment": base,
        "waf": {"profile": "none"},
        "chambers": dict(TWO_HOUSES),
    }
    if tier is not None:
        entry["tier"] = tier
    if scheduled is not None:
        entry["archive"]["scheduled"] = scheduled
    return entry


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "jurisdictions.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "nc": _entry("probing"),
                "US": _entry(tier="primary", replica_routing=False),
            }
        )
    )
    monkeypatch.setenv(ddp_enrollment.MANIFEST_ENV, str(path))
    ddp_enrollment._cache.clear()
    return path


def test_serves_every_jurisdiction_with_its_features(client, manifest):
    body = client.get(URL).json()
    assert set(body["jurisdictions"]) == {"nc", "us"}  # codes are lower-cased
    nc = body["jurisdictions"]["nc"]
    assert nc == {
        "name": "Somewhere",
        "status": "probing",
        "archive": True,
        "enrollment": {
            "people_refresh": True,
            "search_refresh": False,
            "replica_routing": True,
        },
        "chambers": TWO_HOUSES,
    }
    assert body["jurisdictions"]["us"]["enrollment"]["replica_routing"] is False
    assert body["manifest_mtime"].endswith("Z") or "+00:00" in body["manifest_mtime"]


def test_does_not_serve_internal_detail(client, manifest):
    text = client.get(URL).text
    for private in ("timeout_s", "waf", "onboarding", "scrape", "tier"):
        assert private not in text


def _serve(client, tmp_path, monkeypatch, manifest_dict):
    path = tmp_path / "jurisdictions.yaml"
    path.write_text(yaml.safe_dump(manifest_dict))
    monkeypatch.setenv(ddp_enrollment.MANIFEST_ENV, str(path))
    ddp_enrollment._cache.clear()
    response = client.get(URL)
    assert response.status_code == 200, response.text
    return response.json()["jurisdictions"]


EXPECTED_PA = {
    "name": "Somewhere",
    "status": "live",
    "archive": True,
    "enrollment": {
        "people_refresh": True,
        "search_refresh": False,
        "replica_routing": True,
    },
    "chambers": TWO_HOUSES,
}
EXPECTED_NE = {
    "name": "Somewhere",
    "status": "scraping",
    "archive": False,
    "enrollment": {
        "people_refresh": True,
        "search_refresh": False,
        "replica_routing": False,
    },
    "chambers": TWO_HOUSES,
}


def test_a_manifest_with_tier_and_archive_scheduled_is_served_without_tier(
    client, tmp_path, monkeypatch
):
    """OPEN-520: today's manifest shape (tier, archive.scheduled) answers 200, and tier is no longer served."""
    served = _serve(
        client,
        tmp_path,
        monkeypatch,
        {
            "pa": _entry(tier="primary", scheduled=True),
            "ne": _entry(
                "scraping",
                tier="secondary",
                archive=False,
                scheduled=False,
                replica_routing=False,
            ),
        },
    )
    assert served == {"pa": EXPECTED_PA, "ne": EXPECTED_NE}


def test_a_manifest_without_tier_or_archive_scheduled_is_served(
    client, tmp_path, monkeypatch
):
    """OPEN-520: the shape after OPEN-521 (no tier, no archive.scheduled) answers 200 with the same content, not 503."""
    served = _serve(
        client,
        tmp_path,
        monkeypatch,
        {
            "pa": _entry(tier=None),
            "ne": _entry("scraping", tier=None, archive=False, replica_routing=False),
        },
    )
    assert served == {"pa": EXPECTED_PA, "ne": EXPECTED_NE}


def test_a_changed_file_is_picked_up_without_a_restart(client, manifest):
    assert client.get(URL).json()["jurisdictions"]["nc"]["status"] == "probing"
    manifest.write_text(yaml.safe_dump({"nc": _entry("live")}))
    os.utime(manifest, (time.time() + 5, time.time() + 5))
    assert client.get(URL).json()["jurisdictions"]["nc"]["status"] == "live"


def test_a_file_replaced_by_rename_the_way_git_pull_does_is_picked_up(client, manifest):
    """OPEN-483: `git pull` writes a temp file and renames it over the old one (a new inode). Served through a DIRECTORY
    mount (ddp-open-states deploy/manifest/) that is what api-v3 sees, and the loader must answer with the new list."""
    first = client.get(URL).json()
    assert first["jurisdictions"]["nc"]["status"] == "probing"
    replacement = manifest.with_name("jurisdictions.yaml.new")
    replacement.write_text(
        yaml.safe_dump({"nc": _entry("live"), "pa": _entry("scraping")})
    )
    os.utime(replacement, (time.time() + 7, time.time() + 7))
    inode_before = manifest.stat().st_ino
    os.replace(replacement, manifest)
    assert (
        manifest.stat().st_ino != inode_before
    )  # a real replace, not an edit in place
    second = client.get(URL).json()
    assert (
        second["jurisdictions"]["nc"]["status"] == "live"
        and "pa" in second["jurisdictions"]
    )
    assert second["manifest_mtime"] != first["manifest_mtime"]


def test_the_loader_trusts_the_modification_time_alone(client, manifest):
    """The contract deploy/sync-manifest.sh relies on (OPEN-483): changed content with the SAME modification time is not
    re-read, so the sync script gives a changed copy a new time; a new time is re-read at once."""
    pinned = time.time() - 3600
    os.utime(manifest, (pinned, pinned))
    ddp_enrollment._cache.clear()
    assert client.get(URL).json()["jurisdictions"]["nc"]["status"] == "probing"
    manifest.write_text(yaml.safe_dump({"nc": _entry("live")}))
    os.utime(manifest, (pinned, pinned))  # same time, different content
    assert (
        client.get(URL).json()["jurisdictions"]["nc"]["status"] == "probing"
    )  # still the cached list
    os.utime(
        manifest, (pinned + 1, pinned + 1)
    )  # what the sync script's time bump does
    assert client.get(URL).json()["jurisdictions"]["nc"]["status"] == "live"


def test_a_missing_manifest_is_503_naming_the_reason_not_an_empty_list(
    client, tmp_path, monkeypatch
):
    monkeypatch.setenv(ddp_enrollment.MANIFEST_ENV, str(tmp_path / "nope.yaml"))
    ddp_enrollment._cache.clear()
    response = client.get(URL)
    assert response.status_code == 503
    assert "jurisdiction manifest unavailable" in response.json()["detail"]


@pytest.mark.parametrize(
    "text",
    [
        "not: [valid",
        "just a string",
        "{}",
        yaml.safe_dump({"nc": {"name": "x"}}),
        yaml.safe_dump({"nc": _entry(replica_routing="yes")}),
    ],
)
def test_an_unreadable_or_misshapen_manifest_is_503(
    client, tmp_path, monkeypatch, text
):
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    monkeypatch.setenv(ddp_enrollment.MANIFEST_ENV, str(path))
    ddp_enrollment._cache.clear()
    assert client.get(URL).status_code == 503


def test_the_route_is_in_the_openapi_schema(client):
    assert URL in client.get("/openapi.json").json()["paths"]


# --- OPEN-544: no actions block; BROKER-482/494: nothing Voatz ----------------------------------------------------


@pytest.mark.parametrize(
    "actions",
    [
        {"newest_first": True},
        {"newest_first": False},
        # a wrong shape is ignored too: the block is not read at all
        {"newest_first": "yes"},
        {},
        "not a block",
    ],
)
def test_a_stray_actions_block_is_never_served(client, tmp_path, monkeypatch, actions):
    """OPEN-544: `actions.newest_first` is gone from the manifest and the broker no longer reads it. A block left in an
    older copy of the manifest on a host is ignored whatever it holds: no key in the answer, and never a 503."""
    entry = _entry(tier=None)
    entry["actions"] = actions
    served = _serve(client, tmp_path, monkeypatch, {"pa": entry})
    assert served == {"pa": EXPECTED_PA}


@pytest.mark.parametrize(
    "voatz",
    [
        {"test_precinct": "FEDERAL-TEST"},
        {"test_precinct": "FEDERAL-TEST", "creator_id": 999000001},  # made up
        {"test_precinct": 12},
        {},
        "not a block",
    ],
)
def test_a_voatz_block_in_the_manifest_is_never_served(
    client, tmp_path, monkeypatch, voatz
):
    """BROKER-494: api-v3 serves only OpenStates data; Voatz values (the test precinct, and the private creator id of
    BROKER-482) live in AWS Secrets Manager, read by the broker. A stray block in a manifest (an older copy on a host)
    is ignored whatever it holds: neither a key nor a value appears in the answer, and it is never a 503."""
    entry = _entry(tier=None)
    entry["voatz"] = voatz
    path = tmp_path / "jurisdictions.yaml"
    path.write_text(yaml.safe_dump({"us": entry}))
    monkeypatch.setenv(ddp_enrollment.MANIFEST_ENV, str(path))
    ddp_enrollment._cache.clear()
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json()["jurisdictions"]["us"] == EXPECTED_PA
    for text in ("voatz", "test_precinct", "FEDERAL-TEST", "creator_id", "999000001"):
        assert text not in response.text


# --- OPEN-556: chamber names and the one-house flag ---------------------------------------------------------------


def test_serves_each_legislatures_own_chamber_names(client, tmp_path, monkeypatch):
    """OPEN-556: the manifest's `chambers` block is served as it is: an Assembly, a House of Delegates, and Nebraska's
    one house (its one name in `upper`, `lower` null)."""
    nv, va, ne = _entry(), _entry(), _entry()
    nv["chambers"] = {"unicameral": False, "upper": "Senate", "lower": "Assembly"}
    va["chambers"] = {"unicameral": False, "upper": "Senate", "lower": "House of Delegates"}
    ne["chambers"] = {"unicameral": True, "upper": "Legislature", "lower": None}
    served = _serve(client, tmp_path, monkeypatch, {"nv": nv, "va": va, "ne": ne})
    assert served["nv"]["chambers"] == {"unicameral": False, "upper": "Senate", "lower": "Assembly"}
    assert served["va"]["chambers"] == {"unicameral": False, "upper": "Senate", "lower": "House of Delegates"}
    assert served["ne"]["chambers"] == {"unicameral": True, "upper": "Legislature", "lower": None}


@pytest.mark.parametrize(
    "chambers",
    [
        None,  # no block at all: an older manifest copy
        {"upper": "Senate", "lower": "House of Representatives"},
        {"unicameral": "no", "upper": "Senate", "lower": "House of Representatives"},
        {"unicameral": False, "upper": None, "lower": "House of Representatives"},
        {"unicameral": False, "upper": "Senate", "lower": 7},
    ],
)
def test_a_missing_or_misshapen_chambers_block_is_503(client, tmp_path, monkeypatch, chambers):
    """OPEN-556: the manifest's validator requires the block on every entry, so one without it is not the expected
    shape: 503 naming the reason, never an answer without the names."""
    entry = _entry(tier=None)
    if chambers is None:
        del entry["chambers"]
    else:
        entry["chambers"] = chambers
    path = tmp_path / "jurisdictions.yaml"
    path.write_text(yaml.safe_dump({"ne": entry}))
    monkeypatch.setenv(ddp_enrollment.MANIFEST_ENV, str(path))
    ddp_enrollment._cache.clear()
    response = client.get(URL)
    assert response.status_code == 503
    assert "'ne' is not in the expected shape" in response.json()["detail"]
