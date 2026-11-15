#!/usr/bin/env bash
# Run before every deploy:  ./scripts/security_check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
pip install -q pip-audit
pip-audit -r requirements.txt || { echo "Vulnerable dependencies found"; exit 1; }
pip freeze > requirements.lock && echo "Wrote requirements.lock (install with: pip install -r requirements.lock)"
