#!/usr/bin/env bash
#
# smoke_test.sh -- thin wrapper around smoke_test.py.
#
# The checks live in Python because they need to open the database through AER's
# own public API, and because a shell implementation of "is this schema at head"
# would be a second, differently-wrong answer. This wrapper exists only so that
# pipelines and humans have one stable command that does not depend on how Python
# is spelled on this host.
#
# Usage:
#   ./smoke_test.sh                          # production checks, read-only
#   ./smoke_test.sh --temp-db-check          # also prove the write path in a temp db
#   AER_PYTHON=python3.12 ./smoke_test.sh    # pick the interpreter explicitly
#
set -Eeuo pipefail
# NOTE: no `set -x` in the ops scripts. `-x` echoes expanded command lines, and
# these scripts run on a host where a trace could surface values from the
# environment into a CI log that is far more widely readable than the host.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PYTHON_BIN="${AER_PYTHON:-python}"
readonly PYTHON_BIN

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[smoke] FAILED: '${PYTHON_BIN}' not found; set AER_PYTHON to an interpreter" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/smoke_test.py" "$@"
