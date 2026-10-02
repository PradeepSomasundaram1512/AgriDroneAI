#!/bin/bash
# Needs: colima/docker running, mavsdk-grpc installed.  Usage: scripts/sitl_failures.sh lowbat|linkloss
cd "$(dirname "$0")/.." || exit 1
docker rm -f px4 >/dev/null 2>&1; docker run --rm -d -i --name px4 -p 14540:14540/udp jonasvautherin/px4-gazebo-headless:latest >/dev/null; sleep 40
PYTHONPATH=. AGRIDRONE_ARMED=1 .venv/bin/python -u scripts/sitl_failures.py "$1" 2>&1 | grep --line-buffered -avE "deprecated|MAVSDK version|Waiting to discover|New system|System discovered|Server (started|set)"
