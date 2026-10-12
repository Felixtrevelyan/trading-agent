# Reid: BTC trading agent (Coinalyze)

1. Bot token from @BotFather; your numeric ID from @userinfobot.
2. `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
3. `cp .env.example .env` and fill in all keys (Anthropic, Telegram, Coinalyze).
4. `python check.py` - confirm the data looks sane BEFORE running the bot. The SUMMARY lines at the end
   show which sources worked; any section showing "unavailable" says which call failed and why.
5. `python bot.py` (use tmux/systemd on a VPS so it stays up). Restart it after any change to `.env`.

Telegram: `/report` for the BTC report, `/reset`, or just talk to it. Alerts (liquidation spikes, extreme
funding, OI jumps, fast moves) check every 10 min with a 2h cooldown per type. Daily report at DAILY_REPORT_UTC.

Data: Coinalyze (price, perp and spot CVD, open interest, funding, liquidations, daily trend) plus the public
Binance and Coinbase order books. Binance blocks US servers, so host the bot in the UK/EU.
Coinalyze allows 40 calls/min (the client retries on 429). Every answer is logged to ideas.jsonl with the price.
