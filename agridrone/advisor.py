"""Optional Claude advisor. Claude may (a) explain the cycle in plain language and (b) nudge
thresholds within hard bounds. It never issues flight commands: those pass through planner + safety.
Runs only if ANTHROPIC_API_KEY is set and `anthropic` is installed; otherwise the agent is fully deterministic."""
import json
import os

BOUNDS = {"ndvi_stress": (0.35, 0.55), "moisture_irrigate": (0.2, 0.38), "pest_spray": (0.4, 0.7)}
MODEL = os.environ.get("AGRIDRONE_MODEL", "claude-sonnet-5-5")


def advise(summary: dict, thresholds: dict):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"note": "advisor disabled (no API key)", "thresholds": {}}
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=MODEL, max_tokens=500,
            system="You advise an autonomous farm-drone agent. Reply with JSON only: "
                   '{"note": "<2 sentences plain language>", "thresholds": {<optional keys among ndvi_stress, moisture_irrigate, pest_spray>: number}}. '
                   "Only suggest small changes (<=0.03). Never mention flight commands.",
            messages=[{"role": "user", "content": json.dumps({"cycle": summary, "thresholds": thresholds})}],
        )
        out = json.loads(msg.content[0].text)
        safe = {}
        for k, v in (out.get("thresholds") or {}).items():
            if k in BOUNDS and isinstance(v, (int, float)) and abs(v - thresholds[k]) <= 0.03:
                safe[k] = min(max(v, BOUNDS[k][0]), BOUNDS[k][1])
        return {"note": str(out.get("note", ""))[:400], "thresholds": safe}
    except Exception as e:  # advisor failure must never stop operations
        return {"note": f"advisor error: {type(e).__name__}", "thresholds": {}}
