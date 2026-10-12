import asyncio
import html
import re
import datetime as dt
import json
import logging
import os
import time
from dotenv import load_dotenv

load_dotenv(override=True)  # .env wins over stale shell variables
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import agents
import data

logging.basicConfig(level=logging.INFO)
# httpx logs every Telegram poll, and the URL contains the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
OWNER = int(os.environ["OWNER_ID"])
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


async def get_data(parts):
    full = _cache.get("of-all")
    if full and time.time() - full[0] < 120:  # a fresh /report already has everything
        return full[1]
    key = "of-all" if parts == data.ALL_PARTS else "of-" + ",".join(sorted(parts))
    return await cached(key, data.orderflow, 120, "BTC", parts)


async def run(question):
    parts = data.ALL_PARTS if question == agents.DAILY_QUESTION else agents.parts_for(question)
    of = await get_data(parts)
    answer = await agents.analyse(question, of, history)
    history.append((question, answer[:1500]))
    del history[:-8]
    with open("ideas.jsonl", "a") as f:  # log so outcomes can be reviewed later
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "q": question,
                            "price": of["price"], "answer": answer}) + "\n")
    return answer


def to_html(text):
    """Reid marks bold as **text**; Telegram wants HTML."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text, quote=False))


async def send_long(send, text):
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):  # split on lines so bold markers are never cut in half
        if cur and len(to_html(cur + line)) > 3800:  # Telegram's limit is 4096 after formatting
            chunks.append(cur)
            cur = ""
        cur += line
    chunks.append(cur)
    for c in filter(str.strip, chunks):
        try:
            await send(to_html(c), parse_mode=ParseMode.HTML)
        except BadRequest:  # formatting rejected: send it plain rather than lose it
            await send(c.replace("**", ""))


async def start(update: Update, ctx):
    if update.effective_user.id == OWNER:
        await update.message.reply_text(
            "Ready. Ask me anything about BTC (e.g. 'what do you think about longing here?') or use /report. "
            "/reset clears conversation memory. Alerts run automatically.")


async def reset(update: Update, ctx):
    if update.effective_user.id == OWNER:
        history.clear()
        await update.message.reply_text("Memory cleared.")


async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER:
        return
    await update.message.reply_text("Gathering BTC data...")
    try:
        await send_long(update.message.reply_text, await run(agents.DAILY_QUESTION))
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
        text = await run(agents.DAILY_QUESTION)
        await send_long(lambda t, **kw: ctx.bot.send_message(OWNER, t, **kw), text)
    except Exception as e:
        await ctx.bot.send_message(OWNER, f"Daily report failed: {e}")


async def alerts(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        d = await get_data(frozenset({"open_interest", "funding", "liquidations"}))  # all the alerts need
    except Exception as e:
        logging.warning("alert fetch failed: %s", e)
        return
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
