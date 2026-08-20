#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export LANGGRAPH_STRICT_MSGPACK=true

if [ -x .venv/bin/python ]; then
  PYTHON_BIN=.venv/bin/python
else
  PYTHON_BIN=python
fi

: "${ACCESSPILOT_T29_ADMIN_DATABASE_URL:?Set the local PostgreSQL admin URL for disposable T41 databases}"

export ACCESSPILOT_T26_DATABASE_URL="${ACCESSPILOT_T26_DATABASE_URL:-$ACCESSPILOT_T29_ADMIN_DATABASE_URL}"
export ACCESSPILOT_T30_ADMIN_DATABASE_URL="${ACCESSPILOT_T30_ADMIN_DATABASE_URL:-$ACCESSPILOT_T29_ADMIN_DATABASE_URL}"
export ACCESSPILOT_T33_ADMIN_DATABASE_URL="${ACCESSPILOT_T33_ADMIN_DATABASE_URL:-$ACCESSPILOT_T29_ADMIN_DATABASE_URL}"
export ACCESSPILOT_T35_ADMIN_DATABASE_URL="${ACCESSPILOT_T35_ADMIN_DATABASE_URL:-$ACCESSPILOT_T29_ADMIN_DATABASE_URL}"
export ACCESSPILOT_T36_ADMIN_DATABASE_URL="${ACCESSPILOT_T36_ADMIN_DATABASE_URL:-$ACCESSPILOT_T29_ADMIN_DATABASE_URL}"
export ACCESSPILOT_T41_ADMIN_DATABASE_URL="${ACCESSPILOT_T41_ADMIN_DATABASE_URL:-$ACCESSPILOT_T29_ADMIN_DATABASE_URL}"

ACCESSPILOT_T41_EVIDENCE_DIR=${ACCESSPILOT_T41_EVIDENCE_DIR:-$(mktemp -d /private/tmp/accesspilot-t41.XXXXXX)}
export ACCESSPILOT_T41_EVIDENCE_DIR
mkdir -p "$ACCESSPILOT_T41_EVIDENCE_DIR"

"$PYTHON_BIN" scripts/run-t41-alembic.py \
  --output "$ACCESSPILOT_T41_EVIDENCE_DIR/alembic.json"

"$PYTHON_BIN" scripts/run-t41-api-suite.py \
  --output "$ACCESSPILOT_T41_EVIDENCE_DIR/api.xml"

"$PYTHON_BIN" -m ruff check apps/api/src apps/api/tests
"$PYTHON_BIN" -m mypy apps/api/src

"$PYTHON_BIN" scripts/run-t41-parity.py \
  --output "$ACCESSPILOT_T41_EVIDENCE_DIR/parity.json"
"$PYTHON_BIN" scripts/run-t41-rollback.py \
  --output "$ACCESSPILOT_T41_EVIDENCE_DIR/rollback.xml"

pnpm --filter @accesspilot/web exec vitest run \
  --reporter=json \
  --outputFile="$ACCESSPILOT_T41_EVIDENCE_DIR/vitest.json"
"$PYTHON_BIN" scripts/assert-t41-test-report.py \
  "$ACCESSPILOT_T41_EVIDENCE_DIR/vitest.json" \
  --format vitest --require-no-skips

pnpm --filter @accesspilot/web exec eslint . --max-warnings 0
pnpm --filter @accesspilot/web exec tsc --noEmit -p tsconfig.app.json
pnpm --filter @accesspilot/web exec tsc --noEmit -p tsconfig.node.json
pnpm --filter @accesspilot/web exec vite build

"$PYTHON_BIN" scripts/run-t41-product-evals.py \
  --output "$ACCESSPILOT_T41_EVIDENCE_DIR/product-evaluation.json"

git rev-parse --short HEAD >"$ACCESSPILOT_T41_EVIDENCE_DIR/base-head.txt"
git status --short >"$ACCESSPILOT_T41_EVIDENCE_DIR/worktree.txt"
printf 'T41 automatic gates passed. Evidence: %s\n' "$ACCESSPILOT_T41_EVIDENCE_DIR"
