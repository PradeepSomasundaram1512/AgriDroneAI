"""The autopilot's behaviour must not change by accident. golden_cycle.json was captured from the code BEFORE run_cycle was split into stages;
every metric of every day, the final field state and the audit trail must still match exactly. If you change behaviour ON PURPOSE, regenerate it:
    AGRIDRONE_OFFLINE=1 PYTHONPATH=.:tests python -c "import json,golden; json.dump({n: golden.season(**c) for n, c in golden.CONFIGS.items()}, open('tests/golden_cycle.json','w'), indent=0, sort_keys=True)"
"""

import json
from pathlib import Path

import golden
import pytest

GOLD = json.loads((Path(__file__).parent / "golden_cycle.json").read_text())


@pytest.mark.parametrize("name", sorted(golden.CONFIGS))
def test_the_autopilot_still_behaves_exactly_as_recorded(name):
    now = golden.season(**golden.CONFIGS[name])
    want = GOLD[name]
    assert now["yield"] == want["yield"] and now["water"] == want["water"] and now["chem"] == want["chem"]
    assert now["audit"] == want["audit"]
    for i, (a, b) in enumerate(zip(now["rows"], want["rows"])):
        assert a == b, f"day {i + 1} differs: { ({k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}) }"
    assert len(now["rows"]) == len(want["rows"])
