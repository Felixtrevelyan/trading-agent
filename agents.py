import asyncio
import json
import os
import re
import anthropic

client = anthropic.Anthropic()
MAIN_MODEL = os.getenv("MAIN_MODEL", "claude-sonnet-5-5")
SUB_MODEL = os.getenv("SUB_MODEL", "claude-haiku-4-5-20251001")

RULES = ("Use ONLY the numbers in the data provided. Never invent or recall market figures. "
         "If a data section says 'unavailable', say so rather than guessing. Be concise and specific.")

ORDERFLOW_PROMPT = f"""You are an orderflow/derivatives analyst for a discretionary crypto trader.
Data is aggregated across the largest perp exchanges. Interpret CVD (taker delta), price-vs-CVD divergence,
open interest changes, funding (and predicted funding), long/short ratio and liquidations.
Look for: OI rising/falling with price (new positions vs squeeze), crowded positioning, funding extremes,
liquidation flushes that may mark exhaustion. Output: (1) read of current flow, (2) 1-2 candidate setups with
entry zone, invalidation and target using the provided highs/lows, (3) what would change your view. {RULES}"""

MACRO_PROMPT = f"""You are a macro analyst for a crypto trader. From the cross-asset data assess the risk
environment: dollar, yields, equities, volatility, gold, sentiment. Output: (1) risk-on / risk-off / mixed with
reasons, (2) what is supportive or hostile for crypto right now, (3) what to watch next. {RULES}"""

MAIN_PROMPT = f"""You are the lead trading assistant for a discretionary crypto trader. You receive reports from
an orderflow analyst and a macro analyst, plus raw data and recent conversation. Synthesize a clear view.
Be skeptical, not agreeable. For any trade idea give direction, entry zone, invalidation, target and honest
confidence. Always state the strongest argument AGAINST the idea. Flag when a setup conflicts with the macro
regime. If there is no good trade say 'no trade' and why. Format for a phone screen: short paragraphs, no
tables. Not financial advice; the trader decides. {RULES}"""


def _ask(model, system, content, max_tokens=900):
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
    final = (f"Recent conversation:\n{convo or '(none)'}\n\nTrader request: {question}\n\n"
             f"=== ORDERFLOW ANALYST ===\n{of_rep}\n\n=== MACRO ANALYST ===\n{macro_rep}\n\n"
             f"=== RAW DATA ===\n{of_json}\n{macro_json}")
    return await asyncio.to_thread(_ask, MAIN_MODEL, MAIN_PROMPT, final, 8000)  # headroom: thinking counts toward max_tokens


DAILY_QUESTION = ("Write the daily market report: 1) headline summary, 2) orderflow/positioning, "
                  "3) macro backdrop, 4) key levels, 5) trade ideas (or no trade) with invalidation.")
