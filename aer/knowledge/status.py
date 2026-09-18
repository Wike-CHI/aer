"""The minimum an operator needs to know about the knowledge plane.

Modelled on the deployment config's ``describe()``: a flat, log-safe summary that
answers "is it there, is it the right shape, and does it agree with the store" in
one object, so a status command is a formatter rather than a pile of queries.

``reachable=False`` is a first-class outcome rather than an exception. Reporting on
a broken index is the one thing a status command must still be able to do, so this
module is the deliberate exception to the rule that an unreachable index raises.
"""

from __future__ import annotations

from dataclasses import dataclass

from aer.exceptions import KnowledgeError
from aer.knowledge.base import KnowledgeIndex
from aer.knowledge.projector import DriftReport, KnowledgeProjector

__all__ = ["DEFAULT_DRIFT_SAMPLE", "KnowledgeStatus", "collect_status"]

#: How many drifting ids to name before summarising the rest with a count.
DEFAULT_DRIFT_SAMPLE = 5


@dataclass(frozen=True, slots=True)
class KnowledgeStatus:
    """A point-in-time answer to "what state is the knowledge index in"."""

    database_path: str
    reachable: bool
    projection_schema_version: int | None
    store_experiences: int
    index_experiences: int
    drift: DriftReport | None
    detail: str

    @property
    def in_sync(self) -> bool:
        """Whether the index is usable *and* agrees with the store."""
        return self.reachable and self.drift is not None and self.drift.is_clean

    def describe(self) -> str:
        """A human-readable summary, safe to print during an incident."""
        lines = [
            f"knowledge database : {self.database_path}",
            f"reachable          : {'yes' if self.reachable else 'NO'}",
            f"projection schema  : {self.projection_schema_version}",
            f"store experiences  : {self.store_experiences}",
            f"index experiences  : {self.index_experiences}",
        ]
        if self.drift is None:
            lines.append("drift              : unknown")
        elif self.drift.is_clean:
            lines.append("drift              : none")
        else:
            lines.append(
                f"drift              : {self.drift.drifted} "
                f"(missing {len(self.drift.missing)}, stale {len(self.drift.stale)}, "
                f"orphaned {len(self.drift.orphaned)})"
            )
            for label, ids in (
                ("missing", self.drift.missing),
                ("stale", self.drift.stale),
                ("orphaned", self.drift.orphaned),
            ):
                if ids:
                    shown = ", ".join(ids[:DEFAULT_DRIFT_SAMPLE])
                    suffix = (
                        ""
                        if len(ids) <= DEFAULT_DRIFT_SAMPLE
                        else f" (+{len(ids) - DEFAULT_DRIFT_SAMPLE})"
                    )
                    lines.append(f"  {label}: {shown}{suffix}")
        if self.detail:
            lines.append(f"detail             : {self.detail}")
        return "\n".join(lines)


def collect_status(
    index: KnowledgeIndex,
    projector: KnowledgeProjector,
    *,
    store_experiences: int,
) -> KnowledgeStatus:
    """Gather the status, degrading to a reportable failure instead of raising.

    The drift comparison needs the index twice read (fingerprints) and the store
    once. If any of that fails, the failure is described rather than propagated:
    "the index is broken, here is why" is the useful answer, and an operator who
    cannot see the error because the status command died from it has been told
    nothing.
    """
    reachable = True
    detail = ""
    version: int | None = None
    index_count = 0
    drift: DriftReport | None = None

    try:
        version = index.projection_version()
        index.ensure_schema()
        index_count = index.count_experiences()
        drift = projector.drift()
    except KnowledgeError as exc:
        reachable = False
        detail = str(exc)
    except Exception as exc:
        reachable = False
        detail = f"{type(exc).__name__}: {exc}"

    return KnowledgeStatus(
        database_path=str(getattr(index, "path", "<in-memory>")),
        reachable=reachable,
        projection_schema_version=version,
        store_experiences=store_experiences,
        index_experiences=index_count,
        drift=drift,
        detail=detail,
    )
