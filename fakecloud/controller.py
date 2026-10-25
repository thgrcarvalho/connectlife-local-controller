#!/usr/bin/env python3

import asyncio
import datetime
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WINDOWS_DEPS = BASE_DIR / ".windows-deps"
if WINDOWS_DEPS.is_dir():
    sys.path.insert(0, str(WINDOWS_DEPS))

from connectlife.api import ConnectLifeApi

from credential_store import load_credentials
CAPTURE_DIR = BASE_DIR / "captures"
CONTROL_PORT = 8766
SUBPROCESS_LOG = CAPTURE_DIR / "controller-processes.log"
CONFIG_PATH = Path(os.environ.get("AC_LOCAL_CONFIG", BASE_DIR / "config.json"))
REQUIRED_CONFIG = (
    "bind_ip", "upstream_dns", "cloud_upstream_ip", "hijack_domain",
    "offline_capture", "command_map",
)


def load_config():
    if not CONFIG_PATH.is_file():
        raise RuntimeError(
            f"Missing {CONFIG_PATH}; copy config.example.json and edit your settings")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_CONFIG if not config.get(key)]
    if missing:
        raise ValueError(f"Missing configuration values: {', '.join(missing)}")
    return config


def configured_path(value):
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path

ACTION_PROPERTIES = {
    "on": {"t_power": "1"},
    "off": {"t_power": "0"},
    "fan": {"t_work_mode": "0"},
    "cool": {"t_work_mode": "2"},
    "dry": {"t_work_mode": "3"},
    "eco": {"t_work_mode": "5"},
    "fanlow": {"t_fan_speed": "2"},
    "fanmed": {"t_fan_speed": "3"},
    "fanhigh": {"t_fan_speed": "4"},
    "sleep": {"t_sleep": "1"},
    "sleepoff": {"t_sleep": "0"},
}
ACTION_PROPERTIES.update({f"temp{temp}": {"t_temp": str(temp)} for temp in range(16, 31)})
ALLOWED_ACTIONS = frozenset((*ACTION_PROPERTIES, "status", "diagnose"))
STATUS_FIELDS = frozenset({
    "t_power", "t_temp", "t_work_mode", "t_fan_speed", "t_sleep", "f_temp_in",
    "f_e_intemp", "f_e_incoiltemp", "f_e_incom", "f_e_ineeprom",
    "f_e_over_cold", "f_e_over_hot", "f_e_push",
})
DIAGNOSTIC_RULES = (
    ("f_e_intemp", "Sensors", "Indoor room temperature sensor failure"),
    ("f_e_incoiltemp", "Sensors", "Indoor coil temperature sensor failure"),
    ("f_e_incom", "Communications", "Indoor/outdoor communication failure"),
    ("f_e_ineeprom", "Others", "Indoor control-panel EEPROM error"),
    ("f_e_over_cold", "Others", "Overcooling protection in Cool mode"),
    ("f_e_over_hot", "Others", "Overheating protection in Heat mode"),
)


def filtered_status(value):
    return {key: item for key, item in value.items() if key in STATUS_FIELDS}


def query_replay(action):
    with socket.create_connection(("127.0.0.1", 8765), timeout=20) as connection:
        connection.sendall(json.dumps({"action": action}).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(65535)
            if not chunk:
                raise RuntimeError("replay control connection closed")
            data += chunk
    response = json.loads(data.split(b"\n", 1)[0])
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "replay command failed"))
    return response


def diagnostic_summary(status):
    categories = {"Sensors": [], "Communications": [], "Others": []}
    for key, category, label in DIAGNOSTIC_RULES:
        if status.get(key) in (1, "1"):
            categories[category].append(label)
    return {"normal": all(not items for items in categories.values()),
            "categories": categories}


class Controller:
    def __init__(self, config):
        self.config = config
        CAPTURE_DIR.mkdir(exist_ok=True)
        self.api = None
        self.api_test_server = None
        self.puid = None
        self.mode = "online"
        self.cloud_failures = 0
        self.cloud_successes = 0
        self.processes = {}
        self.running = True
        self.last_status = {}

    def log(self, message):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        print(line, flush=True)
        with SUBPROCESS_LOG.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def start_process(self, name, arguments):
        process = self.processes.get(name)
        if process is not None and process.poll() is None:
            return
        self.log(f"starting {name}")
        with SUBPROCESS_LOG.open("a", encoding="utf-8") as stream:
            self.processes[name] = subprocess.Popen(
                arguments, cwd=BASE_DIR, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def stop_process(self, name):
        process = self.processes.pop(name, None)
        if process is None:
            return
        self.log(f"stopping {name}")
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def stop_stale_processes(self):
        script = (
            "$names = @('windows_dns.py','replay_server.py','relay.py'); "
            "Get-CimInstance Win32_Process | Where-Object { "
            "$process = $_; $process.Name -eq 'python.exe' -and $process.CommandLine -and "
            "($names | Where-Object { $process.CommandLine.Contains($_) }) } | "
            "ForEach-Object { Stop-Process -Id $process.ProcessId -Force }")
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                       check=False, capture_output=True)

    def stop_all(self):
        for name in list(self.processes):
            self.stop_process(name)

    def set_mode(self, mode):
        if mode == self.mode and self.processes.get("dns") is not None:
            return
        self.log(f"switching to {mode} mode")
        self.stop_all()
        self.mode = mode
        if mode == "offline":
            self.start_process("replay", [
                sys.executable, str(BASE_DIR / "replay_server.py"),
                "--bind", self.config["bind_ip"],
                "--capture", str(configured_path(self.config["offline_capture"])),
                "--command-map", str(configured_path(self.config["command_map"])),
                "--log-file", str(CAPTURE_DIR / "offline-replay.log"),
            ])
            self.start_process("dns", [
                sys.executable, str(BASE_DIR / "windows_dns.py"),
                "--bind", self.config["bind_ip"],
                "--upstream", self.config["upstream_dns"],
                "--hijack", self.config["hijack_domain"],
            ])
        else:
            self.start_process("relay", [
                sys.executable, str(BASE_DIR / "relay.py"),
                "--bind", self.config["bind_ip"],
                "--upstream-ip", self.config["cloud_upstream_ip"],
                "--log-file", str(CAPTURE_DIR / "online-relay.log"),
            ])
            self.start_process("dns", [
                sys.executable, str(BASE_DIR / "windows_dns.py"),
                "--bind", self.config["bind_ip"],
                "--upstream", self.config["upstream_dns"],
                "--hijack", self.config["hijack_domain"],
            ])
        self.cloud_failures = 0
        self.cloud_successes = 0

    async def cloud_request(self, callback):
        test_file = CAPTURE_DIR / "controller-test-server.txt"
        test_server = test_file.read_text().strip() if test_file.exists() else None
        if self.api is None or self.api_test_server != test_server:
            email, password = load_credentials()
            self.api = ConnectLifeApi(email, password, test_server=test_server)
            self.api_test_server = test_server
        return await asyncio.wait_for(callback(), timeout=25)

    async def check_cloud(self):
        try:
            devices = await self.cloud_request(lambda: self.api.get_appliances_json())
            if not devices:
                raise RuntimeError("no appliances returned")
            self.puid = devices[0]["puid"]
            return True
        except Exception as error:
            self.log(f"cloud health check failed: {type(error).__name__}")
            return False

    async def run(self):
        self.stop_stale_processes()
        self.set_mode("online")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", CONTROL_PORT))
        listener.listen(16)
        self.log(f"controller listening on 127.0.0.1:{CONTROL_PORT}")
        threading.Thread(target=self.control_loop, args=(listener,), daemon=True).start()
        try:
            while self.running:
                healthy = await self.check_cloud()
                if healthy:
                    self.cloud_successes += 1
                    self.cloud_failures = 0
                    if self.mode == "offline" and self.cloud_successes >= 2:
                        self.set_mode("online")
                else:
                    self.cloud_failures += 1
                    self.cloud_successes = 0
                    if self.mode == "online" and self.cloud_failures >= 3:
                        self.set_mode("offline")
                await asyncio.sleep(5)
        finally:
            listener.close()
            self.stop_all()

    async def get_status(self):
        if self.mode == "online":
            devices = await self.cloud_request(lambda: self.api.get_appliances_json())
            if not devices:
                raise RuntimeError("no appliances returned")
            status = filtered_status(devices[0].get("statusList", {}))
            connected = devices[0].get("offlineState") in (1, "1")
        else:
            response = query_replay("status")
            status = filtered_status(response.get("status", {}))
            connected = response.get("connected", False)
        self.last_status = status
        return {"connected": connected, "status": status}

    def validate_action(self, action, status):
        if action.startswith("temp") and (
                status.get("t_power") not in (1, "1") or status.get("t_work_mode") in (0, "0")):
            raise ValueError("temperature requires power on and a temperature-capable mode")
        if action == "sleep" and status.get("t_work_mode") in (0, "0"):
            raise ValueError("Sleep is not available in Fan mode")
        if action in ("fanlow", "fanmed", "fanhigh") and status.get("t_work_mode") in (3, "3"):
            raise ValueError("Dry mode fixes fan speed to High")

    async def handle_action(self, action):
        if action == "status":
            return await self.get_status()
        if action == "diagnose":
            return await self.get_diagnostics()
        properties = ACTION_PROPERTIES[action]
        current = await self.get_status()
        self.validate_action(action, current["status"])
        if self.mode == "online":
            await self.cloud_request(lambda: self.api.update_appliance(self.puid, properties))
            await asyncio.sleep(1)
            return await self.get_status()
        return query_replay(action)

    async def get_diagnostics(self):
        if self.mode == "offline":
            current = await self.get_status()
            return diagnostic_summary(current["status"])
        response = await self.cloud_request(lambda: self.api.get_self_check(self.puid))
        data = response.get("data", {})
        failed = data.get("selfCheckFailedList", [])
        result = {"normal": not failed, "failed": []}
        for item in failed:
            if isinstance(item, dict) and "statusKey" in item:
                result["failed"].append(item["statusKey"])
        return result

    async def handle_connection(self, connection):
        try:
            connection.settimeout(30)
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    raise ValueError("empty request")
                data += chunk
            action = json.loads(data.split(b"\n", 1)[0]).get("action")
            if action not in ALLOWED_ACTIONS:
                raise ValueError("unsupported action")
            result = await self.handle_action(action)
            response = {"ok": True, "mode": self.mode, **result}
        except Exception as error:
            response = {"ok": False, "mode": self.mode, "error": str(error)}
        try:
            connection.sendall(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        finally:
            connection.close()

    def control_loop(self, listener):
        while self.running:
            try:
                connection, _ = listener.accept()
            except OSError:
                break
            threading.Thread(target=self.handle_connection_thread,
                             args=(connection,), daemon=True).start()

    def handle_connection_thread(self, connection):
        asyncio.run(self.handle_connection(connection))


async def main():
    controller = Controller(load_config())
    await controller.run()


if __name__ == "__main__":
    asyncio.run(main())
