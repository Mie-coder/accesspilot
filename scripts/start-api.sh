#!/bin/sh
set -eu

python -m accesspilot.bootstrap
exec uvicorn accesspilot.main:app --host 0.0.0.0 --port 8000
