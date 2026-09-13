import random
import uuid
import datetime
from sqlalchemy import func
from api.db.models import (
    Bill,
    BillAction,
    BillDocument,
    BillDocumentLink,
    BillSource,
    BillSponsorship,
    BillVersion,
    BillVersionLink,
    BillVersionDocument,
    RelatedBill,
    Event,
    EventAgendaItem,
    EventAgendaMedia,
    EventDocument,
    EventLocation,
    EventMedia,
    EventParticipant,
    EventRelatedEntity,
    Jurisdiction,
    LegislativeSession,
    DataExport,
    Membership,
    Organization,
    Person,
    PersonOffice,
    PersonLink,
    PersonName,
    PersonSource,
    PersonVote,
    Post,
    RunPlan,
    SearchableBill,
    VoteCount,
    VoteEvent,
)


def dummy_person_id(n):
    return f"ocd-person/{n*8}-{n*4}-{n*4}-{n*4}-{n*12}"


def create_test_bill(
    session,
    chamber,
    *,
    sponsors=0,
    actions=0,
    votes=0,
    versions=0,
    documents=0,
    sources=0,
    subjects=None,
    identifier=None,
    classification=None,
):
    b = Bill(
        id="ocd-bill/" + str(uuid.uuid4()),
        identifier=identifier or ("Bill " + str(random.randint(1000, 9000))),
        title="Random Bill",
        legislative_session=session,
        from_organization=chamber,
        subject=subjects or [],
        classification=classification or ["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date=f"{session.identifier}-02-{random.randint(10,30)}",
        first_action_date=f"{session.identifier}-01-{random.randint(10,30)}",
    )
    yield b
    for n in range(sponsors):
        yield BillSponsorship(
            bill=b,
            primary=True,
            classification="sponsor",
            name="Someone",
            entity_type="person",
        )
    for n in range(actions):
        yield BillAction(
            bill=b,
            description="an action took place",
            date=session.identifier,
            organization=chamber,
            order=n,
        )
    for n in range(sources):
        yield BillSource(bill=b, url="https://example.com/source", note="")
    for n in range(versions):
        bv = BillVersion(bill=b, note=f"Version {n}", date="2020", classification="")
        yield bv
        yield BillVersionLink(
            version=bv, url=f"https://example.com/{n}", media_type="text/html"
        )
    for n in range(documents):
        bd = BillDocument(bill=b, note=f"Version {n}", date="2020", classification="")
        yield bd
        yield BillDocumentLink(
            document=bd, url=f"https://example.com/{n}", media_type="text/html"
        )


def create_test_event(
    jid, n, *, start_date, deleted=False, related_bill=False, related_committee=False
):
    loc = EventLocation(
        id=str(uuid.uuid4()), name=f"Location #{n}", jurisdiction_id=jid, url=""
    )
    e = Event(
        jurisdiction_id=jid,
        id=f"ocd-event/00000000-0000-0000-0000-{n:012d}",
        name=f"Event #{n}",
        description="",
        classification="",
        start_date=start_date,
        end_date="",
        all_day=False,
        status="normal",
        upstream_id="",
        deleted=deleted,
        location=loc,
        links=[{"note": "source", "url": f"https://example.com/{n}"}],
        sources=[{"note": "source", "url": f"https://example.com/{n}"}],
    )
    yield loc
    yield e
    yield EventMedia(
        event=e,
        note="",
        date=start_date,
        offset=0,
        classification="",
        links=[],
    )
    yield EventDocument(
        event=e,
        date=start_date,
        note="document 1",
        classification="",
        links=[],
    )
    yield EventDocument(
        event=e,
        date=start_date,
        note="document 2",
        classification="",
        links=[],
    )
    yield EventParticipant(
        event=e,
        note="",
        name="John",
        entity_type="person",
    )
    yield EventParticipant(
        event=e,
        note="",
        name="Jane",
        entity_type="person",
    )
    yield EventParticipant(
        event=e,
        note="",
        name="Javier",
        entity_type="person",
    )
    a1 = EventAgendaItem(
        id=str(uuid.uuid4()),
        event=e,
        description="Agenda Item 1",
        classification="",
        subjects=[],
        notes=[],
        extras={},
        order=1,
    )
    yield a1
    yield EventAgendaMedia(
        agenda_item=a1,
        note="",
        date=start_date,
        offset=0,
        classification="",
        links=[],
    )
    yield EventAgendaItem(
        id=str(uuid.uuid4()),
        event=e,
        description="Agenda Item 2",
        classification="",
        subjects=[],
        notes=[],
        extras={},
        order=2,
    )
    if related_bill:
        yield EventRelatedEntity(
            agenda_item=a1,
            note="",
            name="SB 1",
            entity_type="bill",
        )
    if related_committee:
        yield EventRelatedEntity(
            agenda_item=a1, note="", name="Finance", entity_type="organization"
        )


def nebraska():
    j = Jurisdiction(
        id="ocd-jurisdiction/country:us/state:ne/government",
        name="Nebraska",
        url="https://nebraska.gov",
        classification="state",
        division_id="ocd-division/country:us/state:ne",
        latest_bill_update=datetime.datetime(2021, 8, 1),
        latest_people_update=datetime.datetime(2021, 8, 2),
    )
    runs = []
    runs_from = datetime.datetime(2020, 1, 1)
    for n in range(100):
        runs.append(
            RunPlan(
                jurisdiction=j,
                start_time=runs_from + datetime.timedelta(days=n),
                end_time=runs_from + datetime.timedelta(days=n, hours=3),
                success=n % 2 == 0,
            )
        )
    ls2020 = LegislativeSession(
        jurisdiction=j,
        identifier="2020",
        name="2020",
        start_date="2020-01-01",
        end_date="2020-12-31",
    )
    data_export = DataExport(
        session=ls2020,
        data_type="csv",
        created_at="2021-01-01",
        updated_at="2021-01-01",
        url="https://example.com",
    )
    ls2021 = LegislativeSession(
        jurisdiction=j, identifier="2021", name="2020", start_date="2021-01-01"
    )
    leg = Organization(
        id="nel",
        name="Nebraska Legislature",
        classification="legislature",
        jurisdiction=j,
    )
    bills = []
    for n in range(5):
        bills.extend(
            create_test_bill(
                ls2020,
                leg,
                sponsors=2,
                actions=5,
                versions=2,
                documents=3,
                sources=1,
                subjects=["sample"],
            )
        )
    for n in range(2):
        bills.extend(
            create_test_bill(
                ls2021,
                leg,
                subjects=["futurism"],
                classification=["resolution"],
                identifier=f"SB {n+1}",
            )
        )
    events = []
    for n in range(3):
        events.extend(
            create_test_event(
                j.id,
                n,
                start_date=f"2021-01-0{n+1}",
                related_bill=(n == 0),
                related_committee=(n > 1),
            )
        )
    events.extend(create_test_event(j.id, 4, start_date="2021-01-04", deleted=True))

    return [
        j,
        ls2020,
        data_export,
        ls2021,
        leg,
        *runs,
        *bills,
        *events,
        Organization(
            id="nee",
            name="Nebraska Executive",
            classification="executive",
            jurisdiction=j,
        ),
        Post(
            id="a",
            organization=leg,
            label="1",
            role="Senator",
            maximum_memberships=1,
            division_id="ocd-division/country:us/state:ne/sldu:1",
        ),
        Person(
            id=dummy_person_id("1"),
            name="Amy Adams",
            family_name="Amy",
            given_name="Adams",
            gender="female",
            email="aa@example.com",
            birth_date="2000-01-01",
            party="Democratic",
            current_role={
                "org_classification": "legislature",
                "district": 1,
                "title": "Senator",
                "division_id": "ocd-division/country:us/state:ne/sldu:1",
            },
            jurisdiction_id=j.id,
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
        ),
        PersonName(
            person_id=dummy_person_id("1"), name="Amy 'Aardvark' Adams", note="nickname"
        ),
        PersonLink(
            person_id=dummy_person_id("1"), url="https://example.com/amy", note=""
        ),
        PersonSource(
            person_id=dummy_person_id("1"), url="https://example.com/amy", note=""
        ),
        PersonOffice(
            person_id=dummy_person_id("1"),
            classification="capitol",
            voice="555-555-5555",
            fax="",
            address="123 Main St",
            name_="",
        ),
        Person(
            id=dummy_person_id("2"),
            name="Boo Berri",
            birth_date="1973-12-25",
            party="Libertarian",
            current_role={"org_classification": "executive", "title": "Governor"},
            jurisdiction_id=j.id,
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
        ),
        Person(
            id=dummy_person_id("3"),
            name="Rita Red",  # retired
            birth_date="1973-12-25",
            party="Republican",
            jurisdiction_id=j.id,
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
        ),
    ]


def ohio():
    j = Jurisdiction(
        id="ocd-jurisdiction/country:us/state:oh/government",
        name="Ohio",
        url="https://ohio.gov",
        classification="state",
        division_id="ocd-division/country:us/state:oh",
        latest_bill_update=datetime.datetime(2021, 8, 4),
        latest_people_update=datetime.datetime(2021, 8, 5),
    )
    ls2021 = LegislativeSession(jurisdiction=j, identifier="2021", name="2021")
    leg = Organization(
        id="ohl",
        name="Ohio Legislature",
        classification="legislature",
        jurisdiction=j,
    )
    upper = Organization(
        id="ohs",
        name="Ohio Senate",
        classification="upper",
        jurisdiction=j,
    )
    lower = Organization(
        id="ohh",
        name="Ohio House",
        classification="lower",
        jurisdiction=j,
    )
    house_education = Organization(
        id="ocd-organization/11112222-3333-4444-5555-666677778888",
        jurisdiction=j,
        parent_id="ohh",
        name="House Committee on Education",
        classification="committee",
        links=[{"url": "https://example.com/education-link", "note": ""}],
        sources=[{"url": "https://example.com/education-source", "note": ""}],
        extras={"example-room": "Room 84"},
    )
    senate_education = Organization(
        id="ocd-organization/11112222-3333-4444-5555-666677779999",
        jurisdiction=j,
        parent_id="ohs",
        name="Senate Committee on Education",
        classification="committee",
    )
    k5_sub = Organization(
        id="ocd-organization/11112222-3333-4444-5555-000000000000",
        jurisdiction=j,
        parent_id=house_education.id,
        name="K-5 Education Subcommittee",
        classification="subcommittee",
    )
    hb1 = Bill(
        id="ocd-bill/1234",
        identifier="HB 1",
        title="Alphabetization of OHIO Act",
        legislative_session=ls2021,
        from_organization=upper,
        subject=[],
        classification=["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date="2021-01-01",
    )
    # sb1 = Bill(
    #     id="ocd-bill/9999",
    #     identifier="SB 1",
    related_bill = RelatedBill(
        bill=hb1,
        identifier="SB 1",
        legislative_session="2021",
        relation_type="companion",
    )
    ruth = Person(
        id=dummy_person_id("9"),
        name="Ruth",
        party="Democratic",
        current_role={
            "org_classification": "upper",
            "district": 9,
            "title": "Senator",
            "division_id": "ocd-division/country:us/state:oh/sldu:9",
        },
    )
    marge = Person(
        id=dummy_person_id("7"),
        name="Marge",
        party="Democratic",
        current_role={
            "org_classification": "upper",
            "district": 7,
            "title": "Senator",
            "division_id": "ocd-division/country:us/state:oh/sldu:7",
        },
    )
    sp1 = BillSponsorship(
        bill=hb1,
        primary=True,
        classification="sponsor",
        name="Ruth",
        entity_type="person",
        person=ruth,
    )
    sp2 = BillSponsorship(
        bill=hb1,
        primary=True,
        classification="cosponsor",
        name="Marge",
        entity_type="person",
        person=marge,
    )
    btext = SearchableBill(
        bill=hb1,
        search_vector=func.to_tsvector(
            "This bill renames Ohio to HIOO, it is a good idea.", config="english"
        ),
    )
    v1 = VoteEvent(
        id="ocd-vote/1",
        bill=hb1,
        identifier="Vote on HB1",
        motion_text="Floor Vote",
        start_date="2021-01-01",
        result="passed",
        organization=lower,
    )
    v2 = VoteEvent(
        id="ocd-vote/2",
        bill=hb1,
        identifier="Vote on HB1",
        motion_text="Floor Vote",
        start_date="2021-02-01",
        result="passed",
        organization=upper,
    )
    com_mem1 = Membership(
        organization_id=senate_education.id,
        person_id=ruth.id,
        role="Chair",
        person_name="Ruth",
    )
    com_mem2 = Membership(
        organization_id=senate_education.id,
        person_id=marge.id,
        role="Member",
        person_name="Marge",
    )
    return [
        j,
        leg,
        upper,
        lower,
        ls2021,
        hb1,
        related_bill,
        ruth,
        marge,
        sp1,
        sp2,
        v1,
        v2,
        VoteCount(vote_event=v1, option="yes", value=2),
        VoteCount(vote_event=v1, option="no", value=1),
        PersonVote(vote_event=v1, option="yes", voter_name="Bart"),
        PersonVote(vote_event=v1, option="yes", voter_name="Harley"),
        PersonVote(vote_event=v1, option="no", voter_name="Jarvis"),
        VoteCount(vote_event=v2, option="yes", value=42),
        VoteCount(vote_event=v2, option="no", value=0),
        btext,
        Organization(
            id="ohe", name="Ohio Executive", classification="executive", jurisdiction=j
        ),
        house_education,
        senate_education,
        k5_sub,
        com_mem1,
        com_mem2,
        *create_test_bills_with_archived_versions(ls2021, leg),
    ]


def mentor():
    j = Jurisdiction(
        id="ocd-jurisdiction/country:us/state:oh/place:mentor",
        name="Mentor",
        url="https://mentoroh.gov",
        classification="municipality",
        division_id="ocd-division/country:us/state:oh/place:mentor",
        latest_bill_update=datetime.datetime(2021, 8, 1),
        latest_people_update=datetime.datetime(2021, 8, 2),
    )
    return [j]


def create_test_bills_with_archived_versions(session, chamber):
    """
    Bills exercising OPEN-13's raw_text lookup and its bill_changelog extension (ddp-infra
    fix, 2026-07-30): an archived bill (PDF+HTML, to prove PDF is preferred), an unarchived
    bill (raw_text must be omitted, not error), a bill whose *latest* version isn't archived
    even though an older version is (the older, immediately-previous version's raw_text must
    still surface -- but a third, even-older archived version must not), and a bill with two
    adjacent archived versions plus a precomputed diff_from_previous_version, exercising the
    full bill_changelog shape (latest's raw_text + diff, previous version's own raw_text).
    Attached to an existing jurisdiction/session/org (passed in) rather than a dedicated one,
    so as not to disturb jurisdiction-count assumptions elsewhere in the test suite.
    Identifiers are deliberately distinct from every other fixture bill's, since
    /bills?q=<bill id> matches globally, not scoped to one jurisdiction.
    """
    archived_bill = Bill(
        id="ocd-bill/archived-0001",
        identifier="HB 9101",
        title="An Act Relating to Scorpions",
        legislative_session=session,
        from_organization=chamber,
        subject=[],
        classification=["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date="2026-01-01",
    )
    archived_version = BillVersion(
        bill=archived_bill, note="Introduced", date="2026-01-01", classification=""
    )
    archived_pdf_link = BillVersionLink(
        version=archived_version,
        url="https://example.com/hb9101.pdf",
        media_type="application/pdf",
    )
    archived_html_link = BillVersionLink(
        version=archived_version,
        url="https://example.com/hb9101.html",
        media_type="text/html",
    )
    # SYNC-65: a real, explicit updated_at -- these two are the only BillVersionDocument
    # fixtures in this jurisdiction that set it, specifically so
    # test_bills_filter_by_document_updated_since has a real "archived recently" bill
    # (archived_bill) and a real "never archived" bill (unarchived_bill, no document at all)
    # to distinguish between.
    archived_pdf_doc = BillVersionDocument(
        bill=archived_bill,
        version_note="Introduced",
        version_date="2026-01-01",
        source_url="https://example.com/hb9101.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to scorpions; designating the scorpion as the state arachnid.",
        is_error=False,
        updated_at=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
    )
    archived_html_doc = BillVersionDocument(
        bill=archived_bill,
        version_note="Introduced",
        version_date="2026-01-01",
        source_url="https://example.com/hb9101.html",
        media_type="text/html",
        raw_text="<html>AN ACT relating to scorpions (HTML copy)</html>",
        is_error=False,
        updated_at=datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc),
    )

    unarchived_bill = Bill(
        id="ocd-bill/unarchived-0002",
        identifier="HB 9102",
        title="An Act Relating to Newts",
        legislative_session=session,
        from_organization=chamber,
        subject=[],
        classification=["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date="2026-01-01",
    )
    unarchived_version = BillVersion(
        bill=unarchived_bill, note="Introduced", date="2026-01-01", classification=""
    )
    unarchived_link = BillVersionLink(
        version=unarchived_version,
        url="https://example.com/hb9102.pdf",
        media_type="application/pdf",
    )

    stale_archive_bill = Bill(
        id="ocd-bill/stale-archive-0003",
        identifier="HB 9103",
        title="An Act Relating to Salamanders",
        legislative_session=session,
        from_organization=chamber,
        subject=[],
        classification=["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date="2026-02-01",
    )
    stale_archived_version = BillVersion(
        bill=stale_archive_bill, note="Introduced", date="2026-01-01", classification=""
    )
    stale_archived_link = BillVersionLink(
        version=stale_archived_version,
        url="https://example.com/hb9103-introduced.pdf",
        media_type="application/pdf",
    )
    stale_archived_doc = BillVersionDocument(
        bill=stale_archive_bill,
        version_note="Introduced",
        version_date="2026-01-01",
        source_url="https://example.com/hb9103-introduced.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to salamanders (introduced version).",
        is_error=False,
    )
    latest_unarchived_version = BillVersion(
        bill=stale_archive_bill,
        note="Committee Substitute",
        date="2026-02-01",
        classification="",
    )
    latest_unarchived_link = BillVersionLink(
        version=latest_unarchived_version,
        url="https://example.com/hb9103-cs.pdf",
        media_type="application/pdf",
    )
    # A third, even-older archived version -- proves the changelog lookup only ever surfaces
    # the two most recent versions (latest + immediately-previous), not a bill's full archived
    # history, even when older versions genuinely have their own archived text.
    filed_version = BillVersion(
        bill=stale_archive_bill, note="Filed", date="2025-12-01", classification=""
    )
    filed_link = BillVersionLink(
        version=filed_version,
        url="https://example.com/hb9103-filed.pdf",
        media_type="application/pdf",
    )
    filed_doc = BillVersionDocument(
        bill=stale_archive_bill,
        version_note="Filed",
        version_date="2025-12-01",
        source_url="https://example.com/hb9103-filed.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to salamanders (filed version).",
        is_error=False,
    )

    # Two adjacent archived versions, both real (unlike HB 9103's mix of one archived/one not)
    # -- exercises bill_changelog's actual use case: latest's raw_text + its precomputed
    # diff_from_previous_version, plus the prior version's own raw_text (needed as
    # dispatch_bill_changelog's old_bill_source), all surfaced together (ddp-infra's
    # bill_changelog diff-endpoint fix, 2026-07-30).
    changelog_bill = Bill(
        id="ocd-bill/changelog-0004",
        identifier="HB 9104",
        title="An Act Relating to Frogs",
        legislative_session=session,
        from_organization=chamber,
        subject=[],
        classification=["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date="2026-02-01",
    )
    changelog_prior_version = BillVersion(
        bill=changelog_bill, note="Introduced", date="2026-01-01", classification=""
    )
    changelog_prior_link = BillVersionLink(
        version=changelog_prior_version,
        url="https://example.com/hb9104-introduced.pdf",
        media_type="application/pdf",
    )
    changelog_prior_doc = BillVersionDocument(
        bill=changelog_bill,
        version_note="Introduced",
        version_date="2026-01-01",
        source_url="https://example.com/hb9104-introduced.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to frogs (introduced version).",
        is_error=False,
        # First version ever archived -- no prior text to diff against, matching real
        # archive_bill_versions() behavior.
        diff_from_previous_version=None,
    )
    changelog_latest_version = BillVersion(
        bill=changelog_bill, note="Engrossed", date="2026-02-01", classification=""
    )
    changelog_latest_link = BillVersionLink(
        version=changelog_latest_version,
        url="https://example.com/hb9104-engrossed.pdf",
        media_type="application/pdf",
    )
    changelog_latest_doc = BillVersionDocument(
        bill=changelog_bill,
        version_note="Engrossed",
        version_date="2026-02-01",
        source_url="https://example.com/hb9104-engrossed.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to frogs (engrossed version).",
        is_error=False,
        diff_from_previous_version=(
            "--- Introduced\n+++ Engrossed\n"
            "@@ -1 +1 @@\n"
            "-AN ACT relating to frogs (introduced version).\n"
            "+AN ACT relating to frogs (engrossed version).\n"
        ),
    )

    # OPEN-92 regression fixture: three undated versions (BillVersion.date is blank 100% of
    # the time for every non-US-federal jurisdiction audited under OPEN-34) whose real
    # chronological order (Introduced -> Committee Substitute -> Enrolled) disagrees with
    # plain alphabetical order ("Committee Substitute" < "Enrolled" < "Introduced"). A naive
    # (date, note) sort picks "Introduced" as latest and "Enrolled" as previous -- attaching
    # archived text to the wrong two versions and leaving the true previous version
    # ("Committee Substitute") with none at all. The correct, stage-aware ordering must pick
    # "Enrolled" as latest and "Committee Substitute" as previous, leaving "Introduced"
    # unattached. Using three versions (not two) is deliberate: with only two versions,
    # _attach_archived_document's identity-driven lookup (matched by bill+note+date+url, not
    # by which role it was called for) attaches each version's own pre-baked document to
    # itself regardless of mislabeled latest/previous roles, so a naive-vs-correct sort bug
    # can silently pass a 2-version fixture's content assertions -- this was confirmed by
    # reverting to the pre-fix sort and finding the original 2-version test still passed. The
    # fixture rows are also inserted out of chronological order (Enrolled, then Introduced,
    # then Committee Substitute) so the response's array order can't coincidentally match the
    # correct order via insertion order alone if the stage-aware sort/reorder isn't applied.
    stage_divergence_bill = Bill(
        id="ocd-bill/stage-divergence-0005",
        identifier="HB 9105",
        title="An Act Relating to Toads",
        legislative_session=session,
        from_organization=chamber,
        subject=[],
        classification=["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date="2026-01-01",
    )
    # Inserted first despite being the true LATEST version -- see the fixture-ordering note
    # above.
    stage_divergence_enrolled_version = BillVersion(
        bill=stage_divergence_bill, note="Enrolled", date="", classification=""
    )
    stage_divergence_enrolled_link = BillVersionLink(
        version=stage_divergence_enrolled_version,
        url="https://example.com/hb9105-enrolled.pdf",
        media_type="application/pdf",
    )
    stage_divergence_enrolled_doc = BillVersionDocument(
        bill=stage_divergence_bill,
        version_note="Enrolled",
        version_date="",
        source_url="https://example.com/hb9105-enrolled.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to toads (enrolled version).",
        is_error=False,
        diff_from_previous_version=(
            "--- Committee Substitute\n+++ Enrolled\n"
            "@@ -1 +1 @@\n"
            "-AN ACT relating to toads (committee substitute version).\n"
            "+AN ACT relating to toads (enrolled version).\n"
        ),
    )
    # Inserted second despite being the true EARLIEST version.
    stage_divergence_introduced_version = BillVersion(
        bill=stage_divergence_bill, note="Introduced", date="", classification=""
    )
    stage_divergence_introduced_link = BillVersionLink(
        version=stage_divergence_introduced_version,
        url="https://example.com/hb9105-introduced.pdf",
        media_type="application/pdf",
    )
    stage_divergence_introduced_doc = BillVersionDocument(
        bill=stage_divergence_bill,
        version_note="Introduced",
        version_date="",
        source_url="https://example.com/hb9105-introduced.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to toads (introduced version).",
        is_error=False,
        diff_from_previous_version=None,
    )
    # Inserted last despite being the true PREVIOUS (middle) version -- the naive alphabetical
    # sort ("Committee Substitute" < "Enrolled" < "Introduced") never selects this one as
    # either latest or previous, so under the pre-fix code it gets no archived text attached
    # at all.
    stage_divergence_committee_sub_version = BillVersion(
        bill=stage_divergence_bill, note="Committee Substitute", date="", classification=""
    )
    stage_divergence_committee_sub_link = BillVersionLink(
        version=stage_divergence_committee_sub_version,
        url="https://example.com/hb9105-committee-substitute.pdf",
        media_type="application/pdf",
    )
    stage_divergence_committee_sub_doc = BillVersionDocument(
        bill=stage_divergence_bill,
        version_note="Committee Substitute",
        version_date="",
        source_url="https://example.com/hb9105-committee-substitute.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to toads (committee substitute version).",
        is_error=False,
        diff_from_previous_version=(
            "--- Introduced\n+++ Committee Substitute\n"
            "@@ -1 +1 @@\n"
            "-AN ACT relating to toads (introduced version).\n"
            "+AN ACT relating to toads (committee substitute version).\n"
        ),
    )

    # OPEN-118 regression fixture: an unclassifiable-note version sits chronologically
    # between two classifiable, archived versions. Real archive_bill_versions() never
    # updates or reads `prior_text` for an unrecognized-stage version (see text_extract.py's
    # archive_bill_versions() docstring) -- it's fully skipped in the lineage walk, so the
    # next classifiable version's diff_from_previous_version is precomputed against the last
    # *classifiable* version's text, never the unclassifiable one. This fixture's precomputed
    # diff_from_previous_version values encode that same skip (Enrolled's diff references
    # "Introduced", not the unclassifiable version in between), matching what the real
    # pipeline would have written. The API itself never recomputes a diff -- it only decides
    # which archived documents to attach -- so this proves postprocess_includes (a) still
    # excludes the unclassifiable version from attachment entirely (OPEN-118 acceptance
    # criterion) and (b) doesn't disrupt its classifiable neighbors' own already-correct
    # attachments.
    unclassifiable_bill = Bill(
        id="ocd-bill/unclassifiable-0006",
        identifier="HB 9106",
        title="An Act Relating to Newts (Redux)",
        legislative_session=session,
        from_organization=chamber,
        subject=[],
        classification=["bill"],
        extras={},
        created_at=datetime.datetime.utcnow(),
        updated_at=datetime.datetime.utcnow(),
        latest_action_date="2026-03-01",
    )
    unclassifiable_introduced_version = BillVersion(
        bill=unclassifiable_bill, note="Introduced", date="2026-01-01", classification=""
    )
    unclassifiable_introduced_link = BillVersionLink(
        version=unclassifiable_introduced_version,
        url="https://example.com/hb9106-introduced.pdf",
        media_type="application/pdf",
    )
    unclassifiable_introduced_doc = BillVersionDocument(
        bill=unclassifiable_bill,
        version_note="Introduced",
        version_date="2026-01-01",
        source_url="https://example.com/hb9106-introduced.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to newts (introduced version).",
        is_error=False,
        diff_from_previous_version=None,
    )
    # note_stage() matches none of version_ordering.py's known patterns -- the same example
    # openstates-core's own text_extract.py tests use for an unrecognized-stage note.
    unclassifiable_middle_version = BillVersion(
        bill=unclassifiable_bill,
        note="Some Never-Before-Seen Document Type",
        date="2026-02-01",
        classification="",
    )
    unclassifiable_middle_link = BillVersionLink(
        version=unclassifiable_middle_version,
        url="https://example.com/hb9106-mystery.pdf",
        media_type="application/pdf",
    )
    unclassifiable_middle_doc = BillVersionDocument(
        bill=unclassifiable_bill,
        version_note="Some Never-Before-Seen Document Type",
        version_date="2026-02-01",
        source_url="https://example.com/hb9106-mystery.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to newts (mystery version).",
        is_error=False,
        diff_from_previous_version=None,
    )
    unclassifiable_enrolled_version = BillVersion(
        bill=unclassifiable_bill, note="Enrolled", date="2026-03-01", classification=""
    )
    unclassifiable_enrolled_link = BillVersionLink(
        version=unclassifiable_enrolled_version,
        url="https://example.com/hb9106-enrolled.pdf",
        media_type="application/pdf",
    )
    unclassifiable_enrolled_doc = BillVersionDocument(
        bill=unclassifiable_bill,
        version_note="Enrolled",
        version_date="2026-03-01",
        source_url="https://example.com/hb9106-enrolled.pdf",
        media_type="application/pdf",
        raw_text="AN ACT relating to newts (enrolled version).",
        is_error=False,
        # Diffed against "Introduced", skipping the unclassifiable middle version entirely --
        # matching real archive_bill_versions() lineage-walk behavior.
        diff_from_previous_version=(
            "--- Introduced\n+++ Enrolled\n"
            "@@ -1 +1 @@\n"
            "-AN ACT relating to newts (introduced version).\n"
            "+AN ACT relating to newts (enrolled version).\n"
        ),
    )

    return [
        archived_bill,
        archived_version,
        archived_pdf_link,
        archived_html_link,
        archived_pdf_doc,
        archived_html_doc,
        unarchived_bill,
        unarchived_version,
        unarchived_link,
        stale_archive_bill,
        stale_archived_version,
        stale_archived_link,
        stale_archived_doc,
        latest_unarchived_version,
        latest_unarchived_link,
        filed_version,
        filed_link,
        filed_doc,
        changelog_bill,
        changelog_prior_version,
        changelog_prior_link,
        changelog_prior_doc,
        changelog_latest_version,
        changelog_latest_link,
        changelog_latest_doc,
        stage_divergence_bill,
        stage_divergence_enrolled_version,
        stage_divergence_enrolled_link,
        stage_divergence_enrolled_doc,
        stage_divergence_introduced_version,
        stage_divergence_introduced_link,
        stage_divergence_introduced_doc,
        stage_divergence_committee_sub_version,
        stage_divergence_committee_sub_link,
        stage_divergence_committee_sub_doc,
        unclassifiable_bill,
        unclassifiable_introduced_version,
        unclassifiable_introduced_link,
        unclassifiable_introduced_doc,
        unclassifiable_middle_version,
        unclassifiable_middle_link,
        unclassifiable_middle_doc,
        unclassifiable_enrolled_version,
        unclassifiable_enrolled_link,
        unclassifiable_enrolled_doc,
    ]
