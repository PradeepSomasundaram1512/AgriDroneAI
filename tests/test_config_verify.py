import copy
import json
import pytest
from agridrone.config import PolicyError, load_policy, validate_policy
from agridrone.verify import verify
from agridrone.agent import run_cycle


def pol():
    return copy.deepcopy(load_policy())


def test_shipped_policy_is_valid():
    assert validate_policy(pol())


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda p: p.update(autonomy_level="yolo"), "autonomy_level"),
        (lambda p: p["fleet"].update(min_reserve_pct=2), "min_reserve_pct"),
        (lambda p: p["fleet"].update(drones=0), "fleet.drones"),
        (lambda p: p["thresholds"].update(moisture_irrigate=0.9), "moisture_irrigate"),
        (lambda p: p.update(no_fly_cells=[[99, 99]]), "no_fly_cells"),
        (lambda p: p.update(no_fly_cells=[[0, 0]]), "home pad"),
        (lambda p: p["field"].update(size=2), "field.size"),
        (lambda p: p.update(kill_switch="no"), "kill_switch"),
        (lambda p: p["hardware"].update(enabled=True), "hardware"),
    ],
)
def test_bad_policy_is_rejected_with_a_clear_message(mutate, needle):
    p = pol()
    mutate(p)
    with pytest.raises(PolicyError, match=needle):
        validate_policy(p)


def test_all_problems_are_reported_at_once():
    p = pol()
    p["fleet"]["drones"] = 0
    p["fleet"]["min_reserve_pct"] = 1
    with pytest.raises(PolicyError) as e:
        validate_policy(p)
    assert "fleet.drones" in str(e.value) and "min_reserve_pct" in str(e.value)


def test_verify_passes_on_a_healthy_state_and_catches_corruption(tmp_path):
    p = pol()
    p["field"]["size"] = 8
    for _ in range(3):
        run_cycle(p, tmp_path)
    assert verify(tmp_path) == []
    (tmp_path / "model.json").write_text("{broken")
    assert any("model.json" in x for x in verify(tmp_path))


def test_verify_catches_nan_and_wrong_cell_count_and_bad_jsonl(tmp_path):
    p = pol()
    p["field"]["size"] = 8
    run_cycle(p, tmp_path)
    fd = json.loads((tmp_path / "farm.json").read_text())
    fd["cells"] = fd["cells"][:-1]
    (tmp_path / "farm.json").write_text(json.dumps(fd))
    (tmp_path / "audit.jsonl").write_text('{"ok":1}\nnot json\n')
    probs = verify(tmp_path)
    assert any("cells" in x for x in probs) and any("audit.jsonl" in x for x in probs)
    (tmp_path / "x.json").write_text('{"v": NaN}')
    assert any("NaN" in x for x in verify(tmp_path))
