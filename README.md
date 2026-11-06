# Reid: BTC trading agent (Coinalyze)

1. Bot token from @BotFather; your numeric ID from @userinfobot.
2. `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
3. `cp .env.example .env` and fill in all keys (Anthropic, Telegram, Coinalyze).
4. `python check.py` - confirm the data looks sane BEFORE running the bot. The SUMMARY lines at the end
   show which sources worked; any section showing "unavailable" says which call failed and why.
5. `python bot.py` (use tmux/systemd on a VPS so it stays up). Restart it after any change to `.env`.

Telegram: `/report` for the BTC report, `/reset`, or just talk to it. BTC liquidation-spike alerts check every
10 min; each spike is alerted once, even across restarts. Daily report at DAILY_REPORT_UTC.

Confluence alerts (`confluence.py`, from the BTC Flow Desk research) check every 15 min, with no API key:
- **Fear + flush:** DVOL up 3+ pts and Binance OI down 3%+ in 24h. A 1-3 day long setup; re-alerted at most once a day.
- **All three:** fear + flush with Binance funding at or below zero (crowded shorts).
- **HTF bottom:** on the daily close, all five of: 200-day average rising, price 8%+ below the 50-day average,
  RSI(14) <= 38, 7 days of net taker selling, and OI down 8%+ over 30 days. A multi-week swing long; re-alerted at most every 30 days.

`/confluence` shows the current reading of all of them.

Data: Coinalyze (price, perp and spot CVD, open interest, funding, liquidations, daily trend) plus the public
Binance and Coinbase order books. Binance blocks US servers, so host the bot in the UK/EU.
Coinalyze allows 40 calls/min (the client retries on 429). Every answer is logged to ideas.jsonl with the price.
