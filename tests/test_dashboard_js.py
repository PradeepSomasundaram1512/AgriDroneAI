"""The dashboard is ~25 KB of JavaScript inside a Python string. Until now only a human looking at it caught its bugs (an
"Assignment to constant variable" in renderDrones once killed the whole animation). These tests EXECUTE the page's script in Node
against a stub browser, for several kinds of days, and fail on any uncaught exception."""

import json
import re
import shutil
import subprocess

import pytest

from agridrone import dashboard, store
from agridrone.agent import run_cycle
from agridrone.config import load_policy

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

STUB = r"""
const vm = require('vm');
const stub = () => new Proxy(function () {}, {
  get: (t, k) => k === Symbol.toPrimitive ? () => 400 : (k === 'length' ? 0 : (k === 'style' ? styleObj : stub())),
  apply: () => stub(), set: () => true, construct: () => stub(),
});
const styleObj = new Proxy({}, { get: () => '', set: () => true });
const els = {};
const el = (id) => els[id] || (els[id] = new Proxy({ dataset: {}, classList: { toggle() {}, add() {}, remove() {} }, style: styleObj, innerHTML: '', innerText: '', textContent: '', value: '0',
  getContext: () => ctx, getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 400, right: 400, bottom: 400 }), parentElement: { clientWidth: 400 },
  addEventListener() {}, querySelectorAll: () => [], scrollIntoView() {} }, { get: (t, k) => (k in t ? t[k] : (typeof k === 'symbol' ? undefined : 0)), set: (t, k, v) => { t[k] = v; return true; } }));
const ctx = new Proxy({}, { get: (t, k) => (k === 'measureText' ? () => ({ width: 10 }) : (typeof k === 'string' && k.startsWith('create') ? () => ({ addColorStop() {} }) : () => {})), set: () => true });
global.document = { querySelector: (s) => el(s), querySelectorAll: () => [], documentElement: { dataset: {}, style: styleObj }, hidden: false };
global.window = global; global.innerWidth = 900; global.devicePixelRatio = 1;
global.matchMedia = () => ({ matches: false }); global.getComputedStyle = () => ({ getPropertyValue: () => '#fff' });
global.addEventListener = () => {}; global.requestAnimationFrame = () => 0; global.performance = { now: () => 0 };
global.setInterval = () => 0; global.clearInterval = () => {}; global.setTimeout = () => 0; global.clearTimeout = () => {};
const src = require('fs').readFileSync(process.argv[2], 'utf8');
vm.runInThisContext(src, { filename: 'dashboard-script.js' });
"""

DRIVE = r"""
;(function () {
  for (let i = 0; i < 120; i++) frame(0.1);                       // animate the whole replay
  if (typeof drawLapse === 'function') { hi = 0; drawLapse(); hi = H.length - 1; drawLapse(); }
  for (const m of ['health', 'water', 'pests']) { mode = m; drawLapse(); }
  speed = 4; playing = true; reset(); for (let i = 0; i < 60; i++) frame(0.2);
  if (typeof renderDrones === 'function') renderDrones();
})();
"""


def script_of(html):
    return re.search(r"<script>(.*)</script>", html, re.S).group(1)


def run_node(tmp_path, html):
    js = tmp_path / "dash.js"
    js.write_text(script_of(html) + DRIVE)
    harness = tmp_path / "harness.js"
    harness.write_text(STUB)
    r = subprocess.run(["node", str(harness), str(js)], capture_output=True, text=True, timeout=60)
    return r


def state_after(tmp_path, days, **policy_kw):
    p = load_policy()
    p["field"]["size"] = 12
    p["imagery"]["enabled"] = False
    for k, v in policy_kw.items():
        sec, key = k.split("__")
        p[sec][key] = v
    for _ in range(days):
        run_cycle(p, tmp_path / "s")
    return p, tmp_path / "s"


def test_the_script_parses_and_runs_on_a_fresh_farm(tmp_path):
    p = load_policy()
    p["field"]["size"] = 12
    html = dashboard.render(tmp_path / "empty", p)
    r = run_node(tmp_path, html)
    assert r.returncode == 0, r.stderr[-1500:]


def test_the_script_runs_on_a_busy_day_with_waves_and_a_gps_loss(tmp_path):
    p, sd = state_after(tmp_path, 75, gps__loss_per_flight_hour=0.6)
    assert json.loads((sd / "last_mission.json").read_text())["missions"]  # a real flying day, not an empty one
    r = run_node(tmp_path, dashboard.render(sd, p))
    assert r.returncode == 0, r.stderr[-1500:]


def test_the_script_runs_with_every_optional_section_present(tmp_path):
    p, sd = state_after(tmp_path, 45)
    store.save_json("imagery.json", [], sd)
    data = dashboard.build_data(sd, p)
    data["satellite"] = {
        "analysis": {
            "latest": {"date": "2026-09-02"},
            "ndvi": {"mean": 0.8, "median": 0.8, "min": 0.4, "max": 0.9},
            "zones": [{"cells": 5, "centre": [3, 3]}],
            "declining": 2,
            "scouting": [{"cell": [3, 3], "score": 3.0}],
            "series": [{"date": "d1", "ndvi": 0.8, "valid": 1.0}, {"date": "d2", "ndvi": 0.7, "valid": 1.0}],
        },
        "scenes": [
            {"id": "a", "date": "2026-08-20", "valid": 1.0, "ndvi": [60] * 144, "ndmi": [20] * 144},
            {"id": "b", "date": "2026-09-02", "valid": 0.9, "ndvi": [None] + [55] * 143, "ndmi": [20] * 144},
        ],
    }
    html = dashboard.PAGE.replace("/*__DATA__*/null", json.dumps(data))
    r = run_node(tmp_path, html)
    assert r.returncode == 0, r.stderr[-1500:]


def test_the_harness_really_catches_a_broken_script(tmp_path):
    """Guard against a test that can never fail: inject the exact bug class that once broke the page."""
    p = load_policy()
    p["field"]["size"] = 12
    html = dashboard.render(tmp_path / "empty", p).replace("function renderDrones(){", "function renderDrones(){const zz=1;zz=2;")
    r = run_node(tmp_path, html)
    assert r.returncode != 0 and "Assignment to constant" in r.stderr
