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

ORDERFLOW_PROMPT = f"""You are an orderflow analyst for a discretionary crypto trader. For each asset, read
the data as a story of who is aggressive and who is passive:
- Structure: the 72h range, where price sits in it, how many times the edges have been touched, sweeps and
  failed breakouts. Repeated touches of an edge weaken it; a sweep that closes back inside often reverts.
- Aggressive flow: taker CVD on spot vs perps (1h/4h/24h, aggregated across exchanges), plus hourly perp CVD. Spot
  selling into perp buying (or the reverse) is the key tell: perps chasing while spot sells usually fails.
- Passive liquidity: bid vs ask depth near price on the spot books (Binance, Coinbase) and perps, and the
  largest walls. Note which side is heavier and whether aggressive flow is eating into a wall.
- Positioning: open interest change with price (new positions vs squeeze/closing), funding, liquidations.
Give one directional call per asset with the most likely next move (a price from the data), and what would
prove it wrong. {RULES}"""

MACRO_PROMPT = f"""You are a macro analyst for a crypto trader. Only the moves flagged as notable matter.
In 1-2 sentences say what happened and whether it helps or hurts crypto today. {RULES}"""

STYLE_EXAMPLES = """Example 1: BTC is ranging and is trading towards range high for the 3rd time. Short squeeze takes out
the range high into a stacked passive spot book skewed towards the ask side and aggressive perps start buying
while aggressive spot starts selling. More likely than not this will revert.

Example 2: BTC is pulling back into an area where the passive spot book is skewed towards the buy side. However
aggressive perps continue to long the grind down and aggressive spot continues to sell, unwinding that
positioning into new lows. Good chance this passive wall gets eaten through unless this behaviour stabilises."""

MAIN_PROMPT = f"""You are the lead trading assistant for a discretionary crypto trader. You get notes from an
orderflow analyst (and a macro analyst only when macro matters), plus the raw data. Check the notes against the
raw data and give ONE decisive read per asset. Never mention the analysts, never show disagreements or
alternative readings, and do not hedge with "could be either".

Write like an experienced orderflow trader describing the tape, in the style of these examples (style only,
their facts are not current):
{STYLE_EXAMPLES}

That means: describe the structure (range, edges, sweeps), who is aggressive (spot vs perps), where passive
liquidity sits and which side it is skewed to, what positioning is doing (open interest, funding,
liquidations), then the conclusion and how likely it is. Only claim what the data shows: the flow data is
5-minute and hourly, so never mention 1-minute behaviour; if spot or order book data is unavailable, say the
read is based on perps only. Do not use chart patterns (order blocks, fair value gaps), long/short ratios,
sentiment indexes or a key-levels list.

Plain text for a phone: no markdown symbols like # or **. Every number must come from the data, with prices
written with thousands separators. Not financial advice; the trader decides. {RULES}"""


def _ask(model, system, content, max_tokens=2500):
    r = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                               messages=[{"role": "user", "content": content}])
    # newer models return thinking blocks before the text, so only collect text blocks
    text = "".join(b.text for b in r.content if b.type == "text")
    if not text:
        raise RuntimeError(f"{model} returned no text (stop_reason={r.stop_reason})")
    return text


NAMES = {"BITCOIN": "BTC", "ETHEREUM": "ETH", "ETHER": "ETH", "SOLANA": "SOL", "RIPPLE": "XRP",
         "DOGECOIN": "DOGE", "CARDANO": "ADA", "AVALANCHE": "AVAX"}
# tickers that are never ordinary words, so they match in any case ("btc", "Eth")
ANY_CASE = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "BNB", "SUI", "TRX", "LTC", "PEPE"}


async def route(question, history, known):
    """Which coin is the user asking about? Only ever returns a coin in `known`; defaults to BTC."""
    for w in re.findall(r"[A-Za-z0-9]+", question):
        up = w.upper()
        t = NAMES.get(up) or (up if up in ANY_CASE or w.isupper() else None)  # other tickers only in CAPS
        if t in known and len(t) >= 2:
            return t
    ctx = "\n".join(f"User: {q}" for q, _ in history[-2:])
    out = await asyncio.to_thread(
        _ask, SUB_MODEL, "Reply with ONLY the uppercase ticker (e.g. BTC, ETH, SOL) of the crypto asset the "
        "latest user message is about, using prior messages for context. If none is specified reply BTC.",
        f"{ctx}\nUser: {question}", 20)
    out = out.strip().upper()
    return out if out in known else "BTC"  # anything but a bare known ticker (e.g. "I DON'T...") -> BTC


async def analyse(question, of_data, macro_data, history=()):
    of_json = json.dumps(of_data, indent=1)
    jobs = [asyncio.to_thread(_ask, SUB_MODEL, ORDERFLOW_PROMPT, f"Data:\n{of_json}\n\nTrader question: {question}")]
    notable = macro_data.get("notable")
    if notable:  # macro rarely matters; only bring it in on a notable move
        macro_json = json.dumps(macro_data, indent=1)
        jobs.append(asyncio.to_thread(_ask, SUB_MODEL, MACRO_PROMPT, f"Data:\n{macro_json}"))
    reps = await asyncio.gather(*jobs)
    macro_note = reps[1] if notable else "Nothing notable in macro today. Do not mention macro."
    convo = "\n".join(f"Trader: {q}\nYou: {a}" for q, a in history[-4:])
    request = question if question == DAILY_QUESTION else f"{question}\n\n{CHAT_STYLE}"
    final = (f"Recent conversation:\n{convo or '(none)'}\n\nTrader request: {request}\n\n"
             f"=== ORDERFLOW NOTES ===\n{reps[0]}\n\n=== MACRO NOTES ===\n{macro_note}\n\n"
             f"=== RAW DATA ===\n{of_json}" + (f"\n{macro_json}" if notable else ""))
    return await asyncio.to_thread(_ask, MAIN_MODEL, MAIN_PROMPT, final, 8000)  # headroom: thinking counts toward max_tokens


DAILY_QUESTION = """Write the market report for every asset in the data, BTC first, in exactly this layout:

MARKET REPORT

[ASSET] $[price] ([24h change]% 24h)
[One paragraph of 3-5 sentences reading the tape, in the style of the examples.]
Outlook: Bullish, Bearish or Neutral. Likely move to $[price from the data]. Wrong if [condition].

(repeat for each asset)

MACRO: [one or two sentences. Include this line ONLY if the macro notes say something notable happened.]

BOTTOM LINE: [one or two sentences, including trade or no trade.]
Keep the whole report under 250 words."""

CHAT_STYLE = ("Answer in under 150 words as one short paragraph reading the tape in the style of the examples, "
              "then a final line: Outlook: [bias]. Likely move to $[price]. Wrong if [condition].")
