#!/usr/bin/env python3
"""Aggregate the token ledger: lifetime totals, by project, month, model.

  python3 report.py                 # full report
  python3 report.py --csv out.csv   # also dump flat CSV
"""
import collections
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_core as lc  # noqa: E402

KEYS = ("input", "cache_write", "cache_read", "output", "total")


def fmt(n):
    return f"{n:,}"


def table(title, groups, limit=None):
    print()
    print("--- %s ---" % title)
    print("%-38s %12s %12s %12s %14s"
          % ("", "fresh in", "cache rd", "output", "total"))
    items = sorted(groups.items(), key=lambda kv: -kv[1]["total"])
    if limit:
        items = items[:limit]
    for name, agg in items:
        label = str(name)
        if len(label) > 38:
            label = "..." + label[-35:]
        print("%-38s %12s %12s %12s %14s"
              % (label, fmt(agg["input"] + agg["cache_write"]),
                 fmt(agg["cache_read"]), fmt(agg["output"]), fmt(agg["total"])))


def main(argv):
    ledger = lc.default_ledger_path()
    index = lc.load_ledger(ledger)
    rows = list(index.values())
    if not rows:
        print("Ledger is empty or missing: %s" % ledger)
        print("Run:  python3 scan_history.py")
        return 1

    def newagg():
        return dict.fromkeys(KEYS, 0)

    lifetime = newagg()
    by_project, by_month, by_model, by_session = ({} for _ in range(4))

    for r in rows:
        for k in KEYS:
            lifetime[k] += r.get(k, 0)
        proj = (r.get("project") or "unknown").rstrip("/")
        month = (r.get("ts") or "unknown")[:7]
        model = r.get("model") or "unknown"
        sess = r.get("session_id") or "unknown"
        for bucket, key in ((by_project, proj), (by_month, month),
                            (by_model, model), (by_session, sess)):
            agg = bucket.setdefault(key, newagg())
            for k in KEYS:
                agg[k] += r.get(k, 0)

    print("=" * 84)
    print("CLAUDE CODE TOKEN LEDGER")
    print("=" * 84)
    print("ledger file      : %s" % ledger)
    print("API calls logged : %s" % fmt(len(rows)))
    print("sessions         : %s" % fmt(len(by_session)))
    print("projects         : %s" % fmt(len(by_project)))
    stamps = sorted(r["ts"] for r in rows if r.get("ts"))
    if stamps:
        print("date range       : %s  ->  %s" % (stamps[0][:10], stamps[-1][:10]))
    print()
    print("--- LIFETIME ---")
    print("fresh input      %15s" % fmt(lifetime["input"]))
    print("cache writes     %15s" % fmt(lifetime["cache_write"]))
    print("cache reads      %15s" % fmt(lifetime["cache_read"]))
    print("output           %15s" % fmt(lifetime["output"]))
    print("TOTAL PROCESSED  %15s" % fmt(lifetime["total"]))
    print("billable input   %15s" % fmt(lifetime["input"] + lifetime["cache_write"]))
    if lifetime["total"]:
        pct = 100.0 * lifetime["cache_read"] / lifetime["total"]
        print("cache reads are %.1f%% of all tokens processed" % pct)

    table("BY PROJECT", by_project)
    table("BY MONTH", by_month)
    table("BY MODEL", by_model)
    table("TOP 10 SESSIONS", by_session, limit=10)

    if "--csv" in argv:
        out = Path(argv[argv.index("--csv") + 1])
        cols = ["ts", "project", "branch", "model", "effort", "session_id",
                "msg_id", "input", "cache_write", "cache_read", "output",
                "total", "cli_version"]
        with out.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in sorted(rows, key=lambda r: r.get("ts") or ""):
                w.writerow(r)
        print()
        print("CSV written: %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
