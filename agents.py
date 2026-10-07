import asyncio
import json
import os
import re
import anthropic

client = anthropic.Anthropic()
MAIN_MODEL = os.getenv("MAIN_MODEL", "claude-sonnet-5-5")
SUB_MODEL = os.getenv("SUB_MODEL", "claude-haiku-4-5-20251001")

RULES = ("Use ONLY the numbers in the data provided. Never invent or recall market figures. "
         "If a data section says 'unavailable', say so rather than guessing. "
         "Funding values are already in percent per funding interval (0.01 means 0.01%, a normal level).")

ORDERFLOW_PROMPT = f"""You are an orderflow/derivatives analyst for a discretionary crypto trader.
Data is aggregated across the largest perp exchanges, possibly for several assets. For each asset read
price action, CVD (net taker buying minus selling), open interest and funding together, plus liquidations.
Key combinations: price and OI rising = new longs driving it; price falling with OI rising = new shorts;
OI falling = positions closing (squeeze or capitulation); CVD diverging from price = the move lacks real buyers
or sellers; extreme funding = one side crowded. For each asset give one directional bias, the price it points
to next (use the provided 24h/7d highs and lows) and what would prove it wrong. {RULES}"""

MACRO_PROMPT = f"""You are a macro analyst for a crypto trader. From the cross-asset data (dollar, yields,
equities, volatility, gold, oil) say in 2-3 sentences whether the backdrop is risk-on, risk-off or mixed and
whether it helps or hurts crypto right now. {RULES}"""

MAIN_PROMPT = f"""You are the lead trading assistant for a discretionary crypto trader. You get notes from an
orderflow analyst and a macro analyst plus the raw data. Weigh them, check them against the raw data, and give
ONE clear, decisive view. Never mention the analysts, never show disagreements or alternative readings, and do
not hedge with "could be either". Do not mention long/short ratios, sentiment indexes or a key-levels list.

Style: plain text for a phone (no markdown symbols like # or **), short lines, simple words, no jargon without
meaning. Every number must come from the data. Not financial advice; the trader decides. {RULES}"""


def _ask(model, system, content, max_tokens=2500):
    r = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                               messages=[{"role": "user", "content": content}])
    # newer models return thinking blocks before the text, so only collect text blocks
    text = "".join(b.text for b in r.content if b.type == "text")
    if not text:
        raise RuntimeError(f"{model} returned no text (stop_reason={r.stop_reason})")
    return text


async def route(question, history):
    """Which asset is the user asking about? Defaults to BTC."""
    ctx = "\n".join(f"User: {q}" for q, _ in history[-2:])
    out = await asyncio.to_thread(
        _ask, SUB_MODEL, "Reply with ONLY the uppercase ticker (e.g. BTC, ETH, SOL) of the crypto asset the "
        "latest user message is about, using prior messages for context. If none is specified reply BTC.",
        f"{ctx}\nUser: {question}", 10)
    m = re.search(r"[A-Z0-9]{2,10}", out.upper())
    return m.group(0) if m else "BTC"


async def analyse(question, of_data, macro_data, history=()):
    of_json, macro_json = json.dumps(of_data, indent=1), json.dumps(macro_data, indent=1)
    of_rep, macro_rep = await asyncio.gather(
        asyncio.to_thread(_ask, SUB_MODEL, ORDERFLOW_PROMPT, f"Data:\n{of_json}\n\nTrader question: {question}"),
        asyncio.to_thread(_ask, SUB_MODEL, MACRO_PROMPT, f"Data:\n{macro_json}\n\nTrader question: {question}"),
    )
    convo = "\n".join(f"Trader: {q}\nYou: {a}" for q, a in history[-4:])
    request = question if question == DAILY_QUESTION else f"{question}\n\n{CHAT_STYLE}"
    final = (f"Recent conversation:\n{convo or '(none)'}\n\nTrader request: {request}\n\n"
             f"=== ORDERFLOW ANALYST ===\n{of_rep}\n\n=== MACRO ANALYST ===\n{macro_rep}\n\n"
             f"=== RAW DATA ===\n{of_json}\n{macro_json}")
    return await asyncio.to_thread(_ask, MAIN_MODEL, MAIN_PROMPT, final, 8000)  # headroom: thinking counts toward max_tokens


DAILY_QUESTION = """Write the market report for every asset in the data, BTC first. Use exactly this layout
and keep each line to one sentence. Write prices with thousands separators (e.g. $83,361):

MARKET REPORT

[ASSET] $[price] ([24h change]% 24h)
Price: what price has been doing.
CVD: what net buying/selling shows and what it means here.
Open interest: rising or falling, and what that says about who is driving the move.
Funding: what it shows about positioning.
Outlook: Bullish, Bearish or Neutral. Likely move to $[target from the data]. Wrong if [condition].

(repeat the block for each asset)

MACRO: one or two sentences on whether the backdrop helps or hurts crypto.

BOTTOM LINE: one or two sentences on the overall call, including trade or no trade.
Keep the whole report under 300 words."""

CHAT_STYLE = ("Answer in under 120 words: one clear view, which of price/CVD/open interest/funding drive it, "
              "where price is likely to move next and what would make you wrong.")
