#!/usr/bin/env bash
# Run before every deploy:  ./scripts/security_check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# Static assets: the committed app/static/dist + app/build must match the CSS/JS sources (fix: npm ci && npm run build).
if command -v node >/dev/null 2>&1 && [ -d node_modules ]; then
  node tools/build_static.mjs --check || { echo "Rebuild the static assets before deploying"; exit 1; }
else
  echo "note: node/node_modules not found - skipped the static build freshness check"
fi
# Number game safety: fails if it is live for all roles without the safeguards (see README).
python -m app.cli bola-preflight
pip install -q pip-audit
pip-audit -r requirements.txt || { echo "Vulnerable dependencies found"; exit 1; }
pip freeze > requirements.lock && echo "Wrote requirements.lock (install with: pip install -r requirements.lock)"
