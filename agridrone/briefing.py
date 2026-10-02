"""Plain-language daily briefing + "ask your farm".

Everything starts from the dashboard's data dict (the system's own state: metrics, audit events, weather, fleet, satellite). Two layers:
  * RULES (always on, offline, deterministic): `sentences()` writes the morning briefing, `answer()` answers common questions from the data.
  * CLAUDE (optional, needs ANTHROPIC_API_KEY): `ask()` hands Claude the same facts as JSON with instructions to answer ONLY from them and to
    say "I don't know" otherwise. It is read-only (no tools, no ability to change anything) and any failure falls back to the rules answer.
Answers always say which layer produced them, so nobody mistakes a model's wording for a measurement."""

import json
import os
import re

MODEL = os.environ.get("AGRIDRONE_MODEL", "claude-sonnet-5-5")
SYSTEM = (
    "You are the voice of a farm-drone autopilot. Answer the farmer's question using ONLY the JSON facts provided. "
    "If the facts do not contain the answer, say you don't know and what you can answer instead. Plain language, no jargon, at most 120 words. "
    "Never invent numbers. Never suggest changing safety settings. Everything is from a SIMULATED farm unless the facts say otherwise."
)


def _last(data):
    m = data.get("metrics") or []
    return m[-1] if m else {}


def _dir(deg):
    return ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][round(((deg or 0) % 360) / 45) % 8]


def sentences(data):
    """The morning briefing: a handful of plain sentences, most important first."""
    k, m = data["kpi"], _last(data)
    out = [
        f"Day {data['day']}: {k['healthy_pct']}% of the farm is fully healthy, {k['watch']} patches are worth watching and {k['stressed']} are struggling."
    ]
    if m.get("grounded_by_wind"):
        out.append(
            f"The fleet stayed on the ground today because the wind ({m['wind_ms']} m/s from the {_dir(m['wind_from_deg'])}) was above the safe limit. The work waits for a calmer day."
        )
    else:
        done = m.get("executed", 0)
        waves = m.get("sorties", 0)
        out.append(
            f"The drones treated {done} patches in {waves} wave{'s' if waves != 1 else ''}."
            if done
            else "No drone flights were needed today."
        )
        if m.get("spray_deferred"):
            out.append(
                f"Spraying was postponed for {m['spray_deferred']} patches because the wind would have blown the spray off target; watering carried on."
            )
    if m.get("gps_losses"):
        out.append(
            f"{m['gps_losses']} drone{'s' if m['gps_losses'] > 1 else ''} lost GPS, stopped spraying and landed safely in the field; {m.get('gps_patches_deferred', 0)} patches were rescheduled."
        )
    elif m.get("drones_grounded"):
        out.append(f"{m['drones_grounded']} drone(s) are still waiting to be picked up from the field.")
    if m.get("sensors_quarantined") or m.get("sensors_flagged"):
        out.append(
            f"{m.get('sensors_flagged', 0)} suspicious sensor readings were repaired automatically and {m.get('sensors_quarantined', 0)} sensors need maintenance."
        )
    w = (data.get("weather") or {}).get("forecast") or []
    if w and sum(x["rain_mm"] for x in w[:2]) >= 10:
        out.append("Rain is forecast within two days, so non-critical watering is being skipped to save water.")
    sat = data.get("satellite")
    if sat and sat.get("analysis", {}).get("scouting"):
        a = sat["analysis"]
        cells = ", ".join(str(tuple(t["cell"])) for t in a["scouting"][:3])
        out.append(f"The latest real satellite pass ({a['latest']['date']}) suggests checking these spots first: {cells}.")
    if m.get("soc_min_pct") is not None and m["soc_min_pct"] < 30:
        out.append(f"Batteries were run down to {round(m['soc_min_pct'])}% (the safety reserve is kept).")
    return out


# ---------------------------------------------------------------- rule-based question answering
INTENTS = [
    ("grounded", r"ground|wind|windy|not fly|stay(ed)? (home|on)|why .*(no|not) .*fly"),
    ("gps", r"gps|signal|lost|landed|picked up|field"),
    ("battery", r"battery|batter|charg|health|wear"),
    ("saved", r"save|water|spray|chemical|resource"),
    ("sensors", r"sensor|broken|fault|repair"),
    ("satellite", r"satellite|sentinel|space|check first|weak"),
    ("safety", r"safe|collision|crash|bump|clash|separat"),
    ("treated", r"how many|treat|patch|done|today|flew|flight"),
    ("money", r"money|profit|cost|pay|roi|worth|price"),
    ("results", r"work|result|prove|accurate|benchmark|yield"),
]


def answer(question, data):
    """Rules answer: -> (text, source). Always grounded in `data`."""
    q, m, k = question.lower(), _last(data), data["kpi"]
    kind = next((i for i, rx in INTENTS if re.search(rx, q)), None)
    if kind == "grounded":
        if m.get("grounded_by_wind"):
            return (
                f"The fleet stayed home because the wind today ({m['wind_ms']} m/s, gusts {m['gust_ms']} m/s, from the {_dir(m['wind_from_deg'])}) was above the safe flying limit. Nothing was lost: the work is simply done on a calmer day.",
                "rules",
            )
        days = sum(1 for r in data["metrics"] if r.get("grounded_by_wind"))
        return (
            f"The fleet was not grounded today (wind {m.get('wind_ms', '?')} m/s). Over the last {len(data['metrics'])} days it stayed home on {days} windy days."
            + (f" Spraying was postponed for {m['spray_deferred']} patches today." if m.get("spray_deferred") else ""),
            "rules",
        )
    if kind == "gps":
        tot = sum(r.get("gps_losses", 0) for r in data["metrics"])
        return (
            f"GPS losses so far: {tot}. {m.get('drones_grounded', 0)} drone(s) are currently waiting to be picked up. When a drone loses GPS it stops spraying, holds a few seconds, then lands where it is, and drones flying below it are sent home."
        ), "rules"
    if kind == "battery":
        h = (data.get("fleet") or {}).get("drones") or []
        health = ", ".join(f"{round(x['health'] * 100)}%" for x in h) or "n/a"
        return (
            f"Lowest battery after landing today: {m.get('soc_min_pct', '?')}% (the 25% safety reserve is always kept). Battery health per drone: {health}. Charging between flights: {data['fleet'].get('rate_w')} W for {data['fleet'].get('turnaround_min')} minutes; everything is fully recharged overnight.",
            "rules",
        )
    if kind == "saved":
        return (
            f"Compared with a fixed schedule, the crew has used {round(k['water'])}% less water and {round(k['chem'])}% less chemical so far this season (simulated).",
            "rules",
        )
    if kind == "sensors":
        return (
            f"{m.get('sensors_flagged', 0)} suspicious readings today were repaired automatically from neighbouring patches; {m.get('sensors_quarantined', 0)} sensors need maintenance.",
            "rules",
        )
    if kind == "satellite":
        sat = data.get("satellite")
        if not sat:
            return "No satellite imagery has been fetched yet.", "rules"
        a = sat["analysis"]
        spots = ", ".join(str(tuple(t["cell"])) for t in a["scouting"][:5]) or "none"
        return (
            f"Latest real Sentinel-2 pass: {a['latest']['date']}, mean crop vigor {a['ndvi']['mean']}. Weak areas: {len(a['zones'])}. Check these spots first: {spots}.",
            "rules",
        )
    if kind == "safety":
        return (
            f"Every plan is replayed second by second in 3D before takeoff and rejected if two drones would get too close. Today the closest pair kept {m.get('traffic_min_ratio', '?')}x the required safe distance; flights were {m.get('traffic_mode', 'concurrent')}.",
            "rules",
        )
    if kind == "treated":
        return (
            f"Today the drones treated {m.get('executed', 0)} patches of {m.get('targets', 0)} that needed help, in {m.get('sorties', 0)} wave(s). {max(0, m.get('targets', 0) - m.get('executed', 0))} patches wait for tomorrow.",
            "rules",
        )
    if kind == "money":
        eco = data.get("economics")
        if eco:
            ai = next((r for r in eco["rows"] if r["id"] == "agent-3"), None)
            cal = next((r for r in eco["rows"] if r["id"] == "calendar"), None)
            if ai and cal:
                return (
                    f"With placeholder prices, a season earns about ${ai['profit']:,} with the drone crew versus ${cal['profit']:,} with a fixed schedule. Replace the prices in the policy file with yours.",
                    "rules",
                )
        return "Run `agridrone economics` after a benchmark to see the money view.", "rules"
    if kind == "results":
        return (
            "In simulated seasons the AI crew kept about 99% of the crop with roughly 80% less water than a fixed schedule, but a farmer who simply reacts to the same soil sensors does about as well on yield and profit (the AI uses a little less water). These are simulation results, not field trials.",
            "rules",
        )
    return (
        "I can answer questions about today's flights, wind, GPS, batteries, sensors, the satellite view, savings, safety and money. (I don't know about anything else.)",
        "rules",
    )


def ask(question, data, client=None):
    """Answer with Claude if configured, otherwise (or on any failure) with the rules. -> (text, source)."""
    fallback = answer(question, data)
    if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback
    try:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        facts = {k: data[k] for k in ("day", "kpi", "weather", "fleet", "economics") if data.get(k) is not None}
        facts["recent_days"] = (data.get("metrics") or [])[-7:]
        facts["recent_events"] = [e["text"] for e in (data.get("events") or [])[:8]]
        facts["briefing"] = sentences(data)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"FACTS:\n{json.dumps(facts, default=str)[:12000]}\n\nQUESTION: {question[:400]}"}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "text") == "text").strip()
        return (text[:1200], "claude") if text else fallback
    except Exception:  # never let a model/network problem stop an answer
        return fallback
