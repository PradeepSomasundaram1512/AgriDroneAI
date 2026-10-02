import argparse
import sys

from .agent import run_cycle
from .config import REPORT_DIR
from .reporting import build_report
from datetime import UTC


def _export(a):
    from pathlib import Path

    from . import export
    from .config import load_policy
    from .safety import Mission
    from .store import load_json

    pol = load_policy()
    rec = load_json("last_mission.json", None)
    if not rec or not rec.get("missions"):
        print("no flights recorded yet (state/last_mission.json)")
        return 1
    ms = [
        Mission(
            m["drone"],
            m["alt"],
            [((c[0], c[1]), c[2]) for c in m["targets"]],
            m.get("energy_wh", 0),
            m.get("sortie", 0),
            m.get("t0", 0),
            m.get("hold", 0),
            m.get("pad", -1),
        )
        for m in rec["missions"]
        if (m.get("fate") or {}).get("status") != "cancelled"
    ]
    out = Path(a.out) if a.out else Path("exports") / f"day{rec['day']}"
    files = export.write_all(ms, pol, out)
    print(f"{len(ms)} flights -> {out}/")
    for f in files:
        print("  ", f.name)
    return 0


def _economics(a):
    import json

    from . import economics
    from .config import ROOT, load_policy

    pol = load_policy()
    bench = json.loads((ROOT / "docs" / "benchmark.json").read_text())
    ha = bench["patches"] * pol["economics"]["patch_ha"]
    print(f"{bench['patches']} patches ({ha:.0f} ha), {bench['days']}-day season, PLACEHOLDER prices (policy.economics)\n")
    print(f"{'strategy':26s} {'revenue':>10s} {'water':>8s} {'spray':>8s} {'fleet':>8s} {'profit':>10s} {'$/ha':>8s}")
    for r in economics.compare(bench, pol, a.drones):
        cols = (r["revenue"], r["water_cost"], r["spray_cost"], r["fleet_cost"], r["profit"])
        print(f"{r['name']:26s} " + " ".join(f"{v:>{w},}" for v, w in zip(cols, (10, 8, 8, 8, 10))) + f" {r['profit_per_ha']:>8}")
    pb = economics.payback_days(pol, bench, a.drones)
    print(f"\nfleet payback vs the fixed schedule: {'never (under these prices)' if pb is None else f'{pb} days of operation'}")
    return 0


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
    ex = sub.add_parser("export", help="write the latest flights as QGroundControl .plan files + GeoJSON/CSV prescription map")
    ex.add_argument("--out", default=None, help="output folder (default: exports/day<N>)")
    ec = sub.add_parser("economics", help="profit per strategy from the measured benchmark (placeholder prices in policy.economics)")
    ec.add_argument("--drones", type=int, default=3)
    sub.add_parser("brief", help="today's plain-language briefing")
    ak = sub.add_parser("ask", help="ask the farm a question in plain English (rules; Claude too if ANTHROPIC_API_KEY is set)")
    ak.add_argument("question", nargs="+")
    ds = sub.add_parser("docs-sync", help="regenerate (or --check) the benchmark tables in the docs from docs/benchmark.json")
    ds.add_argument("--check", action="store_true")
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
    if a.cmd in ("brief", "ask"):
        from . import briefing
        from .dashboard import build_data

        data = build_data()
        if a.cmd == "brief":
            print("\n".join("- " + s for s in briefing.sentences(data)))
        else:
            text, src = briefing.ask(" ".join(a.question), data)
            print(f"{text}\n\n[answered by: {src}]")
        return 0
    if a.cmd == "docs-sync":
        from . import docsync
        from .config import ROOT

        ok = docsync.sync(ROOT / "docs" / "ARCHITECTURE.md", ROOT / "docs" / "benchmark.json", write=not a.check)
        print(
            "docs match the benchmark data"
            if ok
            else ("docs DRIFTED from docs/benchmark.json" if a.check else "docs updated from docs/benchmark.json")
        )
        return 0 if (ok or not a.check) else 1
    if a.cmd == "export":
        return _export(a)
    if a.cmd == "economics":
        return _economics(a)
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
