import asyncio
import asyncio
import csv
import io
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# =====================================================================
# НАЛАШТУВАННЯ (Встав свій токен та свій числовий Telegram ID)
# =====================================================================
BOT_TOKEN = "8947468465:AAGqwbUgZkWE4EY_3oBa0mRl_u4-LniTYOo"
ADMIN_ID = 954547387  # Встав сюди свій Telegram ID (дізнатися в @userinfobot)

BASE_DIR = Path(__file__).resolve().parent
DB_NAME = str(BASE_DIR / "p2p_pln.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =====================================================================
# ТЕКСТ ПРИВІТАННЯ ТА ІНСТРУКЦІЇ
# =====================================================================
ONBOARDING_TEXT = (
    "🇵🇱 <b>Привіт, доступ підтверджено!</b>\n\n"
    "Це твій особистий P2P-трекер для PLN. Усі твої дані бачиш тільки ти.\n\n"
    "⚡️ <b>Як вносити угоди:</b>\n"
    "Тисни кнопки <b>🟢 Купівля / 🔴 Продаж</b> або пиши одразу в чат (сума в PLN та курс):\n"
    "• <code>+ 1000 3.985</code> — закуп\n"
    "• <code>- 800 4.04</code> — продаж\n\n"
    "🔄 <b>Кнопки меню:</b>\n"
    "• <b>📊 Поточний круг</b> — твій середній курс, спред і залишок USDT на руках.\n"
    "• <b>🏁 Закрити круг</b> — фіксує чистий профіт, а непроданий залишок USDT сам переноситься в наступний круг.\n"
    "• <b>⏪ Скасувати останню дію</b> — видаляє останню дію, якщо помилився цифрою.\n"
    "• <b>📈 Статистика</b> — твій заробіток за день/місяць та вигрузка в Excel.\n\n"
    "Пиши першу угоду в чат або тисни кнопки внизу! 🚀"
)

# =====================================================================
# СТАНИ (FSM)
# =====================================================================
class OrderState(StatesGroup):
    waiting_for_buy = State()
    waiting_for_sell = State()


class AdminState(StatesGroup):
    waiting_for_broadcast = State()


# =====================================================================
# РОБОТА З БАЗОЮ ДАНИХ (SQLite + WAL для одночасної роботи юзерів)
# =====================================================================
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                added_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                round_num INTEGER,
                status TEXT,
                start_time TEXT,
                end_time TEXT,
                buy_pln REAL DEFAULT 0,
                buy_usdt REAL DEFAULT 0,
                sell_pln REAL DEFAULT 0,
                sell_usdt REAL DEFAULT 0,
                avg_buy REAL DEFAULT 0,
                avg_sell REAL DEFAULT 0,
                matched_usdt REAL DEFAULT 0,
                profit_pln REAL DEFAULT 0,
                spread_pct REAL DEFAULT 0,
                carryover_usdt REAL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER,
                user_id INTEGER,
                type TEXT,
                pln REAL,
                rate REAL,
                usdt REAL,
                is_carryover INTEGER DEFAULT 0,
                timestamp TEXT
            )
        """)
        c.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, username, full_name, added_at) VALUES (?, 'admin', 'Адміністратор', ?)",
            (ADMIN_ID, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()


def is_user_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,))
        return c.fetchone() is not None


def get_or_create_active_round(user_id: int):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, round_num, start_time FROM rounds WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        row = c.fetchone()
        if row:
            return row[0], row[1], row[2]

        c.execute(
            "SELECT MAX(round_num) FROM rounds WHERE user_id = ?", (user_id,)
        )
        max_num = c.fetchone()[0]
        next_num = (max_num or 0) + 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        c.execute(
            "INSERT INTO rounds (user_id, round_num, status, start_time) VALUES (?, ?, 'active', ?)",
            (user_id, next_num, now),
        )
        conn.commit()
        return c.lastrowid, next_num, now


def add_order_to_db(
    user_id: int,
    order_type: str,
    pln: float,
    rate: float,
    is_carryover: int = 0,
):
    round_id, round_num, _ = get_or_create_active_round(user_id)
    usdt = pln / rate
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM orders WHERE round_id = ? AND is_carryover = 0",
            (round_id,),
        )
        if c.fetchone()[0] == 0 and is_carryover == 0:
            c.execute(
                "UPDATE rounds SET start_time = ? WHERE id = ?", (now, round_id)
            )

        c.execute(
            """
            INSERT INTO orders (round_id, user_id, type, pln, rate, usdt, is_carryover, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (round_id, user_id, order_type, pln, rate, usdt, is_carryover, now),
        )
        conn.commit()
    return round_id, round_num, usdt


def calculate_round_metrics(round_id: int):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT round_num, start_time FROM rounds WHERE id = ?", (round_id,)
        )
        r_info = c.fetchone()
        round_num, start_time = r_info[0], r_info[1]

        c.execute(
            "SELECT type, SUM(pln), SUM(usdt), COUNT(*) FROM orders WHERE round_id = ? GROUP BY type",
            (round_id,),
        )
        rows = c.fetchall()

    buy_pln = buy_usdt = sell_pln = sell_usdt = 0.0
    buy_count = sell_count = 0

    for r_type, sum_pln, sum_usdt, cnt in rows:
        if r_type == "BUY":
            buy_pln = sum_pln or 0.0
            buy_usdt = sum_usdt or 0.0
            buy_count = cnt
        elif r_type == "SELL":
            sell_pln = sum_pln or 0.0
            sell_usdt = sum_usdt or 0.0
            sell_count = cnt

    avg_buy = (buy_pln / buy_usdt) if buy_usdt > 0 else 0.0
    avg_sell = (sell_pln / sell_usdt) if sell_usdt > 0 else 0.0

    matched_usdt = min(buy_usdt, sell_usdt)
    remaining_usdt = buy_usdt - sell_usdt

    if matched_usdt > 0 and avg_buy > 0 and avg_sell > 0:
        profit_pln = matched_usdt * (avg_sell - avg_buy)
        spread_pct = ((avg_sell - avg_buy) / avg_buy) * 100
    else:
        profit_pln = 0.0
        spread_pct = 0.0

    return {
        "round_id": round_id,
        "round_num": round_num,
        "start_time": start_time,
        "buy_pln": buy_pln,
        "buy_usdt": buy_usdt,
        "buy_count": buy_count,
        "sell_pln": sell_pln,
        "sell_usdt": sell_usdt,
        "sell_count": sell_count,
        "avg_buy": avg_buy,
        "avg_sell": avg_sell,
        "matched_usdt": matched_usdt,
        "remaining_usdt": remaining_usdt,
        "profit_pln": profit_pln,
        "spread_pct": spread_pct,
    }


# =====================================================================
# ФОРМАТУВАННЯ ТА КЛАВІАТУРИ
# =====================================================================
def main_kb(user_id: int = 0):
    kb = [
        [KeyboardButton(text="🟢 Купівля"), KeyboardButton(text="🔴 Продаж")],
        [
            KeyboardButton(text="📊 Поточний круг"),
            KeyboardButton(text="🏁 Закрити круг"),
        ],
        [
            KeyboardButton(text="⏪ Скасувати останній"),
            KeyboardButton(text="📈 Статистика"),
        ],
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Адмін-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def stats_inline_kb():
    kb = [
        [
            InlineKeyboardButton(text="За сьогодні", callback_data="stat_today"),
            InlineKeyboardButton(text="За вчора", callback_data="stat_yesterday"),
        ],
        [
            InlineKeyboardButton(text="За 7 днів", callback_data="stat_7d"),
            InlineKeyboardButton(text="За 30 днів", callback_data="stat_30d"),
        ],
        [
            InlineKeyboardButton(text="За весь час", callback_data="stat_all"),
            InlineKeyboardButton(
                text="📋 Останні 10 кругів", callback_data="stat_last10"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📥 Вивантажити базу (CSV/Excel)", callback_data="export_csv"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def admin_inline_kb():
    kb = [
        [
            InlineKeyboardButton(
                text="📋 Список клієнтів", callback_data="admin_users_list"
            ),
            InlineKeyboardButton(
                text="📢 Зробити розсилку", callback_data="admin_broadcast"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔄 Оновити дані", callback_data="admin_refresh"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def fmt_num(val: float, decimals: int = 2) -> str:
    return f"{val:,.{decimals}f}".replace(",", " ")


def fmt_rate(val: float) -> str:
    if val == 0:
        return "0.00"
    s = f"{val:.4f}".rstrip("0")
    if len(s.split(".")[1]) < 2:
        s = f"{val:.2f}"
    return s


def format_round_status(m: dict) -> str:
    if m["remaining_usdt"] > 0.001:
        rem_pln = m["remaining_usdt"] * m["avg_buy"]
        rem_str = f"{fmt_num(m['remaining_usdt'])} USDT (~{fmt_num(rem_pln)} PLN)"
    elif m["remaining_usdt"] < -0.001:
        rem_str = f"{fmt_num(m['remaining_usdt'])} USDT (продано більше ніж куплено)"
    else:
        rem_str = "0.00 USDT"

    sign = "+" if m["profit_pln"] >= 0 else ""
    return (
        f"🇵🇱 <b>Круг #{m['round_num']} (PLN, активний)</b>\n"
        f"• Куплено: <code>{fmt_num(m['buy_usdt'])} USDT</code> на <code>{fmt_num(m['buy_pln'])} PLN</code> "
        f"(сер. курс: <b>{fmt_rate(m['avg_buy'])}</b>) [{m['buy_count']} орд.]\n"
        f"• Продано: <code>{fmt_num(m['sell_usdt'])} USDT</code> на <code>{fmt_num(m['sell_pln'])} PLN</code> "
        f"(сер. курс: <b>{fmt_rate(m['avg_sell'])}</b>) [{m['sell_count']} орд.]\n"
        f"• Залишок на руках: <code>{rem_str}</code>\n"
        f"• Поточний профіт (з {fmt_num(m['matched_usdt'])} USDT): "
        f"<b>{sign}{fmt_num(m['profit_pln'])} PLN ({sign}{fmt_num(m['spread_pct'])}%)</b>"
    )


def format_duration(start_str: str, end_str: str) -> str:
    try:
        t1 = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        t2 = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
        diff = t2 - t1
        mins = int(diff.total_seconds() // 60)
        h, m = divmod(mins, 60)
        return f"{h} год {m} хв" if h > 0 else f"{m} хв"
    except Exception:
        return "—"


# =====================================================================
# АДМІН-ПАНЕЛЬ ТА РОЗСИЛКА
# =====================================================================
def get_admin_dashboard_text() -> str:
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    with get_db() as conn:
        c = conn.cursor()
        # 1. Усього юзерів у базі
        c.execute("SELECT COUNT(*) FROM allowed_users")
        total_users = c.fetchone()[0] or 0

        # 2. Кількість юзерів, які сьогодні зробили хоч одну угоду
        c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM orders WHERE is_carryover = 0 AND timestamp >= ?",
            (today_start,),
        )
        active_today = c.fetchone()[0] or 0

        # 3. Скільки всього USDT прокрутили юзери (по кожному юзеру беремо макс. між закупом без переносів і продажем)
        c.execute("""
            SELECT
                user_id,
                SUM(CASE WHEN type = 'BUY' AND is_carryover = 0 THEN usdt ELSE 0 END) as bought,
                SUM(CASE WHEN type = 'SELL' THEN usdt ELSE 0 END) as sold
            FROM orders
            GROUP BY user_id
        """)
        total_usdt_cycled = 0.0
        for _, bought, sold in c.fetchall():
            total_usdt_cycled += max(bought or 0.0, sold or 0.0)

    return (
        f"👑 <b>Адмін-панель P2P Бота</b>\n\n"
        f"1️⃣ Усього юзерів: <b>{total_users}</b>\n"
        f"2️⃣ Торгували сьогодні (хоч 1 угода): <b>{active_today}</b>\n"
        f"3️⃣ Всього прокручено юзерами: <code>{fmt_num(total_usdt_cycled)} USDT</code>"
    )


@dp.message(Command("admin"))
@dp.message(F.text == "👑 Адмін-панель")
async def cmd_admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        get_admin_dashboard_text(),
        parse_mode="HTML",
        reply_markup=admin_inline_kb(),
    )


@dp.callback_query(F.data == "admin_refresh")
async def cb_admin_refresh(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        await call.message.edit_text(
            get_admin_dashboard_text(),
            parse_mode="HTML",
            reply_markup=admin_inline_kb(),
        )
    except Exception:
        pass
    await call.answer("Дані оновлено!")


@dp.callback_query(F.data == "admin_users_list")
async def cb_admin_users_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.added_at,
                COUNT(CASE WHEN o.is_carryover = 0 THEN 1 END) as order_cnt,
                SUM(CASE WHEN o.type = 'BUY' AND o.is_carryover = 0 THEN o.usdt ELSE 0 END) as bought,
                SUM(CASE WHEN o.type = 'SELL' THEN o.usdt ELSE 0 END) as sold
            FROM allowed_users u
            LEFT JOIN orders o ON u.user_id = o.user_id
            GROUP BY u.user_id
            ORDER BY u.added_at DESC
        """)
        rows = c.fetchall()

    if not rows:
        await call.answer("Список порожній.", show_alert=True)
        return

    lines = ["📋 <b>Список клієнтів:</b>\n"]
    for idx, (uid, uname, fname, added, o_cnt, bought, sold) in enumerate(rows, 1):
        handle = f"@{uname}" if uname and uname not in ("noname", "admin") else (fname or "Користувач")
        if uid == ADMIN_ID:
            handle += " (Ти)"
        vol = max(bought or 0.0, sold or 0.0)
        dt = added[:10] if added else "—"
        lines.append(
            f"{idx}. <b>{handle}</b> [<code>{uid}</code>]\n"
            f"   📅 {dt} | Угод: <b>{o_cnt}</b> | Прокрут: <code>{fmt_num(vol)} USDT</code>"
        )

    text = "\n".join(lines)
    # Якщо список дуже довгий, розбиваємо на частини
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>...показано перших клієнтів</i>"

    await call.message.answer(text, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await call.message.answer(
        "📢 <b>Введи текст розсилки для всіх користувачів бота:</b>\n\n"
        "<i>Щоб скасувати розсилку — надішли /cancel</i>",
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(AdminState.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text and message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer(
            "❌ Розсилку скасовано.", reply_markup=main_kb(ADMIN_ID)
        )
        return

    if not message.text:
        await message.answer("⚠️ Будь ласка, надішли текстове повідомлення або /cancel.")
        return

    b_text = message.text
    await state.clear()

    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM allowed_users")
        users = [r[0] for r in c.fetchall()]

    status_msg = await message.answer(
        f"⏳ Надсилаю повідомлення {len(users)} користувачам..."
    )
    sent = 0
    failed = 0

    for uid in users:
        try:
            await bot.send_message(uid, b_text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Розсилку завершено!</b>\n\n"
        f"• Успішно доставлено: <b>{sent}</b>\n"
        f"• Не доставлено (заблокували бота): <b>{failed}</b>",
        parse_mode="HTML",
    )


# =====================================================================
# СТАРТ ТА КОНТРОЛЬ ДОСТУПУ
# =====================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    if not is_user_allowed(user_id):
        user_name = message.from_user.full_name or "Користувач"
        user_handle = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else "без нікнейму"
        )

        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Схвалити доступ",
                        callback_data=f"allow_{user_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Відхилити",
                        callback_data=f"deny_{user_id}",
                    ),
                ]
            ]
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔔 <b>Новий запит на доступ до P2P-бота!</b>\n\n"
                f"• Ім'я: <b>{user_name}</b>\n"
                f"• Нікнейм: {user_handle}\n"
                f"• Telegram ID: <code>{user_id}</code>",
                parse_mode="HTML",
                reply_markup=admin_kb,
            )
        except Exception as e:
            logging.error(f"Помилка сповіщення адміна: {e}")

        await message.answer(
            "🔒 <b>Доступ до бота закритий.</b>\n\n"
            "Твій запит надіслано адміністратору. Щойно він підтвердить доступ — тобі прийде сповіщення з інструкцією!",
            parse_mode="HTML",
        )
        return

    get_or_create_active_round(user_id)
    await message.answer(
        ONBOARDING_TEXT,
        parse_mode="HTML",
        reply_markup=main_kb(user_id),
    )


@dp.callback_query(F.data.startswith("allow_"))
async def cb_allow_user(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Тільки для адміністратора!", show_alert=True)
        return

    target_id = int(call.data.replace("allow_", ""))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    username = "noname"
    full_name = "Користувач"
    try:
        chat = await bot.get_chat(target_id)
        if chat.username:
            username = chat.username
        if chat.full_name:
            full_name = chat.full_name
    except Exception:
        pass

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO allowed_users (user_id, username, full_name, added_at) VALUES (?, ?, ?, ?)",
            (target_id, username, full_name, now),
        )
        conn.commit()

    get_or_create_active_round(target_id)

    handle_disp = f"@{username}" if username != "noname" else full_name
    await call.message.edit_text(
        f"✅ <b>Доступ надано!</b>\nКористувач <b>{handle_disp}</b> (<code>{target_id}</code>) доданий у базу.",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            target_id,
            ONBOARDING_TEXT,
            parse_mode="HTML",
            reply_markup=main_kb(target_id),
        )
    except Exception as e:
        logging.error(f"Не вдалося надіслати привітання юзеру {target_id}: {e}")

    await call.answer("Доступ підтверджено!")


@dp.callback_query(F.data.startswith("deny_"))
async def cb_deny_user(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Тільки для адміністратора!", show_alert=True)
        return

    target_id = int(call.data.replace("deny_", ""))
    await call.message.edit_text(
        f"❌ Запит користувача <code>{target_id}</code> відхилено.",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(
            target_id, "❌ На жаль, адміністратор відхилив твій запит на доступ."
        )
    except Exception:
        pass
    await call.answer()


# =====================================================================
# РОБОЧІ КНОПКИ ТРЕЙДЕРА
# =====================================================================
@dp.message(F.text == "🟢 Купівля")
async def btn_buy(message: Message, state: FSMContext):
    if not is_user_allowed(message.from_user.id):
        return
    await state.set_state(OrderState.waiting_for_buy)
    await message.answer(
        "🟢 Введи <b>Суму в PLN</b> та <b>Курс</b> через пробіл (наприклад: <code>1000 3.985</code>):",
        parse_mode="HTML",
    )


@dp.message(F.text == "🔴 Продаж")
async def btn_sell(message: Message, state: FSMContext):
    if not is_user_allowed(message.from_user.id):
        return
    await state.set_state(OrderState.waiting_for_sell)
    await message.answer(
        "🔴 Введи <b>Суму в PLN</b> та <b>Курс</b> через пробіл (наприклад: <code>1000 4.04</code>):",
        parse_mode="HTML",
    )


@dp.message(F.text == "📊 Поточний круг")
async def btn_current_round(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        return
    await state.clear()
    round_id, _, _ = get_or_create_active_round(user_id)
    metrics = calculate_round_metrics(round_id)

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT type, pln, rate, usdt, is_carryover FROM orders WHERE round_id = ? ORDER BY id ASC",
            (round_id,),
        )
        orders = c.fetchall()

    text = format_round_status(metrics)
    if orders:
        text += "\n\n<b>📋 Ордери в цьому крузі:</b>\n"
        for idx, (o_type, pln, rate, usdt, is_co) in enumerate(orders, 1):
            icon = "🟢" if o_type == "BUY" else "🔴"
            co_tag = " <i>(перенос)</i>" if is_co else ""
            text += f"{idx}. {icon} <code>{fmt_num(pln)} PLN</code> по <b>{fmt_rate(rate)}</b> ({fmt_num(usdt)} USDT){co_tag}\n"
    else:
        text += "\n\n<i>У цьому крузі ще немає жодного ордера.</i>"

    await message.answer(text, parse_mode="HTML", reply_markup=main_kb(user_id))


@dp.message(F.text == "🏁 Закрити круг")
async def btn_close_round(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        return
    await state.clear()
    round_id, round_num, start_time = get_or_create_active_round(user_id)
    m = calculate_round_metrics(round_id)

    if m["buy_count"] == 0 and m["sell_count"] == 0:
        await message.answer(
            "⚠️ Поточний круг порожній!", reply_markup=main_kb(user_id)
        )
        return

    if m["sell_count"] == 0 or m["buy_count"] == 0:
        await message.answer(
            "⚠️ У крузі має бути хоча б одна купівля і один продаж для фіксації прибутку.",
            reply_markup=main_kb(user_id),
        )
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    duration = format_duration(start_time, now)
    carryover_usdt = m["remaining_usdt"] if m["remaining_usdt"] > 0.01 else 0.0

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """
            UPDATE rounds SET
                status = 'closed',
                end_time = ?,
                buy_pln = ?,
                buy_usdt = ?,
                sell_pln = ?,
                sell_usdt = ?,
                avg_buy = ?,
                avg_sell = ?,
                matched_usdt = ?,
                profit_pln = ?,
                spread_pct = ?,
                carryover_usdt = ?
            WHERE id = ?
        """,
            (
                now,
                m["buy_pln"],
                m["buy_usdt"],
                m["sell_pln"],
                m["sell_usdt"],
                m["avg_buy"],
                m["avg_sell"],
                m["matched_usdt"],
                m["profit_pln"],
                m["spread_pct"],
                carryover_usdt,
                round_id,
            ),
        )
        conn.commit()

    new_round_id, new_round_num, _ = get_or_create_active_round(user_id)

    carryover_msg = ""
    if carryover_usdt > 0:
        carryover_pln = carryover_usdt * m["avg_buy"]
        add_order_to_db(user_id, "BUY", carryover_pln, m["avg_buy"], is_carryover=1)
        carryover_msg = (
            f"\n📦 <i>Залишок <b>{fmt_num(carryover_usdt)} USDT</b> "
            f"({fmt_num(carryover_pln)} PLN) перенесено в <b>Круг #{new_round_num}</b> "
            f"за курсом <b>{fmt_rate(m['avg_buy'])}</b>.</i>"
        )
    elif m["remaining_usdt"] < -0.01:
        carryover_msg = (
            f"\nℹ️ <i>Продано на {fmt_num(abs(m['remaining_usdt']))} USDT більше, ніж куплено. "
            f"Відкрито чистий <b>Круг #{new_round_num}</b>.</i>"
        )
    else:
        carryover_msg = f"\n✨ <i>Круг закрито рівно в 0! Відкрито чистий <b>Круг #{new_round_num}</b>.</i>"

    diff_rate = m["avg_sell"] - m["avg_buy"]
    sign = "+" if m["profit_pln"] >= 0 else ""

    summary = (
        f"🏆 <b>Круг #{round_num} завершено! (PLN)</b> (Тривалість: {duration})\n\n"
        f"• Прокручений об'єм: <code>{fmt_num(m['matched_usdt'])} USDT</code> (~{fmt_num(m['matched_usdt'] * m['avg_buy'])} PLN)\n"
        f"• Середній закуп: <b>{fmt_rate(m['avg_buy'])}</b> | Середній продаж: <b>{fmt_rate(m['avg_sell'])}</b>\n"
        f"• Спред: <b>{sign}{fmt_num(m['spread_pct'])}%</b> (різниця: {sign}{fmt_rate(diff_rate)} PLN)\n"
        f"• <b>Чистий прибуток: {sign}{fmt_num(m['profit_pln'])} PLN</b> 💰\n"
        f"{carryover_msg}"
    )
    await message.answer(summary, parse_mode="HTML", reply_markup=main_kb(user_id))


@dp.message(F.text == "⏪ Скасувати останній")
async def btn_undo(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        return
    await state.clear()
    round_id, round_num, _ = get_or_create_active_round(user_id)

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, type, pln, rate, usdt FROM orders WHERE round_id = ? AND is_carryover = 0 ORDER BY id DESC LIMIT 1",
            (round_id,),
        )
        last_manual = c.fetchone()

        if last_manual:
            o_id, o_type, pln, rate, usdt = last_manual
            c.execute("DELETE FROM orders WHERE id = ?", (o_id,))
            conn.commit()
            act_name = "Купівля" if o_type == "BUY" else "Продаж"
            metrics = calculate_round_metrics(round_id)
            resp = (
                f"🗑 <b>Скасовано останній ордер:</b>\n"
                f"{act_name}: <code>{fmt_num(pln)} PLN</code> по <b>{fmt_rate(rate)}</b> ({fmt_num(usdt)} USDT)\n\n"
                f"{format_round_status(metrics)}"
            )
            await message.answer(
                resp, parse_mode="HTML", reply_markup=main_kb(user_id)
            )
            return

        c.execute(
            "SELECT id, round_num FROM rounds WHERE user_id = ? AND status = 'closed' ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        prev_round = c.fetchone()

        if prev_round:
            prev_id, prev_num = prev_round
            c.execute("DELETE FROM orders WHERE round_id = ?", (round_id,))
            c.execute("DELETE FROM rounds WHERE id = ?", (round_id,))
            c.execute(
                "UPDATE rounds SET status = 'active', end_time = NULL WHERE id = ?",
                (prev_id,),
            )
            conn.commit()
            metrics = calculate_round_metrics(prev_id)
            resp = (
                f"↩️ <b>Закриття Круга #{prev_num} скасовано!</b>\n"
                f"Круг знову активний:\n\n{format_round_status(metrics)}"
            )
            await message.answer(
                resp, parse_mode="HTML", reply_markup=main_kb(user_id)
            )
        else:
            await message.answer(
                "⚠️ У поточному крузі немає ордерів для скасування.",
                reply_markup=main_kb(user_id),
            )


@dp.message(F.text == "📈 Статистика")
async def btn_stats(message: Message, state: FSMContext):
    if not is_user_allowed(message.from_user.id):
        return
    await state.clear()
    text = build_stats_text(message.from_user.id, "today")
    await message.answer(
        text, parse_mode="HTML", reply_markup=stats_inline_kb()
    )


def build_stats_text(user_id: int, period: str) -> str:
    now = datetime.now()
    params = [user_id]
    date_filter = ""

    if period == "today":
        title = "За сьогодні"
        start_str = now.strftime("%Y-%m-%d 00:00:00")
        date_filter = "AND end_time >= ?"
        params.append(start_str)
    elif period == "yesterday":
        title = "За вчора"
        yest = now - timedelta(days=1)
        start_str = yest.strftime("%Y-%m-%d 00:00:00")
        end_str = yest.strftime("%Y-%m-%d 23:59:59")
        date_filter = "AND end_time BETWEEN ? AND ?"
        params.extend([start_str, end_str])
    elif period == "7d":
        title = "За останні 7 днів"
        start_str = (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        date_filter = "AND end_time >= ?"
        params.append(start_str)
    elif period == "30d":
        title = "За останні 30 днів"
        start_str = (now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        date_filter = "AND end_time >= ?"
        params.append(start_str)
    else:
        title = "За весь час"

    with get_db() as conn:
        c = conn.cursor()
        query = f"""
            SELECT
                COUNT(*),
                SUM(matched_usdt),
                SUM(matched_usdt * avg_buy),
                SUM(profit_pln),
                AVG(spread_pct),
                MAX(profit_pln)
            FROM rounds
            WHERE user_id = ? AND status = 'closed' {date_filter}
        """
        c.execute(query, params)
        row = c.fetchone()

    cnt = row[0] or 0
    vol_usdt = row[1] or 0.0
    vol_pln = row[2] or 0.0
    total_profit = row[3] or 0.0
    avg_spread_simple = row[4] or 0.0
    best_profit = row[5] or 0.0

    if cnt == 0:
        return f"📊 <b>Статистика PLN ({title})</b>\n\n<i>За цей період ще немає закритих кругів.</i>"

    weighted_spread = (
        (total_profit / vol_pln * 100) if vol_pln > 0 else avg_spread_simple
    )
    sign = "+" if total_profit >= 0 else ""

    return (
        f"📊 <b>Статистика PLN: {title}</b>\n\n"
        f"• Закрито кругів: <b>{cnt}</b>\n"
        f"• Прокручений об'єм: <code>{fmt_num(vol_usdt)} USDT</code> (~{fmt_num(vol_pln)} PLN)\n"
        f"• Середній спред (зважений): <b>{fmt_num(weighted_spread)}%</b>\n"
        f"• Найкращий круг: <b>+{fmt_num(best_profit)} PLN</b>\n"
        f"• <b>Чистий прибуток: {sign}{fmt_num(total_profit)} PLN</b> 🔥"
    )


@dp.callback_query(F.data.startswith("stat_"))
async def cb_stats(call: CallbackQuery):
    if not is_user_allowed(call.from_user.id):
        await call.answer("Немає доступу!", show_alert=True)
        return
    action = call.data.replace("stat_", "")
    user_id = call.from_user.id

    if action == "last10":
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT round_num, matched_usdt, avg_buy, avg_sell, spread_pct, profit_pln, end_time
                FROM rounds
                WHERE user_id = ? AND status = 'closed'
                ORDER BY id DESC LIMIT 10
            """,
                (user_id,),
            )
            rows = c.fetchall()

        if not rows:
            text = "📋 <b>Останні 10 кругів (PLN)</b>\n\n<i>Ще немає закритих кругів.</i>"
        else:
            text = "📋 <b>Останні закриті круги (PLN):</b>\n\n"
            for r_num, m_usdt, a_buy, a_sell, spr, prof, e_time in rows:
                dt_short = e_time[5:16] if e_time else ""
                sign = "+" if prof >= 0 else ""
                text += (
                    f"<b>#{r_num}</b> ({dt_short}): <code>{fmt_num(m_usdt)} USDT</code> | "
                    f"{fmt_rate(a_buy)} → {fmt_rate(a_sell)} ({sign}{fmt_num(spr)}%) | "
                    f"<b>{sign}{fmt_num(prof)} PLN</b>\n"
                )
    else:
        text = build_stats_text(user_id, action)

    try:
        await call.message.edit_text(
            text, parse_mode="HTML", reply_markup=stats_inline_kb()
        )
    except Exception:
        pass
    await call.answer()


@dp.callback_query(F.data == "export_csv")
async def cb_export_csv(call: CallbackQuery):
    user_id = call.from_user.id
    if not is_user_allowed(user_id):
        await call.answer("Немає доступу!", show_alert=True)
        return

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT r.round_num, r.status, o.type, o.pln, o.rate, o.usdt, o.is_carryover, o.timestamp
            FROM orders o
            JOIN rounds r ON o.round_id = r.id
            WHERE o.user_id = ?
            ORDER BY o.id ASC
        """,
            (user_id,),
        )
        orders = c.fetchall()

    if not orders:
        await call.answer("Немає даних для експорту!", show_alert=True)
        return

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "Круг №",
            "Статус",
            "Тип",
            "Сума PLN",
            "Курс",
            "Сума USDT",
            "Перенос",
            "Дата і час",
        ]
    )

    for r_num, status, o_type, pln, rate, usdt, is_co, ts in orders:
        writer.writerow([
            r_num,
            "Закритий" if status == "closed" else "Активний",
            "Купівля" if o_type == "BUY" else "Продаж",
            f"{pln:.2f}".replace(".", ","),
            fmt_rate(rate).replace(".", ","),
            f"{usdt:.2f}".replace(".", ","),
            "Так" if is_co else "Ні",
            ts,
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    file = BufferedInputFile(
        csv_bytes,
        filename=f"p2p_pln_orders_{datetime.now().strftime('%Y%m%d')}.csv",
    )
    await call.message.answer_document(
        file, caption="📥 Журнал угод PLN по кругах."
    )
    await call.answer()


# =====================================================================
# ВВЕДЕННЯ ОРДЕРІВ ЧЕРЕЗ ЧАТ
# =====================================================================
@dp.message()
async def handle_text_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_allowed(user_id):
        return
    if not message.text:
        return

    text = message.text.strip().replace(",", ".")
    current_state = await state.get_state()

    match = re.match(r"^([+\-])?\s*(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)$", text)
    if not match:
        await message.answer(
            "🤔 Формат: сума в PLN і курс через пробіл (наприклад: <code>1000 3.985</code>),\n"
            "або зі знаком: <code>+ 1000 3.985</code> (купівля) / <code>- 800 4.04</code> (продаж).",
            parse_mode="HTML",
            reply_markup=main_kb(user_id),
        )
        return

    sign_prefix, pln_str, rate_str = match.groups()
    pln = float(pln_str)
    rate = float(rate_str)

    if pln <= 0 or rate <= 0:
        await message.answer("⚠️ Сума та курс мають бути більшими за нуль!")
        return

    if sign_prefix == "+":
        order_type = "BUY"
    elif sign_prefix == "-":
        order_type = "SELL"
    elif current_state == OrderState.waiting_for_buy.state:
        order_type = "BUY"
    elif current_state == OrderState.waiting_for_sell.state:
        order_type = "SELL"
    else:
        await message.answer(
            "👆 Натисни <b>🟢 Купівля</b> чи <b>🔴 Продаж</b>, або постав знак <code>+</code> / <code>-</code> перед сумою.",
            parse_mode="HTML",
            reply_markup=main_kb(user_id),
        )
        return

    await state.clear()
    round_id, _, usdt = add_order_to_db(user_id, order_type, pln, rate)
    metrics = calculate_round_metrics(round_id)

    header = (
        f"✅ <b>Купівля: {fmt_num(pln)} PLN по {fmt_rate(rate)} ({fmt_num(usdt)} USDT)</b>"
        if order_type == "BUY"
        else f"✅ <b>Продаж: {fmt_num(pln)} PLN по {fmt_rate(rate)} ({fmt_num(usdt)} USDT)</b>"
    )

    response = f"{header}\n\n{format_round_status(metrics)}"
    await message.answer(
        response, parse_mode="HTML", reply_markup=main_kb(user_id)
    )


# =====================================================================
# ЗАПУСК БОТА
# =====================================================================
async def main():
    init_db()
    print("PLN Multi-User Бот з Адмін-панеллю успішно запущений!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
