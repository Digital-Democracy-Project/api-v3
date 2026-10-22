"""OPEN-329: GET /ddp/enrollment serves the mounted jurisdictions.yaml. The route reads a file, so these tests
only need a temp manifest (the session fixtures in conftest still create the test database)."""
import os
import time

import pytest
import yaml

from api import ddp_enrollment

URL = "/ddp/enrollment"


def _entry(status="live", tier="secondary", archive=True, **enrollment):
    base = {"people_refresh": True, "search_refresh": False, "replica_routing": True}
    base.update(enrollment)
    return {
        "name": "Somewhere",
        "status": status,
        "tier": tier,
        "scrape": {"timeout_s": 21600},  # not served
        "archive": {"enabled": archive, "timeout_s": 14400},
        "enrollment": base,
        "waf": {"profile": "none"},
    }


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
        "tier": "secondary",
        "archive": True,
        "enrollment": {
            "people_refresh": True,
            "search_refresh": False,
            "replica_routing": True,
        },
    }
    assert body["jurisdictions"]["us"]["enrollment"]["replica_routing"] is False
    assert body["manifest_mtime"].endswith("Z") or "+00:00" in body["manifest_mtime"]


def test_does_not_serve_internal_detail(client, manifest):
    text = client.get(URL).text
    for private in ("timeout_s", "waf", "onboarding", "scrape"):
        assert private not in text


def test_a_changed_file_is_picked_up_without_a_restart(client, manifest):
    assert client.get(URL).json()["jurisdictions"]["nc"]["status"] == "probing"
    manifest.write_text(yaml.safe_dump({"nc": _entry("live")}))
    os.utime(manifest, (time.time() + 5, time.time() + 5))
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
