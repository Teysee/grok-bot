#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

INSTALL_DIR="$HOME/grok-bot"
SERVICE_NAME="grok-bot"

echo -e "${CYAN}${BOLD}"
echo "╔══════════════════════════════════╗"
echo "║      Grok Bot  Installer         ║"
echo "║  Хранилище аккаунтов Grok        ║"
echo "╚══════════════════════════════════╝"
echo -e "${NC}"

if ! command -v apt-get &>/dev/null; then
    echo -e "${RED}[✗] Поддерживается только Ubuntu/Debian${NC}"; exit 1
fi

echo -e "${YELLOW}[→] Устанавливаю системные пакеты...${NC}"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip

echo -e "${YELLOW}[→] Папка установки: ${INSTALL_DIR}${NC}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo -e "${YELLOW}[→] Создаю bot.py...${NC}"
cat > bot.py << 'BOTEOF'
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

HELP_TEXT = (
    "Бот-хранилище аккаунтов Grok.\n\n"
    "<b>Добавление аккаунтов:</b>\n"
    "Просто пришли список в формате:\n"
    "<code>Email : ...\nPassword : ...</code>\n"
    "Бот распарсит и спросит срок подписки.\n\n"
    "<b>Команды:</b>\n"
    "• /list — все аккаунты с типом подписки\n"
    "• /count — сколько аккаунтов по каждому типу\n"
    "• /3day — выдать и удалить первый 3-дневный\n"
    "• /7day — выдать и удалить первый 7-дневный\n"
    "• /14day — выдать и удалить первый 14-дневный\n"
    "• /30day — выдать и удалить первый 30-дневный\n"
    "• /pop — выдать и удалить первый (любой тип)\n"
    "• /get N — показать аккаунт №N (без удаления)\n"
    "• /use N [N2 N3…] — удалить по номерам\n"
    "• /clear — очистить хранилище\n"
    "• /help — это сообщение"
)

_lock = asyncio.Lock()
pending_add: dict[int, list[dict]] = {}
pending_clear: set[int] = set()


def load_accounts() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        for acc in data:
            if "days" not in acc:
                acc["days"] = 30
        return data
    except Exception as e:
        log.exception("Failed to load %s: %s", DATA_FILE, e)
        return []


def save_accounts(accounts: list[dict]) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, DATA_FILE)


def parse_accounts(text: str) -> list[dict]:
    return [
        {"email": m.group(1), "password": m.group(2)}
        for m in ACCOUNT_RE.finditer(text)
    ]


def days_label(days: int) -> str:
    return {3: "3д", 7: "7д", 14: "14д", 30: "30д"}.get(days, f"{days}д")


def format_list(accounts: list[dict]) -> str:
    if not accounts:
        return "Хранилище пустое."
    lines = []
    for i, a in enumerate(accounts, 1):
        tag = days_label(a.get("days", 30))
        lines.append(f"{i}. {a['email']}  |  {a['password']}  [{tag}]")
    return "\n".join(lines)


def format_account_block(a: dict) -> str:
    return (
        f"<code>Email : {escape(a['email'])}\n"
        f"Password : {escape(a['password'])}</code>"
    )


def days_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="⚡ 3 дня",   callback_data="add_days:3",  style=ButtonStyle.SUCCESS),
            InlineKeyboardButton(text="📅 7 дней",  callback_data="add_days:7",  style=ButtonStyle.PRIMARY),
        ],
        [
            InlineKeyboardButton(text="🌟 14 дней", callback_data="add_days:14", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="👑 30 дней", callback_data="add_days:30", style=ButtonStyle.SUCCESS),
        ],
        [InlineKeyboardButton(text="Отмена",        callback_data="add_cancel",  style=ButtonStyle.DANGER)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == ADMIN_ID


def is_admin_cb(cb: CallbackQuery) -> bool:
    return cb.from_user is not None and cb.from_user.id == ADMIN_ID


dp = Dispatcher()


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
        await message.answer("Хранилище пустое.")
        return
    breakdown = {}
    for a in accounts:
        d = a.get("days", 30)
        breakdown[d] = breakdown.get(d, 0) + 1
    lines = [f"В хранилище: <b>{total}</b> шт.\n"]
    for d in VALID_DAYS:
        cnt = breakdown.get(d, 0)
        if cnt:
            lines.append(f"  • {d} дней: <b>{cnt}</b>")
    for d, cnt in sorted(breakdown.items()):
        if d not in VALID_DAYS:
            lines.append(f"  • {d} дней: <b>{cnt}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message):
        return
    accounts = load_accounts()
    text = format_list(accounts)
    header = f"Всего: {len(accounts)} шт.\n\n"
    full = header + text
    for chunk_start in range(0, len(full), 3500):
        await message.answer(full[chunk_start:chunk_start + 3500])


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
    tag = days_label(a.get("days", 30))
    await message.answer(
        f"{n}. [{tag}]\n{format_account_block(a)}",
        parse_mode="HTML",
    )


@dp.message(Command("pop"))
async def cmd_pop(message: Message):
    if not is_admin(message):
        return
    async with _lock:
        accounts = load_accounts()
        if not accounts:
            await message.answer("Хранилище пустое.")
            return
        a = accounts.pop(0)
        save_accounts(accounts)
    tag = days_label(a.get("days", 30))
    await message.answer(
        f"Выдан и удалён [{tag}]:\n{format_account_block(a)}\n\nОсталось: {len(accounts)} шт.",
        parse_mode="HTML",
    )


async def pop_by_days(message: Message, days: int) -> None:
    async with _lock:
        accounts = load_accounts()
        match = next((a for a in accounts if a.get("days", 30) == days), None)
        if not match:
            await message.answer(f"Нет аккаунтов с подпиской {days} дней.")
            return
        accounts.remove(match)
        save_accounts(accounts)
    await message.answer(
        f"Выдан и удалён [{days}д]:\n{format_account_block(match)}\n\nОсталось: {len(accounts)} шт.",
        parse_mode="HTML",
    )


@dp.message(Command("3day"))
async def cmd_3day(message: Message):
    if not is_admin(message): return
    await pop_by_days(message, 3)


@dp.message(Command("7day"))
async def cmd_7day(message: Message):
    if not is_admin(message): return
    await pop_by_days(message, 7)


@dp.message(Command("14day"))
async def cmd_14day(message: Message):
    if not is_admin(message): return
    await pop_by_days(message, 14)


@dp.message(Command("30day"))
async def cmd_30day(message: Message):
    if not is_admin(message): return
    await pop_by_days(message, 30)


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
        removed_lines = [
            f"№{n}: {a['email']} [{days_label(a.get('days', 30))}]"
            for n, a in sorted(removed)
        ]
        parts.append("Удалены:\n" + "\n".join(removed_lines))
    if skipped:
        parts.append("Не найдены: " + ", ".join(f"№{n}" for n in skipped))
    parts.append(f"Осталось: {len(accounts)} шт.")
    await message.answer("\n\n".join(parts))


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    if not is_admin(message):
        return
    pending_clear.add(message.from_user.id)
    await message.answer(
        "Точно очистить ВСЁ хранилище? Ответь /yes для подтверждения или /no для отмены."
    )


@dp.message(Command("yes"))
async def cmd_yes(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in pending_clear:
        pending_clear.discard(message.from_user.id)
        async with _lock:
            save_accounts([])
        await message.answer("Хранилище очищено.")


@dp.message(Command("no"))
async def cmd_no(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in pending_clear:
        pending_clear.discard(message.from_user.id)
        await message.answer("Отменено.")


@dp.message(F.text)
async def handle_text(message: Message):
    if not is_admin(message):
        return
    parsed = parse_accounts(message.text or "")
    if not parsed:
        await message.answer("Не нашёл аккаунтов в сообщении. /help для справки.")
        return
    pending_add[message.from_user.id] = parsed
    await message.answer(
        f"Найдено <b>{len(parsed)}</b> аккаунт(ов).\nВыбери срок подписки:",
        reply_markup=days_keyboard(),
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
    tag = days_label(days)
    msg = f"✅ Добавлено: <b>{added}</b> шт. [{tag}]"
    if dupes:
        msg += f"\nПропущено дублей: {dupes}"
    msg += f"\nВсего в хранилище: {len(accounts)} шт."
    await cb.answer(f"Добавлено {added} шт. [{tag}]")
    await cb.message.edit_text(msg, parse_mode="HTML")


@dp.callback_query(F.data == "add_cancel")
async def cb_add_cancel(cb: CallbackQuery):
    if not is_admin_cb(cb):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    pending_add.pop(cb.from_user.id, None)
    await cb.answer("Отменено.")
    await cb.message.edit_text("❌ Добавление отменено.")


async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Bot starting. Admin ID: %s. Data file: %s", ADMIN_ID, DATA_FILE.resolve())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
BOTEOF
echo -e "${GREEN}[✓] bot.py создан${NC}"

cat > requirements.txt << 'EOF'
aiogram>=3.20,<4
EOF

echo -e "${YELLOW}[→] Создаю виртуальное окружение...${NC}"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo -e "${GREEN}[✓] Зависимости установлены${NC}"

echo ""
echo -e "${CYAN}${BOLD}══════════════════════════════════════${NC}"
echo -e "${BOLD}  Настройка бота${NC}"
echo -e "${CYAN}══════════════════════════════════════${NC}"

if [ -f ".env" ]; then
    echo -e "${GREEN}[✓] .env уже существует, пропускаю${NC}"
else
    read -rp "  Telegram токен бота (BOT_TOKEN): " BOT_TOKEN
    read -rp "  Ваш Telegram ID    (ADMIN_ID):   " ADMIN_ID
    printf "BOT_TOKEN=%s\nADMIN_ID=%s\n" "$BOT_TOKEN" "$ADMIN_ID" > .env
    echo -e "${GREEN}[✓] .env создан${NC}"
fi

echo -e "${YELLOW}[→] Создаю systemd-сервис ${SERVICE_NAME}...${NC}"

source .env 2>/dev/null || true

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Grok Bot — хранилище аккаунтов
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python -u bot.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=${INSTALL_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
echo -e "${GREEN}[✓] Сервис включён в автозапуск${NC}"

echo ""
echo -e "${GREEN}${BOLD}"
echo "╔══════════════════════════════════╗"
echo "║    ✅  Установка завершена!      ║"
echo "╚══════════════════════════════════╝"
echo -e "${NC}"
echo -e "Команды управления:"
echo -e "  ${CYAN}sudo systemctl start ${SERVICE_NAME}${NC}    — запустить"
echo -e "  ${CYAN}sudo systemctl stop ${SERVICE_NAME}${NC}     — остановить"
echo -e "  ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}  — перезапустить"
echo -e "  ${CYAN}journalctl -u ${SERVICE_NAME} -f${NC}         — логи"
echo ""
read -rp "Запустить бота сейчас? [y/N]: " START_NOW
if [[ "${START_NOW,,}" == "y" ]]; then
    sudo systemctl start ${SERVICE_NAME}
    echo -e "${GREEN}[✓] Бот запущен!${NC}"
else
    echo -e "Запустите: ${CYAN}sudo systemctl start ${SERVICE_NAME}${NC}"
fi
