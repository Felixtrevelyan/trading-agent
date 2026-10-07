# Trading agents v2 (Coinalyze)

1. Bot token from @BotFather; your numeric ID from @userinfobot.
2. `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
3. `cp .env.example .env` and fill in all keys (Anthropic, Telegram, Coinalyze).
4. `python check.py` - confirm the data looks sane BEFORE running the bot. Any section showing
   "unavailable" tells you which Coinalyze call failed and why.
5. `python bot.py` (use tmux/systemd on a VPS so it stays up).

Telegram: `/report` (BTC, ETH, SOL by default; set REPORT_ASSETS) or `/report SOL`, `/reset`, or just talk to it. Alerts (liquidation spikes, extreme funding,
OI jumps, fast moves) check every 10 min with a 2h cooldown per type.

Notes: Coinalyze limit is 40 calls/min (client retries on 429). Funding intervals differ by exchange
(e.g. Hyperliquid hourly vs 8h elsewhere), so treat the OI-weighted figure as approximate.
Every answer is logged to ideas.jsonl with price at the time.
