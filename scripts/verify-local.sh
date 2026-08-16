#!/bin/sh
set -eu
export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export LANGGRAPH_STRICT_MSGPACK=true

if [ -x .venv/bin/python ]; then
  PYTHON_BIN=.venv/bin/python
else
  PYTHON_BIN=python
fi

"$PYTHON_BIN" -m pytest apps/api/tests -q
"$PYTHON_BIN" -m ruff check apps/api/src apps/api/tests
"$PYTHON_BIN" -m mypy apps/api/src
"$PYTHON_BIN" -m alembic check
pnpm test:web
pnpm lint:web
pnpm build:web
./scripts/run-evals.sh

echo "Local code gates passed. Run Docker/Compose and 1440px/390px browser checks separately."
