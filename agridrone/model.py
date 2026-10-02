"""Crop-stress classifier (pure-Python logistic regression) + drift monitoring (PSI).
Swap for a PyTorch/ONNX model by keeping the predict/train/accuracy interface."""
import math
import random


def _sig(z):
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


class StressModel:
    def __init__(self, w=None, version=0):
        self.w = w or [0.0, 0.0, 0.0, 0.0]  # bias, ndvi, moisture, pest
        self.version = version

    def prob(self, ndvi, moisture, pest):
        w = self.w
        return _sig(w[0] + w[1] * ndvi + w[2] * moisture + w[3] * pest)

    def train(self, rows, epochs=60, lr=0.5, seed=0):
        """rows: [(ndvi, moisture, pest, label)]"""
        rng = random.Random(seed)
        w = list(self.w)
        for _ in range(epochs):
            rng.shuffle(rows)
            for n, m, p, y in rows:
                err = _sig(w[0] + w[1] * n + w[2] * m + w[3] * p) - y
                w[0] -= lr * err
                w[1] -= lr * err * n
                w[2] -= lr * err * m
                w[3] -= lr * err * p
        self.w = w
        self.version += 1

    def accuracy(self, rows):
        if not rows:
            return 1.0
        return sum((self.prob(n, m, p) > 0.5) == bool(y) for n, m, p, y in rows) / len(rows)

    def to_dict(self):
        return {"w": self.w, "version": self.version}

    @classmethod
    def from_dict(cls, d):
        return cls(d["w"], d["version"])


def true_label(cell) -> int:
    """Scouted ground truth (stands in for expert/agronomist labels)."""
    return int(cell.moisture < 0.28 or cell.pest > 0.5)


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
