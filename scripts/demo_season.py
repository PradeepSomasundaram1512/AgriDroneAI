"""Fast-forward a full season in a THROWAWAY state folder and render a clearly labelled demo dashboard (docs/dashboard_demo.html).
The real autopilot state (state/) is never touched.   PYTHONPATH=. python scripts/demo_season.py [days=120]"""

import os
import sys
import tempfile

os.environ["AGRIDRONE_OFFLINE"] = "1"
from agridrone import config, dashboard
from agridrone.agent import run_cycle

BANNER = (
    '<div style="background:#b45309;color:#fff;padding:10px 16px;text-align:center;font:600 14px system-ui">'
    "DEMO: {n} SIMULATED days, fast-forwarded in a throwaway copy (synthetic weather). Not the live autopilot record.</div>"
)


def main(days=120):
    d = tempfile.mkdtemp(prefix="agridrone-demo-")
    pol = config.load_policy()
    pol["weather"]["source"] = "synthetic"
    for i in range(days):
        rec = run_cycle(pol, d)
        assert rec["ok"], rec
        if (i + 1) % 20 == 0:
            print(f"day {i + 1}: yield {rec['yield_index']}, water saved {rec['water_saved_pct']}%", flush=True)
    html = dashboard.render(d).replace("<body>", "<body>" + BANNER.format(n=days), 1)
    out = config.ROOT / "docs" / "dashboard_demo.html"
    out.write_text(html)
    print("wrote", out)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120)
