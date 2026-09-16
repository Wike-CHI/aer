#!/usr/bin/env bash
#
# rollback.sh -- put the previous known-good image back.
#
# What this does: repoint the deployment at `AER_PREVIOUS_IMAGE` (recorded by
# deploy.sh) and verify it against the live database. It does **not** roll back the
# schema (docs/DECISIONS.md D-044).
#
# Why there is no `alembic downgrade` here: a downgrade is a data migration written
# by the *newer* release, executed against data the *newer* release already wrote.
# It is the single most dangerous command in the toolbox, it cannot be tested
# against real production data in advance, and AER's later milestones add tables
# whose contents are irreplaceable (experiences distilled from real runs). Reverting
# the application and reverting the database are therefore separate, explicit
# decisions, and the database half is a restore from the verified backup that
# deploy.sh took immediately before the migration.
#
# When the previous image cannot read the current schema, this script says so and
# stops without changing what is recorded as live: the operator must restore the
# backup first. Refusing to record a rollback that does not actually work is the
# point -- a current.env that lies is worse than no current.env at all.
#
# Usage:
#   ./rollback.sh                  # back to AER_PREVIOUS_IMAGE
#   ./rollback.sh --to ghcr.io/example/aer:sha-7c281bc
#   ./rollback.sh --dry-run
#
set -Eeuo pipefail
# No `set -x`: this script reads the deployment record, and a trace would print it
# into whatever log CI keeps.

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

TO_ARG=""
DRY_RUN=false
FORCE=false

log() { printf '[rollback %s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() {
  printf '[rollback %s] FAILED: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Roll the deployment back to a previously deployed image.

Options:
  --to IMAGE   image to switch to (default: AER_PREVIOUS_IMAGE from current.env)
  --force      record the rollback even if the smoke test fails; use only after
               restoring the database by hand
  --dry-run    print the steps that would run, change nothing
  -h, --help   this message

Does not touch the schema. See the header comment for why.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)       TO_ARG="${2:?--to needs a value}"; shift 2 ;;
    --force)    FORCE=true; shift ;;
    --dry-run)  DRY_RUN=true; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -f "${ENV_FILE}" ]] || die "environment file not found: ${ENV_FILE}"

# Read the host-side settings before anything reads them back: AER_LOG_DIR and the
# AER_HOST_* paths both live in this file. The AER_IMAGE it may contain is
# deliberately overwritten below by the rollback target -- a rollback must never be
# steered by a value left lying in a configuration file.
set -a
# shellcheck source=/dev/null
. "${ENV_FILE}"
set +a

if [[ -n "${AER_LOG_DIR:-}" ]]; then
  mkdir -p -- "${AER_LOG_DIR}"
  exec > >(tee -a "${AER_LOG_DIR}/deploy.log") 2>&1
fi

command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
[[ -f "${COMPOSE_FILE}" ]] || die "compose file not found: ${COMPOSE_FILE}"

if [[ ! -f "${CURRENT_ENV}" ]]; then
  # Explicitly not "guess from the image list": picking a rollback target is an
  # operator decision, and a directory listing cannot express intent.
  die "no deployment record at ${CURRENT_ENV}; pass --to <image> to choose a target explicitly"
fi

current_value() { sed -n "s/^$1=//p" "${CURRENT_ENV}" | tail -n 1; }

#: Derive the commit from the tag rather than copying the outgoing SHA: after a
#: rollback the recorded commit must describe the image that is now live. A tag
#: that carries no SHA yields "unknown", which is honest; inheriting the previous
#: release's commit would be a lie that survives in every later `cat current.env`.
sha_from_image() {
  if [[ "$1" =~ :sha-([0-9a-fA-F]+)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    printf 'unknown\n'
  fi
}

CURRENT_IMAGE="$(current_value AER_IMAGE)"
PREVIOUS_IMAGE="$(current_value AER_PREVIOUS_IMAGE)"
LAST_BACKUP="$(current_value AER_BACKUP)"
TARGET="${TO_ARG:-${PREVIOUS_IMAGE}}"

[[ -n "${CURRENT_IMAGE}" ]] || die "${CURRENT_ENV} has no AER_IMAGE; it is not a deployment record"
[[ -n "${TARGET}" ]] || die "no previous image recorded; pass --to <image> to choose a rollback target"
case "${TARGET}" in
  *:latest|*:) die "refusing to roll back to '${TARGET}': an immutable SHA tag is required" ;;
esac
[[ "${TARGET}" != "${CURRENT_IMAGE}" ]] || die "'${TARGET}' is already the live image"

HOST_DATA_DIR="${AER_HOST_DATA_DIR:-/srv/aer/data}"
log "deploy dir : ${DEPLOY_DIR}"
log "live image : ${CURRENT_IMAGE}"
log "target     : ${TARGET}"
log "dry run    : ${DRY_RUN}"

# The compose file interpolates AER_IMAGE from the environment first, so exporting
# the target is what actually repoints the deployment.
export AER_IMAGE="${TARGET}"

compose_run() {
  docker compose --file "${COMPOSE_FILE}" run --rm --no-TTY aer-runtime "$@"
}

step() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "dry-run, would run: $*"
    return 0
  fi
  log "run: $*"
  "$@"
}

step docker pull "${TARGET}"

# A rollback is only complete once the old image has been shown to work against the
# live database. The most likely failure is a schema that moved forward, which the
# smoke test reports as a revision mismatch rather than a crash.
smoke_ok=true
if [[ "${DRY_RUN}" == "true" ]]; then
  log "dry-run, would verify ${TARGET} against ${HOST_DATA_DIR}"
  log "dry-run: would record ${TARGET} as live"
  log "OK (dry run)"
  exit 0
fi

log "verifying ${TARGET} against ${HOST_DATA_DIR}"
if ! step compose_run python /app/scripts/smoke_test.py; then
  smoke_ok=false
fi

if [[ "${smoke_ok}" != "true" && "${FORCE}" != "true" ]]; then
  printf '\n' >&2
  log "SMOKE TEST FAILED: ${TARGET} does not work against the current database."
  log "The live image has NOT been changed; current.env still records ${CURRENT_IMAGE}."
  cat >&2 <<EOF

Most likely cause: the database schema is newer than this image understands
(a revision mismatch is expected after rolling back across a migration).

Recovery, in order:
  1. stop anything holding the database open
  2. restore the backup taken immediately before that migration:
       docker compose --file ${COMPOSE_FILE} run --rm aer-runtime \\
         python /app/scripts/restore_sqlite.py \\
         --source-backup <backup>.db --target ${HOST_DATA_DIR}/aer.db --force
     Last backup recorded by deploy.sh: ${LAST_BACKUP:-<none recorded>}
     All backups:                      ${AER_HOST_BACKUP_DIR:-/srv/aer/backups}
  3. re-run this script (add --force only after the database has been restored)

Automatic schema downgrade is deliberately not implemented (see docs/DECISIONS.md
D-044).
EOF
  exit 1
fi

if [[ "${smoke_ok}" != "true" ]]; then
  log "WARNING: recording the rollback despite a failed smoke test (--force)"
fi

# Same atomic-write discipline as deploy.sh: never leave a truncated record behind.
# The previous image is now the one being replaced, so the pair swaps -- rolling
# back a rollback is therefore possible without any extra bookkeeping.
TARGET_SHA="$(sha_from_image "${TARGET}")"
ROLLED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

record_rollback() {
  local temporary="${CURRENT_ENV}.tmp.$$"
  {
    printf 'AER_IMAGE=%s\n' "${TARGET}"
    printf 'AER_GIT_SHA=%s\n' "${TARGET_SHA}"
    printf 'AER_PREVIOUS_IMAGE=%s\n' "${CURRENT_IMAGE}"
    printf 'AER_DEPLOYED_AT=%s\n' "${ROLLED_AT}"
    printf 'AER_BACKUP=%s\n' "${LAST_BACKUP}"
    printf 'AER_ROLLED_BACK_FROM=%s\n' "${CURRENT_IMAGE}"
  } >"${temporary}"
  mv -- "${temporary}" "${CURRENT_ENV}"
}
step record_rollback

log "rolled back: ${CURRENT_IMAGE} -> ${TARGET}"
log "OK"
