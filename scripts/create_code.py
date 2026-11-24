#!/usr/bin/env python3
"""Create access codes for the shop.

A code is what a customer buys: it adds TKN to their balance once.

Usage:
    python scripts/create_code.py 50000          # one code for 50 000 TKN
    python scripts/create_code.py 50000 --count 5
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Create FreeGate access codes")
    parser.add_argument("amount", type=float, help="How many TKN the code adds")
    parser.add_argument("--count", type=int, default=1, help="How many codes to make")
    args = parser.parse_args()

    if args.amount <= 0:
        parser.error("amount must be positive")

    db.init()
    for _ in range(max(1, args.count)):
        print(db.make_code(args.amount))


if __name__ == "__main__":
    main()
