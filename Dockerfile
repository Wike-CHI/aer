# AER runtime image (Infrastructure milestone).
#
# What this image is: a *disposable* Python environment that can open, migrate,
# verify, back up and roll back an AER store. What it is not: a service. AER is
# an embedded runtime -- there is no HTTP server, no health endpoint and no
# long-running process to supervise, so the image deliberately has no daemon to
# start and its default command exits.
#
# Layout note (see docs/DECISIONS.md D-040 and D-064):
#   aer.storage.migrations looks for `alembic.ini` and `migrations/` in two places:
#   the package's own `aer/_migrations/`, staged into every wheel at build time,
#   and -- checked first -- any ancestor of the installed package, which is where a
#   source checkout or an editable install keeps them.
#
#   D-040 originally documented the second half only, and had to conclude that a
#   regular install could not migrate at all, because the package lands in
#   site-packages and `alembic.ini` is nowhere above it. Measured, not assumed:
#       $ cp -r aer /tmp/fake-site-packages/ && PYTHONPATH=/tmp/fake-site-packages python -c ...
#       StorageError: Cannot locate alembic.ini: AER's migration scripts are missing
#   D-064 removed that constraint by bundling the scripts, so this image is no
#   longer pinned to an editable install for *correctness*; it stays editable
#   because `/app` is meant to be a readable source tree -- see the install step.
#
#   No builder stage either: every dependency ships a manylinux wheel, nothing is
#   compiled, and a second stage would copy the same `/app` tree twice for no gain.

FROM python:3.12-slim AS runtime

# Build metadata. Every production image must be traceable to a Git commit, so
# these are real build arguments rather than a hard-coded version string; CI
# passes the commit it just verified (docs/DEPLOYMENT.md, "Image identity").
#
# `org.opencontainers.image.source` is not cosmetic: GHCR links a package to a
# repository through this label, and CI sets it from github.server_url/repository.
ARG GIT_SHA=unknown
ARG GIT_SOURCE=unknown
ARG GIT_REF=unknown
ARG BUILD_DATE=unknown
ARG AER_VERSION=0.0.0

LABEL org.opencontainers.image.title="AER - Agent Experience Runtime" \
      org.opencontainers.image.description="Embedded agent experience runtime: run trace, verification and experience distillation on SQLite." \
      org.opencontainers.image.source="${GIT_SOURCE}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${AER_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.ref.name="${GIT_REF}"

# AER_* defaults match the documented production layout (/srv/aer on the host).
# They are baked in so a bare `docker run` lands in the right place, and are
# overridable per environment by deploy/env.example.
#
# Only AER_DATA_DIR is set, deliberately. Setting AER_DB_PATH *as well* would look
# more explicit but is a trap: AER_DB_PATH wins in aer.config, so a later
# `-e AER_DATA_DIR=/somewhere-else` would be silently ineffective and the process
# would keep using /data/aer.db. One variable that means one place, honoured by the
# runtime and by the Alembic CLI alike (migrations/env.py resolves AER_DATA_DIR too),
# is worth more than two that can disagree. deploy.sh additionally passes
# `-x db_path` to Alembic, for defence in depth.
#
# There are no credentials here and there never will be: the image is public
# infrastructure, secrets arrive at runtime from the host environment.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AER_ENV=production \
    AER_DATA_DIR=/data \
    AER_ARTIFACT_DIR=/artifacts \
    AER_KNOWLEDGE_DIR=/knowledge \
    AER_BACKUP_DIR=/backups \
    AER_LOG_LEVEL=INFO

WORKDIR /app

# Non-root from the start. The database, artifacts and knowledge directories are
# persistent host directories bind-mounted onto these paths, so the container
# user's uid must own them on the host too -- docker does not translate uids.
# See docs/DEPLOYMENT.md ("Ownership") for the one-time chown.
RUN groupadd --gid 10001 aer \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin aer

# Only what the runtime needs: the package, the migration scripts, alembic.ini and
# the ops scripts, which run *inside* this image so the production host needs no
# Python of its own. `deploy/` and `tests/` are host/CI concerns and are excluded.
#
# `alembic.ini` and `migrations/` are still copied even though the package can now
# carry them (`aer/_migrations`, see docs/DECISIONS.md D-064): this image runs the
# *Alembic CLI* against the production database, and the CLI reads `alembic.ini`
# from the working directory. Keeping them at `/app` is what makes
# `docker compose run --rm aer-runtime alembic upgrade head` work unqualified.
#
# `LICENSE` is here because `project.license-files` in pyproject.toml names it:
# an editable install still builds the distribution metadata, and the metadata
# cannot reference a file that was left out of the build context.
COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY aer/ ./aer/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Editable install, and no `[dev]` extra -- ruff/mypy/pytest must not ship.
#
# Editable is no longer *required* for migrations to be found (that was D-040, and
# D-064 removed the constraint); it is kept because `/app` is deliberately a
# readable source tree: the shell entry points under `/app/scripts` are executed as
# files, and an operator debugging a failed deployment can read exactly the code
# the image is running.
RUN python -m pip install --no-cache-dir --editable . \
 && python -m pip freeze --exclude-editable > /app/requirements.frozen.txt

# Fetch the NeuG full-text extension into the image, and verify it landed.
#
# This is the one build step with a network dependency, and it is here on purpose.
# `LOAD fts` fails on a fresh install: the engine is in the wheel, the full-text
# extension is not, and it is downloaded on first `INSTALL` from Alibaba OSS into
# the *Python environment* -- not into /knowledge, which is a bind mount. A runtime
# image without it would need the network on the first retrieval of its life, and
# would be unable to search at all during an outage. Moving the fetch to build time
# makes the image hermetic; the cost is that this step fails when the extension
# host is unreachable, which is the failure we want to see here rather than later.
RUN python /app/scripts/install_neug_extensions.py \
 && chmod -R a+rX "$(python -c 'import neug, pathlib; print(pathlib.Path(neug.__file__).parent.parent)')/extension"

# Created here so that (a) a bare `docker run` without mounts still works and
# (b) the mount points exist with the right owner. Bind mounts shadow them; the
# host directories are what actually persists.
RUN mkdir -p /data /artifacts /knowledge /backups \
 && chown -R aer:aer /data /artifacts /knowledge /backups /app/requirements.frozen.txt

USER aer

# No ENTRYPOINT on purpose: `docker compose run --rm aer-runtime alembic upgrade
# head` must execute that command directly. The default command proves the image
# is functional and exits immediately, so `docker compose up` can never be
# mistaken for a service that is supposed to stay up, and `docker run <image>`
# doubles as the smallest possible smoke test.
CMD ["python", "-c", "import aer; print('aer-runtime', aer.__version__)"]
