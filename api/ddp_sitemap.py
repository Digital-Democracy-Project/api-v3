"""DDP sitemap listing (OPEN-715, for BROKER-558 / NEXT-283). Mounted at /ddp/sitemap/*; ddp-api's
/openstates/* catch-all proxy forwards these with no ddp-api change.

For one jurisdiction, GET /bills lists every bill (with or without documents or votes) with what the
website's sitemap needs: its id, session, number, title, last action date and newest vote date. It
pages by bill id exactly like /ddp/embedding/ledger. Two queries per page whatever the page size:
the bills, then one grouped query over the page's vote events. Read-only.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import apikey_auth
from .bill_types import EXCLUDE_INTRODUCED_DRAFTS_HELP, is_not_introduced_draft_clause
from .db import get_db, models
from .ddp_search import _jurisdiction_ids

router = APIRouter(prefix="/ddp/sitemap", tags=["ddp-sitemap"])

DEFAULT_LIMIT = 1000
MAX_LIMIT = 1000


class SitemapBill(BaseModel):
    ocd_bill_id: str = Field(
        ..., description="Bare uuid, without the ocd-bill/ prefix."
    )
    session: str = Field(..., example="2026")
    identifier: str = Field(..., example="HB 433")
    title: str
    latest_action_date: Optional[str] = Field(
        None, description="As stored (free text: a date or a full timestamp)."
    )
    latest_vote_date: Optional[str] = Field(
        None,
        description="The newest start_date of the bill's vote events, as stored; null when it has none.",
    )
    has_votes: bool


class SitemapBillPage(BaseModel):
    results: List[SitemapBill]
    next_after: Optional[str] = Field(
        None,
        description=(
            "Pass as `after` to read the next page; null exactly when there are no more bills "
            "(one extra bill is read to know, so a jurisdiction whose bill count is a multiple of "
            "`limit` does not need an empty last request). The full ocd-bill/ id of the page's last bill."
        ),
    )


@router.get("/bills", response_model=SitemapBillPage)
def sitemap_bills(
    jurisdiction: str = Query(
        ..., description="Jurisdiction abbreviation, e.g. ut or us."
    ),
    after: Optional[str] = Query(
        None,
        description="Keyset cursor: the previous page's next_after (an ocd-bill id).",
    ),
    limit: int = Query(
        DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Bills per page."
    ),
    exclude_introduced_drafts: bool = Query(
        False, description=EXCLUDE_INTRODUCED_DRAFTS_HELP
    ),
    auth: str = Depends(apikey_auth),
    db: Session = Depends(get_db),
):
    """Every bill of a jurisdiction, keyset-paged by bill id, with its newest vote date."""
    jids = _jurisdiction_ids([jurisdiction])
    query = (
        db.query(
            models.Bill.id,
            models.Bill.identifier,
            models.Bill.title,
            models.Bill.latest_action_date,
            models.LegislativeSession.identifier.label("session"),
        )
        .join(
            models.LegislativeSession,
            models.Bill.legislative_session_id == models.LegislativeSession.id,
        )
        .filter(models.LegislativeSession.jurisdiction_id.in_(jids))
        .order_by(models.Bill.id)
    )
    if exclude_introduced_drafts:
        query = query.filter(is_not_introduced_draft_clause(models))
    if after:
        query = query.filter(models.Bill.id > after)
    bills = query.limit(
        limit + 1
    ).all()  # one extra tells us whether this is the last page
    more = len(bills) > limit
    bills = bills[:limit]

    vote_dates = {}
    if bills:
        # one grouped query for the whole page, never one per bill
        vote_dates = dict(
            db.query(models.VoteEvent.bill_id, func.max(models.VoteEvent.start_date))
            .filter(models.VoteEvent.bill_id.in_([b.id for b in bills]))
            .group_by(models.VoteEvent.bill_id)
            .all()
        )

    results = [
        SitemapBill(
            ocd_bill_id=b.id.removeprefix("ocd-bill/"),
            session=b.session,
            identifier=b.identifier,
            title=b.title,
            latest_action_date=b.latest_action_date,
            latest_vote_date=vote_dates.get(b.id),
            has_votes=b.id in vote_dates,
        )
        for b in bills
    ]
    return SitemapBillPage(results=results, next_after=bills[-1].id if more else None)
