"""Run this first: python check.py  (verifies Coinalyze + macro data before you start the bot)"""
import json
from dotenv import load_dotenv
load_dotenv(override=True)  # .env wins over stale shell variables
import data

for asset in ("BTC",):
    print(f"--- {asset} orderflow ---")
    d = data.orderflow(asset)
    print(json.dumps(d, indent=1))
    print("alerts now:", data.detect_alerts(d))
print("--- macro ---")
print(json.dumps(data.macro(), indent=1))
