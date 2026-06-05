import asyncio
import json
import logging
import os
import re
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ButtonStyle
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grok-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
DATA_FILE = Path(os.getenv("DATA_FILE", "accounts.json"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var is required")
if not ADMIN_ID:
    raise SystemExit("ADMIN_ID env var is required")

# ─── Паттерны парсинга аккаунтов ─────────────────────────────────────────────
# Паттерн 1: многострочный с метками  Email/Login/User: xxx \n Password/Pass: yyy
RE_LABELED = re.compile(
    r"(?:E-?mail|Login|User(?:name)?|Логин|Почта|Account)\s*[:\-]\s*(\S+)"
    r"\s*[\r\n]+\s*"
    r"(?:Password|Pass|Пароль|Pwd|Пасс)\s*[:\-]\s*(\S+)",
    re.IGNORECASE,
)
# Паттерн 2: одна строка, разделитель | ; : или таб
# email обязательно содержит @, не берём URL (не //после :)
RE_INLINE = re.compile(
    r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})"
    r"\s*[|;:\t]\s*"
    r"(?!//)(\S+)"
)
# Паттерн 3: две строки подряд — email, потом пароль (минимум 4 символа, без @)
RE_TWO_LINE = re.compile(
    r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})"
    r"\s*[\r\n]+\s*"
    r"([^\r\n\s@|;:]{4,})"
)
# Паттерн 4: пробел как разделитель — email пробел password (одна строка)
RE_SPACE = re.compile(
    r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})"
    r"\s{1,3}"
    r"([^\r\n\s@|;:]{4,})"
)

VALID_DAYS = (3, 7, 14, 30)
DAYS_EMOJI = {3: "⚡", 7: "📅", 14: "🌟", 30: "👑"}

HELP_TEXT = (
    "🤖 <b>Grok Bot — хранилище аккаунтов</b>\n\n"
    "➕ <b>Добавление — отправь аккаунты в любом формате:</b>\n"
    "<code>Email : mail@x.com\nPassword : pass</code>\n"
    "<code>mail@x.com|pass</code>\n"
    "<code>mail@x.com:pass</code>\n"
    "<code>mail@x.com;pass</code>\n"
    "<code>mail@x.com pass</code>\n"
    "<code>mail@x.com\npass</code>\n"
    "Бот сам определит формат и спросит срок.\n\n"
    "📦 <b>Выдача — кнопки внизу экрана:</b>\n"
    "Нажми нужный срок → аккаунт выдастся сразу.\n\n"
    "📋 <b>Просмотр:</b>\n"
    "• /list или 📋 Список\n"
    "• /count или 📊 Счёт\n"
    "• /get N — показать №N без удаления\n\n"
    "🗑 <b>Удаление:</b>\n"
    "• /use N [N2…] — удалить по номерам\n"
    "• /clear — очистить всё хранилище\n\n"
    "💡 Если кнопки пропали — отправь /start"
)

_lock = asyncio.Lock()
pending_add: dict[int, list[dict]] = {}
pending_clear: set[int] = set()


# ─── Файловые операции ────────────────────────────────────────────────────────

def load_accounts() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        changed = False
        for acc in data:
            if "days" not in acc:
                acc["days"] = 30
                changed = True
        if changed:
            _save_raw(data)
        return data
    except Exception as e:
        log.exception("Failed to load %s: %s", DATA_FILE, e)
        return []


def _save_raw(accounts: list[dict]) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def save_accounts(accounts: list[dict]) -> None:
    _save_raw(accounts)


def parse_accounts(text: str) -> list[dict]:
    """Парсит аккаунты из текста, поддерживая 6+ форматов."""
    results: list[dict] = []
    seen: set[str] = set()

    def add(email: str, password: str) -> None:
        e = email.strip().lower()
        p = password.strip()
        # Пропускаем если пароль сам похож на email, слишком короткий или пустой
        if not e or not p or len(p) < 3:
            return
        if e in seen:
            return
        seen.add(e)
        results.append({"email": email.strip(), "password": p})

    # Порядок важен: более точные паттерны первее
    for m in RE_LABELED.finditer(text):
        add(m.group(1), m.group(2))
    for m in RE_INLINE.finditer(text):
        add(m.group(1), m.group(2))
    for m in RE_TWO_LINE.finditer(text):
        add(m.group(1), m.group(2))
    for m in RE_SPACE.finditer(text):
        add(m.group(1), m.group(2))

    return results


# ─── Форматирование ───────────────────────────────────────────────────────────

def days_label(days: int) -> str:
    return f"{DAYS_EMOJI.get(days, '📌')} {days}д"


def format_account_block(a: dict) -> str:
    return (
        f"<code>Email : {escape(a['email'])}\n"
        f"Password : {escape(a['password'])}</code>"
    )


def format_list(accounts: list[dict]) -> str:
    if not accounts:
        return "Хранилище пустое."
    groups: dict[int, list[tuple[int, dict]]] = {}
    for i, a in enumerate(accounts, 1):
        d = a.get("days", 30)
        groups.setdefault(d, []).append((i, a))
    lines = []
    for d in sorted(groups.keys()):
        emoji = DAYS_EMOJI.get(d, "📌")
        lines.append(f"\n{emoji} <b>{d} дней</b> — {len(groups[d])} шт.")
        for idx, a in groups[d]:
            lines.append(
                f"  {idx}. <code>{escape(a['email'])}</code>  |  "
                f"<code>{escape(a['password'])}</code>"
            )
    return "\n".join(lines)


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚡ 3 дня"),  KeyboardButton(text="📅 7 дней")],
            [KeyboardButton(text="🌟 14 дней"), KeyboardButton(text="👑 30 дней")],
            [KeyboardButton(text="📋 Список"),  KeyboardButton(text="📊 Счёт")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def days_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ 3 дня",   callback_data=f"{prefix}_days:3",  style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="📅 7 дней",  callback_data=f"{prefix}_days:7",  style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="🌟 14 дней", callback_data=f"{prefix}_days:14", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="👑 30 дней", callback_data=f"{prefix}_days:30", style=ButtonStyle.SUCCESS),
        ],
        [InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}_cancel", style=ButtonStyle.DANGER)],
    ])


def is_admin(msg: Message) -> bool:
    return msg.from_user is not None and msg.from_user.id == ADMIN_ID

def is_admin_cb(cb: CallbackQuery) -> bool:
    return cb.from_user is not None and cb.from_user.id == ADMIN_ID


dp = Dispatcher()

BUTTON_DAYS = {"⚡ 3 дня": 3, "📅 7 дней": 7, "🌟 14 дней": 14, "👑 30 дней": 30}
MK = main_keyboard()  # один экземпляр на весь процесс


# ─── Базовые команды ──────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_admin(message):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=MK)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_admin(message):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=MK)


@dp.message(Command("count"))
async def cmd_count(message: Message):
    if not is_admin(message):
        return
    await _send_count(message)


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message):
        return
    await _send_list(message)


@dp.message(Command("get"))
async def cmd_get(message: Message, command):
    if not is_admin(message):
        return
    args = (command.args or "").strip().split()
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /get N", reply_markup=MK)
        return
    n = int(args[0])
    accounts = load_accounts()
    if n < 1 or n > len(accounts):
        await message.answer(f"Нет аккаунта №{n}. Всего: {len(accounts)}.", reply_markup=MK)
        return
    a = accounts[n - 1]
    await message.answer(
        f"#{n} {days_label(a.get('days', 30))}\n{format_account_block(a)}",
        parse_mode="HTML", reply_markup=MK,
    )


# ─── Вспомогательные функции ──────────────────────────────────────────────────

async def _send_count(message: Message) -> None:
    accounts = load_accounts()
    total = len(accounts)
    if total == 0:
        await message.answer("📭 Хранилище пустое.", reply_markup=MK)
        return
    breakdown: dict[int, int] = {}
    for a in accounts:
        d = a.get("days", 30)
        breakdown[d] = breakdown.get(d, 0) + 1
    lines = [f"📊 <b>В хранилище: {total} шт.</b>\n"]
    for d in VALID_DAYS:
        cnt = breakdown.get(d, 0)
        lines.append(f"  {DAYS_EMOJI.get(d,'📌')} <b>{d} дней:</b> {cnt} шт.")
    for d, cnt in sorted(breakdown.items()):
        if d not in VALID_DAYS:
            lines.append(f"  📌 <b>{d} дней:</b> {cnt} шт.")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=MK)


async def _send_list(message: Message) -> None:
    accounts = load_accounts()
    if not accounts:
        await message.answer("📭 Хранилище пустое.", reply_markup=MK)
        return
    full = f"📋 <b>Хранилище — {len(accounts)} шт.</b>\n" + format_list(accounts)
    for i, chunk_start in enumerate(range(0, len(full), 3800)):
        chunk = full[chunk_start:chunk_start + 3800]
        # reply_markup только на последний чанк чтобы не дублировать
        if chunk_start + 3800 >= len(full):
            await message.answer(chunk, parse_mode="HTML", reply_markup=MK)
        else:
            await message.answer(chunk, parse_mode="HTML")


# ─── Кнопки reply-клавиатуры ─────────────────────────────────────────────────

@dp.message(F.text.in_(set(BUTTON_DAYS.keys())))
async def handle_day_button(message: Message):
    if not is_admin(message):
        return
    await _pop_by_days(message, BUTTON_DAYS[message.text])


@dp.message(F.text == "📋 Список")
async def handle_list_button(message: Message):
    if not is_admin(message):
        return
    await _send_list(message)


@dp.message(F.text == "📊 Счёт")
async def handle_count_button(message: Message):
    if not is_admin(message):
        return
    await _send_count(message)


@dp.message(F.text == "❓ Помощь")
async def handle_help_button(message: Message):
    if not is_admin(message):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=MK)


# ─── /pop с inline-выбором типа ───────────────────────────────────────────────

@dp.message(Command("pop"))
async def cmd_pop(message: Message):
    if not is_admin(message):
        return
    accounts = load_accounts()
    if not accounts:
        await message.answer("📭 Хранилище пустое.", reply_markup=MK)
        return
    available = {}
    for a in accounts:
        d = a.get("days", 30)
        available[d] = available.get(d, 0) + 1
    summary = "  ".join(f"{DAYS_EMOJI.get(d,'📌')}{d}д:{cnt}" for d, cnt in sorted(available.items()))
    await message.answer(
        f"📦 Выбери тип подписки:\n<i>{summary}</i>",
        reply_markup=days_keyboard("pop"), parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("pop_days:"))
async def cb_pop_days(cb: CallbackQuery):
    if not is_admin_cb(cb):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    days = int(cb.data.split(":")[1])
    async with _lock:
        accounts = load_accounts()
        match = next((a for a in accounts if a.get("days", 30) == days), None)
        if not match:
            await cb.answer(f"Нет аккаунтов с подпиской {days} дней.", show_alert=True)
            await cb.message.edit_reply_markup(reply_markup=None)
            return
        accounts.remove(match)
        save_accounts(accounts)
    await cb.answer("✅ Выдан!")
    await cb.message.edit_text(
        f"📤 Выдан [{days}д]:\n{format_account_block(match)}\n\n"
        f"<i>Осталось: {len(accounts)} шт.</i>",
        parse_mode="HTML",
    )
    # Восстанавливаем клавиатуру
    await cb.message.answer("⬆️ Выдан выше", reply_markup=MK)


@dp.callback_query(F.data == "pop_cancel")
async def cb_pop_cancel(cb: CallbackQuery):
    if not is_admin_cb(cb):
        return
    await cb.answer("Отменено.")
    await cb.message.edit_text("❌ Выдача отменена.")
    await cb.message.answer("Отменено.", reply_markup=MK)


# ─── Быстрые команды /Nday ────────────────────────────────────────────────────

async def _pop_by_days(message: Message, days: int) -> None:
    async with _lock:
        accounts = load_accounts()
        match = next((a for a in accounts if a.get("days", 30) == days), None)
        if not match:
            await message.answer(f"📭 Нет аккаунтов с подпиской {days} дней.", reply_markup=MK)
            return
        accounts.remove(match)
        save_accounts(accounts)
    await message.answer(
        f"📤 Выдан [{days}д]:\n{format_account_block(match)}\n\n"
        f"<i>Осталось: {len(accounts)} шт.</i>",
        parse_mode="HTML", reply_markup=MK,
    )


@dp.message(Command("3day"))
async def cmd_3day(message: Message):
    if not is_admin(message): return
    await _pop_by_days(message, 3)

@dp.message(Command("7day"))
async def cmd_7day(message: Message):
    if not is_admin(message): return
    await _pop_by_days(message, 7)

@dp.message(Command("14day"))
async def cmd_14day(message: Message):
    if not is_admin(message): return
    await _pop_by_days(message, 14)

@dp.message(Command("30day"))
async def cmd_30day(message: Message):
    if not is_admin(message): return
    await _pop_by_days(message, 30)


# ─── Удаление ─────────────────────────────────────────────────────────────────

@dp.message(Command("use"))
async def cmd_use(message: Message, command):
    if not is_admin(message):
        return
    args = (command.args or "").strip().split()
    if not args or not all(x.isdigit() for x in args):
        await message.answer("Использование: /use N  или  /use N1 N2 N3 ...", reply_markup=MK)
        return
    indexes = sorted({int(x) for x in args}, reverse=True)
    async with _lock:
        accounts = load_accounts()
        removed, skipped = [], []
        for n in indexes:
            if 1 <= n <= len(accounts):
                removed.append((n, accounts.pop(n - 1)))
            else:
                skipped.append(n)
        save_accounts(accounts)
    parts = []
    if removed:
        lines = [f"  №{n}: {escape(a['email'])} {days_label(a.get('days',30))}" for n, a in sorted(removed)]
        parts.append("🗑 <b>Удалены:</b>\n" + "\n".join(lines))
    if skipped:
        parts.append("⚠️ Не найдены: " + ", ".join(f"№{n}" for n in skipped))
    parts.append(f"<i>Осталось: {len(accounts)} шт.</i>")
    await message.answer("\n\n".join(parts), parse_mode="HTML", reply_markup=MK)


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    if not is_admin(message):
        return
    pending_clear.add(message.from_user.id)
    await message.answer(
        "⚠️ <b>Точно очистить ВСЁ хранилище?</b>\n/yes — подтвердить  |  /no — отмена",
        parse_mode="HTML", reply_markup=MK,
    )

@dp.message(Command("yes"))
async def cmd_yes(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in pending_clear:
        pending_clear.discard(message.from_user.id)
        async with _lock:
            save_accounts([])
        await message.answer("✅ Хранилище очищено.", reply_markup=MK)

@dp.message(Command("no"))
async def cmd_no(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in pending_clear:
        pending_clear.discard(message.from_user.id)
        await message.answer("❌ Отменено.", reply_markup=MK)


# ─── Добавление аккаунтов ─────────────────────────────────────────────────────

@dp.message(F.text)
async def handle_text(message: Message):
    if not is_admin(message):
        return
    parsed = parse_accounts(message.text or "")
    if not parsed:
        await message.answer("Не нашёл аккаунтов. /help для справки.", reply_markup=MK)
        return
    if message.from_user.id in pending_add:
        await message.answer("⚠️ Предыдущая партия заменена новой.", reply_markup=MK)
    pending_add[message.from_user.id] = parsed
    await message.answer(
        f"📥 Найдено <b>{len(parsed)}</b> аккаунт(ов). Выбери срок подписки:",
        reply_markup=days_keyboard("add"), parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("add_days:"))
async def cb_add_days(cb: CallbackQuery):
    if not is_admin_cb(cb):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    days = int(cb.data.split(":")[1])
    parsed = pending_add.pop(cb.from_user.id, None)
    if not parsed:
        await cb.answer("Сессия истекла. Пришли аккаунты снова.", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    for a in parsed:
        a["days"] = days
    async with _lock:
        accounts = load_accounts()
        existing = {a["email"].lower() for a in accounts}
        added, dupes = 0, 0
        for a in parsed:
            if a["email"].lower() in existing:
                dupes += 1
            else:
                accounts.append(a)
                existing.add(a["email"].lower())
                added += 1
        save_accounts(accounts)
    emoji = DAYS_EMOJI.get(days, "📌")
    msg = f"✅ Добавлено: <b>{added}</b> шт. {emoji} {days}д"
    if dupes:
        msg += f"\n⚠️ Пропущено дублей: {dupes}"
    msg += f"\n<i>Всего: {len(accounts)} шт.</i>"
    await cb.answer(f"Добавлено {added} шт.")
    await cb.message.edit_text(msg, parse_mode="HTML")
    # Восстанавливаем клавиатуру
    await cb.message.answer(msg, parse_mode="HTML", reply_markup=MK)


@dp.callback_query(F.data == "add_cancel")
async def cb_add_cancel(cb: CallbackQuery):
    if not is_admin_cb(cb):
        return
    pending_add.pop(cb.from_user.id, None)
    await cb.answer("Отменено.")
    await cb.message.edit_text("❌ Добавление отменено.")
    await cb.message.answer("❌ Отменено.", reply_markup=MK)


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Bot starting. Admin ID: %s. Data file: %s", ADMIN_ID, DATA_FILE.resolve())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
