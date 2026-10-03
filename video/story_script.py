"""Warm, story-style narration for the layman video. Same scene names as the animation set in make_video.py."""

SCENES = [
    ("title", "Imagine a farm that looks after itself. No one walking the rows at dawn... no guessing which field is thirsty. Just a small crew of drones, and an A I that quietly takes care of the crop. This is Agri Drone A I. Let me show you, in simple words, how it works."),
    ("problem", "Here's the problem. Most farms water everything on a fixed schedule... whether the crop needs it or not. That wastes water, and it wastes money. In our tests, the fixed schedule poured on about six hundred millimetres of water. The smart crew used only about one hundred and ten. Same healthy crop. A fraction of the water."),
    ("farm", "This is our pretend farm, made of five hundred and seventy-six small patches. Green means healthy. Yellow means, keep an eye on it. Brown means, this patch is struggling. Little sensors in the soil feel how wet it is, and spot the first signs of pests."),
    ("decide", "Every morning, the A I looks at the whole farm, and asks one question... who needs help the most? It ranks every patch, from most urgent to least. And it checks the weather. If rain is coming tomorrow? It simply waits... and saves the water."),
    ("crew", "Then the drone crew goes to work. Each drone lifts off from its own pad, flies to the patches that need care, and waters them. Or sprays only the ones with pests. A battery only lasts so long, so the drones fly in waves, taking turns."),
    ("charge", "Between waves, every drone recharges on its pad. Just like your phone, batteries slowly wear out, so the system keeps track of every battery's health, and plans each flight around what it can truly give. By morning, everyone is fully charged again."),
    ("collide", "Now... the most important part. Safety. Drones must never bump into each other. Each one has its own pad, and its own height in the sky. And before any drone takes off, the whole plan is replayed, second by second, in three dimensions. If two drones could ever get too close, the plan is rejected. Zero close calls."),
    ("wind", "Wind matters a lot. Flying into a headwind drains the battery fast. So the system checks the wind for every single leg of every flight. And if it's too windy to fly safely? The drones stay home. Safe, not sorry."),
    ("gps", "What if a drone loses its G P S signal? It can't find its way home... so it doesn't try. It stops spraying at once, waits a few seconds in case the signal returns, and if not, it lands gently, right where it is. Drones flying below it are sent home. Nobody crashes."),
    ("satellite", "The system also studies real satellite photos of the field, taken every few days by the European Sentinel-two satellites. The circles mark spots that look weaker than the rest, so a person knows exactly where to check first."),
    ("sensors", "Sensors break sometimes. A dead sensor can read zero, and trick the system into watering a healthy patch forever. So the A I spots readings that make no sense, repairs them using the neighbouring patches, and tells the farmer, which sensor needs fixing."),
    ("results", "So... does it work? In our simulated seasons, doing nothing lost about twelve percent of the crop. The fixed schedule kept the crop, but used six times the water. The A I crew kept about ninety-nine percent of the crop, with roughly eighty percent less water. And to be fair, we also tested a clever farmer using the same soil sensors, and the results were close. The big win here is that the A I does it all by itself."),
    ("auto", "And it really does run by itself. Every morning, a program wakes up, reads the data, plans the flights, checks safety, and saves everything. It writes weekly reports, and raises an alarm if anything goes wrong. And there's always a big stop button, so a person stays in charge."),
    ("close", "One honest note. This farm is a simulation. The satellite photos are real, and the safety software has been tested on a real drone autopilot simulator. The next step is a small pilot, with real drones, on a real field. Now, let's look at the actual dashboard, and what it shows."),
]

DASH = [
    (0, "This is the live dashboard. At the top, a plain English briefing. On day one hundred and twenty, eighty-nine percent of the farm is fully healthy. Eighty-eight percent less water used. Ninety-two percent less chemical."),
    (640, "Here is one day's mission, replayed. Watch the drones lift off, treat the patches that need help, and glide home to recharge."),
    (1450, "Drag the time-lapse, and watch the farm change day by day. And on the right, the A I explains its own decisions, in words anyone can read."),
    (3010, "Safety, at a glance. Zero close calls. Every battery healthy. And the system tells you when it's too windy, or a drone loses its signal."),
    (4150, "And the honest money view. Compared with a fixed schedule, the drones save a lot. But a smart farmer with the same sensors earns almost the same. The real advantage... is that it all happens automatically."),
    (4700, "Six simulated farms. A hundred and twenty days each. About ninety-nine percent of the crop, with roughly eighty percent less water. Not a field trial... but a strong start. Thank you for watching."),
]
