#!/usr/bin/env bash
# Install the token ledger GLOBALLY: one Stop hook in ~/.claude/settings.json
# covers every project on this machine, now and in future. No per-repo setup.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/token-ledger"
SETTINGS="$HOME/.claude/settings.json"

mkdir -p "$DEST" "$HOME/.claude/usage-ledger"
cp "$SRC/ledger_core.py" "$SRC/log_turn.py" "$SRC/scan_history.py" "$SRC/report.py" "$DEST/"
chmod +x "$DEST"/*.py

PY="$(command -v python3)"
if [ -z "$PY" ]; then echo "python3 not found on PATH" >&2; exit 1; fi

"$PY" - "$SETTINGS" "$DEST" "$PY" <<'PYEOF'
import json, sys, pathlib
settings_path, dest, py = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
cmd = f'{py} {dest}/log_turn.py'

data = {}
if settings_path.exists():
    try:
        data = json.loads(settings_path.read_text() or "{}")
    except ValueError:
        backup = settings_path.with_suffix(".json.bak")
        backup.write_text(settings_path.read_text())
        print(f"existing settings.json was not valid JSON; backed up to {backup}")
        data = {}

hooks = data.setdefault("hooks", {})
stop = hooks.setdefault("Stop", [])
if not isinstance(stop, list):
    stop = []
    hooks["Stop"] = stop

already = any(
    isinstance(entry, dict)
    and any(isinstance(h, dict) and "log_turn.py" in str(h.get("command", ""))
            for h in entry.get("hooks", []))
    for entry in stop
)
if already:
    print("Stop hook already installed - left as is")
else:
    stop.append({"hooks": [{"type": "command", "command": cmd, "timeout": 20}]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Stop hook added to {settings_path}")
PYEOF

echo
echo "Backfilling every transcript already on this machine..."
"$PY" "$DEST/scan_history.py"
echo
echo "Done. From now on:   python3 $DEST/report.py"
