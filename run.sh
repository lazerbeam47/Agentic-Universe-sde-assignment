#!/usr/bin/env bash
#!/usr/bin/env bash
# Start with: pip install -r requirements.txt
# The controller persists its cursor, event ledger, proposals and audit trail in SQLite.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
DB_PATH="${CONTROLLER_DB:-controller.sqlite3}"
exec python3 controller.py "$SIM_URL" --db "$DB_PATH"
