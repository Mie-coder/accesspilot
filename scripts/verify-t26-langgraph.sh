#!/bin/sh
set -eu

if [ -z "${ACCESSPILOT_T26_DATABASE_URL:-}" ]; then
  echo "ACCESSPILOT_T26_DATABASE_URL is required for the T26 PostgreSQL probe." >&2
  exit 2
fi

PYTHON_BIN=${T26_PYTHON_BIN:-.venv/bin/python}
if [ ! -x "$PYTHON_BIN" ]; then
  echo "T26 Python interpreter is not executable: $PYTHON_BIN" >&2
  exit 2
fi

export LANGGRAPH_STRICT_MSGPACK=true

"$PYTHON_BIN" scripts/t26_langgraph_compat.py --integration
"$PYTHON_BIN" -m pytest apps/api/tests/agent/test_t26_langgraph_compat.py -q
