#!/usr/bin/env python3
"""Create a new client API key (sk-...) and add it to keys.json.

Usage:
    python scripts/create_key.py my-phone
    python scripts/create_key.py "work laptop" --list
"""

import argparse
import secrets
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import load_keys, save_key  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Create an sk- API key")
    parser.add_argument("name", help="Who/what uses this key (label)")
    parser.add_argument("--list", action="store_true", help="List existing keys")
    args = parser.parse_args()

    if args.list:
        for k, meta in load_keys().items():
            print(f"{k}  ({meta.get('name', '?')}, created {meta.get('created', '?')})")
        return

    key = "sk-" + secrets.token_hex(24)
    save_key(key, args.name)
    print(f"Key for '{args.name}':")
    print(key)


if __name__ == "__main__":
    main()
