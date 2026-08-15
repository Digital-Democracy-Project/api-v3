import uuid
from .conftest import query_logger
from api.bills import BillSortOption


def test_bills_filter_by_jurisdiction_abbr(client):
    # state short ID lower case
    response = client.get("/bills?jurisdiction=ne")
    assert query_logger.count == 2
    response = response.json()
    assert len(response["results"]) == 7

    # state short ID upper case
    response = client.get("/bills?jurisdiction=NE")
    assert query_logger.count == 2
    response = response.json()
    assert len(response["results"]) == 7


def test_bills_filter_by_jurisdiction_name(client):
    # by full name
    response = client.get("/bills?jurisdiction=Nebraska")
    assert query_logger.count == 2
    response = response.json()
    assert len(response["results"]) == 7


def test_bills_filter_by_jurisdiction_jid(client):
    # by jid
    response = client.get(
        "/bills?jurisdiction=ocd-jurisdiction/country:us/state:ne/government"
    )
    assert query_logger.count == 2
    response = response.json()
    assert len(response["results"]) == 7


def test_bills_filter_by_session_no_jurisdiction(client):
    # 5 bills are in 2020
    response = client.get("/bills?session=2020")
    assert query_logger.count == 0
    assert response.status_code == 400
    assert "jurisdiction" in response.json()["detail"]


def test_bills_filter_by_session(client):
    # 5 bills are in 2020
    response = client.get("/bills?jurisdiction=ne&session=2020")
    assert query_logger.count == 2
    assert len(response.json()["results"]) == 5


def test_bills_filter_by_identifier(client):
    # spaces corrected
    response = client.get("/bills?jurisdiction=oh&identifier=HB1")
    assert query_logger.count == 2
    assert len(response.json()["results"]) == 1
    # case insensitive
    response = client.get("/bills?jurisdiction=oh&identifier=hb 1")
    assert query_logger.count == 2
    assert len(response.json()["results"]) == 1


def test_bills_filter_by_identifier_multi(client):
    response = client.get("/bills?jurisdiction=ne&identifier=sb1&identifier=SB 2")
    assert query_logger.count == 2
    assert len(response.json()["results"]) == 2


def test_bills_filter_by_chamber(client):
    response = client.get("/bills?jurisdiction=ne&chamber=legislature")
    assert len(response.json()["results"]) == 7
    response = client.get("/bills?jurisdiction=oh&chamber=upper")
    assert len(response.json()["results"]) == 1
    response = client.get("/bills?jurisdiction=oh&chamber=lower")
    assert len(response.json()["results"]) == 0


def test_bills_filter_by_classification(client):
    response = client.get("/bills?jurisdiction=ne&classification=bill")
    assert len(response.json()["results"]) == 5
    response = client.get("/bills?jurisdiction=ne&classification=resolution")
    assert len(response.json()["results"]) == 2


def test_bills_filter_by_subject(client):
    response = client.get("/bills?jurisdiction=ne&subject=futurism")
    response = response.json()
    assert len(response["results"]) == 2


def test_bills_filter_by_updated_since(client):
    response = client.get("/bills?jurisdiction=ne&updated_since=2020-01-01")
    assert len(response.json()["results"]) == 7
    response = client.get("/bills?jurisdiction=ne&updated_since=2032-01-01T00:00:00")
    assert len(response.json()["results"]) == 0


def test_bills_filter_by_created_since(client):
    response = client.get("/bills?jurisdiction=ne&created_since=2020-01-01T00:00")
    assert len(response.json()["results"]) == 7
    response = client.get("/bills?jurisdiction=ne&created_since=2032-01-01")
    assert len(response.json()["results"]) == 0


def test_bills_filter_by_bad_datetime(client):
    response = client.get("/bills?jurisdiction=ne&created_since=2022")
    assert response.status_code == 400
    assert "ISO-8601" in response.json()["detail"]


def test_bills_filter_by_action_since(client):
    response = client.get("/bills?jurisdiction=ne&action_since=2020")
    assert len(response.json()["results"]) == 7
    response = client.get("/bills?jurisdiction=ne&action_since=2021")
    assert len(response.json()["results"]) == 2


def test_bills_filter_by_query(client):
    response = client.get("/bills?q=HIOO")
    assert len(response.json()["results"]) == 1
    response = client.get("/bills?q=Ohio")
    assert len(response.json()["results"]) == 1
    response = client.get("/bills?q=Cleveland")
    assert len(response.json()["results"]) == 0


def test_bills_filter_by_query_bill_id(client):
    response = client.get("/bills?q=HB 1")
    assert len(response.json()["results"]) == 1
    response = client.get("/bills?q=hb1")
    assert len(response.json()["results"]) == 1
    response = client.get("/bills?q=hb6")
    assert len(response.json()["results"]) == 0


def test_bills_filter_by_sponsor_name(client):
    response = client.get("/bills?jurisdiction=oh&sponsor=Ruth")
    assert len(response.json()["results"]) == 1
    # non-used name
    response = client.get("/bills?jurisdiction=oh&sponsor=Willis")
    assert len(response.json()["results"]) == 0


def test_bills_filter_by_sponsor_id(client):
    response = client.get(
        "/bills?jurisdiction=oh&sponsor=ocd-person/99999999-9999-9999-9999-999999999999"
    )
    assert len(response.json()["results"]) == 1
    # bad ID
    response = client.get(
        "/bills?jurisdiction=oh&sponsor=ocd-person/99999999-9999-9999-9999-000000000000"
    )
    assert len(response.json()["results"]) == 0


def test_bills_filter_by_sponsor_classification(client):
    response = client.get(
        "/bills?jurisdiction=oh&sponsor=Ruth&sponsor_classification=sponsor"
    )
    assert len(response.json()["results"]) == 1
    response = client.get(
        "/bills?jurisdiction=oh&sponsor=Ruth&sponsor_classification=cosponsor"
    )
    assert len(response.json()["results"]) == 0
    response = client.get(
        "/bills?jurisdiction=oh&sponsor=Marge&sponsor_classification=cosponsor"
    )
    assert len(response.json()["results"]) == 1


def test_bills_no_filter(client):
    response = client.get("/bills")
    assert query_logger.count == 0
    assert response.status_code == 400
    assert "required" in response.json()["detail"]


def test_bills_include_basics(client):
    response = client.get(
        "/bills?jurisdiction=ne&session=2020&include=sponsorships&include=abstracts"
        "&include=other_titles&include=other_identifiers&include=actions&include=sources"
    )
    assert query_logger.count == 9
    assert response.status_code == 200
    for b in response.json()["results"]:
        assert len(b["sponsorships"]) == 2
        assert len(b["abstracts"]) == 0
        assert len(b["other_titles"]) == 0
        assert len(b["other_identifiers"]) == 0
        assert len(b["actions"]) == 5
        assert len(b["sources"]) == 1
        assert "documents" not in b


def test_bills_include_documents_versions(client):
    response = client.get(
        "/bills?jurisdiction=ne&session=2020&include=documents&include=versions"
    )
    assert query_logger.count == 6
    assert response.status_code == 200
    for b in response.json()["results"]:
        assert len(b["documents"]) == 3
        assert len(b["versions"]) == 2
        version = b["versions"][0]
        version_id = version.pop("id")  # they are dynamic uuids so...
        assert uuid.UUID(version_id)
        assert isinstance(version_id, str)
        assert version == {
            "note": "Version 0",
            "date": "2020",
            "links": [{"media_type": "text/html", "url": "https://example.com/0"}],
            "classification": "",
        }

        for doc in b["documents"]:
            doc_id = doc.pop("id")
            assert uuid.UUID(doc_id)
            assert "note" in doc
            assert "date" in doc
            assert "links" in doc
        assert "other_titles" not in b


def test_bills_include_votes(client):
    response = client.get("/bills?q=HB1&include=votes")
    assert query_logger.count == 7
    assert response.status_code == 200
    b = response.json()["results"][0]
    votes = b["votes"]
    for vote in votes:
        for person_vote in vote["votes"]:
            vote_people_id = person_vote.pop("id")
            assert uuid.UUID(vote_people_id)
            assert isinstance(vote_people_id, str)

    assert b["votes"] == [
        {
            "id": "ocd-vote/1",
            "identifier": "Vote on HB1",
            "motion_text": "Floor Vote",
            "motion_classification": [],
            "start_date": "2021-01-01",
            "result": "passed",
            "organization": {
                "id": "ohh",
                "name": "Ohio House",
                "classification": "lower",
            },
            "votes": [
                {"option": "yes", "voter_name": "Bart"},
                {"option": "yes", "voter_name": "Harley"},
                {"option": "no", "voter_name": "Jarvis"},
            ],
            "counts": [{"option": "yes", "value": 2}, {"option": "no", "value": 1}],
            "sources": [],
            "extras": {},
        },
        {
            "id": "ocd-vote/2",
            "identifier": "Vote on HB1",
            "motion_text": "Floor Vote",
            "motion_classification": [],
            "start_date": "2021-02-01",
            "result": "passed",
            "organization": {
                "id": "ohs",
                "name": "Ohio Senate",
                "classification": "upper",
            },
            "votes": [],
            "counts": [{"option": "yes", "value": 42}, {"option": "no", "value": 0}],
            "sources": [],
            "extras": {},
        },
    ]


def test_bills_include_related_bills(client):
    response = client.get("/bills?q=HB1&include=related_bills")
    assert query_logger.count == 3
    assert response.status_code == 200
    b = response.json()["results"][0]
    assert b["related_bills"] == [
        {
            "identifier": "SB 1",
            "legislative_session": "2021",
            "relation_type": "companion",
        }
    ]


def test_bills_all_sort_options_valid(client):
    for sort_param in BillSortOption:
        response = client.get(f"/bills/?jurisdiction=ne&sort={sort_param}")
        print(response.json())
        assert response.status_code == 200


def test_bills_sort_ordering_correct(client):
    bills = client.get("/bills/?jurisdiction=ne&sort=updated_desc").json()["results"]
    assert bills[0]["updated_at"] > bills[1]["updated_at"] > bills[2]["updated_at"]

    bills = client.get("/bills/?jurisdiction=ne&sort=updated_asc").json()["results"]
    assert bills[0]["updated_at"] < bills[1]["updated_at"] < bills[2]["updated_at"]

    bills = client.get("/bills/?jurisdiction=ne&sort=first_action_asc").json()[
        "results"
    ]
    assert (
        bills[0]["first_action_date"]
        <= bills[1]["first_action_date"]
        <= bills[2]["first_action_date"]
    )

    bills = client.get("/bills/?jurisdiction=ne&sort=first_action_desc").json()[
        "results"
    ]
    assert (
        bills[0]["first_action_date"]
        >= bills[1]["first_action_date"]
        >= bills[2]["first_action_date"]
    )

    bills = client.get("/bills/?jurisdiction=ne&sort=latest_action_asc").json()[
        "results"
    ]
    assert (
        bills[0]["latest_action_date"]
        <= bills[1]["latest_action_date"]
        <= bills[2]["latest_action_date"]
    )

    bills = client.get("/bills/?jurisdiction=ne&sort=latest_action_desc").json()[
        "results"
    ]
    assert (
        bills[0]["latest_action_date"]
        >= bills[1]["latest_action_date"]
        >= bills[2]["latest_action_date"]
    )


def test_bill_detail_basic(client):
    response = client.get("/bills/oh/2021/HB 1").json()
    assert response["id"] == "ocd-bill/1234"
    assert query_logger.count == 1


def test_bill_detail_404(client):
    response = client.get("/bills/oh/2021/HB 66")
    assert response.status_code == 404
    assert response.json() == {"detail": "No such Bill."}


def test_bill_detail_id_normalization(client):
    response = client.get("/bills/oh/2021/HB1").json()
    assert response["id"] == "ocd-bill/1234"
    assert query_logger.count == 1

    response = client.get("/bills/oh/2021/hb1").json()
    assert response["id"] == "ocd-bill/1234"
    assert query_logger.count == 1


def test_bill_openstates_url(client):
    response = client.get("/bills/oh/2021/HB1").json()
    assert response["openstates_url"] == "https://openstates.org/oh/bills/2021/HB1/"
    assert query_logger.count == 1


def test_bill_detail_includes(client):
    response = client.get("/bills/oh/2021/HB 1?include=votes").json()
    assert response["id"] == "ocd-bill/1234"
    assert len(response["votes"]) == 2
    assert query_logger.count == 6


def test_bill_detail_by_internal_id(client):
    response = client.get("/bills/ocd-bill/1234").json()
    assert response["id"] == "ocd-bill/1234"
    assert response["identifier"] == "HB 1"
    assert query_logger.count == 1


def test_bill_detail_sponsorship_resolution(client):
    response = client.get("/bills/oh/2021/HB 1?include=sponsorships").json()
    assert response["id"] == "ocd-bill/1234"
    assert len(response["sponsorships"]) == 2
    # uses the compact person representation, no more joins
    sponsorship = response["sponsorships"][0]
    sponsor_id = sponsorship.pop("id")
    assert uuid.UUID(sponsor_id)
    assert isinstance(sponsor_id, str)
    assert sponsorship == {
        "name": "Ruth",
        "entity_type": "person",
        "classification": "sponsor",
        "primary": True,
        "person": {
            "id": "ocd-person/99999999-9999-9999-9999-999999999999",
            "name": "Ruth",
            "party": "Democratic",
            "current_role": {
                "title": "Senator",
                "org_classification": "upper",
                "district": "9",
                "division_id": "ocd-division/country:us/state:oh/sldu:9",
            },
        },
    }
    assert query_logger.count == 3


def test_bills_include_actions(client):
    response = client.get("/bills?jurisdiction=ne&session=2020&include=actions")

    for bill in response.json()["results"]:
        assert "actions" in bill
        assert len(bill["actions"]) > 0

        for action in bill["actions"]:
            action_id = action.pop("id")
            assert uuid.UUID(action_id)

            assert action["description"]
            assert action["date"]
            assert isinstance(action["classification"], list)
            assert isinstance(action["organization"], dict)
            assert isinstance(action["related_entities"], list)

            assert "id" in action["organization"]
            org_id = action["organization"]["id"]
            assert isinstance(org_id, str)
            assert "name" in action["organization"]
            assert "classification" in action["organization"]

            for entity in action["related_entities"]:
                assert "id" in entity
                assert "name" in entity
                assert "type" in entity


def test_bill_detail_versions_raw_text_populated_prefers_pdf(client):
    """OPEN-13: archived latest version exposes raw_text, preferring PDF over HTML."""
    response = client.get("/bills/oh/2021/HB 9101?include=versions").json()
    # base bill query + versions + versions.links + the new archived-doc lookup query
    assert query_logger.count == 4
    assert response["identifier"] == "HB 9101"
    assert len(response["versions"]) == 1
    links = {link["media_type"]: link for link in response["versions"][0]["links"]}
    assert (
        links["application/pdf"]["raw_text"]
        == "AN ACT relating to scorpions; designating the scorpion as the state arachnid."
    )
    assert "raw_text" not in links["text/html"]


def test_bill_detail_versions_raw_text_absent_when_not_archived(client):
    """OPEN-13: a bill with no archived BillVersionDocument row omits raw_text, doesn't error."""
    response = client.get("/bills/oh/2021/HB 9102?include=versions").json()
    assert response["identifier"] == "HB 9102"
    assert len(response["versions"]) == 1
    for link in response["versions"][0]["links"]:
        assert "raw_text" not in link


def test_bill_detail_versions_raw_text_surfaced_for_latest_and_previous_only(client):
    """
    bill_changelog fix (2026-07-30): the version immediately before the latest one now also
    surfaces its archived raw_text (needed as dispatch_bill_changelog's old_bill_source) --
    but only that one. HB 9103's latest version ("Committee Substitute") isn't archived, so it
    still shows no raw_text; the immediately-previous version ("Introduced") is archived and
    now does; a third, even-older archived version ("Filed") must not leak through -- only the
    two most recent versions are ever checked, not a bill's full history.
    """
    response = client.get("/bills/oh/2021/HB 9103?include=versions").json()
    assert response["identifier"] == "HB 9103"
    assert len(response["versions"]) == 3
    by_note = {v["note"]: v for v in response["versions"]}

    for link in by_note["Committee Substitute"]["links"]:
        assert "raw_text" not in link
    assert "diff_from_previous_version" not in by_note["Committee Substitute"]

    assert (
        by_note["Introduced"]["links"][0]["raw_text"]
        == "AN ACT relating to salamanders (introduced version)."
    )

    for link in by_note["Filed"]["links"]:
        assert "raw_text" not in link


def test_bills_list_endpoint_never_exposes_raw_text(client):
    """OPEN-13: raw_text is a single-bill-detail-only feature -- the /bills list never sets it,
    even for an archived bill, to keep paginated response size bounded."""
    response = client.get(
        "/bills?jurisdiction=oh&session=2021&identifier=HB 9101&include=versions"
    )
    # base bill query + count + versions + versions.links -- no archived-doc lookup query
    assert query_logger.count == 4
    results = response.json()["results"]
    assert len(results) == 1
    for link in results[0]["versions"][0]["links"]:
        assert "raw_text" not in link


def test_bill_detail_changelog_shape_latest_diff_and_previous_raw_text(client):
    """
    bill_changelog fix (2026-07-30): with two adjacent archived versions, the single-bill
    detail endpoint now surfaces everything dispatch_bill_changelog needs without ddp-sync
    re-fetching or re-diffing anything itself -- latest's own raw_text plus its precomputed
    diff_from_previous_version (diff_source), and the immediately-previous version's raw_text
    (old_bill_source). The previous version's own diff_from_previous_version stays absent --
    it's the first version ever archived for this bill, nothing to diff against.
    """
    response = client.get("/bills/oh/2021/HB 9104?include=versions").json()
    assert response["identifier"] == "HB 9104"
    assert len(response["versions"]) == 2
    by_note = {v["note"]: v for v in response["versions"]}

    latest = by_note["Engrossed"]
    assert latest["links"][0]["raw_text"] == "AN ACT relating to frogs (engrossed version)."
    assert "Introduced" in latest["diff_from_previous_version"]
    assert "Engrossed" in latest["diff_from_previous_version"]

    previous = by_note["Introduced"]
    assert previous["links"][0]["raw_text"] == "AN ACT relating to frogs (introduced version)."
    assert "diff_from_previous_version" not in previous


def test_bills_list_endpoint_never_exposes_diff_from_previous_version(client):
    """Same bounded-response-size rule as raw_text (OPEN-13) applies to
    diff_from_previous_version -- the /bills list must never set it either."""
    response = client.get(
        "/bills?jurisdiction=oh&session=2021&identifier=HB 9104&include=versions"
    )
    results = response.json()["results"]
    assert len(results) == 1
    for version in results[0]["versions"]:
        assert "diff_from_previous_version" not in version


def test_bill_detail_latest_version_resolved_via_stage_not_alphabetical_order(client):
    """OPEN-92: HB 9105 has three undated versions -- "Introduced", "Committee Substitute",
    "Enrolled" -- whose real chronological order (Introduced -> Committee Substitute ->
    Enrolled) disagrees with plain alphabetical order ("Committee Substitute" < "Enrolled" <
    "Introduced"). Before this fix, postprocess_includes's naive
    sorted(data.versions, key=lambda v: (v.date, v.note)) would resolve "Introduced" as latest
    and "Enrolled" as previous, attaching archived text to the wrong two versions and leaving
    "Committee Substitute" -- the true previous version -- with none at all. This asserts the
    correct, stage-aware resolution: "Enrolled" is latest (gets its own diff, from Committee
    Substitute), "Committee Substitute" is previous (gets its own diff, from Introduced), and
    "Introduced" -- selected by neither role under the fix -- gets no archived text at all.

    A two-version version of this fixture (kept in git history) was found NOT to actually
    distinguish old vs. new code: _attach_archived_document matches by bill+note+date+url
    (not by which role it was called for), so with only two versions both always get their
    own pre-baked document attached regardless of which one is mislabeled "latest"/"previous"
    -- confirmed by reverting to the pre-fix sort and finding the 2-version test still passed.
    The third version and the raw_text-presence/absence assertions below are what make this
    version of the test actually fail against the pre-fix code."""
    response = client.get("/bills/oh/2021/HB 9105?include=versions").json()
    assert response["identifier"] == "HB 9105"
    assert len(response["versions"]) == 3
    by_note = {v["note"]: v for v in response["versions"]}

    latest = by_note["Enrolled"]
    assert latest["links"][0]["raw_text"] == "AN ACT relating to toads (enrolled version)."
    assert "Committee Substitute" in latest["diff_from_previous_version"]
    assert "Enrolled" in latest["diff_from_previous_version"]

    previous = by_note["Committee Substitute"]
    assert (
        previous["links"][0]["raw_text"]
        == "AN ACT relating to toads (committee substitute version)."
    )
    assert "Introduced" in previous["diff_from_previous_version"]
    assert "Committee Substitute" in previous["diff_from_previous_version"]

    # The true earliest version is selected as neither latest nor previous, so it must get no
    # archived text at all -- under the pre-fix naive sort this version was wrongly picked as
    # "latest" instead and DID get raw_text attached.
    unattached = by_note["Introduced"]
    assert "raw_text" not in unattached["links"][0]
    assert "diff_from_previous_version" not in unattached

    # SYNC-16: the response array itself is reordered so a caller can take
    # versions[-1]/versions[-2] directly by plain array position, without re-deriving
    # order itself -- this is the whole point of the fix from ddp-sync's side. The fixture
    # rows are inserted out of chronological order specifically so this can't pass by
    # insertion-order coincidence.
    assert response["versions"][-1]["note"] == "Enrolled"
    assert response["versions"][-2]["note"] == "Committee Substitute"


def test_bills_documents_never_get_raw_text(client):
    """OPEN-13: BillDocument (unlike BillVersion) is never archived by the pipeline, so
    documents[].links[].raw_text must always be absent, even on a single-bill detail query."""
    ne_bill_id = client.get("/bills?jurisdiction=ne&session=2020").json()["results"][0][
        "id"
    ]
    response = client.get(
        f"/bills/{ne_bill_id}?include=documents&include=versions"
    ).json()
    assert response["id"] == ne_bill_id
    assert len(response["documents"]) == 3
    for document in response["documents"]:
        for link in document["links"]:
            assert "raw_text" not in link
    # ne bills have versions but no archived BillVersionDocument rows either
    for version in response["versions"]:
        for link in version["links"]:
            assert "raw_text" not in link
