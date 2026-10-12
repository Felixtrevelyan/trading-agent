"""Run this first: python check.py  (verifies the BTC data sources before you start the bot)"""
import json
import time
from dotenv import load_dotenv
load_dotenv(override=True)  # .env wins over stale shell variables
import data

t = time.time()
d = data.orderflow("BTC")
print(json.dumps(d, indent=1))
print("alerts now:", data.detect_alerts(d))
fl, bk = d.get("flow_5m_spot_vs_perps", {}), d.get("order_book", {})
print("SUMMARY spot/perp flow:", fl.get("source") or fl.get("unavailable"))
print("SUMMARY daily trend:", "unavailable: " + d["daily_trend"]["unavailable"] if "unavailable" in d.get("daily_trend", {}) else "ok")
for k, v in bk.items():
    print(f"SUMMARY book {k}:", "unavailable: " + v["unavailable"] if "unavailable" in v else "ok")
print(f"SUMMARY fetched in {time.time() - t:.1f}s")
