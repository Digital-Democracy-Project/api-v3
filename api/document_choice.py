"""Which archived document stands for a bill version: the one rule, used by the bill page
(`BillPagination._archived_row_for`, and through it the embedding ledger) and by search
(`search_projection.pick_current_docs`), so the two never disagree on a version's text (OPEN-533).

XML first (OPEN-317: no page headers, page numbers or line-wrap debris), then OpenDocument
(OPEN-516: Pennsylvania's "DOC" printing, the only source where struck-out words are marked and
left out), then PDF, then anything else. Rows sharing a media type resolve to the one with the
last source_url, which is the document the archiver keeps as that type's diff baseline (it walks
links sorted by media type and url and keeps the last text; OPEN-369). The archiver itself prefers
no type (OPEN-217), so this priority is the API's own. Media types are compared as exact strings
(the archive holds no parameterized or mixed-case variants; checked 2026-10-02).
"""
from typing import Callable, Iterable, Optional, TypeVar

XML = "text/xml"
OPENDOCUMENT = "application/vnd.oasis.opendocument.text"
PDF = "application/pdf"
MEDIA_TYPE_PREFERENCE = (XML, OPENDOCUMENT, PDF)

Row = TypeVar("Row")


def preferred_document(
    rows: Iterable[Row],
    media_type: Callable[[Row], str],
    source_url: Callable[[Row], str],
) -> Optional[Row]:
    """The row that stands for one version among `rows` (all of that version's documents with
    text), or None when there are none. The sort by URL is stable, so rows with an equal URL keep
    the order the caller gave them and the last one wins."""
    by_media_type = {}
    for row in sorted(rows, key=source_url):
        by_media_type[media_type(row)] = row
    for preferred in MEDIA_TYPE_PREFERENCE:
        if preferred in by_media_type:
            return by_media_type[preferred]
    return next(iter(by_media_type.values()), None)
