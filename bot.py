import asyncio
import datetime as dt
import json
import logging
import os
import time
from dotenv import load_dotenv

load_dotenv(override=True)  # .env wins over stale shell variables
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import agents
import data

logging.basicConfig(level=logging.INFO)
# httpx logs every Telegram poll, and the URL contains the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
OWNER = int(os.environ["OWNER_ID"])
ALERT_ASSETS = [a.strip().upper() for a in os.getenv("ALERT_ASSETS", "BTC,ETH").split(",") if a.strip()]
history = []          # [(question, answer)], single-user so one shared list
_cache = {}           # short TTL cache so rapid follow-ups don't burn API calls
last_alert = {}


async def cached(key, fn, ttl, *args):
    c = _cache.get(key)
    if c and time.time() - c[0] < ttl:
        return c[1]
    v = await asyncio.to_thread(fn, *args)
    _cache[key] = (time.time(), v)
    return v


async def run(question, asset=None):
    asset = asset or await agents.route(question, history)
    try:
        of = await cached(f"of-{asset}", data.orderflow, 120, asset)
    except ValueError:
        return f"I couldn't find perp markets for {asset}."
    mc = await cached("macro", data.macro, 600)
    answer = await agents.analyse(question, of, mc, history)
    history.append((question, answer[:1500]))
    del history[:-8]
    with open("ideas.jsonl", "a") as f:  # log so outcomes can be reviewed later
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "asset": asset, "q": question,
                            "price": of["price"], "answer": answer}) + "\n")
    return answer


async def send_long(send, text):
    for i in range(0, len(text), 4000):
        await send(text[i:i + 4000])


async def start(update: Update, ctx):
    if update.effective_user.id == OWNER:
        await update.message.reply_text(
            "Ready. Ask me anything (e.g. 'what do you think of ETH here?') or use /report [asset]. "
            "/reset clears conversation memory. Alerts run automatically.")


async def reset(update: Update, ctx):
    if update.effective_user.id == OWNER:
        history.clear()
        await update.message.reply_text("Memory cleared.")


async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    asset = ctx.args[0].upper() if ctx.args else "BTC"
    await update.message.reply_text(f"Gathering {asset} data...")
    try:
        await send_long(update.message.reply_text, await run(agents.DAILY_QUESTION, asset))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    await update.message.chat.send_action("typing")
    try:
        await send_long(update.message.reply_text, await run(update.message.text))
    except Exception as e:
        logging.exception("chat failed")
        await update.message.reply_text(f"Error: {e}")


async def daily(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        text = await run(agents.DAILY_QUESTION, "BTC")
        await send_long(lambda t: ctx.bot.send_message(OWNER, t), text)
    except Exception as e:
        await ctx.bot.send_message(OWNER, f"Daily report failed: {e}")


async def alerts(ctx: ContextTypes.DEFAULT_TYPE):
    for asset in ALERT_ASSETS:
        try:
            d = await cached(f"of-{asset}", data.orderflow, 120, asset)
        except Exception as e:
            logging.warning("alert fetch failed for %s: %s", asset, e)
            continue
        for key, msg in data.detect_alerts(d):
            if time.time() - last_alert.get(key, 0) > 2 * 3600:  # 2h cooldown per alert type
                last_alert[key] = time.time()
                history.append((f"[alert sent to trader] {msg}", ""))  # so a reply like "what do you make of it?" has context
                del history[:-8]
                await ctx.bot.send_message(OWNER, f"ALERT: {msg}\nReply to ask what I make of it.")


def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    h, m = os.getenv("DAILY_REPORT_UTC", "07:00").split(":")
    app.job_queue.run_daily(daily, time=dt.time(int(h), int(m), tzinfo=dt.timezone.utc))
    app.job_queue.run_repeating(alerts, interval=600, first=30)
    app.run_polling()


if __name__ == "__main__":
    main()
