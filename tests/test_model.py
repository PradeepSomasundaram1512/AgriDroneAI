from agridrone import model as M, planner
from agridrone.config import load_policy
from agridrone.sim import Farm
import random


def labelled(n=400, seed=3, noise=0.02):
    r = random.Random(seed)
    out = []
    for _ in range(n):
        m, p = r.uniform(0.05, 0.55), r.uniform(0, 0.9)
        out.append((r.gauss(0, 0.05), m + r.gauss(0, noise), p + r.gauss(0, noise), M.rule_label(m, p)))
    return out


def test_tree_learns_the_or_rule_a_linear_model_cannot():
    train, test = labelled(500, 1), labelled(300, 2)
    mdl = M.StressModel(); assert mdl.train(train)
    s = mdl.score(test)
    assert s["bal"] > 0.9 and s["recall"] > 0.9


def test_one_class_data_is_refused_not_learned():
    mdl = M.StressModel()
    assert not mdl.train([(0, 0.4, 0.1, 0)] * 50) and mdl.tree is None


def test_rare_positives_are_not_ignored():
    rows = [r for r in labelled(900, 4) if not r[3]][:300] + [r for r in labelled(900, 5) if r[3]][:15]
    mdl = M.StressModel(); mdl.train(rows)
    assert mdl.score(labelled(400, 6))["recall"] > 0.8   # class weighting: 15 positives vs 300 negatives still learned


def test_planner_waters_dry_cells_even_if_model_is_useless():
    pol = load_policy(); f = Farm.create(8, 1)
    for c in f.cells.values():
        c.moisture, c.pest = 0.05, 0.0
    dead = M.StressModel({"p": 0.0}, 1)          # model that never flags anything
    t = planner.find_targets(f, dead, pol["thresholds"])
    assert len(t) == len(f.cells) and all(a == "irrigate" for _, _, a in t)
