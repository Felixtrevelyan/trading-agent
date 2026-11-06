"""Confluence alerts: the long-only setups from the BTC Flow Desk's Confluence tab.

  Fear + flush          DVOL up >= 3 pts over 24 h AND Binance BTCUSDT open interest down >= 3% over 24 h
                        (1-3 day long; backtest +67 bp over 24 h, positive every year 2023-26)
  All three             fear + flush AND Binance funding <= 0 (crowded shorts on top: squeeze fuel)
                        (rare; backtest +221 bp over 72 h, 23 signals)
  HTF bottom            on the UTC daily close, all five of: 200-day average rising vs 30 days ago; price >= 8%
                        below the 50-day average; 14-day RSI <= 38; 7-day taker flow net selling; OI down >= 8%
                        over 30 days (multi-week swing long; 2023-25 +16% over 8 weeks vs +11% buying any day)

Short-term setups are judged on the last closed hour, the HTF bottom on the last closed daily candle, as in the
backtests. Public endpoints only (Deribit DVOL, Binance futures), no keys.
"""
import math
import time

import requests

UA = {"User-Agent": "trading-agent/1.0"}
FAPI = "https://fapi.binance.com"
HOUR, DAY = 3600_000, 86400_000

FEAR_DVOL_PTS = 3.0
FLUSH_OI_24H = -0.03
SHORTS_FUNDING = 0.0
HTF_MA50 = -0.08
HTF_RSI = 38.0
HTF_OI_30D = -0.08
# re-alert the same setup only after this long (the backtests spaced signals the same way)
COOLDOWN_S = {"pair": 24 * 3600, "all3": 24 * 3600, "htf": 30 * 86400}


def _get(url, **params):
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _closed(rows, close_idx, now_ms):
    return [r for r in rows if int(r[close_idx]) < now_ms]


def short_term(now_ms=None):
    """Fear, flush and crowded shorts on the last closed hour."""
    now = now_ms or int(time.time() * 1000)
    dv = _get("https://www.deribit.com/api/v2/public/get_volatility_index_data", currency="BTC",
              start_timestamp=now - 3 * DAY, end_timestamp=now, resolution="3600")["result"]["data"]
    dv = {int(x[0]): float(x[4]) for x in dv if int(x[0]) + HOUR <= now}  # candle start -> close, closed only
    oi = _get(FAPI + "/futures/data/openInterestHist", symbol="BTCUSDT", period="1h", limit=30)
    oi = {int(x["timestamp"]): float(x["sumOpenInterestValue"]) for x in oi}  # snapshot time -> USD
    fund = _get(FAPI + "/fapi/v1/fundingRate", symbol="BTCUSDT", limit=3)
    kl = _closed(_get(FAPI + "/fapi/v1/klines", symbol="BTCUSDT", interval="1h", limit=30), 6, now)

    t = max(dv)                                   # last closed hour (start)
    dvol, dvol_24 = dv[t], dv.get(t - 24 * HOUR)
    oi_t = max(oi)
    oi_now, oi_24 = oi[oi_t], oi.get(oi_t - 24 * HOUR)
    funding = float(sorted(fund, key=lambda x: x["fundingTime"])[-1]["fundingRate"])
    lows = [float(k[3]) for k in kl[-24:]]
    s = {
        "hour": t, "price": float(kl[-1][4]), "dvol": dvol,
        "dvol_chg24": None if dvol_24 is None else dvol - dvol_24,
        "oi_usd": oi_now, "oi_chg24": None if not oi_24 else oi_now / oi_24 - 1,
        "funding_8h": funding, "at_24h_low": bool(lows) and lows[-1] <= min(lows),
    }
    s["fear"] = s["dvol_chg24"] is not None and s["dvol_chg24"] >= FEAR_DVOL_PTS
    s["flush"] = s["oi_chg24"] is not None and s["oi_chg24"] <= FLUSH_OI_24H
    s["shorts"] = funding <= SHORTS_FUNDING
    s["pair"] = s["fear"] and s["flush"]
    s["all3"] = s["pair"] and s["shorts"]
    return s


def htf_from_daily(days):
    """days: closed UTC daily candles, oldest first, each {t, c, qv, tb, oi} (tb = taker buy quote volume,
    oi = USD open interest at the day's close or None). Needs at least 231 days."""
    if len(days) < 231:
        return None
    C = [d["c"] for d in days]
    i = len(C) - 1
    ma = lambda n, j: sum(C[j - n + 1:j + 1]) / n
    ma200, ma200_30, ma50 = ma(200, i), ma(200, i - 30), ma(50, i)
    # 14-day RSI on log returns with exponential weights alpha 1/14 (pandas ewm, adjust=True, as backtested)
    a, up, dn = 1 / 14, 0.0, 0.0
    for k in range(1, i + 1):
        r = math.log(C[k] / C[k - 1])
        up, dn = up * (1 - a) + max(r, 0), dn * (1 - a) + max(-r, 0)
    rsi = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
    tb, qv = sum(d["tb"] for d in days[-7:]), sum(d["qv"] for d in days[-7:])
    taker = (2 * tb / qv - 1) * 100 if qv else None
    back = next((d for d in days if d["t"] == days[i]["t"] - 30 * DAY), None)
    oi30 = days[i]["oi"] / back["oi"] - 1 if days[i].get("oi") and back and back.get("oi") else None
    cond = {
        "trend": (ma200 > ma200_30, f"200-day average rising ({(ma200 / ma200_30 - 1) * 100:+.1f}% vs 30d ago)"),
        "ma50": (C[i] / ma50 - 1 <= HTF_MA50, f"price vs 50-day average {(C[i] / ma50 - 1) * 100:+.1f}% (need <= -8%)"),
        "rsi": (rsi <= HTF_RSI, f"14-day RSI {rsi:.0f} (need <= 38)"),
        "taker": (taker is not None and taker < 0, "7-day taker flow " + ("n/a" if taker is None else f"{taker:+.2f}%") + " (need < 0)"),
        "oi": (oi30 is not None and oi30 <= HTF_OI_30D, "OI 30d " + ("n/a" if oi30 is None else f"{oi30 * 100:+.1f}%") + " (need <= -8%)"),
    }
    met = sum(ok for ok, _ in cond.values())
    return {"day": days[i]["t"], "close": C[i], "met": met, "on": met == 5, "cond": cond}


def htf(now_ms=None):
    """Higher timeframe bottom on the last closed daily candle."""
    now = now_ms or int(time.time() * 1000)
    kl = _closed(_get(FAPI + "/fapi/v1/klines", symbol="BTCUSDT", interval="1d", limit=300), 6, now)
    days = [{"t": int(k[0]), "c": float(k[4]), "qv": float(k[7]), "tb": float(k[10]), "oi": None} for k in kl]
    # Binance keeps 30 days of OI history; a 1d row stamped T is the snapshot at T, i.e. the close of the day before
    oi = _get(FAPI + "/futures/data/openInterestHist", symbol="BTCUSDT", period="1d", limit=31)
    snap = {int(x["timestamp"]) - DAY: float(x["sumOpenInterestValue"]) for x in oi}
    for d in days:
        d["oi"] = snap.get(d["t"])
    return htf_from_daily(days)


def _recent(sent, kind, now_s):
    return any(k.startswith(f"confluence-{kind}-") and now_s - t < COOLDOWN_S[kind] for k, t in sent.items())


def detect_alerts(sent, now_ms=None):
    """[(key, message)] for setups that are on now and were not alerted within their cooldown.
    sent: bot.sent_alerts (key -> unix time sent). Keep those keys for at least 30 days so the HTF cooldown holds."""
    now_s = (now_ms or int(time.time() * 1000)) / 1000
    out = []
    s = short_term(now_ms)
    kind = "all3" if s["all3"] else "pair" if s["pair"] else None
    # an all-three alert is still news after a fear + flush alert, but not the other way round
    if kind and not _recent(sent, kind, now_s) and not (kind == "pair" and _recent(sent, "all3", now_s)):
        head = ("ALL THREE: fear + leverage flush + crowded shorts. Strongest short-term long setup (1-3 days)."
                if kind == "all3" else "FEAR + FLUSH: options panic and leverage washing out. Short-term long setup (1-3 days).")
        out.append((f"confluence-{kind}-{s['hour']}",
                    f"{head}\nBTC {s['price']:,.0f}{' (at a 24h low)' if s['at_24h_low'] else ''}\n"
                    f"DVOL {s['dvol']:.1f} ({s['dvol_chg24']:+.1f} pts in 24h)\n"
                    f"OI {s['oi_chg24'] * 100:+.1f}% in 24h\n"
                    f"Funding {s['funding_8h'] * 3 * 365 * 100:+.1f}% a year{' (shorts paying)' if s['shorts'] else ''}"))
    h = htf(now_ms)
    if h and h["on"] and not _recent(sent, "htf", now_s):
        day = time.strftime("%Y-%m-%d", time.gmtime(h["day"] / 1000))
        out.append((f"confluence-htf-{h['day']}",
                    f"HIGHER TIMEFRAME BOTTOM: all five conditions met on the {day} daily close. Multi-week swing long setup.\n"
                    f"BTC {h['close']:,.0f}\n" + "\n".join(f"✓ {txt}" for _, txt in h["cond"].values())))
    return out


def status():
    """Plain-text reading of all three, for a /confluence command."""
    s, h = short_term(), htf()
    on = lambda b: "ON" if b else "off"
    num = lambda x, fmt: "n/a" if x is None else format(x, fmt)
    lines = [f"BTC {s['price']:,.0f}",
             f"Fear (DVOL {num(s['dvol_chg24'], '+.1f')} pts/24h): {on(s['fear'])}",
             f"Flush (OI {num(s['oi_chg24'] and s['oi_chg24'] * 100, '+.1f')}%/24h): {on(s['flush'])}",
             f"Crowded shorts (funding {s['funding_8h'] * 3 * 365 * 100:+.1f}% a year): {on(s['shorts'])}",
             f"Fear + flush: {on(s['pair'])}   All three: {on(s['all3'])}"]
    if h:
        lines.append(f"HTF bottom: {'SIGNAL' if h['on'] else str(h['met']) + ' of 5'}")
        lines += [f"  {'✓' if ok else '✗'} {txt}" for ok, txt in h["cond"].values()]
    else:
        lines.append("HTF bottom: not enough daily history")
    return "\n".join(lines)
