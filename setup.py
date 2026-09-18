"""Build shim -- the only thing here is bundling the migration scripts.

``pyproject.toml`` is the single source of packaging configuration; this file
exists for exactly one reason, and it is a *distribution* reason rather than a
build convenience.

``aer.storage.migrations`` provisions the schema by handing Alembic a
``script_location``. An editable install or a source checkout satisfies that from
the repository root, where ``alembic.ini`` and ``migrations/`` sit side by side
with ``aer/``. A **wheel** has no repository root: the package lands in
``site-packages`` and the repository is gone, so the scripts have to travel
*inside* the package or an ordinary ``pip install aer-runtime`` produces a
library that cannot open a database at all. That failure was measured, not
assumed -- see ``docs/DECISIONS.md`` D-040 and D-054.

So at build time the repository's own ``alembic.ini`` and ``migrations/`` are
copied into ``aer/_migrations/``, *mirroring the repository layout* underneath it::

    aer/_migrations/alembic.ini
    aer/_migrations/migrations/env.py
    aer/_migrations/migrations/script.py.mako
    aer/_migrations/migrations/versions/0001_...py  (.. 0004)

Mirroring rather than flattening is deliberate: ``_repository_root()`` already
resolves ``<root>/alembic.ini`` and ``<root>/migrations``, so the installed
layout needs no second code path to describe it.

Three properties are worth stating because they are what make this safe:

* **One source of truth.** The copy is produced from the checked-in files during
  the build and is never edited. There is no second revision history to keep in
  sync, and no revision file is ever generated here.
* **Nothing is written into the source tree.** The copy goes to ``build_lib``
  (``build/lib``), so a source checkout and an editable install keep using the
  repository's own files -- which are, by definition, the current ones.
* **It fails loudly.** If ``migrations/`` or ``alembic.ini`` is missing, the
  build stops. A wheel that silently shipped without its migration scripts would
  only reveal itself as ``StorageError`` on a user's first ``AER(...)`` call.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

#: Repository root (this file's directory).
ROOT = Path(__file__).resolve().parent

#: Package directory the copy is placed under, relative to ``build_lib``.
PACKAGE_MIGRATIONS = Path("aer") / "_migrations"

#: Repository files the wheel needs and setuptools cannot infer from ``aer/``.
#: ``(source, destination relative to PACKAGE_MIGRATIONS)``.
BUNDLED = (
    ("alembic.ini", Path("alembic.ini")),
    ("migrations", Path("migrations")),
)


class build_py(_build_py):
    """``build_py`` that first stages the migration scripts inside the package."""

    def run(self) -> None:
        self._bundle_migrations()
        super().run()

    def _bundle_migrations(self) -> None:
        """Copy ``alembic.ini`` and ``migrations/`` into ``build_lib/aer/_migrations``.

        Runs before ``super().run()`` so the declared ``package-data`` patterns
        find the files already staged and record them as build outputs -- a
        file that is only copied afterwards would ship but would be invisible to
        anything that asks ``build_py`` what it produced.
        """
        destination = Path(self.build_lib) / PACKAGE_MIGRATIONS

        # A stale copy is worse than no copy: `python -m build` reuses `build/`
        # between runs, so a revision deleted from the repository would otherwise
        # survive in the wheel.
        if destination.exists():
            shutil.rmtree(destination)

        for source_name, relative_destination in BUNDLED:
            source = ROOT / source_name
            if not source.exists():  # pragma: no cover - defensive
                raise FileNotFoundError(
                    f"{source_name} is required to build a usable wheel: the installed "
                    "package cannot provision its schema without it (see setup.py)"
                )

            target = destination / relative_destination
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)


setup(cmdclass={"build_py": build_py})
