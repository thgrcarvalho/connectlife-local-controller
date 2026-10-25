#!/usr/bin/env python3
"""Replay a recorded ConnectLife session locally, with no cloud connection.

This is an experiment to determine whether the AC accepts signed server
messages and commands verbatim from a previous session. If it does, a library
of captured commands can provide local control without knowing the device key.

  sudo ../.venv/bin/python replay_server.py --bind 192.168.31.100 \
      --capture captures/your-session --command off
"""

import argparse
import base64
import datetime
import json
import socket
import ssl
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "replay.log"
CERT_FILE = BASE_DIR / "server.crt"
KEY_FILE = BASE_DIR / "server.key"

_log_lock = threading.Lock()
_json_decoder = json.JSONDecoder()


def log(message):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + "  " + message
    with _log_lock:
        print(line, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def parse_frame(raw):
    text = raw[:-2].decode("ascii")
    obj, _ = _json_decoder.raw_decode(text[2:])
    return obj


def load_frames(path):
    data = path.read_bytes()
    return [frame + b"\r\n" for frame in data.split(b"\r\n") if frame]


def read_frame(connection, buffer):
    while b"\r\n" not in buffer:
        data = connection.recv(65535)
        if not data:
            raise EOFError
        buffer += data
    raw, buffer = buffer.split(b"\r\n", 1)
    return raw + b"\r\n", buffer


def status_text(obj):
    decoded = []
    for item in obj.get("status", []):
        if "status" in item:
            decoded.append(base64.b64decode(item["status"]).decode("utf-8"))
    return "; ".join(decoded)


class ACState:
    def __init__(self, command_frames):
        self.command_frames = command_frames
        self.client = None
        self.status = {}
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.capture = None
        self.before_command = None
        self.ack_seq = None
        self.status_revision = 0

    def attach(self, client):
        with self.condition:
            self.client = client
            self.condition.notify_all()

    def detach(self, client):
        with self.condition:
            if self.client is client:
                self.client = None
            self.condition.notify_all()

    def update_status(self, obj):
        updates = {}
        for item in obj.get("status", []):
            if "status" in item:
                updates.update(json.loads(base64.b64decode(item["status"])))
        with self.condition:
            self.status.update(updates)
            self.status_revision += 1
            self.condition.notify_all()

    def acknowledge(self, obj):
        with self.condition:
            self.ack_seq = obj.get("seq")
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"connected": self.client is not None,
                    "status": dict(self.status)}

    def send_command(self, name):
        if self.before_command is not None:
            self.before_command()
        frame = self.command_frames[name]
        expected_seq = parse_frame(frame).get("seq")
        with self.condition:
            if self.client is None:
                raise RuntimeError("AC is not connected")
            client = self.client
            self.ack_seq = None
            status_revision = self.status_revision
            with self.write_lock:
                client.sendall(frame)
            if self.capture is not None:
                self.capture.write(frame)
                self.capture.flush()
            log(f"sent recorded command through local CLI: {name}")

            deadline = datetime.datetime.now() + datetime.timedelta(seconds=10)
            while self.ack_seq != expected_seq:
                remaining = (deadline - datetime.datetime.now()).total_seconds()
                if remaining <= 0:
                    raise TimeoutError(f"AC did not acknowledge {name}")
                self.condition.wait(remaining)
                if self.client is None:
                    raise RuntimeError("AC disconnected during command")

            deadline = datetime.datetime.now() + datetime.timedelta(seconds=3)
            while self.status_revision == status_revision:
                remaining = (deadline - datetime.datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            return self.snapshot()


def handle_control(connection, state):
    try:
        connection.settimeout(15)
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                raise ValueError("empty control request")
            data += chunk
        request = json.loads(data.split(b"\n", 1)[0])
        action = request.get("action")
        if action == "status":
            result = state.snapshot()
        elif action in state.command_frames:
            result = state.send_command(action)
        else:
            raise ValueError(f"unknown action: {action}")
        response = {"ok": True, **result}
    except Exception as error:
        response = {"ok": False, "error": str(error)}
    try:
        connection.sendall(json.dumps(response).encode() + b"\n")
    finally:
        connection.close()


def control_listener(state, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(16)
    log(f"local CLI listening on 127.0.0.1:{port}")
    while True:
        connection, _ = listener.accept()
        threading.Thread(target=handle_control, args=(connection, state),
                         daemon=True).start()


def handle_client(raw_client, address, context, responses, state,
                  initial_command, initial_command_name):
    peer = f"{address[0]}:{address[1]}"
    try:
        client = context.wrap_socket(raw_client, server_side=True)
    except (OSError, ssl.SSLError) as error:
        log(f"{peer} TLS accept failed: {error}")
        raw_client.close()
        return

    state.attach(client)
    log(f"{peer} connected; TLS {client.version()}")
    buffer = b""
    command_sent = False
    try:
        client.settimeout(None)
        while True:
            raw, buffer = read_frame(client, buffer)
            obj = parse_frame(raw)
            message_id = obj.get("id")
            log(f"AC -> replay id={message_id} seq={obj.get('seq')} "
                f"status=[{status_text(obj)}]")
            if message_id == 201:
                state.acknowledge(obj)
                log("AC acknowledged a replayed command")
                continue
            if message_id == 903:
                state.update_status(obj)

            response = responses.get(message_id)
            if response is None:
                log(f"no recorded response for id={message_id}")
                continue
            client.sendall(response)
            log(f"replayed server response id={message_id}")

            if message_id == 903 and initial_command is not None and not command_sent:
                client.sendall(initial_command)
                command_sent = True
                log(f"sent recorded command: {initial_command_name}")
    except (EOFError, ConnectionError):
        pass
    except (OSError, ssl.SSLError, ValueError) as error:
        log(f"{peer} session error: {error}")
    finally:
        state.detach(client)
        client.close()
        log(f"{peer} disconnected")


def main():
    global LOG_FILE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--listen-port", type=int, default=443)
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument("--capture", type=Path, required=True,
                        help="capture basename without .c2s.bin/.s2c.bin")
    parser.add_argument("--command", default="none",
                        help="mapped command to send after initial status")
    parser.add_argument("--command-map", type=Path,
                        help="JSON object mapping command names to capture indices")
    parser.add_argument("--log-file", type=Path, default=LOG_FILE)
    args = parser.parse_args()
    LOG_FILE = args.log_file
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    cloud_frames = load_frames(args.capture.with_suffix(".s2c.bin"))
    responses = {}
    commands = []
    for frame in cloud_frames:
        message_id = parse_frame(frame).get("id")
        if message_id not in responses:
            responses[message_id] = frame
        if message_id == 201:
            commands.append(frame)

    command_index = {}
    if args.command_map:
        command_index = json.loads(args.command_map.read_text(encoding="utf-8"))
        if not isinstance(command_index, dict) or not all(
                isinstance(name, str) and isinstance(index, int) and index >= 0
                for name, index in command_index.items()):
            parser.error("command map must contain command names and nonnegative indices")
    try:
        command_frames = {name: commands[index]
                          for name, index in command_index.items()}
    except IndexError:
        parser.error("command map index exceeds captured command count")
    if args.command != "none" and args.command not in command_frames:
        parser.error(f"command is not present in the command map: {args.command}")
    initial_command = command_frames.get(args.command)
    state = ACState(command_frames)
    log(f"loaded {len(cloud_frames)} recorded server frames")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    try:
        context.set_ciphers("ALL:@SECLEVEL=0")
    except ssl.SSLError:
        pass

    threading.Thread(target=control_listener, args=(state, args.control_port),
                     daemon=True).start()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.bind, args.listen_port))
    listener.listen(16)
    log(f"listening on {args.bind}:{args.listen_port}; no cloud connection")
    try:
        while True:
            raw_client, address = listener.accept()
            threading.Thread(target=handle_client,
                             args=(raw_client, address, context, responses,
                                   state, initial_command, args.command),
                             daemon=True).start()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
