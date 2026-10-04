import os
import re
import html
import time
import sqlite3
import threading
from datetime import datetime, timedelta

import requests
import telebot
from telebot import types


# =========================================================
# CONFIG  (лучше задавать через переменные окружения)
# =========================================================

TELEGRAM_TOKEN = os.getenv("8916246189:AAEmOYvMAzjMVhztxMZFNjp2yZzh9avsD0E", "8916246189:AAEmOYvMAzjMVhztxMZFNjp2yZzh9avsD0E")

CLEAN_BASE_URL = "https://www.cleanapis.com/v1"

CLEAN_API_KEY = "cc_HZtzt78aXbrof6G2PD1KXKYTQdf86mr4CHhqRaz7X2dz1gvS"

DB_FILE = "ai_bot.db"

# Свои Telegram ID (через запятую в ADMIN_IDS или впиши сюда)
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "7305115067,7103503850").split(",") if x.strip()
}

DEFAULT_MODEL = "gpt-5.6-luna"

FREE_DAILY_LIMIT = 30
PREMIUM_DAILY_LIMIT = 500

FREE_MEMORY = 20
PREMIUM_MEMORY = 40
MEMORY_KEEP = 50

MODEL_PAGE_SIZE = 8

# =========================================================
# TARIFFS (Telegram Stars)
# =========================================================

FOREVER_DAYS = 36500  # ~100 лет

PLANS = {
    "7d": {
        "title": "👑 Premium 7 дней",
        "days": 7,
        "stars": 15,
        "button": "⭐ 7 дней — 15 звёзд",
    },
    "30d": {
        "title": "👑 Premium 1 месяц",
        "days": 30,
        "stars": 50,
        "button": "🌟 1 месяц — 50 звёзд",
    },
    "forever": {
        "title": "👑 Premium навсегда",
        "days": FOREVER_DAYS,
        "stars": 150,
        "button": "👑 НАВСЕГДА — 150 звёзд",
    },
}

PLAN_DESCRIPTION = (
    f"{PREMIUM_DAILY_LIMIT} сообщений в день, доступ ко всем моделям, "
    f"увеличенная память диалога."
)


# =========================================================
# BOT
# =========================================================

if not TELEGRAM_TOKEN:
    raise SystemExit("Задай TELEGRAM_TOKEN (переменная окружения или в коде).")

bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    parse_mode="HTML",
    threaded=True,
    num_threads=20,   # по умолчанию 2 — долгие запросы к AI блокировали бы бота
)


# =========================================================
# DATABASE
# =========================================================

db_lock = threading.Lock()


def get_db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def init_db():
    with db_lock:
        db = get_db()
        cur = db.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                model TEXT,
                premium_until TEXT,
                messages_today INTEGER DEFAULT 0,
                last_day TEXT,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan TEXT,
                stars INTEGER,
                charge_id TEXT UNIQUE,
                created_at TEXT
            )
        """)

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id)"
        )

        db.commit()
        db.close()


init_db()


# =========================================================
# USER
# =========================================================

def ensure_user(user):
    today = datetime.now().strftime("%Y-%m-%d")

    with db_lock:
        db = get_db()
        cur = db.cursor()

        cur.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
        exists = cur.fetchone()

        if not exists:
            cur.execute("""
                INSERT INTO users
                (user_id, username, first_name, model, premium_until,
                 messages_today, last_day, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user.id,
                user.username or "",
                user.first_name or "",
                DEFAULT_MODEL,
                "",
                0,
                today,
                datetime.now().isoformat(),
            ))
        else:
            cur.execute("""
                UPDATE users SET username=?, first_name=? WHERE user_id=?
            """, (user.username or "", user.first_name or "", user.id))

        db.commit()
        db.close()


def get_user(user_id):
    with db_lock:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        db.close()
        return row


# =========================================================
# PREMIUM
# =========================================================

def get_premium_until(user_id):
    row = get_user(user_id)

    if not row or not row[4]:
        return None

    try:
        return datetime.fromisoformat(row[4])
    except ValueError:
        return None


def is_premium(user_id):
    until = get_premium_until(user_id)
    return bool(until and until > datetime.now())


def format_until(until):
    if until.year >= datetime.now().year + 90:
        return "навсегда"
    return until.strftime("%d.%m.%Y %H:%M")


def give_premium(user_id, days):
    now = datetime.now()
    current = get_premium_until(user_id)

    if current and current > now:
        new_date = current + timedelta(days=days)
    else:
        new_date = now + timedelta(days=days)

    with db_lock:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "UPDATE users SET premium_until=? WHERE user_id=?",
            (new_date.isoformat(), user_id),
        )
        db.commit()
        db.close()

    return new_date


def remove_premium(user_id):
    with db_lock:
        db = get_db()
        cur = db.cursor()
        cur.execute("UPDATE users SET premium_until='' WHERE user_id=?", (user_id,))
        db.commit()
        db.close()


# =========================================================
# DAILY LIMIT
# =========================================================

def reset_daily_if_needed(user_id):
    today = datetime.now().strftime("%Y-%m-%d")

    with db_lock:
        db = get_db()
        cur = db.cursor()

        cur.execute("SELECT last_day FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()

        if row and row[0] != today:
            cur.execute("""
                UPDATE users SET messages_today=0, last_day=? WHERE user_id=?
            """, (today, user_id))
            db.commit()

        db.close()


def can_use_ai(user_id):
    reset_daily_if_needed(user_id)

    row = get_user(user_id)

    if not row:
        return False, 0, 0

    used = row[5] or 0
    limit = PREMIUM_DAILY_LIMIT if is_premium(user_id) else FREE_DAILY_LIMIT

    return used < limit, used, limit


def increment_messages(user_id):
    with db_lock:
        db = get_db()
        cur = db.cursor()
        # FIX: раньше было (user_id) без запятой -> ошибка sqlite
        cur.execute(
            "UPDATE users SET messages_today = messages_today + 1 WHERE user_id=?",
            (user_id,),
        )
        db.commit()
        db.close()


# =========================================================
# MODELS
# =========================================================

models_cache = []
models_cache_time = 0


def load_models():
    global models_cache, models_cache_time

    if models_cache and time.time() - models_cache_time < 600:
        return models_cache

    try:
        response = requests.get(
            CLEAN_BASE_URL + "/models",
            headers={"Authorization": f"Bearer {CLEAN_API_KEY}"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

        models = []
        for item in data.get("data", []):
            model_id = item.get("id")
            if model_id:
                models.append(model_id)

        models = list(dict.fromkeys(models))

        if models:
            models_cache = models
            models_cache_time = time.time()

        return models or models_cache

    except Exception as e:
        print("MODEL ERROR:", e)
        return models_cache  # при ошибке отдаём старый кэш, если он есть


def get_model(user_id):
    row = get_user(user_id)
    if row and row[3]:
        return row[3]
    return DEFAULT_MODEL


def set_model(user_id, model):
    with db_lock:
        db = get_db()
        cur = db.cursor()
        cur.execute("UPDATE users SET model=? WHERE user_id=?", (model, user_id))
        db.commit()
        db.close()


# =========================================================
# MEMORY
# =========================================================

def save_message(user_id, role, content):
    with db_lock:
        db = get_db()
        cur = db.cursor()

        cur.execute("""
            INSERT INTO messages (user_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, role, content, datetime.now().isoformat()))

        cur.execute("""
            DELETE FROM messages
            WHERE user_id=?
            AND id NOT IN (
                SELECT id FROM messages
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
            )
        """, (user_id, user_id, MEMORY_KEEP))

        db.commit()
        db.close()


def get_memory(user_id):
    limit = PREMIUM_MEMORY if is_premium(user_id) else FREE_MEMORY

    with db_lock:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            SELECT role, content FROM messages
            WHERE user_id=? ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        rows = cur.fetchall()
        db.close()

    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]


def clear_memory(user_id):
    with db_lock:
        db = get_db()
        cur = db.cursor()
        cur.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        db.commit()
        db.close()


# =========================================================
# TEXT HELPERS (Markdown от AI -> безопасный Telegram HTML)
# =========================================================

def md_to_html(text):
    parts = re.split(r"(```.*?```)", text, flags=re.S)
    out = []

    for p in parts:
        if len(p) >= 6 and p.startswith("```") and p.endswith("```"):
            body = p[3:-3]
            body = re.sub(r"^[A-Za-z0-9_+#.-]*\n", "", body, count=1)
            out.append(f"<pre>{html.escape(body.strip(), quote=False)}</pre>")
        else:
            s = html.escape(p, quote=False)
            s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
            s = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", s, flags=re.M)
            s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.S)
            out.append(s)

    return "".join(out)


def split_text(text, limit=3500):
    chunks = []

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")

    if text:
        chunks.append(text)

    # не ломаем блоки кода на границе сообщений
    fixed = []
    open_fence = False

    for c in chunks:
        if open_fence:
            c = "```\n" + c
        if c.count("```") % 2 == 1:
            c += "\n```"
            open_fence = True
        else:
            open_fence = False
        fixed.append(c)

    return fixed


def send_ai_answer(chat_id, text):
    for chunk in split_text(text):
        try:
            bot.send_message(chat_id, md_to_html(chunk), parse_mode="HTML")
        except Exception as e:
            print("HTML send failed, fallback to plain:", e)
            bot.send_message(chat_id, chunk, parse_mode=None)


# =========================================================
# CLEAN APIs
# =========================================================

SYSTEM_PROMPT = (
    "Ты современный Telegram AI-ассистент. "
    "Отвечай понятно, полезно и по существу. "
    "Отвечай на языке пользователя. "
    "Если пользователь просит код, давай рабочий код."
)


def ask_ai(user_id, text):
    model = get_model(user_id)
    memory = get_memory(user_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(memory)
    messages.append({"role": "user", "content": text})

    response = requests.post(
        CLEAN_BASE_URL + "/chat/completions",
        headers={
            "Authorization": f"Bearer {CLEAN_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "max_tokens": 8192,
            "temperature": 0.7,
        },
        timeout=180,
    )

    if response.status_code != 200:
        api_message = None
        try:
            api_message = (response.json().get("error") or {}).get("message")
        except ValueError:
            pass

        raise Exception(
            api_message
            or f"API HTTP {response.status_code}: {response.text[:500]}"
        )

    data = response.json()

    try:
        answer = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise Exception("Некорректный ответ API")

    return answer.strip() or "Модель не вернула текстовый ответ."


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🤖 Модель", "🧠 Память")
    kb.row("👑 Premium", "📊 Профиль")

    if user_id in ADMIN_IDS:
        kb.row("🛠 Админ-панель")

    return kb


def model_keyboard(page=0):
    models = load_models()
    kb = types.InlineKeyboardMarkup()

    start = page * MODEL_PAGE_SIZE
    end = start + MODEL_PAGE_SIZE

    # callback_data в Telegram максимум 64 байта, поэтому передаём индекс,
    # а не длинное имя модели
    for i, model in enumerate(models[start:end], start=start):
        kb.add(types.InlineKeyboardButton(text=model, callback_data=f"model:{i}"))

    navigation = []

    if page > 0:
        navigation.append(
            types.InlineKeyboardButton("⬅️", callback_data=f"models_page:{page - 1}")
        )

    if end < len(models):
        navigation.append(
            types.InlineKeyboardButton("➡️", callback_data=f"models_page:{page + 1}")
        )

    if navigation:
        kb.row(*navigation)

    return kb


def premium_keyboard():
    kb = types.InlineKeyboardMarkup()
    for key, plan in PLANS.items():
        kb.add(types.InlineKeyboardButton(plan["button"], callback_data=f"buy:{key}"))
    return kb


# =========================================================
# START
# =========================================================

@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message.from_user)

    status = "👑 Premium" if is_premium(message.from_user.id) else "🆓 Free"
    name = html.escape(message.from_user.first_name or "друг")

    text = f"""
<b>🤖 AI BOT</b>

Добро пожаловать, <b>{name}</b>!

Статус: {status}
Модель: <code>{html.escape(get_model(message.from_user.id))}</code>

<b>Команды:</b>

/model — выбрать модель
/clear — очистить память
/profile — профиль
/premium — купить Premium

Просто отправь сообщение — и AI ответит.
"""

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=main_keyboard(message.from_user.id),
    )


# =========================================================
# MODEL
# =========================================================

@bot.message_handler(commands=["model"])
def model_command(message):
    ensure_user(message.from_user)

    models = load_models()

    if not models:
        bot.reply_to(message, "❌ Не удалось получить список моделей Clean APIs.")
        return

    bot.send_message(
        message.chat.id,
        f"🤖 <b>Выбор модели</b>\n\n"
        f"Доступно моделей: <b>{len(models)}</b>\n\n"
        f"Текущая: <code>{html.escape(get_model(message.from_user.id))}</code>",
        reply_markup=model_keyboard(0),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("model:"))
def model_callback(call):
    ensure_user(call.from_user)

    models = load_models()

    try:
        model = models[int(call.data.split(":", 1)[1])]
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "Список моделей обновился, открой /model заново", show_alert=True
        )
        return

    set_model(call.from_user.id, model)
    bot.answer_callback_query(call.id, "Модель изменена!")

    try:
        bot.edit_message_text(
            f"✅ Модель установлена:\n\n<code>{html.escape(model)}</code>",
            call.message.chat.id,
            call.message.message_id,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("models_page:"))
def models_page(call):
    try:
        page = int(call.data.split(":")[1])
    except ValueError:
        bot.answer_callback_query(call.id)
        return

    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=model_keyboard(page),
        )
    except Exception as e:
        print("PAGE ERROR:", e)

    bot.answer_callback_query(call.id)


# =========================================================
# CLEAR / MEMORY / PROFILE
# =========================================================

@bot.message_handler(commands=["clear"])
def clear_command(message):
    ensure_user(message.from_user)
    clear_memory(message.from_user.id)
    bot.reply_to(message, "🧹 Память диалога очищена.")


@bot.message_handler(commands=["profile"])
def profile(message):
    ensure_user(message.from_user)
    uid = message.from_user.id

    row = get_user(uid)

    if is_premium(uid):
        until = get_premium_until(uid)
        premium_text = f"👑 Premium\nДо: <code>{format_until(until)}</code>"
        limit = PREMIUM_DAILY_LIMIT
    else:
        premium_text = "🆓 Free"
        limit = FREE_DAILY_LIMIT

    reset_daily_if_needed(uid)
    row = get_user(uid)
    used = row[5] or 0

    text = f"""
<b>👤 Профиль</b>

ID: <code>{uid}</code>

Статус:
{premium_text}

🤖 Модель:
<code>{html.escape(get_model(uid))}</code>

📊 Использовано сегодня:
<b>{used}/{limit}</b>
"""

    bot.send_message(message.chat.id, text)


# =========================================================
# PREMIUM + PAYMENTS (Telegram Stars)
# =========================================================

@bot.message_handler(commands=["premium"])
def premium_command(message):
    ensure_user(message.from_user)
    uid = message.from_user.id

    if is_premium(uid):
        until = get_premium_until(uid)
        status = (
            f"✅ <b>У тебя уже есть Premium</b>\n"
            f"Действует до: <code>{format_until(until)}</code>\n"
            f"Новая покупка продлит срок.\n\n"
        )
    else:
        status = f"Сейчас у тебя: 🆓 Free — {FREE_DAILY_LIMIT} сообщений/день\n\n"

    text = (
        "👑 <b>Premium</b>\n\n"
        f"{status}"
        "Premium даёт:\n\n"
        f"• {PREMIUM_DAILY_LIMIT} сообщений/день\n"
        "• доступ ко всем моделям\n"
        f"• увеличенную память ({PREMIUM_MEMORY} сообщений)\n\n"
        "Выбирай тариф ниже 👇"
    )

    bot.send_message(message.chat.id, text, reply_markup=premium_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy:"))
def buy_callback(call):
    key = call.data.split(":", 1)[1]
    plan = PLANS.get(key)

    if not plan:
        bot.answer_callback_query(call.id, "Тариф не найден", show_alert=True)
        return

    ensure_user(call.from_user)
    bot.answer_callback_query(call.id)

    try:
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title=plan["title"],
            description=PLAN_DESCRIPTION,
            invoice_payload=f"premium:{key}:{call.from_user.id}",
            provider_token="",   # для Telegram Stars токен провайдера пустой
            currency="XTR",      # XTR = Telegram Stars
            prices=[types.LabeledPrice(label=plan["title"], amount=plan["stars"])],
        )
    except Exception as e:
        print("INVOICE ERROR:", repr(e))
        bot.send_message(
            call.message.chat.id,
            "❌ Не удалось создать счёт. Попробуй позже.",
        )


@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(query):
    ok = False

    try:
        prefix, key, uid = query.invoice_payload.split(":")
        plan = PLANS.get(key)
        ok = (
            prefix == "premium"
            and plan is not None
            and int(uid) == query.from_user.id
            and query.total_amount == plan["stars"]
            and query.currency == "XTR"
        )
    except Exception:
        ok = False

    if ok:
        bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        bot.answer_pre_checkout_query(
            query.id,
            ok=False,
            error_message="Не удалось проверить платёж. Попробуй ещё раз.",
        )


@bot.message_handler(content_types=["successful_payment"])
def successful_payment(message):
    pay = message.successful_payment
    uid = message.from_user.id

    ensure_user(message.from_user)

    try:
        _, key, _ = pay.invoice_payload.split(":")
        plan = PLANS[key]
    except Exception:
        print("PAYMENT BAD PAYLOAD:", pay.invoice_payload, pay.telegram_payment_charge_id)
        bot.send_message(
            message.chat.id,
            "⚠️ Платёж получен, но тариф не распознан. Напиши в поддержку: /paysupport",
        )
        return

    # защита от повторной обработки одного и того же платежа
    with db_lock:
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute("""
                INSERT INTO payments (user_id, plan, stars, charge_id, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                uid,
                key,
                pay.total_amount,
                pay.telegram_payment_charge_id,
                datetime.now().isoformat(),
            ))
            db.commit()
            duplicate = False
        except sqlite3.IntegrityError:
            duplicate = True
        db.close()

    if duplicate:
        return

    until = give_premium(uid, plan["days"])

    bot.send_message(
        message.chat.id,
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"{plan['title']}\n"
        f"Действует до: <code>{format_until(until)}</code>\n\n"
        f"Спасибо за поддержку! 💙",
    )

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"💰 Новая оплата: <code>{uid}</code> — {plan['title']} "
                f"({pay.total_amount}⭐)",
            )
        except Exception:
            pass


@bot.message_handler(commands=["paysupport"])
def paysupport(message):
    bot.send_message(
        message.chat.id,
        "💬 Проблемы с оплатой? Напиши администратору бота и укажи свой ID: "
        f"<code>{message.from_user.id}</code>",
    )


# =========================================================
# BUTTONS
# =========================================================

@bot.message_handler(func=lambda m: m.text == "🤖 Модель")
def model_button(message):
    model_command(message)


@bot.message_handler(func=lambda m: m.text == "🧠 Память")
def memory_button(message):
    ensure_user(message.from_user)
    memory = get_memory(message.from_user.id)

    bot.send_message(
        message.chat.id,
        f"🧠 В памяти сейчас <b>{len(memory)}</b> сообщений.\n\n/clear — очистить память.",
    )


@bot.message_handler(func=lambda m: m.text == "👑 Premium")
def premium_button(message):
    premium_command(message)


@bot.message_handler(func=lambda m: m.text == "📊 Профиль")
def profile_button(message):
    profile(message)


# =========================================================
# ADMIN
# =========================================================

def admin_only(message):
    return message.from_user.id in ADMIN_IDS


@bot.message_handler(func=lambda m: m.text == "🛠 Админ-панель")
def admin_panel(message):
    if not admin_only(message):
        bot.reply_to(message, "⛔ Нет доступа.")
        return

    text = """
🛠 <b>АДМИН-ПАНЕЛЬ</b>

Команды:

/users — количество пользователей
/stats — статистика
/givepremium ID DAYS — выдать Premium
/unpremium ID — убрать Premium
/refund ID CHARGE_ID — вернуть звёзды
/broadcast ТЕКСТ — рассылка

Пример:

<code>/givepremium 123456789 30</code>

<code>/broadcast Привет!</code>
"""

    bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["users"])
def users_command(message):
    if not admin_only(message):
        return

    now = datetime.now()

    with db_lock:
        db = get_db()
        cur = db.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]

        cur.execute("SELECT premium_until FROM users WHERE premium_until != ''")
        dates = [r[0] for r in cur.fetchall()]

        db.close()

    # считаем только активный Premium (раньше считались и истёкшие)
    active = 0
    for d in dates:
        try:
            if datetime.fromisoformat(d) > now:
                active += 1
        except ValueError:
            pass

    bot.send_message(
        message.chat.id,
        f"👥 <b>Пользователи</b>\n\nВсего: <b>{total}</b>\nPremium: <b>{active}</b>",
    )


@bot.message_handler(commands=["stats"])
def stats_command(message):
    if not admin_only(message):
        return

    with db_lock:
        db = get_db()
        cur = db.cursor()

        cur.execute("SELECT COUNT(*) FROM messages")
        msgs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM users")
        users = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM payments")
        pay_count, pay_stars = cur.fetchone()

        db.close()

    bot.send_message(
        message.chat.id,
        f"""
📊 <b>Статистика</b>

👥 Пользователей: <b>{users}</b>

💬 Сообщений в памяти:
<b>{msgs}</b>

💰 Платежей: <b>{pay_count}</b> ({pay_stars}⭐)

🤖 Моделей API:
<b>{len(load_models())}</b>
""",
    )


@bot.message_handler(commands=["givepremium"])
def give_premium_command(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 3:
        bot.reply_to(message, "Использование:\n<code>/givepremium USER_ID DAYS</code>")
        return

    try:
        user_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        bot.reply_to(message, "❌ ID и DAYS должны быть числами.")
        return

    if not get_user(user_id):
        bot.reply_to(message, "❌ Пользователь не найден.")
        return

    until = give_premium(user_id, days)

    bot.send_message(
        message.chat.id,
        f"""
👑 <b>Premium выдан</b>

User ID:
<code>{user_id}</code>

Дней:
<b>{days}</b>

До:
<code>{format_until(until)}</code>
""",
    )

    try:
        bot.send_message(
            user_id,
            f"👑 <b>Тебе выдан Premium!</b>\n\n"
            f"Действует до:\n<code>{format_until(until)}</code>",
        )
    except Exception:
        pass


@bot.message_handler(commands=["unpremium"])
def unpremium_command(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "<code>/unpremium USER_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ ID должен быть числом.")
        return

    remove_premium(user_id)
    bot.reply_to(message, f"✅ Premium отключён у <code>{user_id}</code>")


@bot.message_handler(commands=["refund"])
def refund_command(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 3:
        bot.reply_to(message, "<code>/refund USER_ID CHARGE_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ ID должен быть числом.")
        return

    try:
        bot.refund_star_payment(user_id, parts[2])
    except Exception as e:
        bot.reply_to(message, f"❌ Не удалось вернуть: <code>{html.escape(str(e)[:300])}</code>")
        return

    remove_premium(user_id)
    bot.reply_to(message, f"✅ Звёзды возвращены, Premium снят у <code>{user_id}</code>")


@bot.message_handler(commands=["broadcast"])
def broadcast(message):
    if not admin_only(message):
        return

    text = message.text[len("/broadcast"):].strip()

    if not text:
        bot.reply_to(message, "Напиши текст после /broadcast")
        return

    with db_lock:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT user_id FROM users")
        users = [row[0] for row in cur.fetchall()]
        db.close()

    sent = 0

    for user_id in users:
        try:
            # parse_mode=None, чтобы символы < > в тексте не ломали отправку
            bot.send_message(user_id, text, parse_mode=None)
            sent += 1
            time.sleep(0.05)
        except Exception:
            pass

    bot.send_message(
        message.chat.id,
        f"📢 Рассылка завершена.\nОтправлено: <b>{sent}</b> из <b>{len(users)}</b>",
    )


# =========================================================
# AI MESSAGE
# =========================================================

busy_users = set()
busy_lock = threading.Lock()


@bot.message_handler(content_types=["text"])
def ai_message(message):
    if message.text.startswith("/"):
        return

    ensure_user(message.from_user)
    uid = message.from_user.id

    allowed, used, limit = can_use_ai(uid)

    if not allowed:
        if is_premium(uid):
            bot.reply_to(message, "⛔ Ты достиг дневного Premium-лимита.")
        else:
            bot.reply_to(
                message,
                f"⛔ Дневной лимит исчерпан: <b>{limit}</b> сообщений.\n\n"
                f"👑 Premium увеличивает лимит — /premium",
            )
        return

    # один запрос за раз от одного пользователя (защита от спама и обхода лимита)
    with busy_lock:
        if uid in busy_users:
            bot.reply_to(message, "⏳ Подожди, я ещё отвечаю на предыдущее сообщение.")
            return
        busy_users.add(uid)

    wait = bot.reply_to(message, "🧠 Думаю...")

    try:
        answer = ask_ai(uid, message.text)

        save_message(uid, "user", message.text)
        save_message(uid, "assistant", answer)
        increment_messages(uid)

        try:
            bot.delete_message(message.chat.id, wait.message_id)
        except Exception:
            pass

        send_ai_answer(message.chat.id, answer)

    except Exception as e:
        print("AI ERROR:", repr(e))

        try:
            bot.edit_message_text(
                "❌ <b>Ошибка AI</b>\n\n"
                f"<code>{html.escape(str(e)[:1000])}</code>",
                message.chat.id,
                wait.message_id,
            )
        except Exception:
            bot.send_message(
                message.chat.id,
                "❌ Произошла ошибка при запросе к AI.",
            )

    finally:
        with busy_lock:
            busy_users.discard(uid)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    print("===================================")
    print("        AI TELEGRAM BOT")
    print("===================================")
    print("Clean APIs:", CLEAN_BASE_URL)
    print("Loading models...")

    loaded = load_models()
    print("Models loaded:", len(loaded))

    for m in loaded[:10]:
        print("-", m)

    print("Bot started!")

    bot.infinity_polling(
        skip_pending=True,
        timeout=60,
        long_polling_timeout=60,
        allowed_updates=[
            "message",
            "callback_query",
            "pre_checkout_query",
        ],
    )