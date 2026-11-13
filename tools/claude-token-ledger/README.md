# Claude Code Token Ledger

A durable, append-only record of token usage across **every** Claude Code
session on a machine — all projects, all repos, all branches, all models —
plus a one-time backfill that recovers the history already sitting on disk.

## Why this exists

Anthropic exposes no lifetime token counter. `/context` shows the *current*
context window (a fuel gauge). `/usage` shows the *current* rate-limit window.
Both reset. Nothing accumulates. This does.

## Install (once per machine, covers everything)

```
bash install.sh
```

That copies the scripts to `~/.claude/token-ledger/`, registers a `Stop` hook in
`~/.claude/settings.json`, and backfills every transcript already on disk.

Because the hook lives in **user-level** settings, it applies to every project
automatically — all six GitHub repos, Roblox project folders, scratch
directories, anything you open in future. There is no per-repo step.

## Use

```
python3 ~/.claude/token-ledger/report.py
python3 ~/.claude/token-ledger/report.py --csv usage.csv
python3 ~/.claude/token-ledger/scan_history.py          # re-backfill anytime
```

Ledger location: `~/.claude/usage-ledger/ledger.jsonl`
Override with `CLAUDE_LEDGER_PATH`. One JSON row per API call.

## What is captured

Anything that writes a Claude Code transcript to `~/.claude/projects/`:

- Claude Code in the terminal (local)
- Claude Code in the desktop app
- Claude Code IDE extensions (VS Code, JetBrains)
- Claude Code cloud/web sessions — **with a caveat, see below**

Per API call it records: timestamp, project path, git branch, model, effort,
session id, fresh input, cache writes, cache reads, output, and total.

## What is NOT captured, and cannot be

- **claude.ai chat conversations.** The chat UI exposes no token counts. The
  official data export gives message text only. There is no API for it. This is
  a hard wall, not a gap in this tool.
- **Claude mobile / desktop *chat* usage** (as opposed to Claude Code) — same
  wall.
- **Roblox Studio work itself.** Editing a place or script in Studio consumes no
  Claude tokens. Only sessions where Claude Code touched the files are counted,
  and those are captured automatically.
- **Past cloud sessions.** Cloud containers are reclaimed after inactivity and
  their transcripts are destroyed with them. Usage from cloud sessions that have
  already ended is unrecoverable. Local transcripts, by contrast, persist — which
  is why the backfill is worth running.

## Cloud sessions

A cloud container's `~/.claude` is wiped when the container dies. When the hook
detects an ephemeral container it mirrors the ledger to
`$CLAUDE_PROJECT_DIR/.claude-usage/ledger.jsonl` inside the repo, so the rows can
be committed and survive. Commit that file, or the session's usage is lost.

To force mirroring anywhere: set `CLAUDE_LEDGER_MIRROR=1`.

## Correctness note

A single API message appears as **multiple** transcript records — different
record uuids, identical `message.id`. Summing per record double-counts. This
tool dedupes on `message.id`. Verified against a real transcript: 38 assistant
records resolved to 20 distinct API calls.

## Reading the numbers

`cache_read` dominates — typically 95%+ of all tokens processed. That is the
fixed prefix (system prompt, tool schemas, skill listings) being re-read on
every single turn. It is billed at a fraction of fresh input, so `billable
input` (fresh input + cache writes) is the figure that tracks real cost, while
`TOTAL PROCESSED` shows how much work the overhead is actually causing.

## Files

| file | role |
|---|---|
| `ledger_core.py` | transcript parsing, dedupe, ledger I/O |
| `log_turn.py` | `Stop` hook; self-healing incremental ingest |
| `scan_history.py` | one-time/any-time full backfill |
| `report.py` | aggregates by project, month, model, session |
| `install.sh` | global install + hook registration + backfill |
