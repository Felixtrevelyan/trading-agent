import asyncio
import json
import re
import os
import anthropic

client = anthropic.Anthropic()
MAIN_MODEL = os.getenv("MAIN_MODEL", "claude-sonnet-5-5")
SUB_MODEL = os.getenv("SUB_MODEL", "claude-haiku-4-5-20251001")

RULES = ("Use ONLY the numbers in the data provided. Never invent or recall market figures. "
         "If a data section says 'unavailable', say so rather than guessing. "
         "Funding values are already in percent per funding interval (0.01 means 0.01%, a normal level).")

ORDERFLOW_PROMPT = f"""You are an orderflow analyst for a discretionary BTC trader. Read
the data as a story of who is aggressive and who is passive:
- Structure: the 72h range, where price sits in it, how many times the edges have been touched, sweeps and
  failed breakouts. Repeated touches of an edge weaken it; a sweep that closes back inside often reverts.
- Aggressive flow: taker CVD on spot vs perps (1h/4h/24h, aggregated across exchanges), plus hourly perp CVD. Spot
  selling into perp buying (or the reverse) is the key tell: perps chasing while spot sells usually fails.
- Passive liquidity: bid vs ask depth near price on the spot books (Binance, Coinbase), and any walls
  (null means no level stands out, so do not invent one). Note which side is heavier and whether aggressive flow is eating into a wall.
- Positioning: open interest change with price (new positions vs squeeze/closing), funding, liquidations.
Give one directional call with the most likely next move (a price from the data), and what would
prove it wrong. {RULES}"""

STYLE_EXAMPLES = """Example 1: BTC is ranging and is trading towards range high for the 3rd time. Short squeeze takes out
the range high into a stacked passive spot book skewed towards the ask side and aggressive perps start buying
while aggressive spot starts selling. More likely than not this will revert.

Example 2: BTC is pulling back into an area where the passive spot book is skewed towards the buy side. However
aggressive perps continue to long the grind down and aggressive spot continues to sell, unwinding that
positioning into new lows. Good chance this passive wall gets eaten through unless this behaviour stabilises."""

MAIN_PROMPT = f"""You are Reid, the lead trading assistant for a discretionary BTC trader. You get notes from an
orderflow analyst (reports only) plus the raw BTC data. Check any notes against the
raw data and give ONE decisive read. Never mention the analysts, never show disagreements or
alternative readings, and do not hedge with "could be either".

Write like an experienced orderflow trader describing the tape, in the style of these examples (style only,
their facts are not current):
{STYLE_EXAMPLES}

That means: describe the structure (range, edges, sweeps), who is aggressive (spot vs perps), where passive
liquidity sits and which side it is skewed to, what positioning is doing (open interest, funding,
liquidations), then the conclusion and how likely it is, as short bullet points starting with •. Only claim what the data shows: the flow data is
5-minute and hourly, so never mention 1-minute behaviour; if spot or order book data is unavailable, say the
read is based on perps only. Do not use chart patterns (order blocks, fair value gaps), long/short ratios,
sentiment indexes or a key-levels list.

Formatting for a phone: use **double asterisks** to bold the important bits only (headers, bias words,
prices and targets, the key conclusion). No other markdown (no #, _, backticks or tables) and no emojis beyond
the ones in the layout. Every number must come from the data, with prices written with thousands separators.
Not financial advice; the trader decides. {RULES}"""


def _ask(model, system, content, max_tokens=2500):
    r = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                               messages=[{"role": "user", "content": content}])
    # newer models return thinking blocks before the text, so only collect text blocks
    text = "".join(b.text for b in r.content if b.type == "text")
    if not text:
        raise RuntimeError(f"{model} returned no text (stop_reason={r.stop_reason})")
    return text


# which data blocks a chat question needs, by keyword (free and instant; no model call)
PART_WORDS = {
    "flow_5m_spot_vs_perps": ["cvd", "flow", "spot", "perp", "aggress", "taker", "delta", "buying", "selling",
                              "buyers", "sellers", "dump", "pump", "hold", "break", "support", "resistance"],
    "order_book": ["book", "wall", "walls", "bid", "bids", "ask", "asks", "liquidity", "passive", "absorb", "spoof",
                   "depth", "hold", "break", "support", "resistance"],
    "open_interest": ["oi", "open interest", "positioning", "leverage", "squeeze", "new longs", "new shorts"],
    "funding": ["funding", "crowded", "leverage", "squeeze", "overheated"],
    "liquidations": ["liq", "liqs", "liquidat", "squeeze", "flush", "cascade", "wick", "stop hunt", "stops"],
    "daily_trend": ["daily", "1d", "weekly", "week", "month", "ema", "sma", "moving average", "ma", "trend",
                    "long term", "cycle", "ath", "high", "highs", "low", "lows", "target"],
}
# a trade question without specifics ("should I long here?") gets the short-term picture
DEFAULT_PARTS = {"flow_5m_spot_vs_perps", "order_book", "open_interest", "funding", "liquidations"}


def _has(q, w):
    # short words must match whole ("oi" is not in "going"); longer ones may be a word start ("liquidat...")
    return re.search(r"\b" + re.escape(w) + (r"\b" if len(w) <= 4 else ""), q) is not None


def parts_for(question):
    q = question.lower()
    picked = {p for p, words in PART_WORDS.items() if any(_has(q, w) for w in words)}
    if re.search(r"\b\d{2,3}(,\d{3}|k\b|\d{3}\b)", q):  # a price level like 75k / 75,000 -> longer-term context
        picked.add("daily_trend")
    return frozenset(picked or DEFAULT_PARTS)


async def analyse(question, of_data, history=()):
    of_json = json.dumps(of_data, indent=1)
    convo = "\n".join(f"Trader: {q}\nYou: {a}" for q, a in history[-4:])
    if question != DAILY_QUESTION:  # chat: one direct call on just the data the question needs
        final = (f"Recent conversation:\n{convo or '(none)'}\n\nTrader question: {question}\n\n{CHAT_STYLE}\n\n"
                 f"=== RAW DATA ===\n{of_json}")
        return await asyncio.to_thread(_ask, MAIN_MODEL, MAIN_PROMPT, final, 6000)
    notes = await asyncio.to_thread(_ask, SUB_MODEL, ORDERFLOW_PROMPT, f"Data:\n{of_json}\n\nTrader question: {question}")
    final = (f"Recent conversation:\n{convo or '(none)'}\n\nTrader request: {question}\n\n"
             f"=== ORDERFLOW NOTES ===\n{notes}\n\n=== RAW DATA ===\n{of_json}")
    return await asyncio.to_thread(_ask, MAIN_MODEL, MAIN_PROMPT, final, 8000)  # headroom: thinking counts toward max_tokens


OUTLOOK_FORMAT = """🔭 **Outlook**
[dot] **Short term** 15m–1d: **Bullish/Bearish/Neutral**. Likely move to **$price**. Wrong if condition.
[dot] **Mid term** 1d–1w: **Bullish/Bearish/Neutral**. Likely move to **$price**. Wrong if condition.
[dot] **Long term** 1w+: **Bullish/Bearish/Neutral**. Likely move to **$price**. Wrong if condition.
where [dot] is 🟢 for Bullish, 🔴 for Bearish, ⚪ for Neutral."""

HORIZONS = """Base each horizon on the data that fits it: short term (15 minutes to 1 day) on the 5-minute spot vs
perp flow, the order book, the range edge and the last hours of CVD and liquidations; mid term (1 day to 1 week)
on hourly CVD, open interest, funding, the 72h range and the 7-day highs/lows; long term (1 week+) on the
daily_trend data (30/90-day change, 90-day range, 20/50/100-day averages, 30-day open interest). If daily_trend
is unavailable, say Neutral for long term. Say Neutral for any horizon with no edge. The horizons can disagree
(e.g. a short-term bounce inside a longer downtrend); that is fine."""

DAILY_QUESTION = f"""Write the BTC market report in exactly this layout:

📊 **MARKET REPORT**

**BTC $price**  📈 or 📉 24h change%
• **Price:** $price, at the **range highs**, **range lows** or **mid-range** (say which, nothing else).
• **Flow:** who is aggressive, spot vs perps.
• **Book:** which side the passive book is heavier on, and any big wall.
• **Positioning:** open interest, funding, liquidations.
• **Read:** what it adds up to and how likely, in the style of the examples.

{OUTLOOK_FORMAT}

━━━━━━━━━━━━

🎯 **BOTTOM LINE:** one or two sentences, including trade or no trade.

Each bullet is one short sentence. {HORIZONS} Keep the whole report under 230 words."""

CHAT_STYLE = """This is a chat question, not a report. Answer it directly in 3-5 short bullet points (•), under 120
words, in the style of the examples. The first bullet answers the question (yes / no / how likely). Use only the
data that matters for the question. Only the data relevant to the question was fetched, so do not mention or
apologise for data that is not there (no "spot/book data unavailable" notes). No report headers, no outlook
section, no bottom line."""
