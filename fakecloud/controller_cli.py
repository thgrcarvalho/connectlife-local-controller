#!/usr/bin/env python3

import asyncio
import json
import socket
import sys

from controller import ALLOWED_ACTIONS, CONTROL_PORT, main as run_controller
from credential_store import reset_credentials, setup_credentials

ACTIONS = ("setup", "reset-credentials", "run", *sorted(ALLOWED_ACTIONS))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ACTIONS:
        sys.exit(f"usage: {sys.argv[0]} {'|'.join(ACTIONS)}")
    action = sys.argv[1]
    if action == "setup":
        setup_credentials()
        return
    if action == "reset-credentials":
        reset_credentials()
        return
    if action == "run":
        asyncio.run(run_controller())
        return

    with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=35) as connection:
        connection.sendall(json.dumps({"action": action}).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(65535)
            if not chunk:
                raise RuntimeError("controller closed without a response")
            data += chunk
    response = json.loads(data.split(b"\n", 1)[0])
    if not response.get("ok"):
        sys.exit(f"error: {response.get('error')}")
    print(json.dumps(response, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
