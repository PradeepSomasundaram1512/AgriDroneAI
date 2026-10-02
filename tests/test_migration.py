import json

from agridrone import store
from agridrone.agent import run_cycle
from agridrone.config import load_policy


def small():
    p = load_policy()
    p["field"]["size"] = 8
    return p


def test_legacy_state_is_archived_and_the_run_starts_clean(tmp_path):
    (tmp_path / "farm.json").write_text(json.dumps({"size": 8, "day": 9}))
    (tmp_path / "metrics.jsonl").write_text('{"ok": true, "day": 9}\n')
    rec = run_cycle(small(), tmp_path)
    assert rec["ok"] and rec["day"] == 1  # clean start on the new model
    assert (tmp_path / "archive-v1" / "farm.json").exists() and (tmp_path / "archive-v1" / "metrics.jsonl").exists()
    assert json.loads((tmp_path / "version.json").read_text())["version"] == store.STATE_VERSION
    assert any(r["kind"] == "state_migrated" for r in store.read_jsonl("audit.jsonl", tmp_path))


def test_current_state_is_never_migrated(tmp_path):
    run_cycle(small(), tmp_path)
    r2 = run_cycle(small(), tmp_path)
    assert r2["day"] == 2 and not (tmp_path / "archive-v1").exists()
    assert store.migrate(tmp_path) is None


def test_fresh_directory_just_gets_a_version_file(tmp_path):
    assert store.migrate(tmp_path) is None
    assert (tmp_path / "version.json").exists()
