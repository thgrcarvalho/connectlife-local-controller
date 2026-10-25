#!/usr/bin/env python3
"""Inspect command indices and validate a map without printing captured payloads."""

import argparse
import json
from pathlib import Path

from replay_server import load_frames, parse_frame


def captured_commands(path):
    return [frame for frame in load_frames(path) if parse_frame(frame).get("id") == 201]


def list_commands(path):
    for index, frame in enumerate(captured_commands(path)):
        obj = parse_frame(frame)
        print(f"index={index} seq={obj.get('seq')} bytes={len(frame)}")


def validate_map(capture_path, map_path):
    commands = captured_commands(capture_path)
    command_map = json.loads(map_path.read_text(encoding="utf-8"))
    if not isinstance(command_map, dict) or not command_map:
        raise ValueError("command map must be a non-empty JSON object")
    for name, index in command_map.items():
        if not isinstance(name, str) or not name:
            raise ValueError("every command name must be a non-empty string")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError(f"{name}: index must be a nonnegative integer")
        if index >= len(commands):
            raise ValueError(f"{name}: index {index} exceeds {len(commands)} captured commands")
    print(f"Validated {len(command_map)} actions against {len(commands)} captured commands")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list id=201 command indices")
    list_parser.add_argument("capture", type=Path, help="path to the .s2c.bin capture")
    validate_parser = subparsers.add_parser("validate", help="validate a command map")
    validate_parser.add_argument("capture", type=Path, help="path to the .s2c.bin capture")
    validate_parser.add_argument("command_map", type=Path, help="command-map JSON")
    args = parser.parse_args()
    try:
        if args.command == "list":
            list_commands(args.capture)
        else:
            validate_map(args.capture, args.command_map)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
