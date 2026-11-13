#!/usr/bin/env python3
"""Recover ALL historical token usage from every Claude Code transcript on disk.

Run this once on each machine where you use Claude Code. It walks every
transcript Claude Code has ever written locally and backfills the ledger.

  python3 scan_history.py              # backfill ledger, print summary
  python3 scan_history.py --dry-run    # report only, write nothing
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_core as lc  # noqa: E402


def main(argv):
    dry = "--dry-run" in argv
    ledger = lc.default_ledger_path()

    transcripts = list(lc.iter_transcripts())
    if not transcripts:
        print("No transcripts found. Searched:")
        for r in lc.transcript_roots() or ["(no transcript dirs exist)"]:
            print("  ", r)
        print("\nIf you use Claude Code on another machine, run this there too.")
        return 1

    all_rows = []
    for path in transcripts:
        all_rows.extend(lc.rows_from_transcript(path))

    uniq = {r["msg_id"]: r for r in all_rows}
    rows = list(uniq.values())

    print("transcripts scanned : %d" % len(transcripts))
    print("API calls found     : %d" % len(rows))

    if dry:
        added = 0
        print("dry run - ledger not written")
    else:
        added = lc.append_rows(ledger, rows)
        print("ledger              : %s" % ledger)
        print("new rows appended   : %d" % added)

    def tot(key):
        return sum(r.get(key, 0) for r in rows)

    print()
    print("--- LIFETIME TOTALS (all local Claude Code sessions) ---")
    print("fresh input      %15s" % f"{tot('input'):,}")
    print("cache writes     %15s" % f"{tot('cache_write'):,}")
    print("cache reads      %15s" % f"{tot('cache_read'):,}")
    print("output           %15s" % f"{tot('output'):,}")
    print("TOTAL PROCESSED  %15s" % f"{tot('total'):,}")
    print("billable input   %15s   (fresh input + cache writes)"
          % f"{tot('billable_input'):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
