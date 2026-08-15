#!/bin/sh
set -eu
export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8

if [ -x .venv/bin/python ]; then
  PYTHON_BIN=.venv/bin/python
else
  PYTHON_BIN=python
fi

"$PYTHON_BIN" -m pytest apps/api/tests/evals/test_fixed_scenarios.py -q
"$PYTHON_BIN" scripts/run-product-evals.py --output "${1:-/private/tmp/accesspilot-product-evaluation.json}"
