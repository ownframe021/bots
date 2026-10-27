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
# НАЛАШТУВАННЯ
# =====================================================================
BOT_TOKEN = "8735182424:AAHGRbtUPkMn3_NaXf9W4qcZwyKgj1YE9Vc"
ADMIN_ID = 954547387  # Встав сюди свій числовий Telegram ID

# Валюта каси (можеш змінити на "$", "USDT", "PLN" або "UAH")
CURRENCY = "₴"

BASE_DIR = Path(__file__).resolve().parent
DB_NAME = str(BASE_DIR / "my_kasa.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =====================================================================
# СТАНИ
# =====================================================================
class KasaState(StatesGroup):
    waiting_for_initial = State()
    waiting_for_plus = State()
    waiting_for_minus = State()


# =====================================================================
# БАЗА ДАНИХ (SQLite)
# =====================================================================
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS kasa_settings (
                user_id INTEGER PRIMARY KEY,
                initial_balance REAL DEFAULT 0,
                updated_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                note TEXT,
                timestamp TEXT
            )
        """)
        conn.commit()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def get_kasa_summary(user_id: int):
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT initial_balance FROM kasa_settings WHERE user_id = ?",
            (user_id,),
        )
        row = c.fetchone()
        has_init = row is not None
        initial_balance = row[0] if row else 0.0

        c.execute(
            "SELECT SUM(amount), COUNT(*) FROM records WHERE user_id = ?",
            (user_id,),
        )
        r_all = c.fetchone()
        total_delta = r_all[0] or 0.0
        total_count = r_all[1] or 0

        c.execute(
            "SELECT SUM(amount) FROM records WHERE user_id = ? AND timestamp >= ?",
            (user_id, today_start),
        )
        today_delta = c.fetchone()[0] or 0.0

    current_balance = initial_balance + total_delta
    growth_pct = (
        (total_delta / initial_balance * 100) if initial_balance > 0 else 0.0
    )

    return {
        "has_init": has_init,
        "initial_balance": initial_balance,
        "total_delta": total_delta,
        "total_count": total_count,
        "today_delta": today_delta,
        "current_balance": current_balance,
        "growth_pct": growth_pct,
    }


def set_initial_balance(user_id: int, amount: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO kasa_settings (user_id, initial_balance, updated_at) VALUES (?, ?, ?)",
            (user_id, amount, now),
        )
        conn.commit()


def add_record(user_id: int, amount: float, note: str = ""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO records (user_id, amount, note, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, amount, note, now),
        )
        conn.commit()


# =====================================================================
# ФОРМАТУВАННЯ ТА КНОПКИ
# =====================================================================
def fmt_money(val: float, with_sign: bool = False) -> str:
    formatted = f"{abs(val):,.2f}".replace(",", " ")
    if with_sign:
        sign = "+" if val >= 0 else "-"
        return f"{sign}{formatted} {CURRENCY}"
    if val < 0:
        return f"-{formatted} {CURRENCY}"
    return f"{formatted} {CURRENCY}"


def main_kb():
    kb = [
        [
            KeyboardButton(text="➕ Плюс за день"),
            KeyboardButton(text="➖ Мінус / Витрата"),
        ],
        [
            KeyboardButton(text="💰 Моя каса"),
            KeyboardButton(text="📈 Статистика"),
        ],
        [
            KeyboardButton(text="⏪ Скасувати останній"),
            KeyboardButton(text="⚙️ Задати старт каси"),
        ],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def stats_inline_kb():
    kb = [
        [
            InlineKeyboardButton(text="За сьогодні", callback_data="stat_today"),
            InlineKeyboardButton(text="За вчора", callback_data="stat_yesterday"),
        ],
        [
            InlineKeyboardButton(text="За тиждень (7д)", callback_data="stat_7d"),
            InlineKeyboardButton(text="За місяць (30д)", callback_data="stat_30d"),
        ],
        [
            InlineKeyboardButton(text="За весь час", callback_data="stat_all"),
            InlineKeyboardButton(
                text="📋 Останні 10 записів", callback_data="stat_last10"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📥 Вивантажити в Excel (CSV)", callback_data="export_csv"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def format_kasa_card(s: dict) -> str:
    g_sign = "+" if s["growth_pct"] >= 0 else ""
    return (
        f"💼 <b>Стан твоєї каси:</b>\n\n"
        f"🏦 <b>Загальний баланс: {fmt_money(s['current_balance'])}</b>\n"
        f"• Стартовий банк: <code>{fmt_money(s['initial_balance'])}</code>\n"
        f"• Чистий приріст за весь час: <b>{fmt_money(s['total_delta'], True)} ({g_sign}{s['growth_pct']:.2f}%)</b>\n"
        f"• Результат за сьогодні: <b>{fmt_money(s['today_delta'], True)}</b>"
    )


# =====================================================================
# ОБРОБНИКИ КОМАНД ТА КНОПОК
# =====================================================================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("🔒 Це приватний фінансовий трекер.")
        return

    await state.clear()
    s = get_kasa_summary(message.from_user.id)

    if not s["has_init"]:
        await state.set_state(KasaState.waiting_for_initial)
        await message.answer(
            "👋 <b>Привіт, братух! Запускаємо облік твоєї загальної каси.</b>\n\n"
            f"Напиши, з якої суми (у <b>{CURRENCY}</b>) стартуємо облік прямо зараз (наприклад: <code>3000</code>):",
            parse_mode="HTML",
        )
        return

    text = (
        "🔥 <b>Трекер твоєї каси активний!</b>\n\n"
        "Пиши результат за день прямо в чат:\n"
        f"• <code>+ 150</code> або <code>+ 150 п2п</code> — додати профіт\n"
        f"• <code>- 40</code> або <code>- 40 витрати</code> — записати мінус\n\n"
        f"{format_kasa_card(s)}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_kb())


@dp.message(F.text == "⚙️ Задати старт каси")
async def btn_set_initial(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(KasaState.waiting_for_initial)
    await message.answer(
        f"⚙️ Введи нову суму <b>стартового балансу каси</b> (у {CURRENCY}), від якої будемо рахувати всі результати (наприклад: <code>3000</code>):",
        parse_mode="HTML",
    )


@dp.message(KasaState.waiting_for_initial)
async def process_initial_balance(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id) or not message.text:
        return

    clean = message.text.strip().replace(",", ".").replace(" ", "")
    try:
        val = float(clean)
    except ValueError:
        await message.answer("⚠️ Введи просто число (наприклад: <code>3000</code>):", parse_mode="HTML")
        return

    set_initial_balance(message.from_user.id, val)
    await state.clear()
    s = get_kasa_summary(message.from_user.id)

    await message.answer(
        f"✅ <b>Стартовий банк встановлено: {fmt_money(val)}</b>\n\n"
        f"Тепер в кінці дня просто пиши в чат свій результат (наприклад: <code>+ 120</code> або <code>- 35</code>).\n\n"
        f"{format_kasa_card(s)}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.message(F.text == "➕ Плюс за день")
async def btn_plus(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(KasaState.waiting_for_plus)
    await message.answer(
        f"🟢 Введи свій плюс за день у <b>{CURRENCY}</b> (можна з коментарем, наприклад: <code>150</code> або <code>150 п2п</code>):",
        parse_mode="HTML",
    )


@dp.message(F.text == "➖ Мінус / Витрата")
async def btn_minus(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(KasaState.waiting_for_minus)
    await message.answer(
        f"🔴 Введи суму мінуса або витрати у <b>{CURRENCY}</b> (наприклад: <code>40</code> або <code>40 дорога</code>):",
        parse_mode="HTML",
    )


@dp.message(F.text == "💰 Моя каса")
async def btn_my_kasa(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    s = get_kasa_summary(message.from_user.id)
    await message.answer(
        format_kasa_card(s), parse_mode="HTML", reply_markup=main_kb()
    )


@dp.message(F.text == "⏪ Скасувати останній")
async def btn_undo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    user_id = message.from_user.id

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, amount, note, timestamp FROM records WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        last = c.fetchone()
        if not last:
            await message.answer(
                "⚠️ Ще немає жодного запису для скасування.",
                reply_markup=main_kb(),
            )
            return

        r_id, amount, note, _ = last
        c.execute("DELETE FROM records WHERE id = ?", (r_id,))
        conn.commit()

    s = get_kasa_summary(user_id)
    note_str = f" ({note})" if note else ""
    await message.answer(
        f"🗑 <b>Скасовано останній запис:</b> <code>{fmt_money(amount, True)}</code>{note_str}\n\n"
        f"{format_kasa_card(s)}",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


@dp.message(F.text == "📈 Статистика")
async def btn_stats(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    text = build_stats_text(message.from_user.id, "7d")
    await message.answer(
        text, parse_mode="HTML", reply_markup=stats_inline_kb()
    )


# =====================================================================
# ЛОГІКА СТАТИСТИКИ ТА ЕКСПОРТУ
# =====================================================================
def build_stats_text(user_id: int, period: str) -> str:
    now = datetime.now()
    params = [user_id]
    date_filter = ""

    if period == "today":
        title = "За сьогодні"
        start_str = now.strftime("%Y-%m-%d 00:00:00")
        date_filter = "AND timestamp >= ?"
        params.append(start_str)
    elif period == "yesterday":
        title = "За вчора"
        yest = now - timedelta(days=1)
        start_str = yest.strftime("%Y-%m-%d 00:00:00")
        end_str = yest.strftime("%Y-%m-%d 23:59:59")
        date_filter = "AND timestamp BETWEEN ? AND ?"
        params.extend([start_str, end_str])
    elif period == "7d":
        title = "За тиждень (7 днів)"
        start_str = (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        date_filter = "AND timestamp >= ?"
        params.append(start_str)
    elif period == "30d":
        title = "За місяць (30 днів)"
        start_str = (now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        date_filter = "AND timestamp >= ?"
        params.append(start_str)
    else:
        title = "За весь час"

    with get_db() as conn:
        c = conn.cursor()
        query = f"""
            SELECT
                COUNT(*),
                SUM(amount),
                SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END),
                COUNT(CASE WHEN amount > 0 THEN 1 END),
                SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END),
                COUNT(CASE WHEN amount < 0 THEN 1 END),
                AVG(amount),
                MAX(amount)
            FROM records
            WHERE user_id = ? {date_filter}
        """
        c.execute(query, params)
        row = c.fetchone()

    cnt = row[0] or 0
    net_res = row[1] or 0.0
    plus_sum = row[2] or 0.0
    plus_cnt = row[3] or 0
    minus_sum = row[4] or 0.0
    minus_cnt = row[5] or 0
    avg_res = row[6] or 0.0
    best_res = row[7] or 0.0

    s = get_kasa_summary(user_id)

    if cnt == 0:
        return (
            f"📊 <b>Статистика каси: {title}</b>\n\n"
            f"<i>За цей період ще немає записів.</i>\n\n"
            f"🏦 Поточна каса: <b>{fmt_money(s['current_balance'])}</b>"
        )

    return (
        f"📊 <b>Статистика каси: {title}</b>\n\n"
        f"• Записів за період: <b>{cnt}</b> (🟢 +{plus_cnt} / 🔴 -{minus_cnt})\n"
        f"• Усього в плюс: <code>+{fmt_money(plus_sum)}</code>\n"
        f"• Усього в мінус: <code>{fmt_money(minus_sum)}</code>\n"
        f"• Середній резолт за запис: <b>{fmt_money(avg_res, True)}</b>\n"
        f"• Найкращий день/запис: <b>{fmt_money(best_res, True)}</b>\n\n"
        f"🔥 <b>Чистий резолт за період: {fmt_money(net_res, True)}</b>\n"
        f"🏦 <b>Загальна каса зараз: {fmt_money(s['current_balance'])}</b>"
    )


@dp.callback_query(F.data.startswith("stat_"))
async def cb_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    action = call.data.replace("stat_", "")
    user_id = call.from_user.id

    if action == "last10":
        with get_db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT amount, note, timestamp FROM records WHERE user_id = ? ORDER BY id DESC LIMIT 10",
                (user_id,),
            )
            rows = c.fetchall()

        if not rows:
            text = "📋 <b>Останні 10 записів</b>\n\n<i>Історія порожня.</i>"
        else:
            text = "📋 <b>Останні 10 записів по касі:</b>\n\n"
            for amt, note, ts in rows:
                dt_short = ts[5:16] if ts else ""
                icon = "🟢" if amt >= 0 else "🔴"
                note_str = f" — <i>{note}</i>" if note else ""
                text += f"{icon} <code>{dt_short}</code> | <b>{fmt_money(amt, True)}</b>{note_str}\n"
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
    if not is_admin(user_id):
        return

    s = get_kasa_summary(user_id)
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, amount, note, timestamp FROM records WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        )
        rows = c.fetchall()

    if not rows:
        await call.answer("Немає записів для експорту!", show_alert=True)
        return

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        ["№", "Дата і час", f"Резолт ({CURRENCY})", f"Баланс каси ({CURRENCY})", "Коментар"]
    )

    running_balance = s["initial_balance"]
    writer.writerow(
        ["Старт", "—", "0,00", f"{running_balance:.2f}".replace(".", ","), "Стартовий банк"]
    )

    for idx, (_, amt, note, ts) in enumerate(rows, 1):
        running_balance += amt
        writer.writerow([
            idx,
            ts,
            f"{amt:.2f}".replace(".", ","),
            f"{running_balance:.2f}".replace(".", ","),
            note or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    file = BufferedInputFile(
        csv_bytes, filename=f"kasa_report_{datetime.now().strftime('%Y%m%d')}.csv"
    )
    await call.message.answer_document(
        file, caption="📥 Повна історія твоєї каси."
    )
    await call.answer()


# =====================================================================
# ШВИДКЕ ВВЕДЕННЯ РЕЗУЛЬТАТУ В ЧАТ
# =====================================================================
@dp.message()
async def handle_daily_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id) or not message.text:
        return

    s_init = get_kasa_summary(user_id)
    if not s_init["has_init"]:
        await state.set_state(KasaState.waiting_for_initial)
        await message.answer(
            f"Спочатку задай свій стартовий баланс каси у {CURRENCY} (наприклад: <code>3000</code>):",
            parse_mode="HTML",
        )
        return

    text = message.text.strip()
    current_state = await state.get_state()

    # Парсимо: необов'язковий знак (+/-), число, та необов'язковий коментар
    match = re.match(r"^([+\-])?\s*(\d+(?:[.,]\d+)?)(?:\s+(.*))?$", text)
    if not match:
        await message.answer(
            "🤔 Напиши результат за день числом:\n"
            "• <code>+ 150</code> або <code>+ 150 п2п</code> (плюс)\n"
            "• <code>- 40</code> або <code>- 40 витрати</code> (мінус)",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )
        return

    sign_str, num_str, note_str = match.groups()
    val = float(num_str.replace(",", "."))
    note = (note_str or "").strip()

    if val == 0:
        await message.answer("⚠️ Сума не може бути нульовою!")
        return

    if sign_str == "-":
        amount = -val
    elif sign_str == "+":
        amount = val
    elif current_state == KasaState.waiting_for_minus.state:
        amount = -val
    else:
        # Якщо просто ввів число без знака (наприклад "150") — рахуємо як плюс за день
        amount = val

    await state.clear()
    add_record(user_id, amount, note)
    s = get_kasa_summary(user_id)

    icon = "🟢" if amount >= 0 else "🔴"
    note_line = f"📝 Коментар: <i>{note}</i>\n" if note else ""

    resp = (
        f"{icon} <b>Записано результат: {fmt_money(amount, True)}</b>\n"
        f"{note_line}\n"
        f"{format_kasa_card(s)}"
    )
    await message.answer(resp, parse_mode="HTML", reply_markup=main_kb())


# =====================================================================
# ЗАПУСК
# =====================================================================
async def main():
    init_db()
    print("Бот Загальної Каси успішно запущений!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
