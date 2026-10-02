import argparse
import sys
from pathlib import Path

from .agent import run_cycle
from .config import REPORT_DIR
from .reporting import build_report


def main(argv=None):
    p = argparse.ArgumentParser("agridrone")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle", help="run N operating cycles (days)")
    c.add_argument("-n", type=int, default=1)
    sub.add_parser("dashboard", help="write docs/dashboard.html")
    r = sub.add_parser("report")
    r.add_argument("kind", choices=["weekly", "monthly"])
    r.add_argument("--write", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "cycle":
        bad = 0
        for _ in range(a.n):
            rec = run_cycle()
            print({k: rec.get(k) for k in ("ok", "day", "executed", "accuracy", "water_saved_pct", "chem_saved_pct", "yield_index")})
            bad += not rec["ok"]
        return 1 if bad else 0
    if a.cmd == "dashboard":
        from .dashboard import write
        print(write())
        return 0
    text = build_report(a.kind)
    if a.write:
        from datetime import datetime, timezone
        REPORT_DIR.mkdir(exist_ok=True)
        path = REPORT_DIR / f"{a.kind}-{datetime.now(timezone.utc):%Y-%m-%d}.md"
        path.write_text(text)
        print(path)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
