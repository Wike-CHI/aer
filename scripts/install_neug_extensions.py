"""Fetch the NeuG extensions the runtime image needs. Run at **image build time**.

Why this exists rather than a line in a Dockerfile: ``LOAD fts`` fails on a fresh
install, because the full-text engine is not part of the wheel. It is downloaded on
first ``INSTALL`` from Alibaba OSS -- measured at ~8 MB for ``fts`` -- and written
into the Python environment, *not* into the mounted knowledge directory. A
container built without it would therefore need the network on the first retrieval
of its life, and would be useless the moment that network was unavailable. Fetching
it here moves that dependency to build time, where the network is expected, and
makes the image self-contained afterwards.

Verified against neug 0.2.0 on the deployment target:

    LOAD fts;     ->  RuntimeError: Extension ... libfts.neug_extension not found
    INSTALL fts;  ->  downloaded to <site-packages>/extension/fts/libfts.neug_extension
    LOAD fts;     ->  loaded and initialized successfully

The script fails loudly if the extension is not on disk afterwards: an image that
silently lacks the full-text engine could only discover it in production.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Sequence

#: Extensions to bake in. Only ``fts``: this milestone has no vector search, and
#: section 5 of the round-6 brief rules it out until BM25 has been shown to be
#: insufficient on real retrieval data.
EXTENSIONS: tuple[str, ...] = ("fts",)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--extension",
        action="append",
        default=None,
        help="extension to install (repeatable); defaults to fts",
    )
    args = parser.parse_args(argv)
    extensions = tuple(args.extension) if args.extension else EXTENSIONS

    try:
        import neug
    except ImportError as exc:  # pragma: no cover - build-time failure path
        print(
            f"neug is not importable, so its extensions cannot be fetched: {exc}", file=sys.stderr
        )
        return 1

    # A throwaway database: INSTALL is a property of the Python environment, not of
    # any particular database, but the binding only exposes it through a connection.
    workdir = tempfile.mkdtemp(prefix="aer-neug-ext-")
    try:
        database = neug.Database(f"{workdir}/scratch", mode="read-write")
        connection = database.connect()
        for name in extensions:
            connection.execute(f"INSTALL {name}")
            connection.execute(f"LOAD {name}")
            print(f"installed extension {name}")
        connection.close()
        database.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    missing = [name for name in extensions if _extension_library(name) is None]
    if missing:
        print(
            f"the extension installer reported success but these are not on disk: {missing}",
            file=sys.stderr,
        )
        return 1
    return 0


def _extension_library(name: str) -> str | None:
    """Where the extension library ended up, or ``None`` if it is not there.

    Resolved through the same environment variable the binding uses, so this check
    cannot disagree with the loader about where to look.
    """
    import os
    from pathlib import Path

    home = os.environ.get("NEUG_EXTENSION_HOME_PYENV")
    if not home:
        import neug

        home = str(Path(neug.__file__).resolve().parent.parent)
    candidate = Path(home) / "extension" / name
    if not candidate.is_dir():
        return None
    for path in sorted(candidate.iterdir()):
        if path.is_file():
            print(f"  {path} ({path.stat().st_size} bytes)")
            return str(path)
    return None


if __name__ == "__main__":  # pragma: no cover - runs at image build time
    sys.exit(main())
