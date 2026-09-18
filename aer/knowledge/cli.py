"""Operator entry point for the knowledge plane.

    python -m aer.knowledge status
    python -m aer.knowledge rebuild
    python -m aer.knowledge project

Deliberately three commands and no framework. AER is an embedded runtime with no
server to talk to, so this is a *script* that opens the store and the index the same
way an agent would -- which is also what makes it trustworthy as a diagnostic: it
exercises the real code path rather than a maintenance-only one.

``rebuild`` is the important one. Because the index is a projection, it is the
answer to every knowledge-plane failure, including "the database is gone": nothing
here needs a backup, only the SQLite store.

Exit codes follow the deployment scripts: ``0`` success, ``1`` the operation ran and
failed, ``2`` the command line was wrong. ``status`` returns ``0`` even when the
index is unreachable -- reporting a broken index is what it is for.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from aer.config import load_deployment_config
from aer.exceptions import AERError

__all__ = ["build_parser", "main"]

logger = logging.getLogger("aer.knowledge")


def build_parser() -> argparse.ArgumentParser:
    """The command line, as a value so tests can introspect it."""
    parser = argparse.ArgumentParser(
        prog="python -m aer.knowledge",
        description=(
            "Inspect and repair the AER knowledge index. SQLite is the source of "
            "truth; this index is a projection of it and can always be rebuilt."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser(
        "status",
        help="report reachability, projection schema version, counts and drift",
    )
    subcommands.add_parser(
        "rebuild",
        help="rebuild the index from SQLite and swap it in (the repair for everything)",
    )
    subcommands.add_parser(
        "project",
        help="incrementally copy any experiences the index has not seen yet",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested command. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    # Imported here rather than at module scope: the composition root pulls in the
    # whole application, and this module is also loaded by tests that only want to
    # check the command line.
    from aer import AER

    try:
        # Inside the try on purpose. A misconfigured deployment is exactly the
        # situation an operator reaches for this command in, and answering it with a
        # traceback instead of "AER_ENV=production requires absolute paths" hides the
        # one line that says what to fix.
        config = load_deployment_config()
        logging.basicConfig(level=config.log_level, format="%(levelname)s %(name)s: %(message)s")
        # ``AER_DB_PATH`` names a file; AER is constructed from its directory plus
        # filename so that an explicit override is honoured exactly rather than
        # silently reverting to ``<data_dir>/aer.db``.
        with AER(
            config.db_path.parent,
            db_filename=config.db_path.name,
            knowledge_dir=config.knowledge_dir,
        ) as runtime:
            if args.command == "status":
                print(runtime.knowledge_status().describe())
                return 0
            if args.command == "project":
                written = runtime.project_experiences()
                print(f"projected {written} experiences")
                print(runtime.knowledge_status().describe())
                return 0
            report = runtime.rebuild_knowledge()
            print(report.description)
            print(runtime.knowledge_status().describe())
            return 0
    except AERError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    sys.exit(main())
