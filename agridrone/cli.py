import argparse
import sys

from .agent import run_cycle
from .config import REPORT_DIR
from .reporting import build_report
from datetime import UTC


def _imagery(a):
    import json

    from . import imagery
    from .config import load_policy
    from .store import STATE_DIR

    pol = load_policy()
    try:
        if a.action == "fetch":
            scenes, new = imagery.fetch(pol, days=a.days)
            print(f"{len(new)} new clear scene(s); {len(scenes)} cached in {STATE_DIR}/imagery.json")
            for s in new:
                print(f"  {s.id}  {s.date}  tile cloud {s.cloud_scene}%  clear over field {round(100 * s.valid_frac)}%")
            return 0
        scenes = imagery.load_scenes()
        if not scenes:
            print("no imagery cached: run `agridrone imagery fetch`")
            return 1
        if a.action == "map":
            print(f"{scenes[-1].id} ({scenes[-1].date}) NDVI, north up; ' ' = low .. '@' = high, '?' = cloud/no data")
            print(imagery.render_ascii(scenes[-1]))
        else:
            print(json.dumps(imagery.analyze(scenes), indent=1))
        return 0
    except imagery.ImageryUnavailable as e:
        print(f"imagery unavailable: {e}")
        return 2


def main(argv=None):
    p = argparse.ArgumentParser("agridrone")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle", help="run N operating cycles (days)")
    c.add_argument("-n", type=int, default=1)
    f = sub.add_parser("fly", help="ground station: fly the newest queued mission once")
    f.add_argument("--approve", help="queue id a human approves for flight")
    f.add_argument("--dry-run", action="store_true", help="build waypoints, connect to nothing")
    g = sub.add_parser("ground-station", help="unattended daemon at the farm")
    g.add_argument("--poll", type=int, default=300)
    g.add_argument("--no-git", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    im = sub.add_parser("imagery", help="real Sentinel-2 satellite imagery for the field")
    im.add_argument("action", choices=["fetch", "analyze", "map"])
    im.add_argument("--days", type=int, default=60)
    sub.add_parser("verify", help="check state files are healthy (autopilot runs this before committing)")
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
    if a.cmd == "fly":
        from .ground_station import process_once

        print(process_once(human_approved=a.approve, dry_run=a.dry_run) or "nothing pending / awaiting approval")
        return 0
    if a.cmd == "ground-station":
        from .ground_station import daemon

        daemon(a.poll, sync=not a.no_git, dry_run=a.dry_run)
    if a.cmd == "imagery":
        return _imagery(a)
    if a.cmd == "verify":
        from .verify import verify

        probs = verify()
        print("state OK" if not probs else "STATE PROBLEMS:\n  - " + "\n  - ".join(probs))
        return 1 if probs else 0
    if a.cmd == "dashboard":
        from .dashboard import write

        print(write())
        return 0
    text = build_report(a.kind)
    if a.write:
        from datetime import datetime

        REPORT_DIR.mkdir(exist_ok=True)
        path = REPORT_DIR / f"{a.kind}-{datetime.now(UTC):%Y-%m-%d}.md"
        path.write_text(text)
        print(path)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
