#!/bin/bash
# Usage: scripts/sitl_multi.sh all|oneweak [N=3]   (needs colima/docker; ~1.5GB RAM per simulator)
cd "$(dirname "$0")/.." || exit 1
N=${2:-3}
for i in $(seq 0 $((N-1))); do docker rm -f px4_$i >/dev/null 2>&1; done
# PX4 sends its API MAVLink to host:(14540+instance). The image always runs instance 0, so rewrite that port per container.
for i in $(seq 0 $((N-1))); do
  docker run --rm -d -i --name px4_$i --entrypoint bash jonasvautherin/px4-gazebo-headless:latest -c \
    "sed -i 's/^udp_offboard_port_remote=.*/udp_offboard_port_remote=$((14540+i))/' \${FIRMWARE_DIR}/build/etc/init.d-posix/px4-rc.mavlink && exec /root/entrypoint.sh" >/dev/null
done
sleep 60
PYTHONPATH=. AGRIDRONE_ARMED=1 .venv/bin/python -u scripts/sitl_multi.py "$1" "$N" 2>&1 | grep --line-buffered -avE "deprecated|MAVSDK version|Waiting to discover|New system|System discovered|Server (started|set)|fork_posix"
for i in $(seq 0 $((N-1))); do docker stop px4_$i >/dev/null 2>&1; done
