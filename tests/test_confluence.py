"""Replays recorded history through confluence.py with fake API responses and checks the signals match the
backtest (BTC Flow Desk research). No network, stdlib only: python tests/test_confluence.py

Fixture: 69 hours from 2023-26 (random hours plus fear + flush and all-three hours) with 72 h of hourly data
before each, and Binance BTCUSDT daily candles Mar 2022 - Oct 2026, with the flags the backtest gave."""
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import confluence as cf  # noqa: E402

FX = json.loads(gzip.decompress(open(os.path.join(HERE, "confluence_fixture.json.gz"), "rb").read()))
HOURLY = {int(k): v for k, v in FX["hourly"].items()}   # hour start -> [o, h, l, c, qv, tbqv, funding, oi, dvol]
DAILY = FX["daily"]                                    # [t, o, h, l, c, qv, tbqv, oi]
H, D = cf.HOUR, cf.DAY
NOW = [0]


def fake_get(url, **p):
    now = NOW[0]
    hours = sorted(t for t in HOURLY if t + H <= now)
    if "volatility_index" in url:
        return {"result": {"data": [[t, 0, 0, 0, HOURLY[t][8]] for t in hours
                                    if t >= p["start_timestamp"] and HOURLY[t][8] is not None]}}
    if "openInterestHist" in url:
        if p["period"] == "1h":  # a row stamped T is the snapshot at T, the close of hour T - 1h
            return [{"timestamp": t + H, "sumOpenInterestValue": HOURLY[t][7]} for t in hours[-p["limit"]:] if HOURLY[t][7]]
        days = [d for d in DAILY if d[0] + D <= now][-p["limit"]:]
        return [{"timestamp": d[0] + D, "sumOpenInterestValue": d[7]} for d in days if d[7]]
    if "fundingRate" in url:
        return [{"fundingTime": now - H, "fundingRate": str(HOURLY[hours[-1]][6])}]
    if "klines" in url:
        if p["interval"] == "1h":
            return [[t, *HOURLY[t][:4], 0, t + H - 1, HOURLY[t][4], 0, 0, HOURLY[t][5]] for t in hours[-p["limit"]:]]
        days = [d for d in DAILY if d[0] + D <= now][-p["limit"]:]
        return [[d[0], *d[1:5], 0, d[0] + D - 1, d[5], 0, 0, d[6]] for d in days]
    raise ValueError(url)


def main():
    cf._get = fake_get
    bad = 0
    for t, (fear, flush, shorts) in FX["expect_short"].items():
        NOW[0] = int(t) + H + 60_000  # a minute after the hour closes
        s = cf.short_term(NOW[0])
        got = (s["fear"], s["flush"], s["shorts"])
        if got != (fear, flush, shorts):
            bad += 1
            print("short-term mismatch", t, got, (fear, flush, shorts))
    print(f"short-term: {len(FX['expect_short']) - bad}/{len(FX['expect_short'])} hours match")
    on, want = [], set(FX["htf_on"])
    for t in FX["htf_days"]:
        NOW[0] = t + D + 60_000
        r = cf.htf(NOW[0])
        if r["on"] != (t in want):
            bad += 1
            print("HTF mismatch", t, r["met"])
        if r["on"]:
            on.append(t)
    print(f"HTF bottom: {len(FX['htf_days'])} days checked, {len(on)} signal days, {len(want)} expected")
    sent = {}  # alert flow: the first all-three hour in the fixture, then the cooldown an hour later
    t3 = min(int(t) for t, v in FX["expect_short"].items() if all(v))
    NOW[0] = t3 + H + 60_000
    keys = [k for k, _ in cf.detect_alerts(sent, NOW[0])]
    for k in keys:
        sent[k] = NOW[0] / 1000
    again = cf.detect_alerts(sent, NOW[0] + H)
    if not any(k.startswith("confluence-all3-") for k in keys) or again:
        bad += 1
        print("alert flow wrong", keys, again)
    print("FAILED" if bad else "OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
