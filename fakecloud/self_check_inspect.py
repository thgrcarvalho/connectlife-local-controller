#!/usr/bin/env python3
"""Retrieve ConnectLife self-check data without printing credentials or identifiers."""

import asyncio
import getpass
import json
import os
from pathlib import Path

from connectlife.api import ConnectLifeApi

OUTPUT = Path(__file__).with_name("self_check_metadata.json")


def print_item_summary(name, items):
    print(f"  {name}: {len(items)} entries")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        print(f"    entry {index}: fields={sorted(item)}")
        for key in ("statusKey", "statusValue", "fault", "contentKey", "faultKey"):
            if key in item:
                print(f"      {key}={item[key]}")


async def main():
    email = input("ConnectLife email: ").strip()
    password = getpass.getpass("ConnectLife password (not stored): ")
    api = ConnectLifeApi(email, password)
    devices = await api.get_appliances_json()
    result = {}
    for device in devices:
        puid = device["puid"]
        try:
            result[puid] = await api.get_self_check(puid)
        except Exception as error:
            result[puid] = {"error": str(error)}

    with os.fdopen(os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)

    for response in result.values():
        if "error" in response:
            print(f"  request error: {response['error']}")
            continue
        data = response.get("data", {})
        print(f"  data fields={sorted(data)}")
        for name in ("selfCheckList", "selfCheckFailedList"):
            items = data.get(name)
            if isinstance(items, list):
                print_item_summary(name, items)
    print(f"Raw responses written to {OUTPUT} (git-ignored; identifiers not printed).")


if __name__ == "__main__":
    asyncio.run(main())
