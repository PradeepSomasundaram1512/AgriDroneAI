"""Policy/config loading. The policy file is the human control surface."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.json"
STATE_DIR = ROOT / "state"
REPORT_DIR = ROOT / "reports"


def load_policy(path: Path = POLICY_PATH) -> dict:
    return json.loads(Path(path).read_text())
