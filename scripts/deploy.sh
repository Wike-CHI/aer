#!/usr/bin/env bash
#
# deploy.sh -- deploy one immutable AER image to the production host.
#
# This script runs **on the production host**, not in CI. CI's job is to build and
# publish an image; this script's job is to move the host from one image to another
# without ever risking the database. The host therefore never clones a repository,
# never installs Python and never builds an image (see docs/DECISIONS.md D-041):
# everything that touches the database runs *inside* the pulled image, which is the
# only way the migration code and the runtime code are guaranteed to be the same
# commit.
#
# Order of the destructive part, and why:
#
#   pull image  ->  BACKUP  ->  migrate  ->  smoke test  ->  record current.env
#
# The invariant that matters is "a verified backup exists before anything writes
# to the schema". Pulling is read-only with respect to the database, and doing it
# first means the backup can be taken *by the image itself*, so the host needs no
# Python of its own. See docs/DECISIONS.md D-042.
#
# Nothing here is allowed to continue after a failure: every step is fatal, and
# `current.env` is only rewritten once the new release has passed its smoke test
# (D-043). A half-applied deployment that claims success is the one outcome this
# script exists to prevent.
#
# Usage:
#   IMAGE=ghcr.io/example/aer:sha-a81d92f GIT_SHA=a81d92f ./deploy.sh
#   ./deploy.sh --image ghcr.io/example/aer:sha-a81d92f --git-sha a81d92f
#   ./deploy.sh --image ... --git-sha ... --dry-run
#
set -Eeuo pipefail
# NOTE: deliberately no `set -x`. These scripts run where a trace could echo
# environment values into a CI log; the step log below is the intended audit trail.

# ---------------------------------------------------------------------------
# Locate the deployment directory.
#
# Two layouts are supported on purpose: in the repository the scripts live in
# `scripts/` beside `deploy/`, while on the server both are copied into the same
# directory. Resolving instead of hard-coding means one script works in both and
# nobody has to keep two copies in sync.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
resolve_deploy_dir() {
  if [[ -n "${AER_DEPLOY_DIR:-}" ]]; then
    printf '%s\n' "${AER_DEPLOY_DIR}"
  elif [[ -f "${SCRIPT_DIR}/compose.yaml" ]]; then
    printf '%s\n' "${SCRIPT_DIR}"
  elif [[ -f "${SCRIPT_DIR}/../deploy/compose.yaml" ]]; then
    (cd -- "${SCRIPT_DIR}/../deploy" && pwd)
  else
    printf '%s\n' "${SCRIPT_DIR}"
  fi
}
DEPLOY_DIR="$(resolve_deploy_dir)"
readonly DEPLOY_DIR
COMPOSE_FILE="${DEPLOY_DIR}/compose.yaml"
readonly COMPOSE_FILE
ENV_FILE="${AER_ENV_FILE:-${DEPLOY_DIR}/.env}"
readonly ENV_FILE
CURRENT_ENV="${DEPLOY_DIR}/current.env"
readonly CURRENT_ENV

DRY_RUN=false
KEEP="${AER_BACKUP_RETENTION:-20}"
IMAGE_ARG=""
SHA_ARG=""

log() {
  printf '[deploy %s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}
die() {
  printf '[deploy %s] FAILED: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Deploy an immutable AER image to this host.

Options:
  --image IMAGE     image reference to deploy, e.g. ghcr.io/owner/aer:sha-abc1234
                    (or set IMAGE / AER_IMAGE in the environment)
  --git-sha SHA     commit the image was built from; recorded in current.env
                    (or set GIT_SHA / AER_GIT_SHA)
  --keep N          how many backups to retain (default: AER_BACKUP_RETENTION or 20)
  --dry-run         print the steps that would run, change nothing
  -h, --help        this message

Refuses to deploy an image tagged `latest`: production records an immutable SHA
tag, because "which commit is live?" must be answerable from the running system.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)       IMAGE_ARG="${2:?--image needs a value}"; shift 2 ;;
    --git-sha)     SHA_ARG="${2:?--git-sha needs a value}"; shift 2 ;;
    --keep)        KEEP="${2:?--keep needs a value}"; shift 2 ;;
    --dry-run)     DRY_RUN=true; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -f "${ENV_FILE}" ]] || die "environment file not found: ${ENV_FILE} (start from deploy/env.example)"

# ---------------------------------------------------------------------------
# Read the host-side settings (bind-mount sources, log/backup locations).
#
# Done before the logging below, because AER_LOG_DIR lives in this same file: set
# up the log first and a manual `bash ./deploy.sh` would quietly write nothing.
#
# `deploy/.env` is our own plain KEY=VALUE file, so sourcing it is safe here -- but
# the *image* is deliberately not taken from it. A stale AER_IMAGE left in a file
# must not be able to change what gets released; the caller decides, and what
# actually ran is recorded in current.env.
# ---------------------------------------------------------------------------
CALLER_IMAGE="${IMAGE_ARG:-${IMAGE:-${AER_IMAGE:-}}}"
CALLER_SHA="${SHA_ARG:-${GIT_SHA:-${AER_GIT_SHA:-}}}"
set -a
# shellcheck source=/dev/null
. "${ENV_FILE}"
set +a
IMAGE="${CALLER_IMAGE}"
GIT_SHA="${CALLER_SHA}"

# ---------------------------------------------------------------------------
# Log to stdout always, and to a file when the host has a log directory. stdout is
# what CI captures, so a log file is a convenience for on-call, never the record.
# ---------------------------------------------------------------------------
if [[ -n "${AER_LOG_DIR:-}" ]]; then
  mkdir -p -- "${AER_LOG_DIR}"
  exec > >(tee -a "${AER_LOG_DIR}/deploy.log") 2>&1
fi

log "deploy dir : ${DEPLOY_DIR}"
log "dry run    : ${DRY_RUN}"

command -v docker >/dev/null 2>&1 || die "docker is not on PATH; this host is not ready to deploy"
[[ -f "${COMPOSE_FILE}" ]] || die "compose file not found: ${COMPOSE_FILE}"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"

#: Container-side database path. Passed explicitly to Alembic as well as being part
#: of the image environment, because the two failure modes are not equivalent: with
#: the wrong path Alembic happily creates a *second*, empty database inside the
#: container filesystem, which is discarded on the next `down`. Resolved here, after
#: deploy/.env has been read, so an override in that file is honoured.
CONTAINER_DB_PATH="${AER_DB_PATH:-/data/aer.db}"

# Exported because the compose file interpolates ${AER_IMAGE} from the environment
# first and only then from deploy/.env. Without this, `compose run` would resolve
# the image to whatever happens to be in that file -- which is exactly the stale
# value we refused to trust two lines above.
export AER_IMAGE="${IMAGE}"

[[ -n "${IMAGE}" ]] || die "no image given; pass --image (or set IMAGE)"
[[ -n "${GIT_SHA}" ]] || die "no git sha given; pass --git-sha (or set GIT_SHA)"

# `latest` is never a production identity: it names "whatever was pushed last",
# which is exactly the information a rollback needs to not have. A tagless
# reference is implicitly :latest, so both forms are rejected.
case "${IMAGE}" in
  *:latest|*:) die "refusing to deploy '${IMAGE}': production requires an immutable tag, e.g. :sha-<commit>" ;;
esac
[[ "${IMAGE}" == *":${GIT_SHA}"* || "${IMAGE}" == *"sha-"* ]] \
  || die "image '${IMAGE}' does not look like a SHA-tagged release; refusing to deploy"

HOST_DATA_DIR="${AER_HOST_DATA_DIR:-/srv/aer/data}"
HOST_BACKUP_DIR="${AER_HOST_BACKUP_DIR:-/srv/aer/backups}"
HOST_ARTIFACT_DIR="${AER_HOST_ARTIFACT_DIR:-/srv/aer/artifacts}"
HOST_KNOWLEDGE_DIR="${AER_HOST_KNOWLEDGE_DIR:-/srv/aer/knowledge}"
HOST_DB_PATH="${HOST_DATA_DIR}/$(basename -- "${CONTAINER_DB_PATH}")"

log "image      : ${IMAGE}"
log "git sha    : ${GIT_SHA}"
log "database   : ${HOST_DB_PATH}"

compose_run() {
  docker compose --file "${COMPOSE_FILE}" run --rm --no-TTY aer-runtime "$@"
}

#: Run a command unless this is a dry run, in which case only report it.
step() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "dry-run, would run: $*"
    return 0
  fi
  log "run: $*"
  "$@"
}

#: Capture a command's stdout, or an empty string during a dry run.
capture() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "dry-run, would run (capture): $*" >&2
    printf ''
    return 0
  fi
  "$@"
}

# ---------------------------------------------------------------------------
# 0. Make sure the persistent directories exist, and hand them to the container
#    user.
#
#    The image runs as uid/gid 10001 and docker does not translate ownership, so a
#    directory created by root is not writable from inside. `chown` rather than
#    `chmod 777`: world-writable production data directories are not an acceptable
#    way to make this work.
# ---------------------------------------------------------------------------
prepare_directories() {
  local dir
  for dir in "${HOST_DATA_DIR}" "${HOST_BACKUP_DIR}" "${HOST_ARTIFACT_DIR}" \
             "${HOST_KNOWLEDGE_DIR}" "${AER_LOG_DIR:-}"; do
    [[ -n "${dir}" ]] || continue
    mkdir -p -- "${dir}"
  done
  if [[ "$(id -u)" == "0" ]]; then
    chown -R 10001:10001 -- "${HOST_DATA_DIR}" "${HOST_BACKUP_DIR}" \
      "${HOST_ARTIFACT_DIR}" "${HOST_KNOWLEDGE_DIR}" 2>/dev/null \
      || log "warning: could not chown the data directories to 10001:10001"
  fi
}
step prepare_directories

# ---------------------------------------------------------------------------
# 1. Pull the exact image being deployed. Immutable tag, so this either gets the
#    bytes CI built or fails -- it can never silently resolve to something newer.
# ---------------------------------------------------------------------------
step docker pull "${IMAGE}"

# ---------------------------------------------------------------------------
# 2. Back up the database, before anything can change the schema.
#
#    Skipped only when the database does not exist yet (a first deployment has
#    nothing to lose). Any other failure is fatal: deploying without a verified
#    rollback point is the one risk this pipeline exists to remove.
# ---------------------------------------------------------------------------
BACKUP_PATH=""
if [[ -f "${HOST_DB_PATH}" ]]; then
  BACKUP_PATH="$(capture compose_run python /app/scripts/backup_sqlite.py \
      --source "${CONTAINER_DB_PATH}" \
      --git-sha "${GIT_SHA}" \
      --image "${IMAGE}" \
      --keep "${KEEP}" \
      --print-path | tail -n 1)"
  [[ "${DRY_RUN}" == "true" || -n "${BACKUP_PATH}" ]] || die "backup produced no path"
  log "backup     : ${BACKUP_PATH:-<dry run>}"
else
  log "no database at ${HOST_DB_PATH} yet: first deployment, nothing to back up"
fi

# ---------------------------------------------------------------------------
# 3. Apply migrations with the new image against the production database.
#
#    `-x db_path` is passed explicitly even though the image already sets it: the
#    cost of being redundant is nil, while the cost of being wrong is a second,
#    empty database created inside the container and thrown away on exit.
#    On failure the schema may be partially migrated -- the deployment stops here
#    and the backup above is the recovery point (no automatic downgrade; D-044).
# ---------------------------------------------------------------------------
step compose_run alembic -x "db_path=${CONTAINER_DB_PATH}" upgrade head

# ---------------------------------------------------------------------------
# 4. Prove the release works against the real data before recording it.
# ---------------------------------------------------------------------------
step compose_run python /app/scripts/smoke_test.py

# ---------------------------------------------------------------------------
# 5. Record what is live. Only reached when every step above succeeded, which is
#    what makes current.env trustworthy rather than aspirational.
# ---------------------------------------------------------------------------
record_current() {
  local previous_image=""
  if [[ -f "${CURRENT_ENV}" ]]; then
    previous_image="$(sed -n 's/^AER_IMAGE=//p' "${CURRENT_ENV}" | tail -n 1)"
  fi
  local temporary="${CURRENT_ENV}.tmp.$$"
  {
    printf 'AER_IMAGE=%s\n' "${IMAGE}"
    printf 'AER_GIT_SHA=%s\n' "${GIT_SHA}"
    printf 'AER_PREVIOUS_IMAGE=%s\n' "${previous_image}"
    printf 'AER_DEPLOYED_AT=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'AER_BACKUP=%s\n' "${BACKUP_PATH}"
  } >"${temporary}"
  # Written to a temporary file and renamed: a crash must never leave a truncated
  # record of what is deployed.
  mv -- "${temporary}" "${CURRENT_ENV}"
}
step record_current

log "deployed   : ${GIT_SHA} (${IMAGE})"
log "current.env: ${CURRENT_ENV}"
log "OK"
