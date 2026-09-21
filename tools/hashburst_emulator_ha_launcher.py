#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import signal
import subprocess
import sys
import time
import urllib.request

LOG = logging.getLogger("hashburst-emulator-ha")


def discover(url: str, timeout: float) -> tuple[str, int, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HashBurst-PCB-HA/2.2"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"discovery HTTP {response.status}")
        data = json.load(response)

    if data.get("available") is not True:
        raise RuntimeError("master unavailable")
    master = data.get("master")
    if not isinstance(master, dict):
        raise RuntimeError("invalid discovery response")

    host = str(master.get("tep_host") or "").strip()
    port = int(master.get("tep_port") or 0)
    node_id = str(master.get("node_id") or "").strip()
    lease_ms = int(master.get("lease_remaining_ms") or 0)

    ipaddress.ip_address(host)
    if not node_id:
        raise RuntimeError("empty master node_id")
    if not 1 <= port <= 65535:
        raise RuntimeError("invalid TEP port")
    if lease_ms <= 0:
        raise RuntimeError("expired HA lease")
    return host, port, node_id


def stop_child(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--discovery-url",
        default=(
            "https://blockchainapi.one"
            "/api/hashburst/master/discovery"
        ),
    )
    parser.add_argument(
        "--emulator",
        default="/opt/hashburst/pcb_emulator.py",
    )
    parser.add_argument("--api-port", type=int, default=9100)
    parser.add_argument("--boards", type=int, default=2)
    parser.add_argument("--check-interval", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    stopping = False
    child: subprocess.Popen | None = None
    selected: tuple[str, int, str] | None = None

    def handle_stop(signum, frame) -> None:
        nonlocal stopping
        stopping = True
        stop_child(child)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    while not stopping:
        try:
            discovered = discover(args.discovery_url, args.timeout)
            if discovered != selected:
                host, port, node_id = discovered
                LOG.warning(
                    "HA master selected node=%s host=%s port=%d",
                    node_id,
                    host,
                    port,
                )
                stop_child(child)
                child = subprocess.Popen([
                    sys.executable,
                    args.emulator,
                    "--master",
                    host,
                    "--tep-port",
                    str(port),
                    "--api-port",
                    str(args.api_port),
                    "--boards",
                    str(args.boards),
                ])
                selected = discovered
            elif child is None or child.poll() is not None:
                LOG.error("emulator exited; rediscovering")
                child = None
                selected = None
        except Exception as exc:
            LOG.error("HA discovery failed: %s", exc)
            # Keep the last HA-authorized target during a transient outage.
            # Before the first successful discovery, fail closed.
            if selected is None:
                stop_child(child)
                child = None

        deadline = time.monotonic() + args.check_interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.25)

    stop_child(child)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
