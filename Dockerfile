# AER runtime image (Infrastructure milestone).
#
# What this image is: a *disposable* Python environment that can open, migrate,
# verify, back up and roll back an AER store. What it is not: a service. AER is
# an embedded runtime -- there is no HTTP server, no health endpoint and no
# long-running process to supervise, so the image deliberately has no daemon to
# start and its default command exits.
#
# Layout note (this is load-bearing, see docs/DECISIONS.md D-040):
#   aer.storage.migrations locates the migration scripts by walking **up** from
#   the installed package until it finds `alembic.ini`. A regular (non-editable)
#   install therefore cannot migrate anything, because the package lands in
#   site-packages and `alembic.ini` is nowhere above it. Verified by experiment,
#   not assumed:
#       $ cp -r aer /tmp/fake-site-packages/ && PYTHONPATH=/tmp/fake-site-packages python -c ...
#       StorageError: Cannot locate alembic.ini: AER's migration scripts are missing
#   So the package is installed **editable** and `/app` holds exactly one copy of
#   the source, sitting at the same level as `alembic.ini` and `migrations/`.
#   That is also why there is no builder stage: every dependency ships a
#   manylinux wheel, nothing is compiled, and a second stage would copy the same
#   `/app` tree twice for no gain.

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

# Only what the runtime needs: the package, the migration scripts, alembic.ini
# (found by walking up from the package -- see the layout note above) and the ops
# scripts, which run *inside* this image so the production host needs no Python
# of its own. `deploy/` and `tests/` are host/CI concerns and are excluded.
COPY pyproject.toml README.md alembic.ini ./
COPY aer/ ./aer/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Editable install: the only install mode under which migrations can be located
# (see the layout note above). No `[dev]` extra -- ruff/mypy/pytest must not ship.
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
