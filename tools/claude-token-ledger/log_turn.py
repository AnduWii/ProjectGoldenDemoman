#!/usr/bin/env python3
"""Stop-hook: append this turn's token usage to the durable ledger.

Wired as a `Stop` hook, so it runs at the end of every turn in every project.
Reads the hook payload on stdin (hook_event_name, session_id, transcript_path,
cwd) and ingests any API calls from that transcript not already in the ledger.
Re-ingesting the whole transcript each time makes it self-healing: a missed
firing is backfilled on the next one.

Fails silently and always exits 0 - usage logging must never block a session.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOG = Path.home() / ".claude" / "usage-ledger" / "hook.log"


def note(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(msg.rstrip() + "\n")
    except OSError:
        pass


def is_ephemeral_container():
    """Cloud sessions get a fresh container; the home ledger won't survive."""
    if os.environ.get("CLAUDE_LEDGER_MIRROR"):
        return True
    return Path("/root/.ccr").exists()


def main():
    try:
        import ledger_core as lc
    except Exception as exc:  # noqa: BLE001
        note("import failed: %r" % exc)
        return 0

    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass

    payload = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            pass

    tpath = payload.get("transcript_path")
    candidates = []
    if tpath and Path(tpath).is_file():
        candidates.append(Path(tpath))
    else:
        # Fall back to every transcript for this session id.
        sid = payload.get("session_id")
        for p in lc.iter_transcripts():
            if not sid or sid in p.name:
                candidates.append(p)

    if not candidates:
        note("no transcript resolved; payload keys=%s" % sorted(payload))
        return 0

    rows = []
    for p in candidates:
        rows.extend(lc.rows_from_transcript(p))
    if not rows:
        return 0

    targets = [lc.default_ledger_path()]
    if is_ephemeral_container():
        proj = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
        if proj:
            targets.append(Path(proj) / ".claude-usage" / "ledger.jsonl")

    for t in targets:
        try:
            added = lc.append_rows(t, rows)
            if added:
                note("+%d rows -> %s" % (added, t))
        except Exception as exc:  # noqa: BLE001
            note("write failed %s: %r" % (t, exc))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001
        note("fatal: %r" % exc)
        code = 0
    sys.exit(code or 0)
