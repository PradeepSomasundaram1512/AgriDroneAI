"""Actuation boundary. Only SimAdapter ships. A real PX4/MAVSDK adapter must implement
`execute(missions)` and is only used when policy.autonomy_level == "autonomous"."""


class SimAdapter:
    name = "sim"

    def __init__(self, farm):
        self.farm = farm

    def execute(self, missions):
        done = 0
        for m in missions:
            for cell, action in m.targets:
                self.farm.apply(tuple(cell), action)
                done += 1
        return done
