import re
import datetime
from typing import Optional, List
from enum import Enum
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import func, desc, nullslast
from sqlalchemy.orm import contains_eager, object_session
from openstates.utils.transformers import fix_bill_id
from .db import SessionLocal, get_db, models
from .schemas import Bill
from .pagination import Pagination
from .auth import apikey_auth
from .utils import jurisdiction_filter
from .version_ordering import STAGE_UNKNOWN, note_stage, version_sort_key


class BillInclude(str, Enum):
    sponsorships = "sponsorships"
    abstracts = "abstracts"
    other_titles = "other_titles"
    other_identifiers = "other_identifiers"
    actions = "actions"
    sources = "sources"
    documents = "documents"
    versions = "versions"
    votes = "votes"
    related_bills = "related_bills"


class BillSortOption(str, Enum):
    updated_asc = "updated_asc"
    updated_desc = "updated_desc"
    first_action_asc = "first_action_asc"
    first_action_desc = "first_action_desc"
    latest_action_asc = "latest_action_asc"
    latest_action_desc = "latest_action_desc"


class BillPagination(Pagination):
    ObjCls = Bill
    IncludeEnum = BillInclude
    include_map_overrides = {
        BillInclude.sponsorships: ["sponsorships", "sponsorships.person"],
        BillInclude.versions: ["versions", "versions.links"],
        BillInclude.documents: ["documents", "documents.links"],
        BillInclude.votes: [
            "votes",
            "votes.votes",
            "votes.counts",
            "votes.sources",
            "votes.votes.voter",
        ],
        BillInclude.actions: ["actions", "actions.related_entities"],
    }
    max_per_page = 20

    @classmethod
    def _attach_archived_document(cls, db, data, obj, version):
        """Attach archived raw_text (OPEN-13) -- and, since PLAN-bill-document-provenance.md
        Phase 8's bill_changelog work, diff_from_previous_version -- to `version`'s preferred
        link/version object. BillVersionDocument isn't FK-linked to BillVersion (see its
        docstring in db/models/bills.py), so this matches by content instead: bill + version
        note/date + link url, same natural key openstates-core's archive_bill_versions() writes.

        Returns the chosen BillVersionDocument row (so callers needing its raw_text for
        something other than the response body, e.g. bill_changelog's old_bill_source, don't
        have to re-query), or None if nothing archived matches.
        """
        if not version.links:
            return None

        urls = [link.url for link in version.links]
        archived = (
            db.query(models.BillVersionDocument)
            .filter(
                models.BillVersionDocument.bill_id == data.id,
                models.BillVersionDocument.version_note == version.note,
                models.BillVersionDocument.version_date == version.date,
                models.BillVersionDocument.source_url.in_(urls),
                models.BillVersionDocument.is_error.is_(False),
            )
            .all()
        )
        by_media_type = {row.media_type: row for row in archived if row.raw_text}
        if not by_media_type:
            return None
        # Same PDF-over-HTML priority archive_bill_versions() uses for diff lineage.
        chosen = by_media_type.get("application/pdf") or next(
            iter(by_media_type.values())
        )

        version_obj = obj.versions[list(data.versions).index(version)]
        version_obj.diff_from_previous_version = chosen.diff_from_previous_version
        for link_index, link_row in enumerate(version.links):
            if link_row.url == chosen.source_url:
                version_obj.links[link_index].raw_text = chosen.raw_text
                break
        return chosen

    @classmethod
    def postprocess_includes(cls, obj, data, includes, *, detail=False):
        """
        Attach archived raw_text (OPEN-13) and diff_from_previous_version to every
        classifiable version's preferred link (OPEN-118; originally just the latest version
        and the one immediately before it, per PLAN-bill-document-provenance.md Phase 8's
        bill_changelog work, ddp-infra "excellent news" fix 2026-07-30) -- single-bill detail
        queries only, a paginated /bills list never gets full document text, to keep response
        size bounded.
        """
        if not detail or BillInclude.versions not in includes or not data.versions:
            return

        db = object_session(data)

        # OPEN-92: "latest"/"previous" must be resolved via openstates-core's own audited,
        # content-based stage classifier (version_sort_key, OPEN-34) -- not a naive (date,
        # note) alphabetical sort. A naive sort gets this wrong for most jurisdictions:
        # BillVersion.date is blank 100% of the time outside US federal, so it degrades to a
        # pure alphabetical note-string sort, which has no relationship to real chronology
        # (e.g. "Enrolled" < "Introduced" alphabetically, but Enrolled is the later stage).
        # version_ordering.py here is a deliberate, explicitly-synced copy of
        # openstates-core's own implementation -- see that module's own docstring for why
        # this isn't a real import (yet).
        #
        # A version whose note doesn't match any known stage (STAGE_UNKNOWN) is excluded from
        # latest/previous selection entirely, matching openstates-core's own
        # archive_bill_versions()/text_extract.py posture: a version this classifier can't
        # confidently place is never guessed into the diff lineage.
        classifiable = [
            v for v in data.versions if note_stage(v.note)[0] != STAGE_UNKNOWN
        ]
        if not classifiable:
            return
        ordered = sorted(classifiable, key=lambda v: version_sort_key(v.note, v.date))

        # OPEN-118: every classifiable version gets its own archived raw_text and
        # diff_from_previous_version resolved here, not just the latest version and the one
        # immediately before it -- NEXT-23 (via BROKER-101's /api/bill-versions/history/
        # endpoint) lets a reader pick any version transition, so a diff must be resolvable
        # for every hop, not only the last one. Both fields are already computed and archived
        # per-version by archive_bill_versions(), never re-derived here; a version whose stage
        # note is unclassifiable (excluded from `classifiable` above) still never gets either
        # field, same as before this change. Response size stays single-bill-detail-bound --
        # this widens the bound from "2 versions" to "this bill's own version count", it
        # doesn't remove the bound (the paginated /bills list is untouched, see below).
        for version in ordered:
            cls._attach_archived_document(db, data, obj, version)

        # SYNC-16: ddp-sync's local_openstates_client.py used to re-derive "latest"/
        # "previous" itself via the same naive (date, note) sort this fix just removed --
        # the whole point of OPEN-90 is that no downstream consumer should ever need to
        # re-derive this ordering again. Reorder obj.versions in place (unknown-stage
        # versions first, in their original relative order, then the classifiable ones in
        # correct chronological order) so the JSON response's own `versions` array always
        # ends with [..., previous, latest] by plain array position -- a caller can take
        # versions[-1]/versions[-2] directly, no re-sort of its own required. This changes
        # array order only, never which/how many versions are returned.
        data_versions = list(data.versions)
        unknown_stage_indexes = [
            i for i, v in enumerate(data_versions) if note_stage(v.note)[0] == STAGE_UNKNOWN
        ]
        ordered_indexes = [data_versions.index(v) for v in ordered]
        obj.versions = [obj.versions[i] for i in unknown_stage_indexes] + [
            obj.versions[i] for i in ordered_indexes
        ]


router = APIRouter()


_likely_bill_id = re.compile(r"\w{1,3}\s*\d{1,5}")


def base_query(db):
    return (
        db.query(models.Bill)
        .join(models.Bill.legislative_session)
        .join(models.LegislativeSession.jurisdiction)
        .join(models.Bill.from_organization)
        .options(
            contains_eager(
                models.Bill.legislative_session, models.LegislativeSession.jurisdiction
            )
        )
        .options(contains_eager(models.Bill.from_organization))
    )


@router.get(
    "/bills",
    response_model=BillPagination.response_model(),
    response_model_exclude_none=True,
    tags=["bills"],
)
async def bills_search(
    jurisdiction: Optional[str] = Query(
        None, description="Filter by jurisdiction name or ID."
    ),
    session: Optional[str] = Query(None, description="Filter by session identifier."),
    chamber: Optional[str] = Query(
        None, description="Filter by chamber of origination."
    ),
    identifier: Optional[List[str]] = Query(
        [],
        description="Filter to only include bills with this identifier.",
    ),
    classification: Optional[str] = Query(
        None, description="Filter by classification, e.g. bill or resolution"
    ),
    subject: Optional[List[str]] = Query(
        [], description="Filter by one or more subjects."
    ),
    updated_since: Optional[str] = Query(
        None,
        description="Filter to only include bills with updates since a given date.",
    ),
    created_since: Optional[str] = Query(
        None, description="Filter to only include bills created since a given date."
    ),
    action_since: Optional[str] = Query(
        None,
        description="Filter to only include bills with an action since a given date.",
    ),
    sort: Optional[BillSortOption] = Query(
        BillSortOption.updated_desc, description="Desired sort order for bill results."
    ),
    sponsor: Optional[str] = Query(
        None,
        description="Filter to only include bills sponsored by a given name or person ID.",
    ),
    sponsor_classification: Optional[str] = Query(
        None,
        description="Filter matched sponsors to only include particular types of sponsorships.",
    ),
    q: Optional[str] = Query(None, description="Filter by full text search term."),
    include: List[BillInclude] = Query(
        [], description="Additional information to include in response."
    ),
    db: SessionLocal = Depends(get_db),
    pagination: BillPagination = Depends(),
    auth: str = Depends(apikey_auth),
):
    """
    Search for bills matching given criteria.

    Must either specify a jurisdiction or a full text query (q).  Additional parameters will
    futher restrict bills returned.
    """
    query = base_query(db)

    if sort == BillSortOption.updated_asc:
        query = query.order_by(models.Bill.updated_at)
    elif sort == BillSortOption.updated_desc:
        query = query.order_by(desc(models.Bill.updated_at))
    elif sort == BillSortOption.first_action_asc:
        query = query.order_by(nullslast(models.Bill.first_action_date))
    elif sort == BillSortOption.first_action_desc:
        query = query.order_by(nullslast(desc(models.Bill.first_action_date)))
    elif sort == BillSortOption.latest_action_asc:
        query = query.order_by(nullslast(models.Bill.latest_action_date))
    elif sort == BillSortOption.latest_action_desc:
        query = query.order_by(nullslast(desc(models.Bill.latest_action_date)))
    else:
        raise HTTPException(500, "Unknown sort option, this shouldn't happen!")

    if jurisdiction:
        query = query.filter(
            jurisdiction_filter(
                jurisdiction, jid_field=models.LegislativeSession.jurisdiction_id
            )
        )
    if session:
        if not jurisdiction:
            raise HTTPException(
                400, "filtering by session requires a jurisdiction parameter as well"
            )
        query = query.filter(models.LegislativeSession.identifier == session)
    if chamber:
        query = query.filter(models.Organization.classification == chamber)
    if identifier:
        if len(identifier) > 20:
            raise HTTPException(
                400,
                "can only provide up to 20 identifiers in one request",
            )
        identifiers = [fix_bill_id(bill_id).upper() for bill_id in identifier]
        query = query.filter(models.Bill.identifier.in_(identifiers))
    if classification:
        query = query.filter(models.Bill.classification.any(classification))
    if subject:
        query = query.filter(models.Bill.subject.contains(subject))
    if sponsor:
        # need to join this way, or sqlalchemy will try to join via from_organization
        query = query.join(models.Bill.sponsorships)
        if sponsor.startswith("ocd-person/"):
            query = query.filter(models.BillSponsorship.person_id == sponsor)
        else:
            query = query.filter(models.BillSponsorship.name == sponsor)
    if sponsor_classification:
        if not sponsor:
            raise HTTPException(
                400,
                "filtering by sponsor_classification requires sponsor parameter as well",
            )
        query = query.filter(
            models.BillSponsorship.classification == sponsor_classification
        )
    try:
        if updated_since:
            query = query.filter(
                models.Bill.updated_at >= datetime.datetime.fromisoformat(updated_since)
            )
        if created_since:
            query = query.filter(
                models.Bill.created_at >= datetime.datetime.fromisoformat(created_since)
            )
    except ValueError:
        raise HTTPException(
            400,
            "datetime must be in ISO-8601 format, try YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS",
        )

    if action_since:
        query = query.filter(models.Bill.latest_action_date >= action_since)
    if q:
        if _likely_bill_id.match(q):
            query = query.filter(
                func.upper(models.Bill.identifier) == fix_bill_id(q).upper()
            )
        else:
            query = query.join(models.SearchableBill).filter(
                models.SearchableBill.search_vector.op("@@")(
                    func.websearch_to_tsquery("english", q)
                )
            )

    if not q and not jurisdiction:
        raise HTTPException(400, "either 'jurisdiction' or 'q' required")

    # handle includes

    resp = pagination.paginate(query, includes=include)

    return resp


@router.get(
    # we have to use the Starlette path type to allow slashes here
    "/bills/ocd-bill/{openstates_bill_id}",
    response_model=Bill,
    response_model_exclude_none=True,
    tags=["bills"],
)
async def bill_detail_by_id(
    openstates_bill_id: str,
    include: List[BillInclude] = Query([]),
    db: SessionLocal = Depends(get_db),
    auth: str = Depends(apikey_auth),
):
    """Obtain bill information by internal ID in the format ocd-bill/*uuid*."""
    query = base_query(db).filter(models.Bill.id == "ocd-bill/" + openstates_bill_id)
    return BillPagination.detail(query, includes=include)


@router.get(
    # we have to use the Starlette path type to allow slashes here
    "/bills/{jurisdiction}/{session}/{bill_id}",
    response_model=Bill,
    response_model_exclude_none=True,
    tags=["bills"],
)
async def bill_detail(
    jurisdiction: str,
    session: str,
    bill_id: str,
    include: List[BillInclude] = Query([]),
    db: SessionLocal = Depends(get_db),
    auth: str = Depends(apikey_auth),
):
    """Obtain bill information based on (state, session, bill_id)."""
    query = base_query(db).filter(
        models.Bill.identifier == fix_bill_id(bill_id).upper(),
        models.LegislativeSession.identifier == session,
        jurisdiction_filter(
            jurisdiction, jid_field=models.LegislativeSession.jurisdiction_id
        ),
    )
    return BillPagination.detail(query, includes=include)
