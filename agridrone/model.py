"""Crop-stress classifier (pure-Python class-balanced decision tree) + drift monitoring (PSI).
Swap for a PyTorch/ONNX model by keeping the prob/train/score/to_dict/from_dict interface.

Why a tree: stress is "too dry OR too infested", an OR of two thresholds that a single linear/logistic boundary
cannot represent (it missed whole stress events). Why class weights: stressed cells are rare most days, and an
unweighted model scores 95%+ accuracy by never flagging anything."""
import math
import random

FEATURES = ("ndvi", "moisture", "pest")


def rule_label(moisture, pest) -> int:
    """Agronomic rule used for the expert-labelled seed set and as simulator ground truth."""
    return int(moisture < 0.28 or pest > 0.5)


def true_label(cell) -> int:
    """Scouted ground truth (stands in for expert/agronomist labels)."""
    return rule_label(cell.moisture, cell.pest)


def median(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def seed_rows(n=160, seed=11):
    """Expert-labelled starter set spread over the feature space, so the model is never blind on day 1.
    Feature 0 is the NDVI *anomaly* (cell NDVI minus the field median that day), which removes the crop-growth trend.
    NOTE: simulator labels come from the same rule, so seed quality is optimistic vs. real agronomist labels."""
    r = random.Random(seed)
    rows = []
    for i in range(n):
        m, p = r.uniform(0.05, 0.55), r.uniform(0.0, 0.9)
        rows.append((r.gauss(0, 0.06), m + r.gauss(0, 0.02), p + r.gauss(0, 0.02), rule_label(m, p)))
    return rows


def _gini(wp, wn):
    t = wp + wn
    return 0.0 if t == 0 else 2 * (wp / t) * (wn / t)


def _build(rows, wpos, wneg, depth, max_depth, min_leaf, min_gain=0.02, root_imp=None):
    wp = sum(wpos for r in rows if r[3])
    wn = sum(wneg for r in rows if not r[3])
    leaf = {"p": (wp / (wp + wn)) if wp + wn else 0.0}
    if depth >= max_depth or len(rows) < 2 * min_leaf or wp == 0 or wn == 0:
        return leaf
    best = None
    base = _gini(wp, wn) * (wp + wn)
    root_imp = base if root_imp is None else root_imp
    for f in range(3):
        vals = sorted({r[f] for r in rows})
        cands = [(vals[i] + vals[i + 1]) / 2 for i in range(0, len(vals) - 1, max(1, len(vals) // 24))]
        for t in cands:
            lp = sum(wpos for r in rows if r[f] <= t and r[3]); ln = sum(wneg for r in rows if r[f] <= t and not r[3])
            nl = sum(1 for r in rows if r[f] <= t)
            if nl < min_leaf or len(rows) - nl < min_leaf:
                continue
            score = _gini(lp, ln) * (lp + ln) + _gini(wp - lp, wn - ln) * (wp + wn - lp - ln)
            if best is None or score < best[0]:
                best = (score, f, t)
    if best is None or base - best[0] < min_gain * root_imp:   # split must clearly help, else it is fitting noise/shortcuts
        return leaf
    _, f, t = best
    left = [r for r in rows if r[f] <= t]; right = [r for r in rows if r[f] > t]
    return {"f": f, "t": t, "l": _build(left, wpos, wneg, depth + 1, max_depth, min_leaf, min_gain, root_imp),
            "r": _build(right, wpos, wneg, depth + 1, max_depth, min_leaf, min_gain, root_imp)}


class StressModel:
    def __init__(self, tree=None, version=0):
        self.tree, self.version = tree, version

    def prob(self, ndvi, moisture, pest):
        if self.tree is None:
            return 0.0
        x, node = (ndvi, moisture, pest), self.tree
        while "f" in node:
            node = node["l"] if x[node["f"]] <= node["t"] else node["r"]
        return node["p"]

    def train(self, rows, max_depth=3, min_leaf=6):
        """rows: [(ndvi, moisture, pest, label)]. Class-balanced: each class gets equal total weight."""
        rows = [tuple(r) for r in rows]
        npos = sum(1 for r in rows if r[3]); nneg = len(rows) - npos
        if npos == 0 or nneg == 0:
            return False  # a one-class model is useless: refuse rather than learn "everything is fine"
        self.tree = _build(rows, 0.5 / npos, 0.5 / nneg, 0, max_depth, min_leaf)
        self.version += 1
        return True

    def score(self, rows):
        """-> dict(bal, recall, spec, npos, nneg). bal/recall are None if the set lacks that class."""
        tp = fn = tn = fp = 0
        for n, m, p, y in rows:
            pred = self.prob(n, m, p) > 0.5
            tp += pred and y; fn += (not pred) and y; tn += (not pred) and not y; fp += pred and not y
        rec = tp / (tp + fn) if tp + fn else None
        spec = tn / (tn + fp) if tn + fp else None
        bal = (rec + spec) / 2 if rec is not None and spec is not None else None
        return {"bal": bal, "recall": rec, "spec": spec, "npos": tp + fn, "nneg": tn + fp}

    def accuracy(self, rows):
        if not rows:
            return 1.0
        return sum((self.prob(n, m, p) > 0.5) == bool(y) for n, m, p, y in rows) / len(rows)

    def to_dict(self):
        return {"tree": self.tree, "version": self.version}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("tree"), d.get("version", 0) if d.get("tree") else 0)  # legacy logistic weights => retrain


def psi(ref, cur, bins=10):
    """Population Stability Index between two samples."""
    lo, hi = min(ref + cur), max(ref + cur)
    if hi == lo:
        return 0.0
    def hist(v):
        h = [0] * bins
        for x in v:
            h[min(bins - 1, int((x - lo) / (hi - lo) * bins))] += 1
        return [(c + 0.5) / (len(v) + 0.5 * bins) for c in h]
    a, b = hist(ref), hist(cur)
    return sum((y - x) * math.log(y / x) for x, y in zip(a, b))
