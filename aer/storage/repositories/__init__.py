"""Repository layer -- the only sanctioned path to AER data (agent.md #15).

Each repository takes a :class:`~aer.storage.database.Database` and speaks in
domain models (:class:`~aer.runtime.models.Run`, ``Event``, ``ErrorRecord``,
``RecoveryRecord``, ``VerificationRecord``, ``Experience``). SQL, JSON column
encoding and ORM rows stay on the storage side of this boundary, so a future
PostgreSQL backend means replacing these classes -- not rewriting the runtime.
"""

from aer.storage.repositories.error import ErrorRepository
from aer.storage.repositories.event import EventRepository
from aer.storage.repositories.experience import (
    ExperienceRepository,
    ExperienceSourceRepository,
)
from aer.storage.repositories.recovery import RecoveryRepository
from aer.storage.repositories.run import RunRepository
from aer.storage.repositories.verification import VerificationRepository

__all__ = [
    "ErrorRepository",
    "EventRepository",
    "ExperienceRepository",
    "ExperienceSourceRepository",
    "RecoveryRepository",
    "RunRepository",
    "VerificationRepository",
]
