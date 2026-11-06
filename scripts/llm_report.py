"""Summarise logs/llm_calls.jsonl.

    python -m scripts.llm_report              # everything logged so far
    python -m scripts.llm_report --clear      # start a fresh measurement run
"""
import json
import statistics
import sys
from collections import defaultdict

from app.metrics import LOG_FILE


def avg(rows, key):
    return statistics.mean(r.get(key, 0) for r in rows)


def main() -> None:
    if "--clear" in sys.argv:
        LOG_FILE.unlink(missing_ok=True)
        print("cleared", LOG_FILE)
        return
    if not LOG_FILE.exists():
        print("No data yet. Run the app and perform some actions first.")
        return
    ops = defaultdict(list)
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["type"] == "operation":
            ops[r["operation"]].append(r)
    print(f"{'operation':<18}{'runs':>5}{'total s':>9}{'max s':>8}{'model s':>9}{'AI calls':>9}"
          f"{'DB stmts':>9}{'DB s':>7}{'checkouts':>10}{'commits':>8}{'emails':>7}{'email s':>9}")
    for name, rows in sorted(ops.items()):
        print(f"{name:<18}{len(rows):>5}{avg(rows, 'total_seconds'):>9.2f}"
              f"{max(r['total_seconds'] for r in rows):>8.2f}{avg(rows, 'model_seconds'):>9.2f}"
              f"{avg(rows, 'llm_calls'):>9.1f}{avg(rows, 'db_statements'):>9.1f}"
              f"{avg(rows, 'db_seconds'):>7.2f}{avg(rows, 'db_checkouts'):>10.1f}"
              f"{avg(rows, 'db_commits'):>8.1f}{avg(rows, 'emails_sent'):>7.1f}"
              f"{avg(rows, 'email_seconds'):>9.2f}")
    print("\n(all numbers are averages per run, except 'runs' and 'max s')")
    print("\nLast 10 operations:")
    flat = sorted((r for rows in ops.values() for r in rows), key=lambda r: r["at"])[-10:]
    for r in flat:
        print(f"  {r['at'][11:19]} {r['operation']:<16} {r['total_seconds']:>6.2f}s  "
              f"AI {r['llm_calls']}  DB {r.get('db_statements', 0)}  {r.get('note', '')[:70]}")


if __name__ == "__main__":
    main()
