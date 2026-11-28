"""
One-shot seeding script for Uptime Kuma.

Creates a Docker Host pointed at the local socket, then one "Docker
Container" monitor per service in this stack, checked hourly. This is
NOT meant to run continuously — it's a `docker compose run --rm
kuma-seed` job you run once after Kuma's own first-run setup wizard has
already created an admin account (this script can't do that part for
you; Kuma requires it be done through the UI on first load).

Re-running this script will create duplicate monitors — it does not
check for existing ones by name. If you need to re-seed, delete the
monitors it created in the Kuma UI first.

Uses the unofficial `uptime-kuma-api` package (a Socket.IO wrapper,
not an official API — see SCRYPTED-SETUP.md's note on this same class
of risk for the Blink plugin). If a method call below fails with a
TypeError about an unexpected keyword argument, the installed version
of the library has renamed a field — run:
    docker compose run --rm --entrypoint python kuma-seed \\
      -c "import uptime_kuma_api, inspect; print(inspect.signature(uptime_kuma_api.UptimeKumaApi.add_monitor))"
to see the current signature and adjust below.
"""

import os
import sys
import time

from uptime_kuma_api import UptimeKumaApi, MonitorType

KUMA_URL = os.environ.get("KUMA_URL", "http://uptime-kuma:3001")
KUMA_USERNAME = os.environ["KUMA_USERNAME"]
KUMA_PASSWORD = os.environ["KUMA_PASSWORD"]

# Every long-running container in this stack. kuma-seed itself and
# uptime-kuma's own container are handled separately below (self-monitor
# is fine; a monitor watching a one-shot job that always exits is not).
CONTAINERS = [
    "nginx",
    "tailscale",
    "fail2ban",
    "watchtower",
    "uptime-kuma",
    "homepage",
    "netdata",
    "dozzle",
    "scrutiny",
    "portainer",
    "diun",
    "control-api",
    "duplicati",
    "prometheus",
    "grafana",
    "node-exporter",
    "cadvisor",
    "smartctl-exporter",
    "nut-server",
    "nut-exporter",
    "unpoller",
    "scrypted",
]

HOURLY_SECONDS = 3600


def main():
    # Kuma can take a few seconds to finish booting after `docker compose
    # up` even once its container shows "running" — retry the connection
    # briefly instead of failing on the very first attempt.
    last_err = None
    for attempt in range(10):
        try:
            api = UptimeKumaApi(KUMA_URL)
            api.login(KUMA_USERNAME, KUMA_PASSWORD)
            break
        except Exception as e:  # noqa: BLE001 - just retrying on anything
            last_err = e
            time.sleep(3)
    else:
        print(f"Could not connect/login to Kuma at {KUMA_URL}: {last_err}")
        sys.exit(1)

    try:
        host = api.add_docker_host(
            name="dxp2800",
            dockerType="socket",
            dockerDaemon="/var/run/docker.sock",
        )
        host_id = host.get("id") or host.get("dockerHostID") or host.get("dockerHostId")
        if host_id is None:
            print(f"add_docker_host returned no recognizable id: {host}")
            sys.exit(1)
        print(f"Docker host created/found: id={host_id}")

        created, skipped = 0, 0
        for container in CONTAINERS:
            try:
                result = api.add_monitor(
                    type=MonitorType.DOCKER,
                    name=f"Container: {container}",
                    docker_container=container,
                    docker_host=host_id,
                    interval=HOURLY_SECONDS,
                )
                print(f"  + {container}: {result}")
                created += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ! {container}: failed — {e}")
                skipped += 1

        print(f"Done. Created {created}, failed {skipped} (of {len(CONTAINERS)}).")
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
