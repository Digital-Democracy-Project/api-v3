#!/usr/bin/env bash
#
# CodeBot test helper for api-v3.
#
# Runs pytest against THIS checkout (the clone CodeBot is editing) in a fully
# isolated Docker stack — a throwaway Postgres plus a `test` service built
# from this repo's own Dockerfile. api-v3's own conftest.py creates/drops its
# test database and seeds fixtures in-process, so no real secrets or seed
# data are needed here — just a reachable Postgres and the right env vars.
#
# The devos `test-ticket` skill invokes this via the required `.codebot/test.sh`
# entrypoint (Step 0 of that skill looks for that exact path at the repo root).
#
# Usage:
#   .codebot/test.sh [TICKET_KEY] [extra pytest args/paths...]
#
#   TICKET_KEY is optional and used only for labelling the run and scoping the
#   compose project/image tag. Any args after it are passed through to pytest
#   (e.g. `api/tests/test_bills.py::test_something`). With no extra args, the
#   full suite runs.
#
# Exit code is pytest's exit code (0 = all tests passed).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.test.yml"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "ERROR: ${COMPOSE_FILE} not found" >&2
  exit 2
fi

TICKET_KEY="${1:-run}"
shift || true
TEST_ARGS=("$@")

# Unique, lowercase, compose-safe project name — never collides across
# concurrent CodeBot tickets or with a developer's own dev stack.
SAFE_KEY="$(echo "${TICKET_KEY}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
PROJECT="codebot-test-${SAFE_KEY:-run}-$$"

# Image tag scoped by ticket key, not a fixed shared name — CAMS's worker
# pool can run multiple CodeBot tickets concurrently, and a shared tag would
# let one ticket's concurrent build retag the image a different ticket's
# `compose run` is about to use.
export CODEBOT_TEST_IMAGE="${CODEBOT_TEST_IMAGE:-codebot-api-v3:${SAFE_KEY:-run}}"

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' is available" >&2
  exit 2
fi

compose() { "${DC[@]}" -p "${PROJECT}" -f "${COMPOSE_FILE}" "$@"; }

cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo ">>> CodeBot isolated test run for ${TICKET_KEY} (api-v3)"
echo ">>> project=${PROJECT}  image=${CODEBOT_TEST_IMAGE}"

echo ">>> building test image from this checkout..."
compose build test

echo ">>> running: pytest ${TEST_ARGS[*]:-(full suite)}"
# pytest is a dev-only dependency the Dockerfile's `--only main` install
# never includes, and invoking it via `poetry run` is broken in this image
# (see docker-compose.test.yml's comment) — install it directly via pip
# instead of going anywhere near poetry's own CLI. Args passed as "$@"
# positional params (not string-interpolated) so paths with spaces survive.
compose run --rm test sh -c \
  'pip install --quiet --disable-pip-version-check --no-cache-dir "pytest>=6.0.1,<7" && pytest "$@"' \
  sh ${TEST_ARGS[@]+"${TEST_ARGS[@]}"}
