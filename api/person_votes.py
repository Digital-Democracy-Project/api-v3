"""
GET /people/{person_id}/votes: the votes one person cast, newest first (OPEN-481).

A DDP addition (like current_session.py and ddp_search.py): upstream api-v3 serves votes only
inside a bill (GET /bills/...?include=votes), so showing "how this legislator voted" meant
fetching every bill they could have voted on. This reads opencivicdata_personvote directly,
through the existing voter_id index; no new table, index or setting.

Completeness is bounded by attribution quality. A vote whose voter_id is NULL (a name the
importer could not match to a legislator) is not returned for anyone, and nothing here matches
on voter_name to fill the gap (the same-surname misattribution of OPEN-141 is why).
"""
from enum import Enum
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, create_model
from sqlalchemy import func
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from .auth import apikey_auth
from .db import SessionLocal, get_db, models
from .pagination import Pagination, PaginationMeta
from .schemas import CompactJurisdiction, Organization, VoteCount
from .utils import jurisdiction_filter

# shown even when zero, so a page can print "0 no" without guessing which keys exist
STANDARD_OPTIONS = ("yes", "no", "absent", "abstain")


class PersonVoteBill(BaseModel):
    id: str = Field(..., example="ocd-bill/12345678-0000-1111-2222-333344445555")
    identifier: str = Field(..., example="HB 12")
    title: str
    session: str = Field(..., example="2025")
    jurisdiction: CompactJurisdiction

    class Config:
        orm_mode = True


class PersonVoteEvent(BaseModel):
    id: str = Field(..., example="ocd-vote/12345678-0000-1111-2222-333344445555")
    start_date: Optional[str] = Field(None, example="2025-03-04")
    motion_text: Optional[str] = Field(None, example="Shall the bill be passed?")
    motion_classification: List[str] = Field([], example=["passage"])
    result: Optional[str] = Field(None, example="pass")
    identifier: Optional[str] = Field(None, example="HV #3312")
    organization: Optional[Organization]
    counts: List[VoteCount]

    class Config:
        orm_mode = True


class PersonVoteRow(BaseModel):
    id: UUID
    option: str = Field(..., example="yes")
    voter_name: Optional[str] = Field(None, example="Adams")
    vote_event: PersonVoteEvent
    bill: Optional[PersonVoteBill] = None  # None: a vote event with no bill


class PersonVoteSummary(BaseModel):
    counts: Dict[str, int] = Field(
        ..., example={"yes": 459, "no": 9, "absent": 9, "abstain": 4}
    )


class PersonVotePagination(Pagination):
    ObjCls = PersonVoteRow
    IncludeEnum = Enum("NoInclude", {})  # this route has no include=
    include_map_overrides = {}
    max_per_page = 50

    @classmethod
    def select_or_noload(cls, query, includes):
        return query

    @classmethod
    def to_obj_with_includes(cls, data, includes, *, detail=False):
        event = data.vote_event
        return PersonVoteRow(
            id=data.id,
            option=data.option,
            voter_name=data.voter_name,
            vote_event=PersonVoteEvent.from_orm(event),
            bill=PersonVoteBill.from_orm(event.bill) if event.bill else None,
        )


PersonVoteList = create_model(
    "PersonVoteList",
    results=(List[PersonVoteRow], ...),
    pagination=(PaginationMeta, ...),
    summary=(Optional[PersonVoteSummary], None),
)

router = APIRouter()


def _votes_of(db, person_id, jurisdiction, session):
    """PersonVote rows of one person, joined so the optional filters can apply."""
    query = (
        db.query(models.PersonVote)
        .join(models.PersonVote.vote_event)
        .filter(models.PersonVote.voter_id == person_id)
    )
    if jurisdiction or session:
        query = query.join(
            models.LegislativeSession,
            models.VoteEvent.legislative_session_id == models.LegislativeSession.id,
        )
    if jurisdiction:
        query = query.join(
            models.Jurisdiction,
            models.LegislativeSession.jurisdiction_id == models.Jurisdiction.id,
        ).filter(
            jurisdiction_filter(
                jurisdiction, jid_field=models.LegislativeSession.jurisdiction_id
            )
        )
    if session:
        query = query.filter(models.LegislativeSession.identifier == session)
    return query


@router.get(
    "/people/{person_id:path}/votes",
    response_model=PersonVoteList,
    response_model_exclude_none=True,
    tags=["people"],
)
async def person_votes(
    person_id: str,
    jurisdiction: Optional[str] = Query(
        None,
        description="Only votes in this jurisdiction (name, abbreviation or id). "
        "Without it, a person who served in more than one jurisdiction gets all of them.",
    ),
    session: Optional[str] = Query(
        None, description="Only votes in this legislative session identifier."
    ),
    db: SessionLocal = Depends(get_db),
    pagination: PersonVotePagination = Depends(),
    auth: str = Depends(apikey_auth),
):
    """
    The votes one person cast, newest first (ties broken by vote event id, so pages never
    shuffle). `person_id` is the person's full id, `ocd-person/<uuid>`.

    Each row has the person's own `option` and the name as printed, the vote event with its
    totals, and a compact `bill` (null for a vote that is not on a bill). The first page also
    carries `summary.counts`, the person's votes by option over every page.

    An unknown person is a 404. A known person with no votes is an empty page.

    **Completeness is bounded by attribution quality:** a vote whose voter was not matched to a
    person when it was imported is not returned for anyone, and this call never guesses by name.
    """
    if not db.query(models.Person.id).filter(models.Person.id == person_id).first():
        raise HTTPException(404, "No such Person.")

    query = (
        _votes_of(db, person_id, jurisdiction, session)
        .options(
            contains_eager(models.PersonVote.vote_event).options(
                joinedload(models.VoteEvent.organization),
                selectinload(models.VoteEvent.counts),
                joinedload(models.VoteEvent.bill)
                .joinedload(models.Bill.legislative_session)
                .joinedload(models.LegislativeSession.jurisdiction),
            )
        )
        .order_by(
            models.VoteEvent.start_date.desc().nullslast(),
            models.VoteEvent.id.desc(),
            models.PersonVote.id,
        )
    )
    result = pagination.paginate(query)

    if pagination.page == 1:
        grouped = (
            _votes_of(db, person_id, jurisdiction, session)
            .with_entities(models.PersonVote.option, func.count())
            .group_by(models.PersonVote.option)
            .all()
        )
        counts = {option: 0 for option in STANDARD_OPTIONS}
        counts.update({option: n for option, n in grouped})
        result["summary"] = PersonVoteSummary(counts=counts)
    return result
