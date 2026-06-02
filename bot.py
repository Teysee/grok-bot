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

ACCOUNT_RE = re.compile(
    r"Email\s*[:\-]\s*(\S+)\s*[\r\n]+\s*Password\s*[:\-]\s*(\S+)",
    re.IGNORECASE,
)

VALID_DAYS = (3, 7, 14, 30)

DAYS_EMOJI = {3: "⚡", 7: "📅", 14: "🌟", 30: "👑"}
DAYS_COLOR = {
    3:  ButtonStyle.SUCCESS,
    7:  ButtonStyle.PRIMARY,
    14: ButtonStyle.PRIMARY,
    30: ButtonStyle.SUCCESS,
}

HELP_TEXT = (
    "🤖 <b>Grok Bot — хранилище аккаунтов</b>\n\n"
    "➕ <b>Добавление:</b>\n"
    "Отправь аккаунты в формате:\n"
    "<code>Email : почта@mail.com\n"
    "Password : пароль123</code>\n"
    "Бот покажет кнопки выбора срока подписки.\n\n"
    "📦 <b>Выдача по типу:</b>\n"
    "• /pop — выбрать тип и выдать аккаунт\n"
    "• /3day /7day /14day /30day — быстрая выдача\n\n"
    "📋 <b>Просмотр:</b>\n"
    "• /list — все аккаунты\n"
    "• /count — количество по типам\n"
    "• /get N — показать №N без удаления\n\n"
    "🗑 <b>Удаление:</b>\n"
    "• /use N [N2 N3…] — удалить по номерам\n"
    "• /clear — очистить всё хранилище\n\n"
    "❓ /help — это сообщение"
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
        # Если были старые аккаунты без поля — сохраняем сразу
        if changed:
            _save_raw(data)
        return data
    except Exception as e:
        log.exception("Failed to load %s: %s", DATA_FILE, e)
        return []


def _save_raw(accounts: list[dict]) -> None:
    """Внутренняя запись без блокировки (вызывать только внутри _lock)."""
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, DATA_FILE)


def save_accounts(accounts: list[dict]) -> None:
    _save_raw(accounts)


def parse_accounts(text: str) -> list[dict]:
    return [
        {"email": m.group(1), "password": m.group(2)}
        for m in ACCOUNT_RE.finditer(text)
    ]


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

    # Группируем по типу подписки
    groups: dict[int, list[tuple[int, dict]]] = {}
    for i, a in enumerate(accounts, 1):
        d = a.get("days", 30)
        groups.setdefault(d, []).append((i, a))

    lines = []
    for d in sorted(groups.keys()):
        emoji = DAYS_EMOJI.get(d, "📌")
        group = groups[d]
        lines.append(f"\n{emoji} <b>{d} дней</b> — {len(group)} шт.")
        for idx, a in group:
            lines.append(f"  {idx}. <code>{escape(a['email'])}</code>  |  <code>{escape(a['password'])}</code>")

    return "\n".join(lines)


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def days_keyboard(prefix: str) -> InlineKeyboardMarkup:
    """prefix = 'add' для добавления, 'pop' для выдачи."""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"⚡ 3 дня",
                callback_data=f"{prefix}_days:3",
                style=ButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                text=f"📅 7 дней",
                callback_data=f"{prefix}_days:7",
                style=ButtonStyle.PRIMARY,
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"🌟 14 дней",
                callback_data=f"{prefix}_days:14",
                style=ButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                text=f"👑 30 дней",
                callback_data=f"{prefix}_days:30",
                style=ButtonStyle.SUCCESS,
            ),
        ],
        [
            InlineKeyboardButton(
                text="Отмена",
                callback_data=f"{prefix}_cancel",
                style=ButtonStyle.DANGER,
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def is_admin(msg: Message) -> bool:
    return msg.from_user is not None and msg.from_user.id == ADMIN_ID

def is_admin_cb(cb: CallbackQuery) -> bool:
    return cb.from_user is not None and cb.from_user.id == ADMIN_ID


dp = Dispatcher()


# ─── Базовые команды ──────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_admin(message):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_admin(message):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("count"))
async def cmd_count(message: Message):
    if not is_admin(message):
        return
    accounts = load_accounts()
    total = len(accounts)
    if total == 0:
        await message.answer("📭 Хранилище пустое.")
        return
    breakdown: dict[int, int] = {}
    for a in accounts:
        d = a.get("days", 30)
        breakdown[d] = breakdown.get(d, 0) + 1
    lines = [f"📊 <b>В хранилище: {total} шт.</b>\n"]
    for d in VALID_DAYS:
        cnt = breakdown.get(d, 0)
        emoji = DAYS_EMOJI.get(d, "📌")
        lines.append(f"  {emoji} <b>{d} дней:</b> {cnt} шт.")
    for d, cnt in sorted(breakdown.items()):
        if d not in VALID_DAYS:
            lines.append(f"  📌 <b>{d} дней:</b> {cnt} шт.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message):
        return
    accounts = load_accounts()
    if not accounts:
        await message.answer("📭 Хранилище пустое.")
        return
    header = f"📋 <b>Хранилище — {len(accounts)} шт.</b>"
    body   = format_list(accounts)
    full   = header + "\n" + body
    for chunk_start in range(0, len(full), 3800):
        await message.answer(full[chunk_start:chunk_start + 3800], parse_mode="HTML")


@dp.message(Command("get"))
async def cmd_get(message: Message, command):
    if not is_admin(message):
        return
    args = (command.args or "").strip().split()
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: /get N")
        return
    n = int(args[0])
    accounts = load_accounts()
    if n < 1 or n > len(accounts):
        await message.answer(f"Нет аккаунта №{n}. Всего: {len(accounts)}.")
        return
    a = accounts[n - 1]
    await message.answer(
        f"#{n} {days_label(a.get('days', 30))}\n{format_account_block(a)}",
        parse_mode="HTML",
    )


# ─── /pop с выбором типа ──────────────────────────────────────────────────────

@dp.message(Command("pop"))
async def cmd_pop(message: Message):
    if not is_admin(message):
        return
    accounts = load_accounts()
    if not accounts:
        await message.answer("📭 Хранилище пустое.")
        return
    # Подсчитаем доступные типы
    available = {}
    for a in accounts:
        d = a.get("days", 30)
        available[d] = available.get(d, 0) + 1
    summary = "  ".join(
        f"{DAYS_EMOJI.get(d,'📌')}{d}д: {cnt}"
        for d, cnt in sorted(available.items())
    )
    await message.answer(
        f"📦 Выбери тип подписки для выдачи:\n<i>{summary}</i>",
        reply_markup=days_keyboard("pop"),
        parse_mode="HTML",
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
        f"📤 Выдан и удалён {days_label(days)}:\n"
        f"{format_account_block(match)}\n\n"
        f"<i>Осталось: {len(accounts)} шт.</i>",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "pop_cancel")
async def cb_pop_cancel(cb: CallbackQuery):
    if not is_admin_cb(cb):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await cb.answer("Отменено.")
    await cb.message.edit_text("❌ Выдача отменена.")


# ─── Быстрые команды /Nday ────────────────────────────────────────────────────

async def _pop_by_days(message: Message, days: int) -> None:
    async with _lock:
        accounts = load_accounts()
        match = next((a for a in accounts if a.get("days", 30) == days), None)
        if not match:
            await message.answer(f"📭 Нет аккаунтов с подпиской {days} дней.")
            return
        accounts.remove(match)
        save_accounts(accounts)
    await message.answer(
        f"📤 Выдан и удалён {days_label(days)}:\n"
        f"{format_account_block(match)}\n\n"
        f"<i>Осталось: {len(accounts)} шт.</i>",
        parse_mode="HTML",
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


# ─── Удаление по номерам ──────────────────────────────────────────────────────

@dp.message(Command("use"))
async def cmd_use(message: Message, command):
    if not is_admin(message):
        return
    args = (command.args or "").strip().split()
    if not args or not all(x.isdigit() for x in args):
        await message.answer("Использование: /use N  или  /use N1 N2 N3 ...")
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
        lines = [
            f"  №{n}: {escape(a['email'])} {days_label(a.get('days', 30))}"
            for n, a in sorted(removed)
        ]
        parts.append("🗑 <b>Удалены:</b>\n" + "\n".join(lines))
    if skipped:
        parts.append("⚠️ Не найдены: " + ", ".join(f"№{n}" for n in skipped))
    parts.append(f"<i>Осталось: {len(accounts)} шт.</i>")
    await message.answer("\n\n".join(parts), parse_mode="HTML")


# ─── Очистка ──────────────────────────────────────────────────────────────────

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    if not is_admin(message):
        return
    pending_clear.add(message.from_user.id)
    await message.answer(
        "⚠️ <b>Точно очистить ВСЁ хранилище?</b>\n"
        "Ответь /yes для подтверждения или /no для отмены.",
        parse_mode="HTML",
    )

@dp.message(Command("yes"))
async def cmd_yes(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in pending_clear:
        pending_clear.discard(message.from_user.id)
        async with _lock:
            save_accounts([])
        await message.answer("✅ Хранилище очищено.")

@dp.message(Command("no"))
async def cmd_no(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in pending_clear:
        pending_clear.discard(message.from_user.id)
        await message.answer("❌ Отменено.")


# ─── Добавление аккаунтов ────────────────────────────────────────────────────

@dp.message(F.text)
async def handle_text(message: Message):
    if not is_admin(message):
        return
    parsed = parse_accounts(message.text or "")
    if not parsed:
        await message.answer("Не нашёл аккаунтов. /help для справки.")
        return
    # Если уже было ожидающее добавление — предупреждаем
    if message.from_user.id in pending_add:
        await message.answer(
            "⚠️ Предыдущая партия заменена новой. Выбери срок для новых аккаунтов:"
        )
    pending_add[message.from_user.id] = parsed
    await message.answer(
        f"📥 Найдено <b>{len(parsed)}</b> аккаунт(ов).\n"
        f"Выбери срок подписки:",
        reply_markup=days_keyboard("add"),
        parse_mode="HTML",
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
    msg += f"\n<i>Всего в хранилище: {len(accounts)} шт.</i>"
    await cb.answer(f"Добавлено {added} шт.")
    await cb.message.edit_text(msg, parse_mode="HTML")


@dp.callback_query(F.data == "add_cancel")
async def cb_add_cancel(cb: CallbackQuery):
    if not is_admin_cb(cb):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    pending_add.pop(cb.from_user.id, None)
    await cb.answer("Отменено.")
    await cb.message.edit_text("❌ Добавление отменено.")


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Bot starting. Admin ID: %s. Data file: %s", ADMIN_ID, DATA_FILE.resolve())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
