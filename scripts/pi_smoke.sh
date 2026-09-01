#!/bin/bash
# On-Pi smoke test: run the unit suite (no pytest needed), render the status
# screen without hardware, and report the environment.  Touches no vehicle.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "### $(date -Is) on $(hostname) in $ROOT"
echo "### python"; python3 -V
echo "### unit tests (unittest, no pytest dependency)"
PYTHONPATH=src python3 -m unittest discover -s tests -t tests -q 2>&1 | grep -E "^(OK|FAILED|Ran |ERROR)" | tail -3
echo "### status render (simulated, no panel)"
PYTHONPATH=src python3 -m hummer_obd.display.status --once \
    --simulate "$ROOT/evidence/status-render.png" --root "$ROOT" 2>&1 | tail -2
ls -l "$ROOT/evidence/status-render.png" 2>/dev/null
echo "### safety gate spot check"
PYTHONPATH=src python3 - <<'PY'
from hummer_obd.safety import is_safe
allowed = ["0100", "010C", "03", "07", "0A", "0902", "ATI", "ATRV", "STI"]
denied = ["04", "0400", "2E1234", "2701", "3101FF", "22ABCD", "08", "0100;04"]
print("  allowed:", all(is_safe(c) for c in allowed), allowed)
print("  denied :", not any(is_safe(c) for c in denied), denied)
PY
