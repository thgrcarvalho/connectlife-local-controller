#!/usr/bin/env python3
"""CLI for the localhost control socket exposed by replay_server.py."""

import json
import socket
import sys

ACTIONS = ("status", "on", "off", "fan", "cool", "dry", "eco", "fanlow", "fanmed", "fanhigh",
           "sleep", "sleepoff", *(f"temp{temp}" for temp in range(16, 31)))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ACTIONS:
        sys.exit(f"usage: {sys.argv[0]} {'|'.join(ACTIONS)}")

    action = sys.argv[1]
    with socket.create_connection(("127.0.0.1", 8765), timeout=20) as connection:
        connection.sendall(json.dumps({"action": action}).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(65535)
            if not chunk:
                raise RuntimeError("control server closed without a response")
            data += chunk

    response = json.loads(data.split(b"\n", 1)[0])
    if not response.get("ok"):
        sys.exit(f"error: {response.get('error')}")

    status = response.get("status", {})
    power = {"0": "OFF", "1": "ON"}.get(status.get("t_power"), "unknown")
    print(f"connected={response['connected']} power={power} "
          f"mode={status.get('t_work_mode', '?')} "
          f"setpoint={status.get('t_temp', '?')} "
          f"fan={status.get('t_fan_speed', '?')} "
          f"room={status.get('f_temp_in', '?')}°C")


if __name__ == "__main__":
    main()
