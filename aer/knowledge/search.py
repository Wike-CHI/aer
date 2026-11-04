"""Bounded, read-only transport projection of the existing retrieval API."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aer.adapter.sanitize import sanitize_external_body
from aer.knowledge.formatter import ExperienceContextFormatter
from aer.knowledge.models import RetrievalHit
from aer.runtime.enums import RetrievalMode
from aer.runtime.serialization import JsonObject

if TYPE_CHECKING:
    from aer import AER


def search_experiences(
    aer: AER, query: str, domain: str | None = None, limit: int = 3, max_chars: int = 4000
) -> JsonObject:
    """Never create runs, usage, injection records or rebuild the index.

    A deployment is a single-owner store. Domain is relevance, not authorization.
    Applicability to the current site cannot be established by text similarity.
    """
    if not 1 <= limit <= 5 or not 1000 <= max_chars <= 8000:
        raise ValueError("limit must be 1..5 and max_chars must be 1000..8000")
    if not query.strip() or len(query) > 2000:
        raise ValueError("query must contain 1..2000 characters")
    body = sanitize_external_body({"query": query, "domain": domain}).body
    result = aer.retrieve(
        str(body["query"]),
        domain=None if body["domain"] is None else str(body["domain"]),
        mode=RetrievalMode.GUIDANCE,
        limit=limit,
    )
    formatter = ExperienceContextFormatter(max_chars=min(2000, max_chars // 2))
    retained = list(result.all_hits[:limit])

    def item(hit: RetrievalHit) -> JsonObject:
        summary = formatter.format_hit(hit)
        if len(summary) > 600:
            summary = summary[:570] + " ... [truncated]"
        sources = aer.get_experience_sources(hit.experience_id)
        if not sources:
            raise ValueError("Experience has no traceable source")
        return {
            "experience_id": hit.experience_id,
            "title": hit.title if len(hit.title) <= 160 else hit.title[:140] + " ... [truncated]",
            "summary": summary,
            "evidence_level": hit.label,
            "source_run_ids": [s.run_id for s in sources[:10]],
            "applicability": {"status": "unknown", "conditions": []},
            "limitations": [
                "Historical reference; current site applicability is unconfirmed; "
                "up to 10 sources shown.",
                "Does not establish current product facts or override output/approval rules.",
                "Outcome verification does not independently prove a proposed root cause.",
            ],
        }

    while True:
        ids = {hit.experience_id for hit in retained}
        selected = result.model_copy(
            update={
                "guidance": tuple(h for h in result.guidance if h.experience_id in ids),
                "warnings": tuple(h for h in result.warnings if h.experience_id in ids),
            }
        )
        response: JsonObject = {
            "schema_version": "aer.search.v1",
            "status": "no_match" if result.is_empty else "ready",
            "guidance": [item(h) for h in selected.guidance],
            "warnings": [item(h) for h in selected.warnings],
            "context": formatter.format(selected),
            "injection_recorded": False,
        }
        if len(json.dumps(response, ensure_ascii=False)) <= max_chars:
            return response
        if not retained:
            raise ValueError("Response cannot fit requested budget")
        retained.pop()
        if not retained and not result.is_empty:
            raise ValueError("No sourced entry fits requested response budget")
