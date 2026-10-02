"""Does planning around forecast wind blackouts pay?  Same worlds, lookahead off vs on.
PYTHONPATH=. python scripts/eval_lookahead.py [seeds=4] [days=120]"""

import os
import statistics as st
import sys

os.environ["AGRIDRONE_OFFLINE"] = "1"
sys.path.insert(0, os.path.dirname(__file__))
from benchmark import agent  # noqa: E402


def main(seeds=(1, 2, 3, 4), days=120, size=24):
    print(
        f"{days}-day season, {size}x{size} patches, seeds {list(seeds)}\n{'':14s} {'yield':>14s} {'water mm/patch':>15s} {'sprays/patch':>13s}"
    )
    out = {}
    for label, ov in (("no lookahead", None), ("lookahead", {"lookahead": {"enabled": True}})):
        v = [agent(s, days, size, 3, overrides=ov) for s in seeds]
        y = [r[0] for r in v]
        out[label] = st.mean(y)
        print(
            f"{label:14s} {st.mean(y):.4f} +- {st.pstdev(y):.4f}   {st.mean(r[1] for r in v) / size**2:12.0f}   {st.mean(r[2] for r in v) / size**2:11.2f}",
            flush=True,
        )
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    main(tuple(range(1, int(a[0]) + 1)) if a else (1, 2, 3, 4), int(a[1]) if len(a) > 1 else 120)
