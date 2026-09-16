"""Transport/API schemas -- intentionally empty in this milestone.

The agreed project layout reserves this package for the request/response models of
the future FastAPI surface (Milestone 12: ``GET /runs``, ``POST /experiences/search``,
...). Those schemas exist to serialise *over the wire*, which is a different
concern from the domain models in :mod:`aer.runtime.models`.

Nothing is defined yet on purpose. The embedded runtime of this milestone passes
domain models around directly, so introducing DTOs now would be an unverified
layer with no caller (agent.md #47, #48). The first schema belongs here when the
HTTP server milestone starts.
"""
