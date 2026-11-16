"""
OTP Number Bot (Pro) - python-telegram-bot v20.x + httpx + SQLite
Env vars:
    BOT_TOKEN, AX_API_KEY, ADMIN_IDS="123456,789012", SUPPORT_USERNAME="@yourname"
    OTP_GROUP_ID, OTP_GROUP_URL
"""

import asyncio
import html
import logging
import os
import time
import threading
from urllib.parse import urlparse, unquote

import httpx
from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================== COLORED BUTTONS ===============================
# Telegram (Bot API 9.4+) supports 3 button colors: primary=blue, success=green, danger=red
from telegram import KeyboardButton as _KB
_RawBtn = Btn

_STYLE_RULES = [
    ("wd:ok", "success"), ("wd:no", "danger"),
    ("adm:ban", "danger"), ("adm:delnum", "danger"),
    ("adm:unban", "success"), ("adm:wd", "success"), ("adm:prem", "success"),
    ("adm:bal", "success"), ("adm:addnum", "success"), ("adm:bc", "success"),
    ("adm:home", None), ("adm:set:", None), ("adm:", "primary"),
    ("svc:", "primary"), ("rid:", "success"),
    ("switch_num", "primary"), ("switch_country", "primary"),
]


def _pick_style(text, cb, url):
    if url:
        return "primary"
    if cb == "noop":
        if text.startswith("✅"):
            return "success"
        if text.startswith("❌"):
            return "danger"
        return "primary"
    for prefix, st in _STYLE_RULES:
        if cb.startswith(prefix):
            return st
    return None


def Btn(text, callback_data=None, **kw):
    style = _pick_style(text, callback_data or "", kw.get("url"))
    if style:
        api = dict(kw.get("api_kwargs") or {})
        api["style"] = style
        kw["api_kwargs"] = api
    return _RawBtn(text, callback_data=callback_data, **kw)


# ------------------------------------------------------------------ CONFIG
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_KEY = ""  # Loaded securely from PostgreSQL settings
ADMIN_IDS = [int(i) for i in os.environ.get("ADMIN_IDS", "").split(",") if i.strip()]
SUPPORT = os.environ.get("SUPPORT_USERNAME", "@admin")
OTP_GROUP_ID = int(os.environ["OTP_GROUP_ID"]) if os.environ.get("OTP_GROUP_ID") else None
OTP_GROUP_URL = os.environ.get("OTP_GROUP_URL", "")
BASE_URL = "https://miahsms.com/bot/api"
POLL_INTERVAL, POLL_TIMEOUT = 5, 600

DEFAULTS = {
    "reward_free": "0.10",
    "reward_premium": "0.15",
    "min_wd_free": "5",
    "min_wd_premium": "2",
    "daily_free": "20",
    "daily_premium": "200",
    "currency": "BDT",
    "premium_info": "100 BDT / 30 days",
    "group_show_code": "0",
}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("otp-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------- DATABASE
from pg_adapter import db

db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id BIGINT PRIMARY KEY, name TEXT, balance DOUBLE PRECISION DEFAULT 0,
  premium_until BIGINT DEFAULT 0, banned INTEGER DEFAULT 0,
  otp_count INTEGER DEFAULT 0, joined BIGINT);
CREATE TABLE IF NOT EXISTS numbers(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, number TEXT, rid TEXT,
  service TEXT, country TEXT, otp TEXT, status TEXT, created BIGINT);
CREATE TABLE IF NOT EXISTS withdrawals(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount DOUBLE PRECISION,
  method TEXT, status TEXT DEFAULT 'pending', created BIGINT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
""")

for k, v in DEFAULTS.items():
    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT (key) DO NOTHING", (k, v))
db.commit()


def q(sql, args=(), one=False):
    with db.lock:
        cur = db.execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()


def x(sql, args=()):
    with db.lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur


def xid(sql, args=()):
    with db.lock:
        cur = db.execute(sql + " RETURNING id", args)
        row = cur.fetchone()
        db.commit()
        return row["id"]


def S(key):
    return q("SELECT value FROM settings WHERE key=?", (key,), one=True)["value"]


def get_api_key():
    row = q("SELECT value FROM settings WHERE key=?", ("api_key",), one=True)
    return row["value"] if row else ""


def now():
    return int(time.time())


def is_premium(u):
    return u["premium_until"] > now()


def today_start():
    t = time.localtime()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))


def mask(n: str) -> str:
    return f"{n[:5]}****{n[-2:]}" if len(n) > 8 else n


# --------------------------------------------------------------- API LAYER
http = None


async def api_get(path, params=None):
    headers = {"X-API-Key": get_api_key()}
    r = await http.get(
        f"{BASE_URL}/{path}",
        params=params or {},
        headers=headers
    )
    r.raise_for_status()
    return r.json()


async def fetch_ranges():
    return await api_get("liveaccess")


async def allocate_number(rid):
    r = await http.post(
        f"{BASE_URL}/getnum",
        json={"range": rid},
        headers={"X-API-Key": get_api_key()}
    )
    raw = r.json()
    meta = raw.get("meta", {})
    data = raw.get("data") or {}

    if r.status_code == 200 and meta.get("code") == 200 and data.get("full_number"):
        number = str(data["full_number"]).replace("+", "")
        return {
            "status": "success",
            "number": number,
            "country": data.get("country", "Unknown")
        }

    return {"status": "error", "http": r.status_code, "body": raw}


async def check_otp(number):
    raw = await api_get("live-console", {"since": 0, "limit": 200})
    items = _feed_items(raw)
    target = str(number).replace("+", "").strip()

    for item in items:
        found = str(item.get("number", "")).replace("+", "").strip()
        otp = str(item.get("otp", "") or "")
        if found == target and otp:
            return {"status": "success", "otp": otp, "number": target}

    return {"status": "pending"}


async def global_pool():
    return await api_get("success-otp-info")

def parse_ranges(raw) -> dict:
    """/liveaccess -> {service: [{rid, label}]} (data.services[].sid / ranges)"""
    out = {}
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    services = data.get("services", []) if isinstance(data, dict) else data
    for svc in services or []:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("sid") or "Other")
        for rng in svc.get("ranges", []):
            out.setdefault(name, []).append({"rid": str(rng), "label": "+" + str(rng)})
    return out


def range_rows(ranges):
    btns = [Btn(r["label"], callback_data="rid:" + r["rid"]) for r in ranges]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


# -------------------------------------------------------------- UI HELPERS
MENU = ReplyKeyboardMarkup(
    [["📲 NUMBERS"], ["💰 BALANCE", "📜 HISTORY"],
     ["💸 WITHDRAW", "📊 LIVE TRAFFIC"], ["💎 PREMIUM"]],
    resize_keyboard=True,
)


async def ensure_user(update: Update):
    u = update.effective_user
    x("INSERT INTO users(id,name,joined) VALUES(?,?,?) ON CONFLICT (id) DO NOTHING", (u.id, u.full_name, now()))
    row = q("SELECT * FROM users WHERE id=?", (u.id,), one=True)
    if row["banned"]:
        if update.callback_query:
            await update.callback_query.answer("🚫 You are banned.", show_alert=True)
        else:
            await update.effective_message.reply_text("🚫 You are banned.")
        return None
    return row


async def safe_edit(bot, chat_id, msg_id, text, markup=None):
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                    reply_markup=markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)


def number_kb(code_label):
    rows = [
        [Btn(code_label, callback_data="noop")],
        [Btn("🔄 Switch Number", callback_data="switch_num"),
         Btn("🌍 Switch Country", callback_data="switch_country")],
    ]
    if OTP_GROUP_URL:
        rows.append([Btn("🔐 OTP GROUP", url=OTP_GROUP_URL)])
    return Markup(rows)


# ---------------------------------------------------------------- USER FLOW
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    await update.message.reply_text(
        welcome_text(update.effective_user),
        reply_markup=(MENU_ADMIN if update.effective_user.id in ADMIN_IDS else MENU),
        parse_mode=ParseMode.HTML)


async def show_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    msg = update.effective_message
    try:
        services = await load_services()
    except Exception as e:
        log.exception("liveaccess")
        return await msg.reply_text(f"❌ Could not load services: {e}")
    if not services:
        return await msg.reply_text("⚠️ No live services right now. Try later.")

    context.user_data["services"] = services
    names = list(services)
    rows = [[Btn(svc_icon(n) + " " + n, callback_data=f"svc:{i}") for i, n in enumerate(names)][j:j + 2]
            for j in range(0, len(names), 2)]
    text = f"{BRAND}\n{LINE}\n📲 <b>Choose a service</b>\n<i>Live ranges update automatically</i>"
    if update.callback_query:
        await safe_edit(context.bot, msg.chat_id, msg.message_id, text, Markup(rows))
    else:
        await msg.reply_text(text, reply_markup=Markup(rows), parse_mode=ParseMode.HTML)


async def on_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    services = context.user_data.get("services")
    if not services:
        return await show_services(update, context)
    name = list(services)[int(cq.data.split(":")[1])]
    context.user_data["service"] = name
    save_state(cq.from_user.id, service=name)
    rows = range_rows(services[name])
    rows.append([Btn("⬅️ Back", callback_data="back_services")])
    await safe_edit(context.bot, cq.message.chat_id, cq.message.message_id,
                    f"{BRAND}\n{LINE}\n🌍 <b>{html.escape(name)}</b>\n<i>Choose a range 👇</i>", Markup(rows))


async def on_back_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await show_services(update, context)


async def on_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = await ensure_user(update)
    if not user:
        return
    rid = context.user_data.pop("_force_rid", None) or cq.data.split(":", 1)[1]
    context.user_data["rid"] = rid
    save_state(user["id"], rid=rid, service=context.user_data.get("service"))

    limit = int(float(S("daily_premium" if is_premium(user) else "daily_free")))
    used = q("SELECT COUNT(*) c FROM numbers WHERE user_id=? AND created>=?",
             (user["id"], today_start()), one=True)["c"]
    if used >= limit:
        return await cq.answer(f"Daily limit reached ({limit}). Upgrade to Premium 💎",
                               show_alert=True)

    await cq.answer("Allocating number…")
    try:
        data = await allocate_for(rid, context)
    except Exception as e:
        log.exception("getnumber")
        return await cq.message.reply_text(f"❌ API error: {e}")
    if data.get("status") != "success":
        return await cq.message.reply_text(f"⚠️ No number available: {data}")

    number, country = data["number"], data.get("country", "Unknown")
    service = context.user_data.get("service", "?")

    tasks = context.application.bot_data.setdefault("tasks", {})
    old = tasks.pop(user["id"], None)
    if old and not old.done():
        old.cancel()

    num_id = xid("INSERT INTO numbers(user_id,number,rid,service,country,status,created) "
                 "VALUES(?,?,?,?,?,'waiting',?)",
                 (user["id"], number, rid, service, country, now()))

    header = (f"{BRAND}\n{LINE}\n✅ <b>NUMBER ASSIGNED</b>\n\n"
              f"📞 Number: <code>+{number}</code>\n"
              f"🌍 Country: <b>{html.escape(country)}</b>\n"
              f"🛠 Service: <b>{html.escape(service)}</b>\n"
              f"{LINE}\n")
    await safe_edit(context.bot, cq.message.chat_id, cq.message.message_id,
                    header + "⏳ <i>Waiting for the code - it appears here automatically.</i>",
                    number_kb("⏳ Waiting for code…"))
    tasks[user["id"]] = context.application.create_task(
        poll_otp(context.application, user["id"], cq.message.chat_id,
                 cq.message.message_id, num_id, number, header))


async def poll_otp(app, uid, chat_id, msg_id, num_id, number, header):
    waited = 0
    try:
        while waited < POLL_TIMEOUT:
            try:
                res = app.bot_data.get("manual_otp", {}).pop(number, None) or await check_otp(number)
                if res.get("status") == "success" and res.get("otp"):
                    return await handle_otp(app, uid, chat_id, msg_id, num_id, header, res)
            except Exception as e:
                log.warning("poll error %s: %r", number, e)
            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
        x("UPDATE numbers SET status='expired' WHERE id=? AND status='waiting'", (num_id,))
        await safe_edit(app.bot, chat_id, msg_id,
                        header + f"⏰ <b>Timed out</b> - no code in {POLL_TIMEOUT // 60} min.\n<i>Tap Switch Number to try another.</i>",
                        number_kb("❌ Expired"))
    except asyncio.CancelledError:
        x("UPDATE numbers SET status='cancelled' WHERE id=? AND status='waiting'", (num_id,))
        raise
    finally:
        if app.bot_data.get("tasks", {}).get(uid) is asyncio.current_task():
            app.bot_data["tasks"].pop(uid, None)


async def handle_otp(app, uid, chat_id, msg_id, num_id, header, res):
    otp = str(res["otp"])
    done = x("UPDATE numbers SET status='done', otp=? WHERE id=? AND status='waiting'",
             (otp, num_id)).rowcount
    reward_txt = ""
    if done:
        user = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
        reward = float(S("reward_premium" if is_premium(user) else "reward_free"))
        x("UPDATE users SET balance=balance+?, otp_count=otp_count+1 WHERE id=?", (reward, uid))
        reward_txt = f"\n\n💰 Reward: <b>+{reward:g} {S('currency')}</b>"
    await safe_edit(
        app.bot, chat_id, msg_id,
        header + f"🔔 <b>OTP RECEIVED</b>\n\n🔑 <b>CODE</b>\n{big_digits(otp)}\n<code>{html.escape(otp)}</code>\n"
                 f"💬 <i>{html.escape(str(res.get('full_msg', '')))}</i>{reward_txt}",
        number_kb(f"✅ {otp}"))
    await app.bot.send_message(chat_id, otp_push_text(num_id, otp),
                               parse_mode=ParseMode.HTML)

    if done and OTP_GROUP_ID:
        row = q("SELECT number, service, country FROM numbers WHERE id=?", (num_id,), one=True)
        shown = otp if S("group_show_code") == "1" else otp[:2] + "*" * max(len(otp) - 2, 0)
        try:
            await app.bot.send_message(
                OTP_GROUP_ID,
                f"{BRAND}\n{LINE}\n✅ <b>NEW OTP RECEIVED</b>\n\n"
                f"🛠 Service: {html.escape(row['service'])}\n"
                f"🌍 Country: {html.escape(row['country'])}\n"
                f"📞 Number: <code>+{mask(row['number'])}</code>\n"
                f"🔑 Code: <code>{html.escape(shown)}</code>\n{LINE}\n⚡ Live & automatic",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("OTP group post failed: %s", e)


async def on_switch_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rid = context.user_data.get("rid")
    if not rid:
        await update.callback_query.answer("Session expired, pick a country again.")
        return await show_services(update, context)
    update.callback_query.data = f"rid:{rid}"
    await on_range(update, context)


async def on_switch_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    name = context.user_data.get("service")
    services = context.user_data.get("services")
    if not (name and services):
        return await show_services(update, context)
    rows = range_rows(services[name])
    rows.append([Btn("⬅️ Back", callback_data="back_services")])
    cq = update.callback_query
    await safe_edit(context.bot, cq.message.chat_id, cq.message.message_id,
                    f"{BRAND}\n{LINE}\n🌍 <b>{html.escape(name)}</b>\n<i>Choose a range 👇</i>", Markup(rows))


async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update)
    if not u:
        return
    prem = f"💎 Premium until {time.strftime('%Y-%m-%d', time.localtime(u['premium_until']))}" \
        if is_premium(u) else "🆓 Free plan"
    await update.message.reply_text(
        f"{BRAND}\n{LINE}\n💰 <b>MY WALLET</b>\n\n"
        f"💵 Balance: <b>{u['balance']:.4f} {S('currency')}</b>\n"
        f"✅ Total OTPs: <b>{u['otp_count']}</b>\n"
        f"💳 Plan: {prem}\n{LINE}\n"
        f"💸 Tap <b>WITHDRAW</b> to cash out.",
        parse_mode=ParseMode.HTML)


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update)
    if not u:
        return
    rows = q("SELECT * FROM numbers WHERE user_id=? ORDER BY id DESC LIMIT 10", (u["id"],))
    if not rows:
        return await update.message.reply_text("No history yet.")
    lines = [f"{'✅' if r['status'] == 'done' else '⏳' if r['status'] == 'waiting' else '❌'} "
             f"<code>+{r['number']}</code> · {html.escape(r['service'] or '?')} · "
             f"<b>{html.escape(r['otp'] or r['status'])}</b>" for r in rows]
    await update.message.reply_text(f"{BRAND}\n{LINE}\n📜 <b>HISTORY</b> (last 10)\n\n" + "\n".join(lines),
                                    parse_mode=ParseMode.HTML)


async def show_traffic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    try:
        raw = await global_pool()
        items = raw.get("data") if isinstance(raw, dict) else raw
        items = items if isinstance(items, list) else []
    except Exception as e:
        return await update.message.reply_text(f"❌ {e}")
    lines = []
    for it in items[:8]:
        if isinstance(it, dict):
            n = str(it.get("number", ""))
            lines.append(f"✅ {mask(n)} | {it.get('service', it.get('app', ''))}")
    await update.message.reply_text(
        f"{BRAND}\n{LINE}\n📊 <b>LIVE TRAFFIC</b>\n🟢 {len(items)} recent OTPs\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML)


async def show_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    await update.message.reply_text(
        f"{BRAND}\n{LINE}\n💎 <b>PREMIUM MEMBERSHIP</b>\n\n"
        f"💰 Reward / OTP: {S('reward_free')} ➜ <b>{S('reward_premium')}</b> {S('currency')}\n"
        f"📞 Daily numbers: {S('daily_free')} ➜ <b>{S('daily_premium')}</b>\n"
        f"💸 Min withdraw: {S('min_wd_free')} ➜ <b>{S('min_wd_premium')}</b> {S('currency')}\n"
        f"{LINE}\n🏷 Price: <b>{S('premium_info')}</b>\n📩 Contact: {SUPPORT}",
        parse_mode=ParseMode.HTML)


async def start_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update)
    if not u:
        return
    mn = S("min_wd_premium" if is_premium(u) else "min_wd_free")
    context.user_data["await"] = "withdraw"
    await update.message.reply_text(
        f"{BRAND}\n{LINE}\n💸 <b>WITHDRAW</b>\n\n"
        f"💵 Balance: <b>{u['balance']:.4f} {S('currency')}</b>\n"
        f"📉 Minimum: <b>{mn} {S('currency')}</b>\n{LINE}\n"
        "Send: <code>amount method account</code>\n"
        "Example: <code>10 bKash 01XXXXXXXXX</code>", parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "withdraw":
        return
    u = await ensure_user(update)
    if not u:
        return
    context.user_data.pop("await")
    parts = update.message.text.split(maxsplit=1)
    try:
        amount, method = round(float(parts[0]), 2), parts[1]
    except (ValueError, IndexError):
        return await update.message.reply_text("❌ Wrong format. Tap WITHDRAW and retry.")
    mn = float(S("min_wd_premium" if is_premium(u) else "min_wd_free"))
    if amount < mn or amount > u["balance"]:
        return await update.message.reply_text(f"❌ Amount must be {mn:g} - {u['balance']:.2f}.")

    x("UPDATE users SET balance=balance-? WHERE id=?", (amount, u["id"]))
    wid = xid("INSERT INTO withdrawals(user_id,amount,method,created) VALUES(?,?,?,?)",
               (u["id"], amount, method, now()))
    await update.message.reply_text(f"✅ Request #{wid} submitted. Pending admin approval.")
    kb = Markup([[Btn("✅ Approve", callback_data=f"wd:ok:{wid}"),
                  Btn("❌ Reject", callback_data=f"wd:no:{wid}")]])
    for aid in ADMIN_IDS:
        await context.bot.send_message(
            aid, f"💸 Withdraw #{wid}\nUser: {u['name']} ({u['id']})\n"
                 f"Amount: {amount} {S('currency')}\n{method}", reply_markup=kb)


# --------------------------------------------------------------- ADMIN PANEL
admin_only = filters.User(user_id=ADMIN_IDS)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 <b>Admin commands</b>\n"
        "/stats\n/pending\n/broadcast text\n"
        "/premium uid days\n/unpremium uid\n/ban uid\n/unban uid\n"
        "/addbal uid amount\n/set key value\n/debug\n\n"
        "Settings keys: " + ", ".join(DEFAULTS), parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def c(sql, args=()):
        return q(sql, args, one=True)[0]
    await update.message.reply_text(
        f"👥 Users: {c('SELECT COUNT(*) FROM users')}\n"
        f"💎 Premium: {c('SELECT COUNT(*) FROM users WHERE premium_until>?', (now(),))}\n"
        f"✅ OTPs done: {c('SELECT COUNT(*) FROM numbers WHERE status=?', ('done',))}\n"
        f"💰 Users' balance total: {c('SELECT COALESCE(SUM(balance),0) FROM users'):.2f}\n"
        f"⏳ Pending withdrawals: {c('SELECT COUNT(*) FROM withdrawals WHERE status=?', ('pending',))}")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = q("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id")
    if not rows:
        return await update.message.reply_text("No pending withdrawals.")
    for r in rows:
        kb = Markup([[Btn("✅ Approve", callback_data=f"wd:ok:{r['id']}"),
                      Btn("❌ Reject", callback_data=f"wd:no:{r['id']}")]])
        await update.message.reply_text(
            f"#{r['id']} | user {r['user_id']} | {r['amount']} | {r['method']}", reply_markup=kb)


async def on_withdraw_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("Admins only", show_alert=True)
    _, act, wid = cq.data.split(":")
    r = q("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (int(wid),), one=True)
    if not r:
        return await cq.answer("Already processed.", show_alert=True)
    if act == "ok":
        x("UPDATE withdrawals SET status='paid' WHERE id=?", (r["id"],))
        note = f"✅ Withdrawal #{r['id']} approved. Payment sent."
    else:
        x("UPDATE withdrawals SET status='rejected' WHERE id=?", (r["id"],))
        x("UPDATE users SET balance=balance+? WHERE id=?", (r["amount"], r["user_id"]))
        note = f"❌ Withdrawal #{r['id']} rejected. Amount refunded."
    await cq.answer("Done")
    await cq.message.edit_text(cq.message.text + f"\n\n→ {act.upper()}")
    try:
        await context.bot.send_message(r["user_id"], note)
    except Exception:
        pass


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        return await update.message.reply_text("Usage: /broadcast message")
    ok = 0
    for r in q("SELECT id FROM users WHERE banned=0"):
        try:
            await context.bot.send_message(r["id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📢 Sent to {ok} users.")


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid, days = int(context.args[0]), int(context.args[1])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: /premium uid days")
    u = q("SELECT premium_until FROM users WHERE id=?", (uid,), one=True)
    if not u:
        return await update.message.reply_text("User not found.")
    start = max(now(), u["premium_until"])
    x("UPDATE users SET premium_until=? WHERE id=?", (start + days * 86400, uid))
    await update.message.reply_text(f"💎 {uid} premium +{days} days.")
    try:
        await context.bot.send_message(uid, f"💎 Premium activated for {days} days!")
    except Exception:
        pass


async def simple_user_cmd(update, context, sql, msg):
    try:
        uid = int(context.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: /command uid")
    x(sql, (uid,))
    await update.message.reply_text(msg.format(uid))


async def cmd_unpremium(u, c): await simple_user_cmd(u, c, "UPDATE users SET premium_until=0 WHERE id=?", "{} premium removed.")
async def cmd_ban(u, c): await simple_user_cmd(u, c, "UPDATE users SET banned=1 WHERE id=?", "{} banned.")
async def cmd_unban(u, c): await simple_user_cmd(u, c, "UPDATE users SET banned=0 WHERE id=?", "{} unbanned.")


async def cmd_addbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid, amt = int(context.args[0]), float(context.args[1])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: /addbal uid amount")
    x("UPDATE users SET balance=balance+? WHERE id=?", (amt, uid))
    await update.message.reply_text(f"✅ {uid} balance {amt:+g}")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or context.args[0] not in DEFAULTS:
        return await update.message.reply_text("Usage: /set key value\nKeys: " + ", ".join(DEFAULTS))
    x("UPDATE settings SET value=? WHERE key=?", (" ".join(context.args[1:]), context.args[0]))
    await update.message.reply_text("✅ Updated.")


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import json
    try:
        await update.message.reply_text(json.dumps(await fetch_ranges(), indent=2)[:3900])
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# -------------------------------------------------------------------- MAIN

# ============================ ADMIN PANEL (buttons) ============================
from telegram.ext import ApplicationHandlerStop

MENU_ADMIN = ReplyKeyboardMarkup(
    [["📲 NUMBERS"], ["💰 BALANCE", "📜 HISTORY"],
     ["💸 WITHDRAW", "📊 LIVE TRAFFIC"], ["💎 PREMIUM"], ["🛠 ADMIN"]],
    resize_keyboard=True,
)

ADMIN_PROMPTS = {
    "bc": ("adm_bc", "📢 Send the message to broadcast to ALL users:"),
    "prem": ("adm_prem", "💎 Send: <code>user_id days</code>\nExample: <code>123456789 30</code>"),
    "bal": ("adm_bal", "➕ Send: <code>user_id amount</code> (negative = subtract)\nExample: <code>123456789 1.5</code>"),
    "ban": ("adm_ban", "🚫 Send the user_id to ban:"),
    "unban": ("adm_unban", "✅ Send the user_id to unban:"),
}

ADMIN_BACK = Markup([[Btn("⬅️ Admin Panel", callback_data="adm:home")]])


def admin_kb():
    return Markup([
        [Btn("📊 Stats", callback_data="adm:stats"), Btn("👥 Users", callback_data="adm:users")],
        [Btn("💸 Withdrawals", callback_data="adm:wd"), Btn("📢 Broadcast", callback_data="adm:bc")],
        [Btn("💎 Give Premium", callback_data="adm:prem"), Btn("➕ Add Balance", callback_data="adm:bal")],
        [Btn("🚫 Ban", callback_data="adm:ban"), Btn("✅ Unban", callback_data="adm:unban")],
        [Btn("📥 Add Numbers", callback_data="adm:addnum"), Btn("📦 Stock", callback_data="adm:stock")],
        [Btn("🗑 Delete Stock", callback_data="adm:delnum"), Btn("📨 Send OTP", callback_data="adm:sendotp")],
        [Btn("🔍 User Info", callback_data="adm:uinfo"), Btn("🧾 Recent OTPs", callback_data="adm:otps")],
        [Btn("⚙️ Settings", callback_data="adm:set")],
        [Btn("🔑 API Key", callback_data="adm:apikey")],
    ])


def stats_text():
    def c(sql, args=()):
        return q(sql, args, one=True)[0]
    return (
        "📊 <b>Statistics</b>\n\n"
        f"👥 Users: {c('SELECT COUNT(*) FROM users')}\n"
        f"💎 Premium: {c('SELECT COUNT(*) FROM users WHERE premium_until>?', (now(),))}\n"
        f"🚫 Banned: {c('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
        f"✅ OTPs total: {c('SELECT COUNT(*) FROM numbers WHERE status=?', ('done',))}\n"
        f"📅 OTPs today: {c('SELECT COUNT(*) FROM numbers WHERE status=? AND created>=?', ('done', today_start()))}\n"
        f"💰 Users balance owed: {c('SELECT COALESCE(SUM(balance),0) FROM users'):.4f} {S('currency')}\n"
        f"⏳ Pending withdrawals: {c('SELECT COUNT(*) FROM withdrawals WHERE status=?', ('pending',))}"
    )


def users_text():
    rows = q("SELECT * FROM users ORDER BY id DESC LIMIT 15")
    if not rows:
        return "No users yet."
    lines = []
    for r in rows:
        tag = ("💎" if is_premium(r) else "🆓") + ("🚫" if r["banned"] else "")
        name = html.escape((r["name"] or "")[:16])
        lines.append(f"{tag} <code>{r['id']}</code> {name} | {r['balance']:.4f} | {r['otp_count']} OTP")
    return "👥 <b>Last 15 users</b>\n\n" + "\n".join(lines)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("await", None)
    await update.effective_message.reply_text(
        f"{BRAND}\n{LINE}\n🛠 <b>ADMIN PANEL</b>\n<i>Users · Numbers · Payouts</i>", reply_markup=admin_kb(), parse_mode=ParseMode.HTML)


async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("Admins only", show_alert=True)
    await cq.answer()
    parts = cq.data.split(":")
    act = parts[1]
    msg = cq.message
    context.user_data.pop("await", None)

    async def show(text, kb=ADMIN_BACK):
        await safe_edit(context.bot, msg.chat_id, msg.message_id, text, kb)

    if act == "home":
        await show(f"{BRAND}\n{LINE}\n🛠 <b>ADMIN PANEL</b>\n<i>Users · Numbers · Payouts</i>", admin_kb())
    elif act == "stats":
        await show(stats_text())
    elif act == "users":
        await show(users_text())
    elif act == "wd":
        rows = q("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id")
        if not rows:
            return await show("✅ No pending withdrawals.")
        await show(f"⏳ {len(rows)} pending withdrawal(s) below:")
        for r in rows:
            kb = Markup([[Btn("✅ Approve", callback_data=f"wd:ok:{r['id']}"),
                          Btn("❌ Reject", callback_data=f"wd:no:{r['id']}")]])
            await context.bot.send_message(
                msg.chat_id,
                f"#{r['id']} | user {r['user_id']}\n{r['amount']} {S('currency')} | {r['method']}",
                reply_markup=kb)
    elif act == "stock":
        await show(stock_text())
    elif act == "otps":
        await show(otps_text())
    elif act in ADMIN_PROMPTS:
        state, prompt = ADMIN_PROMPTS[act]
        context.user_data["await"] = state
        await show(prompt)
    elif act == "apikey":
        context.user_data["await"] = "adm_api_key"
        await show("🔑 <b>API Key Update</b>\nনতুন API Key পাঠাও। Key নিরাপদে সংরক্ষিত হবে; প্যানেলে দেখানো হবে না।")
    elif act == "set":
        if len(parts) == 2:
            rows = [[Btn(f"{k} = {S(k)}", callback_data=f"adm:set:{k}")] for k in DEFAULTS]
            rows.append([Btn("⬅️ Admin Panel", callback_data="adm:home")])
            await show("⚙️ <b>Settings</b> - tap one to change:", Markup(rows))
        else:
            key = parts[2]
            if key in DEFAULTS:
                context.user_data["await"] = f"adm_setval:{key}"
                await show(f"⚙️ Send new value for <b>{key}</b>\nCurrent: <code>{html.escape(S(key))}</code>")


async def on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("await", "")
    if not state.startswith("adm_"):
        return
    context.user_data.pop("await", None)
    text = (update.message.text or "").strip()

    def reply(t):
        return update.message.reply_text(t, reply_markup=ADMIN_BACK, parse_mode=ParseMode.HTML)

    try:
        if state == "adm_bc":
            ok = 0
            for r in q("SELECT id FROM users WHERE banned=0"):
                try:
                    await context.bot.send_message(r["id"], text)
                    ok += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            await reply(f"📢 Sent to {ok} users.")
        elif state == "adm_prem":
            uid, days = text.split()
            uid, days = int(uid), int(days)
            u = q("SELECT premium_until FROM users WHERE id=?", (uid,), one=True)
            if not u:
                await reply("❌ User not found (they must have started the bot).")
            else:
                start = max(now(), u["premium_until"])
                x("UPDATE users SET premium_until=? WHERE id=?", (start + days * 86400, uid))
                await reply(f"💎 {uid} is Premium +{days} days.")
                try:
                    await context.bot.send_message(uid, f"💎 Premium activated for {days} days!")
                except Exception:
                    pass
        elif state == "adm_bal":
            uid, amt = text.split()
            uid, amt = int(uid), float(amt)
            n = x("UPDATE users SET balance=balance+? WHERE id=?", (amt, uid)).rowcount
            await reply(f"✅ {uid} balance {amt:+g}" if n else "❌ User not found.")
        elif state in ("adm_ban", "adm_unban"):
            uid = int(text)
            val = 1 if state == "adm_ban" else 0
            n = x("UPDATE users SET banned=? WHERE id=?", (val, uid)).rowcount
            word = "banned" if val else "unbanned"
            await reply(f"✅ {uid} {word}." if n else "❌ User not found.")
        elif state in ("adm_addnum", "adm_delnum", "adm_sendotp", "adm_uinfo"):
            await admin_extra_input(update, context, state, text, reply)
        elif state == "adm_api_key":
            if not text:
                await reply("❌ API Key খালি হতে পারবে না।")
            else:
                x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                  ("api_key", text))
                await reply("✅ API Key PostgreSQL-এ নিরাপদে সংরক্ষিত হয়েছে।")
        elif state.startswith("adm_setval:"):
            key = state.split(":", 1)[1]
            if key not in ("currency", "premium_info"):
                float(text)
            x("UPDATE settings SET value=? WHERE key=?", (text, key))
            await reply(f"✅ <b>{key}</b> = <code>{html.escape(text)}</code>")
    except (ValueError, IndexError):
        await reply("❌ Wrong format. Open the panel and try again.")
    raise ApplicationHandlerStop



# =============================== MANUAL NUMBERS ================================
db.executescript("""
CREATE TABLE IF NOT EXISTS stock_groups(
  id BIGSERIAL PRIMARY KEY, service TEXT, country TEXT);
CREATE TABLE IF NOT EXISTS stock(
  id BIGSERIAL PRIMARY KEY, gid BIGINT, number TEXT,
  status TEXT DEFAULT 'free', added BIGINT);
""")
db.commit()

ADMIN_PROMPTS.update({
    "addnum": ("adm_addnum",
               "📥 <b>Add numbers</b>\nFirst line: <code>Service | Country</code>\n"
               "Then one number per line:\n\n<code>FACEBOOK | Cameroon\n237612345678\n237698765432</code>\n\n"
               "Tip: use the same service name as in the menu (e.g. FACEBOOK) to merge with live ranges."),
    "delnum": ("adm_delnum", "🗑 Send: <code>Service | Country</code> to delete its FREE numbers."),
    "sendotp": ("adm_sendotp",
                "📨 Send: <code>number code</code> (optional message after)\n"
                "Example: <code>237612345678 482913</code>\n"
                "Delivers the code to the user waiting on that number."),
    "uinfo": ("adm_uinfo", "🔍 Send the user_id:"),
})


def stock_text():
    rows = q("""SELECT g.service, g.country,
                COUNT(s.id) FILTER (WHERE s.status='free') AS free,
                COUNT(s.id) FILTER (WHERE s.status='used') AS used
                FROM stock_groups g LEFT JOIN stock s ON s.gid=g.id
                GROUP BY g.id ORDER BY g.service, g.country""")
    if not rows:
        return "📦 Stock is empty.\nUse 📥 Add Numbers."
    lines = [f"{html.escape(r['service'])} / {html.escape(r['country'])}: "
             f"🟢 {r['free'] or 0} free | 🔴 {r['used'] or 0} used" for r in rows]
    return "📦 <b>Your number stock</b>\n\n" + "\n".join(lines)


def otps_text():
    rows = q("SELECT * FROM numbers WHERE status='done' ORDER BY id DESC LIMIT 10")
    if not rows:
        return "No OTPs yet."
    lines = [f"✅ {html.escape(r['service'] or '?')} | {html.escape(r['country'] or '?')} | "
             f"+{mask(r['number'])} | user <code>{r['user_id']}</code>" for r in rows]
    return "🧾 <b>Last 10 OTPs</b>\n\n" + "\n".join(lines)


async def load_services():
    # live API services + your own stock (stock still works if the API is down)
    try:
        services = parse_ranges(await fetch_ranges())
    except Exception:
        log.exception("liveaccess failed")
        services = {}
        if not q("SELECT 1 FROM stock WHERE status='free' LIMIT 1", one=True):
            raise
    lower = {k.lower(): k for k in services}
    groups = q("""SELECT g.id, g.service, g.country, COUNT(s.id) AS n
                  FROM stock_groups g JOIN stock s ON s.gid=g.id AND s.status='free'
                  GROUP BY g.id HAVING COUNT(s.id)>0 ORDER BY g.service, g.country""")
    for g in groups:
        name = lower.get(g["service"].lower(), g["service"])
        services.setdefault(name, []).insert(
            0, {"rid": f"S{g['id']}", "label": f"📦 {g['country']} ({g['n']})"})
    return services


async def allocate_any(rid):
    if rid.startswith("S") and rid[1:].isdigit():
        gid = int(rid[1:])
        row = q("SELECT id, number FROM stock WHERE gid=? AND status='free' ORDER BY id LIMIT 1",
                (gid,), one=True)
        if not row:
            return {"status": "error", "message": "Out of stock"}
        g = q("SELECT country FROM stock_groups WHERE id=?", (gid,), one=True)
        x("UPDATE stock SET status='used' WHERE id=?", (row["id"],))
        return {"status": "success", "number": row["number"],
                "country": g["country"] if g else "Unknown"}
    return await allocate_number(rid)


async def admin_extra_input(update, context, state, text, reply):
    if state == "adm_addnum":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines or "|" not in lines[0]:
            raise ValueError
        service, country = [p.strip() for p in lines[0].split("|", 1)]
        nums = []
        for l in lines[1:]:
            n = l.replace("+", "").replace(" ", "").replace("-", "")
            if n.isdigit() and 6 <= len(n) <= 15:
                nums.append(n)
        if not service or not country or not nums:
            raise ValueError
        g = q("SELECT id FROM stock_groups WHERE service=? AND country=?", (service, country), one=True)
        gid = g["id"] if g else xid("INSERT INTO stock_groups(service,country) VALUES(?,?)",
                                      (service, country))
        added = 0
        for n in dict.fromkeys(nums):
            if q("SELECT 1 FROM stock WHERE number=? AND status='free'", (n,), one=True):
                continue
            x("INSERT INTO stock(gid,number,status,added) VALUES(?,?,'free',?)", (gid, n, now()))
            added += 1
        await reply(f"✅ Added {added} number(s) to <b>{html.escape(service)}</b> / {html.escape(country)}.")
    elif state == "adm_delnum":
        service, country = [p.strip() for p in text.split("|", 1)]
        g = q("SELECT id FROM stock_groups WHERE service=? AND country=?", (service, country), one=True)
        if not g:
            await reply("❌ Group not found. Check 📦 Stock for exact names.")
        else:
            n = x("DELETE FROM stock WHERE gid=? AND status='free'", (g["id"],)).rowcount
            await reply(f"🗑 Removed {n} free number(s).")
    elif state == "adm_sendotp":
        parts = text.split(maxsplit=2)
        number, otp = parts[0].replace("+", ""), parts[1]
        note = parts[2] if len(parts) > 2 else "Sent by admin"
        if not q("SELECT 1 FROM numbers WHERE number=? AND status='waiting'", (number,), one=True):
            await reply("❌ No user is waiting on that number (or the time expired).")
        else:
            context.application.bot_data.setdefault("manual_otp", {})[number] = {
                "status": "success", "otp": otp, "full_msg": note}
            await reply("📨 Queued. Delivered within ~5 seconds.")
    elif state == "adm_uinfo":
        uid = int(text)
        u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
        if not u:
            await reply("❌ User not found.")
        else:
            nn = q("SELECT COUNT(*) c FROM numbers WHERE user_id=?", (uid,), one=True)["c"]
            prem = "💎 Premium" if is_premium(u) else "🆓 Free"
            await reply(f"🔍 <b>{html.escape(u['name'] or '')}</b> (<code>{uid}</code>)\n"
                        f"{prem}{' | 🚫 Banned' if u['banned'] else ''}\n"
                        f"💰 {u['balance']:.4f} {S('currency')}\n"
                        f"✅ OTPs: {u['otp_count']} | 📞 Numbers: {nn}\n"
                        f"📅 Joined: {time.strftime('%Y-%m-%d', time.localtime(u['joined'] or 0))}")



# ================================ PREMIUM THEME ================================
BRAND = "🔥 <b>" + html.escape(os.environ.get("BOT_NAME", "FIRE NUMBER BOT")) + "</b>"
LINE = "━━━━━━━━━━━━━━━━━━"
ICONS = {"facebook": "📘", "instagram": "📸", "whatsapp": "💬", "tiktok": "🎵",
         "telegram": "✈️", "imo": "📞", "google": "🔎", "twitter": "🐦",
         "snapchat": "👻", "twilio": "☎️", "888starz": "🎰", "bizbet": "🎲"}


def svc_icon(name):
    n = name.lower()
    for k, v in ICONS.items():
        if k in n:
            return v
    return "📲"


def welcome_text(tg_user):
    row = q("SELECT * FROM users WHERE id=?", (tg_user.id,), one=True)
    plan = "💎 <b>PREMIUM</b>" if is_premium(row) else "🆓 Free"
    return (f"{BRAND}\n{LINE}\n"
            f"👋 Welcome, <b>{html.escape(tg_user.first_name or 'friend')}</b>!\n\n"
            f"💳 Plan: {plan}\n"
            f"💰 Balance: <b>{row['balance']:.4f} {S('currency')}</b>\n"
            f"✅ Total OTPs: <b>{row['otp_count']}</b>\n"
            f"{LINE}\n"
            f"⚡ Tap <b>NUMBERS</b> to get a live number.\n"
            f"🔔 Codes arrive here automatically.")


# ---------------------- colored main menus (reply keyboard) ----------------------
_MENU_STYLES = {
    "📲 NUMBERS": "success", "💰 BALANCE": "primary", "📜 HISTORY": "primary",
    "💸 WITHDRAW": "success", "📊 LIVE TRAFFIC": "primary",
    "💎 PREMIUM": "danger", "🛠 ADMIN": "danger",
}


def _color_kb(rows):
    return ReplyKeyboardMarkup(
        [[_KB(t, api_kwargs={"style": _MENU_STYLES[t]}) for t in row] for row in rows],
        resize_keyboard=True,
    )


MENU = _color_kb([["📲 NUMBERS"], ["💰 BALANCE", "📜 HISTORY"],
                  ["💸 WITHDRAW", "📊 LIVE TRAFFIC"], ["💎 PREMIUM"]])
MENU_ADMIN = _color_kb([["📲 NUMBERS"], ["💰 BALANCE", "📜 HISTORY"],
                        ["💸 WITHDRAW", "📊 LIVE TRAFFIC"], ["💎 PREMIUM"], ["🛠 ADMIN"]])



# =============================== COUNTRY NAMES =================================
import random

CALLING = {
    "1": ("US", "USA / Canada"), "7": ("RU", "Russia"), "20": ("EG", "Egypt"),
    "27": ("ZA", "South Africa"), "30": ("GR", "Greece"), "31": ("NL", "Netherlands"),
    "32": ("BE", "Belgium"), "33": ("FR", "France"), "34": ("ES", "Spain"),
    "36": ("HU", "Hungary"), "39": ("IT", "Italy"), "40": ("RO", "Romania"),
    "41": ("CH", "Switzerland"), "43": ("AT", "Austria"), "44": ("GB", "United Kingdom"),
    "45": ("DK", "Denmark"), "46": ("SE", "Sweden"), "47": ("NO", "Norway"),
    "48": ("PL", "Poland"), "49": ("DE", "Germany"), "51": ("PE", "Peru"),
    "52": ("MX", "Mexico"), "53": ("CU", "Cuba"), "54": ("AR", "Argentina"),
    "55": ("BR", "Brazil"), "56": ("CL", "Chile"), "57": ("CO", "Colombia"),
    "58": ("VE", "Venezuela"), "60": ("MY", "Malaysia"), "61": ("AU", "Australia"),
    "62": ("ID", "Indonesia"), "63": ("PH", "Philippines"), "64": ("NZ", "New Zealand"),
    "65": ("SG", "Singapore"), "66": ("TH", "Thailand"), "81": ("JP", "Japan"),
    "82": ("KR", "South Korea"), "84": ("VN", "Vietnam"), "86": ("CN", "China"),
    "90": ("TR", "Turkiye"), "91": ("IN", "India"), "92": ("PK", "Pakistan"),
    "93": ("AF", "Afghanistan"), "94": ("LK", "Sri Lanka"), "95": ("MM", "Myanmar"),
    "98": ("IR", "Iran"), "211": ("SS", "South Sudan"), "212": ("MA", "Morocco"),
    "213": ("DZ", "Algeria"), "216": ("TN", "Tunisia"), "218": ("LY", "Libya"),
    "220": ("GM", "Gambia"), "221": ("SN", "Senegal"), "222": ("MR", "Mauritania"),
    "223": ("ML", "Mali"), "224": ("GN", "Guinea"), "225": ("CI", "Ivory Coast"),
    "226": ("BF", "Burkina Faso"), "227": ("NE", "Niger"), "228": ("TG", "Togo"),
    "229": ("BJ", "Benin"), "230": ("MU", "Mauritius"), "231": ("LR", "Liberia"),
    "232": ("SL", "Sierra Leone"), "233": ("GH", "Ghana"), "234": ("NG", "Nigeria"),
    "235": ("TD", "Chad"), "236": ("CF", "Central African Rep."), "237": ("CM", "Cameroon"),
    "238": ("CV", "Cape Verde"), "239": ("ST", "Sao Tome"), "240": ("GQ", "Equatorial Guinea"),
    "241": ("GA", "Gabon"), "242": ("CG", "Congo"), "243": ("CD", "DR Congo"),
    "244": ("AO", "Angola"), "245": ("GW", "Guinea-Bissau"), "248": ("SC", "Seychelles"),
    "249": ("SD", "Sudan"), "250": ("RW", "Rwanda"), "251": ("ET", "Ethiopia"),
    "252": ("SO", "Somalia"), "253": ("DJ", "Djibouti"), "254": ("KE", "Kenya"),
    "255": ("TZ", "Tanzania"), "256": ("UG", "Uganda"), "257": ("BI", "Burundi"),
    "258": ("MZ", "Mozambique"), "260": ("ZM", "Zambia"), "261": ("MG", "Madagascar"),
    "262": ("RE", "Reunion"), "263": ("ZW", "Zimbabwe"), "264": ("NA", "Namibia"),
    "265": ("MW", "Malawi"), "266": ("LS", "Lesotho"), "267": ("BW", "Botswana"),
    "268": ("SZ", "Eswatini"), "269": ("KM", "Comoros"), "291": ("ER", "Eritrea"),
    "297": ("AW", "Aruba"), "298": ("FO", "Faroe Islands"), "299": ("GL", "Greenland"),
    "350": ("GI", "Gibraltar"), "351": ("PT", "Portugal"), "352": ("LU", "Luxembourg"),
    "353": ("IE", "Ireland"), "354": ("IS", "Iceland"), "355": ("AL", "Albania"),
    "356": ("MT", "Malta"), "357": ("CY", "Cyprus"), "358": ("FI", "Finland"),
    "359": ("BG", "Bulgaria"), "370": ("LT", "Lithuania"), "371": ("LV", "Latvia"),
    "372": ("EE", "Estonia"), "373": ("MD", "Moldova"), "374": ("AM", "Armenia"),
    "375": ("BY", "Belarus"), "376": ("AD", "Andorra"), "377": ("MC", "Monaco"),
    "378": ("SM", "San Marino"), "380": ("UA", "Ukraine"), "381": ("RS", "Serbia"),
    "382": ("ME", "Montenegro"), "383": ("XK", "Kosovo"), "385": ("HR", "Croatia"),
    "386": ("SI", "Slovenia"), "387": ("BA", "Bosnia"), "389": ("MK", "North Macedonia"),
    "420": ("CZ", "Czechia"), "421": ("SK", "Slovakia"), "423": ("LI", "Liechtenstein"),
    "500": ("FK", "Falkland Islands"), "501": ("BZ", "Belize"), "502": ("GT", "Guatemala"),
    "503": ("SV", "El Salvador"), "504": ("HN", "Honduras"), "505": ("NI", "Nicaragua"),
    "506": ("CR", "Costa Rica"), "507": ("PA", "Panama"),
    "508": ("PM", "Saint Pierre & Miquelon"), "509": ("HT", "Haiti"),
    "590": ("GP", "Guadeloupe"), "591": ("BO", "Bolivia"), "592": ("GY", "Guyana"),
    "593": ("EC", "Ecuador"), "594": ("GF", "French Guiana"), "595": ("PY", "Paraguay"),
    "596": ("MQ", "Martinique"), "597": ("SR", "Suriname"), "598": ("UY", "Uruguay"),
    "599": ("CW", "Curacao"), "670": ("TL", "Timor-Leste"), "673": ("BN", "Brunei"),
    "675": ("PG", "Papua New Guinea"), "676": ("TO", "Tonga"), "677": ("SB", "Solomon Islands"),
    "678": ("VU", "Vanuatu"), "679": ("FJ", "Fiji"), "685": ("WS", "Samoa"),
    "687": ("NC", "New Caledonia"), "689": ("PF", "French Polynesia"),
    "850": ("KP", "North Korea"), "852": ("HK", "Hong Kong"), "853": ("MO", "Macao"),
    "855": ("KH", "Cambodia"), "856": ("LA", "Laos"), "880": ("BD", "Bangladesh"),
    "886": ("TW", "Taiwan"), "960": ("MV", "Maldives"), "961": ("LB", "Lebanon"),
    "962": ("JO", "Jordan"), "963": ("SY", "Syria"), "964": ("IQ", "Iraq"),
    "965": ("KW", "Kuwait"), "966": ("SA", "Saudi Arabia"), "967": ("YE", "Yemen"),
    "968": ("OM", "Oman"), "970": ("PS", "Palestine"), "971": ("AE", "UAE"),
    "972": ("IL", "Israel"), "973": ("BH", "Bahrain"), "974": ("QA", "Qatar"),
    "975": ("BT", "Bhutan"), "976": ("MN", "Mongolia"), "977": ("NP", "Nepal"),
    "992": ("TJ", "Tajikistan"), "993": ("TM", "Turkmenistan"), "994": ("AZ", "Azerbaijan"),
    "995": ("GE", "Georgia"), "996": ("KG", "Kyrgyzstan"), "998": ("UZ", "Uzbekistan"),
}


def _flag(iso):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso.upper())


def country_of(rid):
    # "2290165XXX" -> ("BJ", flag, "Benin")
    d = "".join(ch for ch in str(rid) if ch.isdigit())
    if d.startswith(("76", "77")):
        return ("KZ", _flag("KZ"), "Kazakhstan")
    for n in (3, 2, 1):
        hit = CALLING.get(d[:n])
        if hit:
            return (hit[0], _flag(hit[0]), hit[1])
    return None


def _is_stock(rid):
    return rid.startswith("S") and rid[1:].isdigit()


def range_rows(ranges):
    # one button per COUNTRY (flag + name) instead of one per number range
    own, items, seen = [], [], set()
    for r in ranges:
        rid = r["rid"]
        if _is_stock(rid):
            own.append(Btn(r["label"], callback_data="rid:" + rid))
            continue
        info = country_of(rid)
        if info is None:
            items.append(("~" + rid, "🌐 +" + rid, "rid:" + rid))
        elif info[0] not in seen:
            seen.add(info[0])
            items.append((info[2], f"{info[1]} {info[2]}", "rid:C:" + info[0]))
    items.sort(key=lambda t: t[0].lower())
    btns = own + [Btn(label, callback_data=cb) for _, label, cb in items]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


async def allocate_for(rid, context):
    # "C:BJ" = any live range of that country (tries a few until one gives a number)
    if not rid.startswith("C:"):
        return await allocate_any(rid)
    iso = rid[2:]
    svc = context.user_data.get("service")
    pool = [r["rid"] for r in (context.user_data.get("services") or {}).get(svc, [])
            if not _is_stock(r["rid"]) and (country_of(r["rid"]) or ("",))[0] == iso]
    if not pool:
        return {"status": "error", "message": "No live range for this country right now"}
    random.shuffle(pool)
    last = None
    for cand in pool[:6]:
        last = await allocate_number(cand)
        if last.get("status") == "success":
            return last
    return last



# ========================== JOIN GATE AND LIVE FEED ============================
try:
    BRAND, LINE
except NameError:
    BRAND, LINE = "<b>OTP BOT</b>", "━━━━━━━━━━━━━━━━━━"

FEED_INTERVAL = 10          # seconds between live-feed checks
_MEMBER_CACHE = {}          # user_id -> time until which membership is trusted


def big_digits(code):
    # 4️⃣8️⃣2️⃣9️⃣ - keycap emoji look big in Telegram
    return " ".join((ch + "\ufe0f\u20e3") if ch.isdigit() else ch for ch in str(code))


async def is_group_member(bot, uid, use_cache=True):
    if use_cache and _MEMBER_CACHE.get(uid, 0) > now():
        return True
    try:
        m = await bot.get_chat_member(OTP_GROUP_ID, uid)
        ok = m.status in ("member", "administrator", "creator") or (
            m.status == "restricted" and getattr(m, "is_member", False))
    except Exception as e:
        # bot not admin / API hiccup: do not lock everybody out
        log.warning("membership check failed: %r", e)
        return True
    if ok:
        _MEMBER_CACHE[uid] = now() + 600
    return ok


def gate_kb():
    return Markup([
        [Btn("📢 Join OTP Group", url=OTP_GROUP_URL)],
        [Btn("✅ I've Joined", callback_data="joined")],
    ])


GATE_TEXT = (f"{BRAND}\n{LINE}\n🔒 <b>Join required</b>\n\n"
             "1️⃣ Join our OTP group\n2️⃣ Tap <b>I've Joined</b>\n\n"
             "Then you can get numbers and receive OTP.")

_orig_ensure_user = ensure_user


async def ensure_user(update):
    row = await _orig_ensure_user(update)
    if not row:
        return None
    if not (OTP_GROUP_ID and OTP_GROUP_URL) or row["id"] in ADMIN_IDS:
        return row
    bot = update.get_bot()
    if await is_group_member(bot, row["id"]):
        return row
    chat_id = update.effective_chat.id
    if update.callback_query:
        await update.callback_query.answer("🔒 Join the OTP group first", show_alert=True)
    await bot.send_message(chat_id, GATE_TEXT, reply_markup=gate_kb(), parse_mode=ParseMode.HTML)
    return None


async def on_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    u = cq.from_user
    x("INSERT INTO users(id,name,joined) VALUES(?,?,?) ON CONFLICT (id) DO NOTHING", (u.id, u.full_name, now()))
    _MEMBER_CACHE.pop(u.id, None)
    if not await is_group_member(context.bot, u.id, use_cache=False):
        return await cq.answer("❌ You have not joined the group yet.", show_alert=True)
    await cq.answer("✅ Verified!")
    text = welcome_text(u) if "welcome_text" in globals() else "✅ Verified!"
    await context.bot.send_message(
        cq.message.chat_id, text, parse_mode=ParseMode.HTML,
        reply_markup=(MENU_ADMIN if u.id in ADMIN_IDS else MENU))
    try:
        await cq.message.delete()
    except Exception:
        pass


# ---- live feed: posts OTPs seen on the server into the group (display only, no income)
def _g(it, *keys):
    for k in keys:
        v = it.get(k)
        if v not in (None, ""):
            return v
    return None


def _feed_items(raw):
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if isinstance(data, dict):
        for k in ("otps", "items", "list", "results", "success", "messages", "data"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
    return data if isinstance(data, list) else []


def _feed_text(number, otp, service, country):
    shown = otp if S("group_show_code") == "1" else (otp[:2] + "*" * max(len(otp) - 2, 0))
    if not country and "country_of" in globals():
        info = country_of(number)
        country = f"{info[1]} {info[2]}" if info else "-"
    return (f"{BRAND}\n{LINE}\n🟢 <b>LIVE OTP</b>\n\n"
            f"🛠 Service: <b>{html.escape(service)}</b>\n"
            f"🌍 Country: <b>{html.escape(country or '-')}</b>\n"
            f"📞 Number: <code>+{mask(number)}</code>\n"
            f"🔑 Code: <code>{html.escape(shown or '-')}</code>\n{LINE}\n"
            f"⚡ Live traffic")


async def feed_loop(app):
    seen, first, queue = set(), True, []
    while True:
        try:
            fresh = []
            for it in _feed_items(await global_pool()):
                if not isinstance(it, dict):
                    continue
                number = str(_g(it, "number", "num", "phone") or "").replace("+", "")
                if not number:
                    continue
                otp = str(_g(it, "otp", "code") or "")
                stamp = str(_g(it, "time", "created_at", "timestamp", "date") or "")
                key = f"{number}|{otp}|{stamp}"
                if key in seen:
                    continue
                seen.add(key)
                fresh.append((number, otp,
                              str(_g(it, "service", "sid", "app", "name") or "?"),
                              str(_g(it, "country") or "")))
            if first:
                first = False            # do not flood the group with old OTPs on startup
            else:
                queue = (queue + fresh)[-20:]
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])
        except Exception as e:
            log.warning("feed fetch failed: %r", e)

        for _ in range(min(2, len(queue))):  # max ~12 posts/min (Telegram group limit is 20)
            number, otp, svc, country = queue.pop(0)
            # OTPs of our own users are already posted instantly, skip duplicates
            if q("SELECT 1 FROM numbers WHERE number=? AND status='done' AND created>?",
                 (number, now() - 900), one=True):
                continue
            try:
                await app.bot.send_message(OTP_GROUP_ID, _feed_text(number, otp, svc, country),
                                           parse_mode=ParseMode.HTML)
            except Exception as e:
                log.warning("feed post failed: %r", e)
            await asyncio.sleep(3)
        await asyncio.sleep(FEED_INTERVAL)


async def cmd_trafficraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import json
    try:
        await update.message.reply_text(json.dumps(await global_pool(), indent=1)[:3900])
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")



# ============================== LIVE RANGES POST ===============================
# One message in the OTP group that the bot edits every minute (no spam).
try:
    BRAND, LINE
except NameError:
    BRAND, LINE = "<b>OTP BOT</b>", "━━━━━━━━━━━━━━━━━━"

LIVE_EVERY = 60


def _ago(last):
    t = float(last)
    if t > 1e12:
        t /= 1000
    d = max(0, int(time.time() - t))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    return f"{d // 86400} d ago"


def live_text(raw):
    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    svcs = data.get("services", []) if isinstance(data, dict) else []
    rows = []
    for sv in svcs:
        if isinstance(sv, dict):
            rows.append((float(sv.get("last_at") or 0), str(sv.get("sid") or "?"),
                         sv.get("ranges") or []))
    rows.sort(key=lambda r: r[0], reverse=True)
    lines = []
    for last, name, rng in rows[:8]:
        secs = last / 1000 if last > 1e12 else last
        age = (time.time() - secs) if last else 1e9
        dot = "🟢" if age < 300 else "🟡" if age < 1800 else "⚪"
        icon = svc_icon(name) if "svc_icon" in globals() else "📲"
        extra = f" · {len(rng)} range" + ("" if len(rng) == 1 else "s")
        if "country_of" in globals():
            nc = len({(country_of(r) or ('?',))[0] for r in rng})
            extra += f" · {nc} countr" + ("y" if nc == 1 else "ies")
        when = _ago(last) if last else "-"
        lines.append(f"{dot} {icon} <b>{html.escape(name)}</b> · {when}{extra}")
    if not lines:
        lines = ["No live ranges right now."]
    stamp = time.strftime("%H:%M UTC", time.gmtime())
    return (f"{BRAND}\n{LINE}\n📡 <b>LIVE RANGES</b>\n\n" + "\n".join(lines) +
            f"\n{LINE}\n🕒 Updated {stamp} · refreshes every minute")


async def live_ranges_loop(app):
    await asyncio.sleep(5)
    while True:
        try:
            text = live_text(await fetch_ranges())
            kb = Markup([[Btn("🤖 Get a number", url=f"https://t.me/{app.bot.username}")]])
            row = q("SELECT value FROM settings WHERE key='live_msg_id'", one=True)
            msg_id = int(row["value"]) if row else None
            done = False
            if msg_id:
                try:
                    await app.bot.edit_message_text(
                        text, chat_id=OTP_GROUP_ID, message_id=msg_id,
                        reply_markup=kb, parse_mode=ParseMode.HTML)
                    done = True
                except BadRequest as e:
                    if "not modified" in str(e).lower():
                        done = True
                    else:
                        log.info("live message gone, posting a new one: %s", e)
            if not done:
                m = await app.bot.send_message(OTP_GROUP_ID, text, reply_markup=kb,
                                               parse_mode=ParseMode.HTML)
                x("INSERT INTO settings(key,value) VALUES('live_msg_id',?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                  (str(m.message_id),))
                try:
                    await app.bot.pin_chat_message(OTP_GROUP_ID, m.message_id,
                                                   disable_notification=True)
                except Exception:
                    pass
        except Exception as e:
            log.warning("live ranges update failed: %r", e)
        await asyncio.sleep(LIVE_EVERY)


async def cmd_livepost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    x("DELETE FROM settings WHERE key='live_msg_id'")
    await update.message.reply_text(
        "📡 A fresh live-ranges message will be posted in the group within a minute. "
        "Delete the old one there if you want.")



# =========================== SWITCH AND TIMEOUT FIX ============================
try:
    BRAND, LINE
except NameError:
    BRAND, LINE = "<b>OTP BOT</b>", "━━━━━━━━━━━━━━━━━━"

if "big_digits" not in globals():
    def big_digits(code):
        return " ".join((ch + "\ufe0f\u20e3") if ch.isdigit() else ch for ch in str(code))

db.executescript("""
CREATE TABLE IF NOT EXISTS user_state(
  user_id INTEGER PRIMARY KEY, service TEXT, rid TEXT);
""")
db.commit()


def save_state(uid, service=None, rid=None):
    # remember each user's last service/range so Switch buttons survive bot restarts
    row = q("SELECT service, rid FROM user_state WHERE user_id=?", (uid,), one=True)
    svc = service if service is not None else (row["service"] if row else None)
    r = rid if rid is not None else (row["rid"] if row else None)
    x("INSERT INTO user_state(user_id,service,rid) VALUES(?,?,?) ON CONFLICT (user_id) DO UPDATE SET service=EXCLUDED.service, rid=EXCLUDED.rid", (uid, svc, r))


async def restore_state(uid, context):
    ud = context.user_data
    if not ud.get("rid") or not ud.get("service"):
        row = q("SELECT service, rid FROM user_state WHERE user_id=?", (uid,), one=True)
        if row:
            if row["service"]:
                ud.setdefault("service", row["service"])
            if row["rid"]:
                ud.setdefault("rid", row["rid"])
    if not ud.get("services"):
        try:
            ud["services"] = await load_services()
        except Exception as e:
            log.warning("could not reload services: %r", e)


async def on_switch_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await restore_state(cq.from_user.id, context)
    rid = context.user_data.get("rid")
    if not rid:
        await cq.answer("Pick a country again 🌍")
        return await show_services(update, context)
    context.user_data["_force_rid"] = rid     # (CallbackQuery is read-only, so no cq.data edit)
    await on_range(update, context)


async def on_switch_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    await restore_state(cq.from_user.id, context)
    name = context.user_data.get("service")
    services = context.user_data.get("services") or {}
    if not (name and name in services):
        return await show_services(update, context)
    rows = range_rows(services[name])
    rows.append([Btn("⬅️ Back", callback_data="back_services")])
    await safe_edit(context.bot, cq.message.chat_id, cq.message.message_id,
                    f"{BRAND}\n{LINE}\n🌍 <b>{html.escape(name)}</b>\n<i>Choose a range 👇</i>",
                    Markup(rows))


def otp_push_text(num_id, otp):
    # separate message at the bottom of the chat so the code is seen instantly (+ push alert)
    row = q("SELECT number, service, country FROM numbers WHERE id=?", (num_id,), one=True)
    info = ""
    if row:
        info = (f"📞 <code>+{row['number']}</code>\n"
                f"🛠 {html.escape(row['service'] or '?')} · 🌍 {html.escape(row['country'] or '?')}\n\n")
    return (f"🔔 <b>OTP RECEIVED</b>\n{info}"
            f"🔑 {big_digits(otp)}\n\n<code>{html.escape(otp)}</code>\n<i>Tap the code to copy</i>")



# =============================== NG STYLE SYSTEM ===============================
# 3 numbers per request (copy buttons) + VIEW OTP / CHANGE NUMBER + OTP group cards
# + 2FA Auth + Leaderboard + Support + new main menu
import base64, hashlib, hmac, re, struct
from telegram import KeyboardButton as _KB2

try:
    BRAND, LINE
except NameError:
    BRAND, LINE = "<b>OTP BOT</b>", "━━━━━━━━━━━━━━━━━━"

NUMS_PER_REQUEST = 3
COPY_OK = True

if "_STYLE_RULES" in globals():
    _STYLE_RULES.insert(0, ("switch_num", "danger"))
    _STYLE_RULES.insert(0, ("back_services", "danger"))

_MENU2 = [["📲 Get Number"], ["📊 Live Traffic", "🔐 2FA Auth"], ["💰 Balance", "💸 Withdraw"],
          ["🏆 Leaderboard", "💎 Premium"], ["🆘 Support", "📜 History"]]
_ST2 = {"📲 Get Number": "primary", "📊 Live Traffic": "primary", "🔐 2FA Auth": "primary",
        "💰 Balance": "success", "💸 Withdraw": "success", "🏆 Leaderboard": "primary",
        "💎 Premium": "danger", "🆘 Support": "success", "📜 History": "primary",
        "🛠 ADMIN": "danger"}


def _kb2(rows):
    return ReplyKeyboardMarkup(
        [[_KB2(t, api_kwargs={"style": _ST2.get(t, "primary")}) for t in r] for r in rows],
        resize_keyboard=True)


MENU = _kb2(_MENU2)
MENU_ADMIN = _kb2(_MENU2 + [["🛠 ADMIN"]])
MENU_LABELS = {t for r in _MENU2 for t in r} | {"🛠 ADMIN"}


# ------------------------------------------------------------------ 2FA (TOTP)
def totp_now(secret, digits=6, step=30):
    sec = re.sub(r"[\s\-]", "", secret).upper()
    sec += "=" * (-len(sec) % 8)
    key = base64.b32decode(sec, casefold=True)
    t = time.time()
    h = hmac.new(key, struct.pack(">Q", int(t // step)), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits), step - int(t) % step


def _2fa_text(code, left):
    return (f"{BRAND}\n{LINE}\n🔐 <b>2FA CODE</b>\n\n"
            f"🔑 <code>{code}</code>\n⏳ Expires in <b>{left}s</b>\n\n<i>Tap the code to copy</i>")


_2FA_KB = Markup([[Btn("🔄 Refresh", callback_data="2fa:r")]])


async def start_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    context.user_data["await"] = "2fa"
    await update.effective_message.reply_text(
        f"{BRAND}\n{LINE}\n🔐 <b>2FA AUTH</b>\n\n"
        "Send your 2FA secret key (the Base32 text, for example <code>JBSWY3DPEHPK3PXP</code>).\n"
        "I will reply with the 6-digit code and try to delete your message.",
        parse_mode=ParseMode.HTML)


async def on_text_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await") != "2fa":
        return
    text = (update.message.text or "").strip()
    context.user_data.pop("await", None)
    if text in MENU_LABELS:       # user pressed another menu button instead
        return
    try:
        code, left = totp_now(text)
    except Exception:
        await update.message.reply_text("❌ That is not a valid secret key. Open 🔐 2FA Auth and try again.")
        raise ApplicationHandlerStop
    context.user_data["2fa_secret"] = text      # memory only, never saved to disk
    try:
        await update.message.delete()
    except Exception:
        pass
    await context.bot.send_message(update.effective_chat.id, _2fa_text(code, left),
                                   reply_markup=_2FA_KB, parse_mode=ParseMode.HTML)
    raise ApplicationHandlerStop


async def on_2fa_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    secret = context.user_data.get("2fa_secret")
    if not secret:
        return await cq.answer("Send your key again from 🔐 2FA Auth.", show_alert=True)
    code, left = totp_now(secret)
    await cq.answer()
    await safe_edit(context.bot, cq.message.chat_id, cq.message.message_id,
                    _2fa_text(code, left), _2FA_KB)


# ------------------------------------------------- leaderboard / support / traffic
def _mask_name(n):
    n = (n or "User").strip()
    return (n[:3] if len(n) > 3 else n[:1]) + "***"


async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await ensure_user(update)
    if not u:
        return
    rows = q("SELECT name, otp_count FROM users WHERE otp_count>0 ORDER BY otp_count DESC LIMIT 10")
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = [f"{medals[i]} {html.escape(_mask_name(r['name']))} · <b>{r['otp_count']}</b> OTP"
             for i, r in enumerate(rows)] or ["No OTPs yet. Be the first!"]
    rank = q("SELECT COUNT(*) c FROM users WHERE otp_count>?", (u["otp_count"],), one=True)["c"] + 1
    await update.effective_message.reply_text(
        f"{BRAND}\n{LINE}\n🏆 <b>LEADERBOARD</b>\n\n" + "\n".join(lines) +
        f"\n{LINE}\n👤 Your rank: <b>#{rank}</b> · {u['otp_count']} OTP",
        parse_mode=ParseMode.HTML)


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    kb = Markup([[Btn("💬 Contact support", url="https://t.me/" + SUPPORT.lstrip("@"))]])
    await update.effective_message.reply_text(
        f"{BRAND}\n{LINE}\n🆘 <b>SUPPORT</b>\n\nNeed help, a withdrawal or Premium?\nMessage {SUPPORT}",
        reply_markup=kb, parse_mode=ParseMode.HTML)


async def show_live_traffic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    text = ""
    if "live_text" in globals():
        try:
            text = live_text(await fetch_ranges())
        except Exception as e:
            log.warning("live traffic failed: %r", e)
    recent = q("SELECT service, country, number FROM numbers WHERE status='done' ORDER BY id DESC LIMIT 5")
    if recent:
        text += "\n\n🧾 <b>Recent OTPs</b>\n" + "\n".join(
            f"✅ {html.escape(r['service'] or '?')} · {html.escape(r['country'] or '?')} · +{mask(r['number'])}"
            for r in recent)
    await update.effective_message.reply_text(text or "No live data right now.",
                                              parse_mode=ParseMode.HTML)


# ------------------------------------------------------------ number card system
def card_text(s, timed_out=False):
    got, total = len(s["codes"]), len(s["numbers"])
    if timed_out and not got:
        status = (f"⏰ <b>Timed out</b> - no code in {POLL_TIMEOUT // 60} min.\n"
                  "<i>Tap CHANGE NUMBER to try another.</i>")
    elif got:
        status = f"✅ <b>OTP received</b> ({got}/{total} numbers)"
        if got < total and not timed_out:
            status += "\n🕒 <i>Still waiting on the others…</i>"
    else:
        status = "🕒 <b>Waiting for OTP........</b>"
    return (f"{BRAND}\n{LINE}\n{s['title']}\n🛠 Service: <b>{html.escape(s['service'])}</b>\n"
            f"{LINE}\n{status}")


def card_kb(s, use_copy=True):
    rows = []
    for n in s["numbers"]:
        code = s["codes"].get(n)
        txt = code or ("+" + n)
        label = f"✅ {code} · +{n}" if code else f"📋 +{n}"
        btn = None
        if use_copy:
            try:
                btn = Btn(label, api_kwargs={"copy_text": {"text": txt}, "style": "success"})
            except Exception:
                btn = None
        if btn is None:
            btn = Btn(label, callback_data=f"cpn:{txt}", api_kwargs={"style": "success"})
        rows.append([btn])
    row2 = [Btn("🌍 COUNTRY", callback_data="switch_country")]
    if OTP_GROUP_URL:
        row2.append(Btn("✉️ VIEW OTP", url=OTP_GROUP_URL))
    rows.append(row2)
    rows.append([Btn("🔄 CHANGE NUMBER", callback_data="switch_num")])
    return Markup(rows)


async def edit_card(app, s, timed_out=False):
    global COPY_OK
    text = card_text(s, timed_out)
    for use_copy in ((True, False) if COPY_OK else (False,)):
        try:
            await app.bot.edit_message_text(
                text, chat_id=s["chat"], message_id=s["msg"],
                reply_markup=card_kb(s, use_copy), parse_mode=ParseMode.HTML)
            return
        except BadRequest as e:
            msg = str(e).lower()
            if "not modified" in msg:
                return
            log.warning("card edit failed (copy=%s): %s", use_copy, e)
            if use_copy and ("button" in msg or "markup" in msg or "copy" in msg):
                COPY_OK = False          # this Telegram/PTB build cannot do copy buttons
            else:
                return
        except Exception as e:
            log.warning("card edit error: %r", e)
            return


def _mask_msg(text):
    return re.sub(r"\d{4,}", lambda m: m.group()[:2] + "*" * (len(m.group()) - 2), text)


async def group_post(app, s, number, otp, full):
    if not OTP_GROUP_ID:
        return
    show = S("group_show_code") == "1"
    msg = (full or f"{s['service']} code: {otp}")
    if not show:
        msg = _mask_msg(msg)
    iso = (country_of(number) or ("", "", ""))[0] if "country_of" in globals() else ""
    flag = (country_of(number) or ("", "🌍", ""))[1] if "country_of" in globals() else "🌍"
    icon = svc_icon(s["service"]) if "svc_icon" in globals() else "📲"
    masked = f"{number[:3]}●NV●{number[-4:]}"
    text = (f"🔥 <b>NOVA SMS VIEW</b> 🟢🟢🟢\n{flag} <b>{iso or '--'}</b> {icon} <code>{masked}</code>\n\n"
            f"💬💬💬\n<blockquote>{html.escape(msg[:300])}</blockquote>")
    rows = []
    if show:
        try:
            rows.append([Btn(f"📋 {otp}", api_kwargs={"copy_text": {"text": otp}, "style": "success"})])
        except Exception:
            pass
    rows.append([Btn("🤖 GET NUMBER", url=f"https://t.me/{app.bot.username}")])
    try:
        await app.bot.send_message(OTP_GROUP_ID, text, reply_markup=Markup(rows),
                                   parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("OTP group post failed: %r", e)


async def deliver_otp(app, s, number, res):
    otp = str(res["otp"])
    full = str(res.get("full_msg") or "")
    nid = s["ids"][number]
    first = x("UPDATE numbers SET status='done', otp=? WHERE id=? AND status='waiting'",
              (otp, nid)).rowcount
    if not first:
        x("UPDATE numbers SET otp=? WHERE id=?", (otp, nid))
    s["codes"][number] = otp
    user = q("SELECT * FROM users WHERE id=?", (s["uid"],), one=True)
    reward = float(S("reward_premium" if is_premium(user) else "reward_free"))
    x("UPDATE users SET balance=balance+?, otp_count=otp_count+1 WHERE id=?", (reward, s["uid"]))
    await edit_card(app, s)
    big = big_digits(otp) if "big_digits" in globals() else otp
    push = (f"🔔 <b>OTP RECEIVED</b>\n📞 <code>+{number}</code>\n"
            f"🛠 {html.escape(s['service'])}\n\n🔑 {big}\n\n<code>{html.escape(otp)}</code>\n"
            f"💰 +{reward:g} {S('currency')}")
    kb = None
    try:
        kb = Markup([[Btn(f"📋 {otp}", api_kwargs={"copy_text": {"text": otp}, "style": "success"})]])
    except Exception:
        pass
    try:
        await app.bot.send_message(s["chat"], push, reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest:
        await app.bot.send_message(s["chat"], push, parse_mode=ParseMode.HTML)
    await group_post(app, s, number, otp, full)


async def _check_one(app, n):
    man = app.bot_data.get("manual_otp", {}).pop(n, None)
    return man or await check_otp(n)


async def poll_many(app, s):
    waited = 0
    try:
        while waited < POLL_TIMEOUT:
            results = await asyncio.gather(*[_check_one(app, n) for n in s["numbers"]],
                                           return_exceptions=True)
            for n, res in zip(s["numbers"], results):
                if isinstance(res, Exception):
                    log.warning("poll error %s: %r", n, res)
                    continue
                if res.get("status") == "success" and res.get("otp") and \
                        s["codes"].get(n) != str(res["otp"]):
                    await deliver_otp(app, s, n, res)
            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
        for nid in s["ids"].values():
            x("UPDATE numbers SET status='expired' WHERE id=? AND status='waiting'", (nid,))
        await edit_card(app, s, timed_out=True)
    except asyncio.CancelledError:
        for nid in s["ids"].values():
            x("UPDATE numbers SET status='cancelled' WHERE id=? AND status='waiting'", (nid,))
        raise
    finally:
        if app.bot_data.get("tasks", {}).get(s["uid"]) is asyncio.current_task():
            app.bot_data["tasks"].pop(s["uid"], None)


async def _alloc_one(rid, context):
    if "allocate_for" in globals():
        return await allocate_for(rid, context)
    return await allocate_any(rid)


async def on_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = await ensure_user(update)
    if not user:
        return
    ud = context.user_data
    if (not ud.get("service") or not ud.get("services")) and "restore_state" in globals():
        await restore_state(user["id"], context)
    rid = ud.pop("_force_rid", None) or cq.data.split(":", 1)[1]
    ud["rid"] = rid
    if "save_state" in globals():
        save_state(user["id"], rid=rid, service=ud.get("service"))

    limit = int(float(S("daily_premium" if is_premium(user) else "daily_free")))
    cnt = q("SELECT COUNT(*) c FROM numbers WHERE user_id=? AND created>=?",
            (user["id"], today_start()), one=True)["c"]
    if (cnt + NUMS_PER_REQUEST - 1) // NUMS_PER_REQUEST >= limit:
        return await cq.answer(f"Daily limit reached ({limit}). Upgrade to Premium 💎", show_alert=True)

    await cq.answer("Allocating numbers…")
    got, last = [], None
    for _ in range(NUMS_PER_REQUEST + 2):
        if len(got) >= NUMS_PER_REQUEST:
            break
        try:
            data = await _alloc_one(rid, context)
        except Exception as e:
            log.warning("allocation error: %r", e)
            last = {"error": repr(e)}
            break
        last = data
        if data and data.get("status") == "success" and data.get("number") \
                and str(data["number"]) not in [g[0] for g in got]:
            got.append((str(data["number"]), data.get("country", "Unknown")))
    if not got:
        return await cq.message.reply_text(f"⚠️ No number available right now.\n{last}")

    tasks = context.application.bot_data.setdefault("tasks", {})
    old = tasks.pop(user["id"], None)
    if old and not old.done():
        old.cancel()

    service = ud.get("service", "?")
    ids = {}
    for n, c in got:
        ids[n] = xid("INSERT INTO numbers(user_id,number,rid,service,country,status,created) "
                      "VALUES(?,?,?,?,?,'waiting',?)", (user["id"], n, rid, service, c, now()))
    info = country_of(got[0][0]) if "country_of" in globals() else None
    title = (f"{info[1]} <b>{html.escape(info[2])} ({info[0]})</b> Allocated ✅" if info
             else f"🌍 <b>{html.escape(got[0][1])}</b> Allocated ✅")
    sess = {"uid": user["id"], "chat": cq.message.chat_id, "msg": cq.message.message_id,
            "service": service, "title": title, "numbers": [n for n, _ in got],
            "ids": ids, "codes": {}}
    await edit_card(context.application, sess)
    tasks[user["id"]] = context.application.create_task(poll_many(context.application, sess))


async def on_copy_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    txt = cq.data.split(":", 1)[1]
    await cq.answer("Tap the text below to copy")
    await context.bot.send_message(cq.message.chat_id, f"<code>{html.escape(txt)}</code>",
                                   parse_mode=ParseMode.HTML)


def _stop_after(fn):
    async def inner(update, context):
        await fn(update, context)
        raise ApplicationHandlerStop
    return inner


async def post_init(app: Application):
    global http
    http = httpx.AsyncClient(timeout=30)
    if OTP_GROUP_ID:
        app.bot_data['live_task'] = asyncio.create_task(live_ranges_loop(app))
    if OTP_GROUP_ID:
        app.bot_data['feed_task'] = asyncio.create_task(feed_loop(app))


async def post_shutdown(app: Application):
    ft = app.bot_data.get('feed_task')
    if ft:
        ft.cancel()
    lt = app.bot_data.get('live_task')
    if lt:
        lt.cancel()
    if http is not None:
        await http.aclose()


def main():
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())

    app.add_handler(CommandHandler("start", cmd_start))
    for name, fn in [("admin", cmd_admin), ("stats", cmd_stats), ("pending", cmd_pending),
                     ("broadcast", cmd_broadcast), ("premium", cmd_premium),
                     ("unpremium", cmd_unpremium), ("ban", cmd_ban), ("unban", cmd_unban),
                     ("addbal", cmd_addbal), ("set", cmd_set), ("debug", cmd_debug)]:
        app.add_handler(CommandHandler(name, fn, filters=admin_only))

    for pattern, fn in [("^📲 NUMBERS$", show_services), ("^💰 BALANCE$", show_balance),
                        ("^📜 HISTORY$", show_history), ("^💸 WITHDRAW$", start_withdraw),
                        ("^📊 LIVE TRAFFIC$", show_traffic), ("^💎 PREMIUM$", show_premium)]:
        app.add_handler(MessageHandler(filters.Regex(pattern), fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_handler(CallbackQueryHandler(on_service, pattern=r"^svc:\d+$"))
    app.add_handler(CallbackQueryHandler(on_range, pattern=r"^rid:"))
    app.add_handler(CallbackQueryHandler(on_switch_num, pattern=r"^switch_num$"))
    app.add_handler(CallbackQueryHandler(on_switch_country, pattern=r"^switch_country$"))
    app.add_handler(CallbackQueryHandler(on_back_services, pattern=r"^back_services$"))
    app.add_handler(CallbackQueryHandler(on_withdraw_action, pattern=r"^wd:"))
    app.add_handler(CallbackQueryHandler(noop, pattern=r"^noop$"))

    app.add_handler(MessageHandler(admin_only & filters.TEXT & ~filters.COMMAND, on_admin_text), group=-1)
    app.add_handler(MessageHandler(admin_only & filters.Regex("^🛠 ADMIN$"), cmd_admin), group=-2)
    app.add_handler(CallbackQueryHandler(on_admin, pattern=r"^adm:"))

    app.add_handler(CallbackQueryHandler(on_joined, pattern=r"^joined$"))
    app.add_handler(CommandHandler("trafficraw", cmd_trafficraw, filters=admin_only))

    app.add_handler(CommandHandler("livepost", cmd_livepost, filters=admin_only))

    for _pat, _fn in [("^📲 Get Number$", show_services), ("^📊 Live Traffic$", show_live_traffic),
                      ("^🔐 2FA Auth$", start_2fa), ("^💰 Balance$", show_balance),
                      ("^💸 Withdraw$", start_withdraw), ("^🏆 Leaderboard$", show_leaderboard),
                      ("^💎 Premium$", show_premium), ("^🆘 Support$", show_support),
                      ("^📜 History$", show_history)]:
        app.add_handler(MessageHandler(filters.Regex(_pat), _stop_after(_fn)), group=-2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_2fa), group=-3)
    app.add_handler(CallbackQueryHandler(on_2fa_refresh, pattern=r"^2fa:r$"))
    app.add_handler(CallbackQueryHandler(on_copy_number, pattern=r"^cpn:"))

    log.info("Bot started")
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    port = int(os.environ.get("PORT", "10000"))
    if not render_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="telegram",
        webhook_url=f"{render_url}/telegram",
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
