#!/usr/bin/env python3
"""Shared core for the Claude Code token ledger.

Reads Claude Code transcripts (~/.claude/projects/**/*.jsonl) and maintains a
durable append-only ledger of per-API-call token usage.

CRITICAL: a single API message can appear as MULTIPLE transcript records
(different record uuids, same message.id). Dedupe on message.id or you will
over-count. Verified on real transcripts: 38 assistant records -> 20 API calls.
"""
import json
import os
import sys
from pathlib import Path

LEDGER_ENV = "CLAUDE_LEDGER_PATH"


def default_ledger_path() -> Path:
    env = os.environ.get(LEDGER_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / "usage-ledger" / "ledger.jsonl"


def transcript_roots():
    """Every directory that may hold Claude Code transcripts."""
    roots = []
    for base in (Path.home() / ".claude" / "projects",
                 Path("/root/.claude/projects")):
        if base.is_dir():
            roots.append(base)
    extra = os.environ.get("CLAUDE_EXTRA_TRANSCRIPT_DIRS", "")
    for part in extra.split(os.pathsep):
        p = Path(part).expanduser()
        if part and p.is_dir():
            roots.append(p)
    # de-dupe while preserving order
    seen, out = set(), []
    for r in roots:
        k = r.resolve()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def iter_transcripts():
    for root in transcript_roots():
        for p in sorted(root.rglob("*.jsonl")):
            yield p


def _num(v):
    return v if isinstance(v, (int, float)) else 0


def rows_from_transcript(path: Path):
    """Yield one normalized row per unique API message in a transcript."""
    seen = set()
    meta = {"project": None, "branch": None, "version": None, "session": None}
    try:
        fh = path.open("r", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (ValueError, TypeError):
                continue

            # Track session metadata from any record type.
            meta["session"] = d.get("sessionId") or meta["session"]
            if d.get("cwd"):
                meta["project"] = d["cwd"]
            if d.get("gitBranch"):
                meta["branch"] = d["gitBranch"]
            if d.get("version"):
                meta["version"] = d["version"]

            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            mid = msg.get("id")
            usage = msg.get("usage") or {}
            if not mid or mid in seen or not usage:
                continue
            seen.add(mid)

            inp = _num(usage.get("input_tokens"))
            out = _num(usage.get("output_tokens"))
            cr = _num(usage.get("cache_read_input_tokens"))
            cw = _num(usage.get("cache_creation_input_tokens"))

            yield {
                "msg_id": mid,
                "session_id": meta["session"] or path.stem,
                "ts": d.get("timestamp"),
                "project": meta["project"] or path.parent.name,
                "branch": meta["branch"],
                "model": msg.get("model") or d.get("requestedModel"),
                "effort": d.get("effort") or d.get("perTurnEffort"),
                "cli_version": meta["version"],
                "input": inp,
                "output": out,
                "cache_read": cr,
                "cache_write": cw,
                "total": inp + out + cr + cw,
                "billable_input": inp + cw,
                "transcript": str(path),
            }


def load_ledger(ledger: Path):
    """Return {msg_id: row} for an existing ledger."""
    index = {}
    if not ledger.exists():
        return index
    with ledger.open("r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            mid = row.get("msg_id")
            if mid:
                index[mid] = row
    return index


def append_rows(ledger: Path, rows):
    """Append rows whose msg_id is not already in the ledger. Returns count."""
    index = load_ledger(ledger)
    new = [r for r in rows if r.get("msg_id") and r["msg_id"] not in index]
    if not new:
        return 0
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as fh:
        for r in new:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return len(new)
