"""DDP knowledge-base embedding ledger (SYNC-95 / OPEN-319). Mounted at /ddp/embedding/*; ddp-api's
/openstates/* catch-all proxy forwards these with no ddp-api change.

For one jurisdiction, GET /ledger lists, per bill, the archived documents the embedder would write
(one per version that has a usable archived row), so ddp-sync can compare what SHOULD be embedded
with what IS, per document, without reading every bill's 150 to 380 KB detail payload.

It cannot disagree with the bill detail: each row is chosen by the same picker the detail endpoint
uses (`BillPagination._archived_row_for`: XML first, then PDF, then any other format with text,
lowest id on a tie; OPEN-317), applied to the same bill versions. Read-only.
"""
from typing import List, Optional

import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload

from . import bills as bills_module
from .auth import apikey_auth
from .db import get_db, models
from .ddp_search import _jurisdiction_ids

router = APIRouter(prefix="/ddp/embedding", tags=["ddp-embedding"])

DEFAULT_LIMIT = 200
MAX_LIMIT = 500  # bills per page; each bill costs one picker query per version


class LedgerDocument(BaseModel):
    archived_document_id: int = Field(..., example=62651)
    updated_at: Optional[datetime.datetime] = Field(
        None,
        description=(
            "updated_at of the ddp_bill_version_document row, the column "
            "/bills?document_updated_since= filters on: the per-document change signal."
        ),
    )


class LedgerBill(BaseModel):
    ocd_bill_id: str = Field(
        ..., description="Bare uuid, without the ocd-bill/ prefix."
    )
    session: str = Field(..., example="2026")
    documents: List[LedgerDocument]


class LedgerPage(BaseModel):
    results: List[LedgerBill]
    next_after: Optional[str] = Field(
        None,
        description=(
            "Pass as `after` to read the next page; null on the last page. It is a position in "
            "the jurisdiction's bills, not a count of results: a page can return fewer bills "
            "than `limit` (bills with no archived document are left out) and still have more."
        ),
    )


@router.get("/ledger", response_model=LedgerPage)
def ledger(
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
    auth: str = Depends(apikey_auth),
    db: Session = Depends(get_db),
):
    """The documents the knowledge-base embedder would write for each of a jurisdiction's bills,
    keyset-paged by bill id. Only bills with at least one archived document appear (the same
    bills `/bills?document_updated_since=` can return)."""
    jids = _jurisdiction_ids([jurisdiction])
    query = (
        db.query(models.Bill)
        .join(
            models.LegislativeSession,
            models.Bill.legislative_session_id == models.LegislativeSession.id,
        )
        .filter(models.LegislativeSession.jurisdiction_id.in_(jids))
        .options(
            selectinload(models.Bill.versions).selectinload(models.BillVersion.links),
            selectinload(models.Bill.legislative_session),
        )
        .order_by(models.Bill.id)
    )
    if after:
        query = query.filter(models.Bill.id > after)
    bills = query.limit(limit).all()

    results = []
    for bill in bills:
        documents = {}
        for version in bill.versions:
            row = bills_module.BillPagination._archived_row_for(db, bill, version)
            if row is not None:  # two versions can resolve to one row: list it once
                documents.setdefault(
                    row.id,
                    LedgerDocument(
                        archived_document_id=row.id, updated_at=row.updated_at
                    ),
                )
        if documents:
            results.append(
                LedgerBill(
                    ocd_bill_id=bill.id.removeprefix("ocd-bill/"),
                    session=bill.legislative_session.identifier,
                    documents=sorted(
                        documents.values(), key=lambda d: d.archived_document_id
                    ),
                )
            )
    return LedgerPage(
        results=results, next_after=bills[-1].id if len(bills) == limit else None
    )
