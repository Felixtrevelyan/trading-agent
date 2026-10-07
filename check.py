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
    fl, bk = d.get("flow_5m_spot_vs_perps", {}), d.get("order_book", {})
    print("SUMMARY spot/perp flow:", fl.get("source") or fl.get("unavailable"))
    for k, v in bk.items():
        print(f"SUMMARY book {k}:", "unavailable: " + v["unavailable"] if "unavailable" in v else "ok")
print("--- macro ---")
print(json.dumps(data.macro(), indent=1))
