"""Narration for the plain-English explainer video. One entry per scene; the scene lasts as long as its narration (+ a short pause).
Edit the text and re-run `python video/make_video.py` to regenerate the video."""

SCENES = [
    (
        "title",
        "This is AgriDroneAI: a farm that looks after itself. A small fleet of drones, a lot of data, and an AI that decides which parts of the farm need water, which need help, and which are fine. Let me show you how it works, in plain English.",
    ),
    (
        "problem",
        "Most farms water every field on a fixed schedule, whether it needs it or not. That wastes water and chemicals. In our tests, a fixed schedule used about six hundred millimetres of water. The smart system used about one hundred and twenty-six, and the crop was just as healthy.",
    ),
    (
        "farm",
        "Here is a pretend farm made of five hundred and seventy-six small patches. Green means healthy. Yellow means keep an eye on it. Brown means struggling. Sensors in the soil measure how wet it is and how many pests there are, and the AI reads all of it, every day.",
    ),
    (
        "decide",
        "Every morning the AI works out which patches need help, and ranks them from most urgent to least. It also reads the weather forecast. If rain is coming tomorrow, it skips the watering, and saves the water.",
    ),
    (
        "crew",
        "Then the drone crew goes to work. Each drone takes off from its own pad, flies to the patches that need help, and waters them, or sprays only the ones with pests. A battery only lasts so long, so the drones go out in waves.",
    ),
    (
        "charge",
        "Between waves, each drone recharges on its pad. A slow charger means later flights can do less, so the system plans every flight around what the battery can really hold. Overnight everything charges to full, and batteries slowly wear out, just like your phone.",
    ),
    (
        "collide",
        "Safety comes first. Drones must never bump into each other. Each one has its own pad and its own flying height. But the risky moments are takeoff and landing, when one drone climbs through another one's height. So before any flight, the system replays the whole plan, second by second, in three dimensions. If two drones would ever get too close, it staggers their takeoffs, or flies them one at a time.",
    ),
    (
        "wind",
        "Wind matters a lot. Flying into a headwind drains the battery fast, and a tailwind saves some. At four metres a second, a plan that ignored the wind would use over a third more battery than expected. So every leg of every flight is costed with the wind. If it is too windy, the drones stay home. And spraying has an even lower limit, because wind blows the spray off target.",
    ),
    (
        "gps",
        "What if a drone loses its GPS? Without GPS it cannot find its way home, so it does not try. It instantly stops spraying, waits ten seconds in case the signal comes back, and if not, lands safely right where it is. Any drone flying below it is sent home at once, so nothing can fall on it.",
    ),
    (
        "satellite",
        "The system also looks at real satellite photos of the field, taken every few days by the European Sentinel-2 satellites. This is a real farm field in Iowa. The circles mark spots that look weaker than the rest, so a person knows where to check first.",
    ),
    (
        "sensors",
        "Sensors break sometimes. A dead sensor can read zero, and trick the system into watering a healthy patch forever. So the system spots readings that make no sense, repairs them from the neighbouring patches, and tells the farmer which sensor needs fixing.",
    ),
    (
        "results",
        "Does it work? In our simulated seasons, doing nothing lost about twelve percent of the crop. The fixed schedule kept the whole crop, but used six times the water. The AI crew kept ninety-nine point seven percent of the crop, with about four fifths less water, and far less spray.",
    ),
    (
        "auto",
        "It runs by itself. Every morning a program wakes up, reads the data, plans the flights, checks safety, and saves everything. It writes weekly reports, and raises an alarm if anything goes wrong. And there is always a big stop button, so a person stays in charge.",
    ),
    (
        "close",
        "One honest note. The farm in this video is a simulation. The satellite photos are real, and the safety software has been tested on a real drone autopilot simulator. The next step is a small pilot, with real drones, on a real field. Thanks for watching.",
    ),
]
