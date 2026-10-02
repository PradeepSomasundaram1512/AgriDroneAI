"""Ground station: runs at the farm (Raspberry Pi / laptop / self-hosted runner), the only place that can reach the drones.
Loop: git pull -> pick newest unflown queued mission -> re-validate against policy -> fly -> log -> git push.
Cloud autopilot plans; ground station executes. The cloud can never actuate hardware directly."""
import asyncio
import subprocess
import time

from . import safety, store
from .config import load_policy
from .hardware import FlightExecutor, PreflightError


def to_missions(rec):
    return [safety.Mission(m["drone"], m["alt"], [(tuple(c), a) for c, a in m["targets"]], m["energy_wh"]) for m in rec["missions"]]


def pending(state_dir=None, now=None, max_age_h=12):
    """Newest queued record with no flight result; older unflown ones are superseded, stale ones expired."""
    now = now or time.time()
    done = {f["id"] for f in store.read_jsonl("flights.jsonl", state_dir)}
    open_ = [q for q in store.read_jsonl("queue.jsonl", state_dir) if q["id"] not in done]
    skipped = []
    if not open_:
        return None, skipped
    newest = open_[-1]
    skipped += [(q["id"], "superseded") for q in open_[:-1]]
    if now - newest["ts"] > max_age_h * 3600:
        skipped.append((newest["id"], "expired"))
        newest = None
    return newest, skipped


def process_once(policy=None, state_dir=None, executor=None, human_approved=None, dry_run=False, now=None):
    """Returns the flight record written (or None). human_approved: queue id the operator explicitly approved."""
    policy = policy or load_policy()
    hw = policy["hardware"]
    rec, skipped = pending(state_dir, now, hw.get("max_queue_age_h", 12))
    log = lambda r: store.append_jsonl("flights.jsonl", r, state_dir)
    for qid, why in skipped:
        log({"id": qid, "status": why, "ts": now or time.time()})
    if rec is None:
        return None
    if policy.get("kill_switch"):
        out = {"id": rec["id"], "status": "refused", "reason": "kill switch", "ts": time.time()}
    elif rec["approval"] != "auto" and human_approved != rec["id"] and not dry_run:
        return None  # waits for a human: `agridrone fly --approve <id>`
    else:
        ex = executor or FlightExecutor(policy)
        try:
            results = asyncio.run(ex.fly(to_missions(rec), dry_run=dry_run))
            out = {"id": rec["id"], "status": "dry_run" if dry_run else ("completed" if all(r.status == "completed" for r in results) else "partial"),
                   "drones": [{"drone": r.drone, "status": r.status, "done": r.completed, "total": r.total, "reason": r.reason} for r in results],
                   "ts": time.time()}
        except PreflightError as e:
            out = {"id": rec["id"], "status": "refused", "reason": str(e), "ts": time.time()}
    log(out)
    return out


def _git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True)


def daemon(poll_s=300, sync=True, dry_run=False):
    """Never exits on error: logs and keeps polling (run under systemd/docker restart=always)."""
    while True:
        try:
            if sync:
                _git("pull", "--rebase", "--autostash")
            out = process_once(dry_run=dry_run)
            if out and sync:
                _git("add", "state/flights.jsonl")
                _git("commit", "-m", f"ground-station: flight {out['id']} {out['status']}")
                if _git("push").returncode:
                    _git("pull", "--rebase", "--autostash"); _git("push")
        except Exception as e:
            print("ground-station error:", repr(e), flush=True)
        time.sleep(poll_s)
