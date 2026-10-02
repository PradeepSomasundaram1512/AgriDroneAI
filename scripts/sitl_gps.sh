#!/bin/bash
# Usage: scripts/sitl_gps.sh preflight|dip|loss   (needs colima/docker; fresh PX4 container per scenario)
cd "$(dirname "$0")/.." || exit 1
docker rm -f px4 >/dev/null 2>&1; docker run --rm -d -i --name px4 -p 14540:14540/udp jonasvautherin/px4-gazebo-headless:latest >/dev/null; sleep 45
PYTHONPATH=. AGRIDRONE_ARMED=1 .venv/bin/python -u scripts/sitl_gps.py "$1" 2>&1 | grep --line-buffered -avE "deprecated|MAVSDK version|Waiting to discover|New system|System discovered|Server (started|set)|fork_posix"
