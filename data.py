"""Deterministic BTC data layer (Coinalyze, plus Binance/Coinbase order books). All numbers computed here, never by the LLM."""
import json
import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://api.coinalyze.net/v1"
STATS = {"coinalyze_calls": 0, "rate_limited": 0, "rate_limit_wait_s": 0.0}  # printed by check.py
TIMINGS = {}  # seconds per data block on the last orderflow() call
_slots = threading.Semaphore(4)  # at most 4 Coinalyze requests at once (limit is 40/min)


def _get(path, **params):
    key = os.getenv("COINALYZE_API_KEY")
    if not key:
        raise RuntimeError("COINALYZE_API_KEY not set")
    for _ in range(4):
        with _slots:
            STATS["coinalyze_calls"] += 1
            r = requests.get(BASE + path, params=params, headers={"api_key": key}, timeout=25)
        if r.status_code == 429:  # rate limit (40 calls/min)
            wait = min(float(r.headers.get("Retry-After", 5)), 65) + 0.5
            STATS["rate_limited"] += 1
            STATS["rate_limit_wait_s"] += wait
            logging.warning("Coinalyze rate limit on %s, waiting %.0fs", path, wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Coinalyze rate limit hit repeatedly")


_cache = {}
# which markets to use changes slowly, so the picks are saved to disk and survive restarts
_DISK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".market_cache.json")
_PICK_TTL = 12 * 3600


def _recall(key):
    try:
        with open(_DISK) as f:
            t, v = json.load(f)[key]
        return v if time.time() - t < _PICK_TTL else None
    except Exception:
        return None


def _remember(key, value):
    try:
        with open(_DISK) as f:
            saved = json.load(f)
    except Exception:
        saved = {}
    saved[key] = [time.time(), value]
    try:
        with open(_DISK, "w") as f:
            json.dump(saved, f)
    except OSError:
        pass


def _markets():
    m = _cache.get("_markets")
    if not m or time.time() - m[0] > 6 * 3600:
        m = _cache["_markets"] = (time.time(), _get("/future-markets"))
    return m[1]


def top_symbols(asset, n=8):
    """Largest perp markets for an asset by open interest (picked every 12h, saved to disk)."""
    c = _cache.get(asset)
    if c and time.time() - c[0] < _PICK_TTL:
        return c[1]
    saved = _recall("perps-" + asset)
    if saved:
        _cache[asset] = (time.time(), saved)
        return saved
    markets = _markets()
    perps = [m for m in markets if m.get("base_asset") == asset and m.get("is_perpetual")]
    stable = [m for m in perps if m.get("margined") == "STABLE"]
    cand = [m["symbol"] for m in (stable or perps)]
    if not cand:
        raise ValueError(f"No perpetual markets found for {asset}")
    ois = []
    for i in range(0, len(cand), 20):
        ois += _get("/open-interest", symbols=",".join(cand[i:i + 20]), convert_to_usd="true")
    ois = sorted([o for o in ois if o.get("value")], key=lambda o: o["value"], reverse=True)[:n]
    out = {"symbols": [o["symbol"] for o in ois], "oi": {o["symbol"]: o["value"] for o in ois}}
    _cache[asset] = (time.time(), out)
    _remember("perps-" + asset, out)
    return out


def _hist(path, symbols, interval, hours, **extra):
    now = int(time.time())
    params = {"symbols": ",".join(symbols), "interval": interval,
              "from": now - hours * 3600, "to": now, **extra}
    return _get(path, **params)


def _agg(res, field, how="sum"):
    """Aggregate a field across symbols per timestamp -> sorted [(t, value)]. Drops partial candles."""
    sums, cnt = defaultdict(float), defaultdict(int)
    for s in res:
        for p in s.get("history", []):
            if p.get(field) is not None:
                sums[p["t"]] += p[field]
                cnt[p["t"]] += 1
    if not cnt:
        return []
    full = max(cnt.values())
    rows = []
    for t in sorted(sums):
        if cnt[t] >= 0.75 * full:  # skip hours only a few exchanges have reported yet
            rows.append((t, sums[t] / cnt[t] if how == "mean" else sums[t]))
    return rows


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if b else None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return default if default is not None else {"unavailable": f"{e.__class__.__name__}: {e}"}


UA = {"User-Agent": "trading-agent/1.0"}


def _pub(url, **params):
    """Public exchange endpoint (no key)."""
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _range_block(h):
    """Range structure from hourly candles: the 72h range before the last 6h, and what price is doing at its edges."""
    prior, recent, win = h[-78:-6], h[-6:], h[-78:]
    rh, rl, price = max(x["h"] for x in prior), min(x["l"] for x in prior), h[-1]["c"]

    def tests(level, high):
        n, last = 0, -99  # separate touches = at least 4h apart
        for i, x in enumerate(win):
            near = x["h"] >= level * 0.997 if high else x["l"] <= level * 1.003
            if near and i - last > 3:
                n += 1
            if near:
                last = i
        return n

    hi6, lo6 = max(x["h"] for x in recent), min(x["l"] for x in recent)
    pos = (price - rl) / (rh - rl) if rh > rl else 0.5
    if hi6 > rh and price < rh:
        state = "swept above range high in the last 6h and is back inside (failed breakout)"
    elif price > rh:
        state = "breaking out above range high"
    elif lo6 < rl and price > rl:
        state = "swept below range low in the last 6h and reclaimed it (failed breakdown)"
    elif price < rl:
        state = "breaking down below range low"
    else:
        state = "near range high" if pos > 0.8 else "near range low" if pos < 0.2 else "mid-range"
    return {"range_high_72h": rh, "range_low_72h": rl, "range_width_pct": _pct(rh, rl),
            "position_in_range_pct": round(pos * 100), "state": state,
            "touches_of_range_high": tests(rh, True), "touches_of_range_low": tests(rl, False)}


SPOT_QUOTES = {"USDT", "USD", "USDC", "FDUSD"}


def spot_symbols(asset, n=6):
    """Largest spot markets for an asset on Coinalyze by 24h dollar volume (picked every 12h)."""
    c = _cache.get("spot-" + asset)
    if c and time.time() - c[0] < _PICK_TTL:
        return c[1]
    saved = _recall("spot-" + asset)
    if saved:
        _cache["spot-" + asset] = (time.time(), saved)
        return saved
    m = _cache.get("_spot_markets")
    if not m or time.time() - m[0] > 6 * 3600:
        m = _cache["_spot_markets"] = (time.time(), _get("/spot-markets"))
    cand = [x["symbol"] for x in m[1] if str(x.get("base_asset", "")).upper() == asset
            and str(x.get("quote_asset", "")).upper() in SPOT_QUOTES and x.get("has_buy_sell_data", True)]
    if not cand:
        raise ValueError(f"No spot markets found for {asset}")
    vol = {}
    for i in range(0, len(cand), 20):
        for s in _hist("/ohlcv-history", cand[i:i + 20], "1hour", 24):
            vol[s["symbol"]] = sum((p.get("v") or 0) * (p.get("c") or 0) for p in s.get("history", []))
    out = sorted((s for s in vol if vol[s] > 0), key=vol.get, reverse=True)[:n]
    if not out:
        raise ValueError(f"No spot volume data for {asset}")
    _cache["spot-" + asset] = (time.time(), out)
    _remember("spot-" + asset, out)
    return out


def _cvd_windows(delta):
    return {f"cvd_{n}": round(sum(delta[-c:]), 1) for n, c in (("1h", 12), ("4h", 48), ("24h", 288))}


def _coinalyze_cvd_5m(syms):
    res = _hist("/ohlcv-history", syms, "5min", 24)
    for s in res:
        for p in s.get("history", []):
            p["delta"] = 2 * p["bv"] - p["v"] if p.get("bv") is not None and p.get("v") is not None else None
    delta = [v for _, v in _agg(res, "delta")]
    if len(delta) < 48:
        raise RuntimeError(f"only {len(delta)} 5min candles returned")
    return _cvd_windows(delta), res


def _binance_cvd_5m(url, symbol):
    k = _pub(url, symbol=symbol, interval="5m", limit=288)
    return _cvd_windows([2 * float(x[9]) - float(x[5]) for x in k])  # taker buy vs total volume


def flow_block(asset, perp_syms):
    """Aggressive (taker) buying minus selling, spot vs perps, from 5min candles. Coinalyze aggregates across
    exchanges; Binance alone is the fallback if Coinalyze spot data is unavailable."""
    perps, res = _coinalyze_cvd_5m(perp_syms)
    try:
        spot_syms = spot_symbols(asset)
        spot, _ = _coinalyze_cvd_5m(spot_syms)
        spot["markets"], source = spot_syms, "Coinalyze, aggregated across exchanges"
    except Exception as e:
        spot = _binance_cvd_5m("https://api.binance.com/api/v3/klines", asset + "USDT")
        source = f"spot from Binance only (Coinalyze spot failed: {e.__class__.__name__}); perps from Coinalyze"
    closes = [p["c"] for p in next(s["history"] for s in res if s["symbol"] == perp_syms[0])]
    return {"source": source, "units": f"{asset}; positive = aggressive buyers, negative = aggressive sellers",
            "spot": spot, "perps": perps,
            "price_change_1h_pct": _pct(closes[-1], closes[-13]), "price_change_4h_pct": _pct(closes[-1], closes[-49]),
            "last_hour_5m_closes": [round(c, 4) for c in closes[-12:]]}


def _depth_summary(bids, asks):
    mid = (bids[0][0] + asks[0][0]) / 2
    reach = min(mid / bids[-1][0] - 1, asks[-1][0] / mid - 1) * 100  # how far the returned book extends
    out = {"mid": round(mid, 2), "book_depth_covered_pct": round(min(reach, 100), 2),
           "walls_note": "a wall is a 0.1% price level 0.15-2% from price with 3x+ the typical size; null = none stands out"}
    for pct in (0.5, 1, 2):
        if pct > reach:
            break
        b = sum(p * s for p, s in bids if p >= mid * (1 - pct / 100))
        a = sum(p * s for p, s in asks if p <= mid * (1 + pct / 100))
        out[f"within_{pct}pct"] = {"bids_usd": round(b), "asks_usd": round(a),
                                   "bid_share_pct": round(b / (a + b) * 100) if a + b else None}

    def wall(levels):
        """Biggest 0.1% price bucket between 0.15% and 2% from price, only if it is at least 3x a typical bucket.
        Liquidity always piles up right at the touch, so that zone is not counted as a wall."""
        bucket, agg, far = mid * 0.001, defaultdict(float), min(reach, 2) / 100
        for p, s in levels:
            if 0.0015 < abs(p / mid - 1) <= far:
                agg[round(p / bucket)] += p * s
        if len(agg) < 4:
            return None
        sizes = sorted(agg.values())
        typical = sizes[len(sizes) // 2]
        k, v = max(agg.items(), key=lambda kv: kv[1])
        if v < 3 * typical:
            return None
        return {"price": round(k * bucket, 2), "size_usd": round(v), "distance_pct": round((k * bucket / mid - 1) * 100, 2),
                "x_typical_level": round(v / typical, 1)}

    out["largest_bid_wall"], out["largest_ask_wall"] = wall(bids), wall(asks)
    return out


def book_block(asset):
    """Resting (passive) liquidity near price on the two biggest spot books."""
    def binance(url, limit):
        d = _pub(url, symbol=asset + "USDT", limit=limit)
        return [(float(p), float(q)) for p, q in d["bids"]], [(float(p), float(q)) for p, q in d["asks"]]

    def coinbase():
        d = _pub(f"https://api.exchange.coinbase.com/products/{asset}-USD/book", level=2)
        return [(float(x[0]), float(x[1])) for x in d["bids"]], [(float(x[0]), float(x[1])) for x in d["asks"]]

    books = {"binance_spot": lambda: binance("https://api.binance.com/api/v3/depth", 5000),
             "coinbase_spot": coinbase}
    with ThreadPoolExecutor(len(books)) as ex:
        futs = {k: ex.submit(_safe, lambda fn=fn: _depth_summary(*fn())) for k, fn in books.items()}
    return {k: f.result() for k, f in futs.items()}


def daily_block(main, syms):
    """Longer-term picture from daily candles: 30/90-day trend, 90-day range, moving averages, 30-day OI change."""
    h = next(s["history"] for s in _hist("/ohlcv-history", [main], "daily", 24 * 120) if s["symbol"] == main)
    closes = [x["c"] for x in h]
    if len(closes) < 31:
        raise RuntimeError(f"only {len(closes)} daily candles returned")
    price, ma = closes[-1], lambda n: round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None
    hi, lo = max(x["h"] for x in h[-90:]), min(x["l"] for x in h[-90:])
    out = {"change_30d_pct": _pct(price, closes[-31]), "change_90d_pct": _pct(price, closes[-91]) if len(closes) > 90 else None,
           "high_90d": hi, "low_90d": lo, "position_in_90d_range_pct": round((price - lo) / (hi - lo) * 100) if hi > lo else None,
           "ma_20d": ma(20), "ma_50d": ma(50), "ma_100d": ma(100)}
    oi = _agg(_hist("/open-interest-history", syms, "daily", 24 * 31, convert_to_usd="true"), "c")
    if len(oi) >= 2:
        out["open_interest_change_30d_pct"] = _pct(oi[-1][1], oi[0][1])
    return out


def _timed(name, fn):
    t = time.time()
    try:
        return _safe(fn)
    finally:
        TIMINGS[name] = round(time.time() - t, 1)


def orderflow(asset="BTC", micro=True):
    asset = asset.upper()
    TIMINGS.clear()
    t = time.time()
    info = top_symbols(asset)
    TIMINGS["pick_perp_markets"] = round(time.time() - t, 1)
    syms, main = info["symbols"], info["symbols"][0]

    ohlcv = _hist("/ohlcv-history", syms, "1hour", 170)
    h = next(s["history"] for s in ohlcv if s["symbol"] == main)
    if len(h) < 26:
        raise RuntimeError("Not enough price history returned")
    price = h[-1]["c"]
    p24, p7d, p1h = h[-25]["c"], h[0]["c"], h[-2]["c"]

    # taker delta / CVD, aggregated across top exchanges (in base asset units)
    # only markets that report buy volume, otherwise their total volume skews delta negative
    for s in ohlcv:
        for p in s.get("history", []):
            p["delta"] = 2 * p["bv"] - p["v"] if p.get("bv") is not None and p.get("v") is not None else None
    delta = [v for _, v in _agg(ohlcv, "delta")]
    cvd = {f"cvd_{k}_{asset}": round(sum(delta[-n:]), 1) for k, n in (("6h", 6), ("24h", 24), ("7d", len(delta)))}
    ch24 = _pct(price, p24)
    cvd24 = cvd[f"cvd_24h_{asset}"]
    divergence = "none"
    if ch24 is not None and abs(ch24) > 0.5 and cvd24 != 0:
        if ch24 > 0 and cvd24 < 0:
            divergence = "price up while CVD down (bearish divergence)"
        elif ch24 < 0 and cvd24 > 0:
            divergence = "price down while CVD up (bullish divergence)"
        else:
            divergence = "price and CVD agree"

    def oi_block():
        s = _agg(_hist("/open-interest-history", syms, "1hour", 30, convert_to_usd="true"), "c")
        return {"total_usd": round(s[-1][1]), "change_1h_pct": _pct(s[-1][1], s[-2][1]),
                "change_4h_pct": _pct(s[-1][1], s[-5][1]), "change_24h_pct": _pct(s[-1][1], s[-25][1])}

    def funding_block():
        cur = {x["symbol"]: x["value"] for x in _get("/funding-rate", symbols=",".join(syms))}
        pred = {x["symbol"]: x["value"] for x in _get("/predicted-funding-rate", symbols=",".join(syms))}
        w = {s: info["oi"].get(s, 0) for s in cur}
        tot = sum(w.values()) or 1
        fh = _agg(_hist("/funding-rate-history", syms, "1hour", 24), "c", how="mean")
        vals = [v for _, v in fh]
        return {"oi_weighted_current_pct": round(sum(cur[s] * w[s] for s in cur) / tot, 4),
                "oi_weighted_predicted_pct": round(sum(pred.get(s, 0) * w[s] for s in cur) / tot, 4),
                "range_24h_pct": [round(min(vals), 4), round(max(vals), 4)] if vals else None,
                "by_exchange_pct": {s: round(v, 4) for s, v in cur.items()}}

    def liq_block():
        lq = _hist("/liquidation-history", syms, "1hour", 170, convert_to_usd="true")
        lo, sh = dict(_agg(lq, "l")), dict(_agg(lq, "s"))
        ts_ = sorted(set(lo) | set(sh))
        tot = [lo.get(t, 0) + sh.get(t, 0) for t in ts_]
        L = lambda d, n: round(sum(d.get(t, 0) for t in ts_[-n:]))
        return {"last_1h_total_usd": round(tot[-1]), "last_1h_longs_liquidated_usd": L(lo, 1),
                "last_1h_shorts_liquidated_usd": L(sh, 1), "last_24h_longs_usd": L(lo, 24),
                "last_24h_shorts_usd": L(sh, 24), "avg_hourly_7d_usd": round(sum(tot) / len(tot)),
                "max_hour_7d_usd": round(max(tot))}

    jobs = {"open_interest": oi_block, "funding": funding_block, "liquidations": liq_block}
    if micro:
        jobs.update(daily_trend=lambda: daily_block(main, syms), flow_5m_spot_vs_perps=lambda: flow_block(asset, syms),
                    order_book=lambda: book_block(asset))
    with ThreadPoolExecutor(len(jobs)) as ex:  # independent requests, so fetch them at the same time
        futs = {k: ex.submit(_timed, k, fn) for k, fn in jobs.items()}
    got = {k: f.result() for k, f in futs.items()}

    return {
        "asset": asset, "markets_used": syms, "price": price,
        "change_1h_pct": _pct(price, p1h), "change_24h_pct": ch24, "change_7d_pct": _pct(price, p7d),
        "high_24h": max(x["h"] for x in h[-24:]), "low_24h": min(x["l"] for x in h[-24:]),
        "high_7d": max(x["h"] for x in h), "low_7d": min(x["l"] for x in h),
        "range": _safe(lambda: _range_block(h)),
        **cvd, "price_vs_cvd_24h": divergence,
        **got,
    }


FLOORS = {"BTC": (50e6, 2.5)}  # (liquidation $ floor, 1h move %)


def detect_alerts(d):
    """Returns [(key, message)] for notable conditions."""
    a, out = d["asset"], []
    floor, move = FLOORS.get(a, (5e6, 4.0))
    lq = d.get("liquidations", {})
    if "last_1h_total_usd" in lq and lq["last_1h_total_usd"] >= max(floor, 4 * lq["avg_hourly_7d_usd"]):
        out.append((f"{a}-liq", f"{a}: liquidation spike. ${lq['last_1h_total_usd']/1e6:.0f}M in 1h "
                    f"(longs ${lq['last_1h_longs_liquidated_usd']/1e6:.0f}M / shorts ${lq['last_1h_shorts_liquidated_usd']/1e6:.0f}M), "
                    f"7d hourly avg ${lq['avg_hourly_7d_usd']/1e6:.0f}M."))
    f = d.get("funding", {}).get("oi_weighted_current_pct")
    if f is not None and abs(f) >= 0.05:
        out.append((f"{a}-funding", f"{a}: extreme funding {f:+.3f}% (OI-weighted)."))
    oi = d.get("open_interest", {}).get("change_4h_pct")
    if oi is not None and abs(oi) >= 5:
        out.append((f"{a}-oi", f"{a}: open interest {oi:+.1f}% in 4h."))
    if d.get("change_1h_pct") is not None and abs(d["change_1h_pct"]) >= move:
        out.append((f"{a}-move", f"{a}: price moved {d['change_1h_pct']:+.2f}% in 1h, now {d['price']:,.2f}."))
    return out
