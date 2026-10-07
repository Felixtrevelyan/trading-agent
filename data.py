"""Deterministic data layer (Coinalyze + yfinance + FRED). All numbers computed here, never by the LLM."""
import os
import time
from collections import defaultdict

import requests
import yfinance as yf

BASE = "https://api.coinalyze.net/v1"


def _get(path, **params):
    key = os.getenv("COINALYZE_API_KEY")
    if not key:
        raise RuntimeError("COINALYZE_API_KEY not set")
    for _ in range(4):
        r = requests.get(BASE + path, params=params, headers={"api_key": key}, timeout=25)
        if r.status_code == 429:  # rate limit (40 calls/min)
            time.sleep(float(r.headers.get("Retry-After", 5)) + 0.5)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Coinalyze rate limit hit repeatedly")


_cache = {}


def _markets():
    m = _cache.get("_markets")
    if not m or time.time() - m[0] > 6 * 3600:
        m = _cache["_markets"] = (time.time(), _get("/future-markets"))
    return m[1]


def base_assets():
    """Every coin with a perpetual market on Coinalyze, e.g. {'BTC', 'ETH', ...}."""
    return {m["base_asset"].upper() for m in _markets() if m.get("is_perpetual") and m.get("base_asset")}


def top_symbols(asset, n=8):
    """Largest perp markets for an asset by open interest (cached 6h)."""
    c = _cache.get(asset)
    if c and time.time() - c[0] < 6 * 3600:
        return c[1]
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


def _taker_flow(url, symbol):
    k = _pub(url, symbol=symbol, interval="5m", limit=288)  # last 24h of 5m candles
    rows = [(float(x[4]), float(x[5]), float(x[9])) for x in k]  # close, volume, taker buy volume
    delta = [2 * tb - v for _, v, tb in rows]
    return {f"cvd_{n}": round(sum(delta[-c:]), 1) for n, c in (("1h", 12), ("4h", 48), ("24h", 288))}, rows


def flow_block(asset):
    """Aggressive (taker) buying minus selling on Binance spot vs Binance perps, from 5m candles."""
    spot, _ = _taker_flow("https://api.binance.com/api/v3/klines", asset + "USDT")
    perps, rows = _taker_flow("https://fapi.binance.com/fapi/v1/klines", asset + "USDT")
    closes = [c for c, _, _ in rows]
    return {"units": f"{asset}; positive = aggressive buyers, negative = aggressive sellers",
            "spot_binance": spot, "perps_binance": perps,
            "price_change_1h_pct": _pct(closes[-1], closes[-13]), "price_change_4h_pct": _pct(closes[-1], closes[-49]),
            "last_hour_5m_closes": [round(c, 4) for c in closes[-12:]]}


def _depth_summary(bids, asks):
    mid = (bids[0][0] + asks[0][0]) / 2
    reach = min(mid / bids[-1][0] - 1, asks[-1][0] / mid - 1) * 100  # how far the returned book extends
    out = {"mid": round(mid, 4), "book_depth_covered_pct": round(reach, 2)}
    for pct in (0.5, 1, 2):
        if pct > reach:
            break
        b = sum(p * s for p, s in bids if p >= mid * (1 - pct / 100))
        a = sum(p * s for p, s in asks if p <= mid * (1 + pct / 100))
        out[f"within_{pct}pct"] = {"bids_usd": round(b), "asks_usd": round(a),
                                   "bid_share_pct": round(b / (a + b) * 100) if a + b else None}

    def wall(levels):
        bucket, agg = mid * 0.001, defaultdict(float)  # group into 0.1% price buckets
        for p, s in levels:
            if abs(p / mid - 1) <= 0.02:
                agg[round(p / bucket)] += p * s
        if not agg:
            return None
        k, v = max(agg.items(), key=lambda kv: kv[1])
        return {"price": round(k * bucket, 4), "size_usd": round(v), "distance_pct": round((k * bucket / mid - 1) * 100, 2)}

    out["largest_bid_wall"], out["largest_ask_wall"] = wall(bids), wall(asks)
    return out


def book_block(asset):
    """Resting (passive) liquidity near price on the main spot books and Binance perps."""
    def binance(url, limit):
        d = _pub(url, symbol=asset + "USDT", limit=limit)
        return [(float(p), float(q)) for p, q in d["bids"]], [(float(p), float(q)) for p, q in d["asks"]]

    def coinbase():
        d = _pub(f"https://api.exchange.coinbase.com/products/{asset}-USD/book", level=2)
        return [(float(x[0]), float(x[1])) for x in d["bids"]], [(float(x[0]), float(x[1])) for x in d["asks"]]

    out = {}
    for name, fn in (("binance_spot", lambda: binance("https://api.binance.com/api/v3/depth", 5000)),
                     ("coinbase_spot", coinbase),
                     ("binance_perps", lambda: binance("https://fapi.binance.com/fapi/v1/depth", 1000))):
        out[name] = _safe(lambda fn=fn: _depth_summary(*fn()))
    return out


def orderflow(asset="BTC", micro=True):
    asset = asset.upper()
    info = top_symbols(asset)
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

    return {
        "asset": asset, "markets_used": syms, "price": price,
        "change_1h_pct": _pct(price, p1h), "change_24h_pct": ch24, "change_7d_pct": _pct(price, p7d),
        "high_24h": max(x["h"] for x in h[-24:]), "low_24h": min(x["l"] for x in h[-24:]),
        "high_7d": max(x["h"] for x in h), "low_7d": min(x["l"] for x in h),
        "range": _safe(lambda: _range_block(h)),
        **cvd, "price_vs_cvd_24h": divergence,
        "open_interest": _safe(oi_block), "funding": _safe(funding_block),
        "liquidations": _safe(liq_block),
        **({"flow_5m_spot_vs_perps": _safe(lambda: flow_block(asset)),
            "order_book": _safe(lambda: book_block(asset))} if micro else {}),
    }


FLOORS = {"BTC": (50e6, 2.5), "ETH": (25e6, 3.0)}  # (liquidation $ floor, 1h move %)


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


MACRO_TICKERS = {"DXY": "DX-Y.NYB", "US10Y_yield": "^TNX", "VIX": "^VIX", "SPX": "^GSPC",
                 "NASDAQ": "^IXIC", "GOLD": "GC=F", "OIL_WTI": "CL=F"}


# a 1-day move at least this big (in %) counts as macro worth mentioning
MACRO_NOTABLE_1D = {"DXY": 0.7, "US10Y_yield": 2.5, "VIX": 15, "SPX": 1.5, "NASDAQ": 2.0, "GOLD": 2.5, "OIL_WTI": 4}


def macro():
    out = {}
    for name, t in MACRO_TICKERS.items():
        try:
            c = yf.Ticker(t).history(period="1mo")["Close"].dropna()
            out[name] = {"last": round(float(c.iloc[-1]), 2),
                         "change_1d_pct": round((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2),
                         "change_1w_pct": round((c.iloc[-1] / c.iloc[-6] - 1) * 100, 2),
                         "change_1m_pct": round((c.iloc[-1] / c.iloc[0] - 1) * 100, 2)}
        except Exception as e:
            out[name] = f"unavailable ({e.__class__.__name__})"
    notable = []
    for name, limit in MACRO_NOTABLE_1D.items():
        v = out.get(name)
        if isinstance(v, dict) and abs(v["change_1d_pct"]) >= limit:
            notable.append(f"{name} {v['change_1d_pct']:+.2f}% today")
    if isinstance(out.get("VIX"), dict) and out["VIX"]["last"] >= 25:
        notable.append(f"VIX elevated at {out['VIX']['last']}")
    out["notable"] = notable
    key = os.getenv("FRED_API_KEY")
    if key:
        for name, sid in {"fed_funds_rate": "DFF", "yield_curve_10y2y": "T10Y2Y"}.items():
            try:
                r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                                 params={"series_id": sid, "api_key": key, "file_type": "json",
                                         "sort_order": "desc", "limit": 1}, timeout=10).json()
                out[name] = r["observations"][0]["value"]
            except Exception:
                out[name] = "unavailable"
    return out
