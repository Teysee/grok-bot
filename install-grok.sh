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

# ─── OS check ─────────────────────────────────────────────────────────────────
if ! command -v apt-get &>/dev/null; then
    echo -e "${RED}[✗] Поддерживается только Ubuntu/Debian${NC}"; exit 1
fi

# ─── System deps ──────────────────────────────────────────────────────────────
echo -e "${YELLOW}[→] Устанавливаю системные пакеты...${NC}"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip

# ─── Install dir ──────────────────────────────────────────────────────────────
echo -e "${YELLOW}[→] Папка установки: ${INSTALL_DIR}${NC}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ─── Write bot.py ─────────────────────────────────────────────────────────────
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
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grok-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATA_FILE = Path(os.getenv("DATA_FILE", "accounts.json"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var is required")
if not ADMIN_ID:
    raise SystemExit("ADMIN_ID env var is required")

ACCOUNT_RE = re.compile(
    r"Email\s*[:\-]\s*(\S+)\s*[\r\n]+\s*Password\s*[:\-]\s*(\S+)",
    re.IGNORECASE,
)

HELP_TEXT = (
    "Бот-хранилище аккаунтов Grok.\n\n"
    "Команды:\n"
    "• Просто пришли список аккаунтов в формате\n"
    "   Email : ...\n"
    "   Password : ...\n"
    "  — бот сам распарсит и добавит.\n"
    "• /list — показать все аккаунты с номерами\n"
    "• /count — сколько аккаунтов в наличии\n"
    "• /use N — пометить аккаунт №N как использованный и удалить\n"
    "• /use N1 N2 N3 — удалить сразу несколько\n"
    "• /get N — показать аккаунт №N (без удаления)\n"
    "• /pop — выдать первый аккаунт и удалить его\n"
    "• /clear — очистить хранилище (требует подтверждения)\n"
    "• /help — это сообщение"
)


def load_accounts() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.exception("Failed to load %s: %s", DATA_FILE, e)
        return []


def save_accounts(accounts: list[dict]) -> None:
    DATA_FILE.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_accounts(text: str) -> list[dict]:
    return [{"email": m.group(1), "password": m.group(2)} for m in ACCOUNT_RE.finditer(text)]


def format_list(accounts: list[dict]) -> str:
    if not accounts:
        return "Хранилище пустое."
    lines = []
    for i, a in enumerate(accounts, 1):
        lines.append(f"{i}. {a['email']}  |  {a['password']}")
    return "\n".join(lines)


def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == ADMIN_ID


dp = Dispatcher()
pending_clear: set[int] = set()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_admin(message):
        return
    await message.answer(HELP_TEXT)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_admin(message):
        return
    await message.answer(HELP_TEXT)


@dp.message(Command("count"))
async def cmd_count(message: Message):
    if not is_admin(message):
        return
    accounts = load_accounts()
    await message.answer(f"В хранилище: {len(accounts)} шт.")


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
    block = f"<code>Email : {escape(a['email'])}\nPassword : {escape(a['password'])}</code>"
    await message.answer(f"{n}.\n{block}", parse_mode="HTML")


@dp.message(Command("pop"))
async def cmd_pop(message: Message):
    if not is_admin(message):
        return
    accounts = load_accounts()
    if not accounts:
        await message.answer("Хранилище пустое.")
        return
    a = accounts.pop(0)
    save_accounts(accounts)
    block = f"<code>Email : {escape(a['email'])}\nPassword : {escape(a['password'])}</code>"
    await message.answer(
        f"Выдан и удалён:\n{block}\n\nОсталось: {len(accounts)} шт.",
        parse_mode="HTML",
    )


@dp.message(Command("use"))
async def cmd_use(message: Message, command):
    if not is_admin(message):
        return
    args = (command.args or "").strip().split()
    if not args or not all(x.isdigit() for x in args):
        await message.answer("Использование: /use N  или  /use N1 N2 N3 ...")
        return
    indexes = sorted({int(x) for x in args}, reverse=True)
    accounts = load_accounts()
    removed = []
    skipped = []
    for n in indexes:
        if 1 <= n <= len(accounts):
            removed.append((n, accounts.pop(n - 1)))
        else:
            skipped.append(n)
    save_accounts(accounts)
    parts = []
    if removed:
        removed_lines = [f"№{n}: {a['email']}" for n, a in sorted(removed)]
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
    await message.answer("Точно очистить ВСЁ хранилище? Ответь /yes для подтверждения или /no для отмены.")


@dp.message(Command("yes"))
async def cmd_yes(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in pending_clear:
        pending_clear.discard(message.from_user.id)
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
    accounts = load_accounts()
    existing_emails = {a["email"].lower() for a in accounts}
    added = 0
    duplicates = 0
    for a in parsed:
        if a["email"].lower() in existing_emails:
            duplicates += 1
            continue
        accounts.append(a)
        existing_emails.add(a["email"].lower())
        added += 1
    save_accounts(accounts)
    msg = f"Добавлено: {added} шт."
    if duplicates:
        msg += f"\nПропущено дубликатов: {duplicates}"
    msg += f"\nВсего в хранилище: {len(accounts)} шт."
    await message.answer(msg)


async def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Bot starting. Admin ID: %s. Data file: %s", ADMIN_ID, DATA_FILE.resolve())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
BOTEOF
echo -e "${GREEN}[✓] bot.py создан${NC}"

# ─── Write requirements.txt ───────────────────────────────────────────────────
cat > requirements.txt << 'EOF'
aiogram>=3.4,<4
EOF

# ─── Virtual env + deps ───────────────────────────────────────────────────────
echo -e "${YELLOW}[→] Создаю виртуальное окружение...${NC}"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo -e "${GREEN}[✓] Зависимости установлены${NC}"

# ─── Config ───────────────────────────────────────────────────────────────────
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

# ─── Systemd service ──────────────────────────────────────────────────────────
echo -e "${YELLOW}[→] Создаю systemd-сервис ${SERVICE_NAME}...${NC}"

# Загружаем переменные из .env для сервиса
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

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}"
echo "╔══════════════════════════════════╗"
echo "║    ✅  Установка завершена!      ║"
echo "╚══════════════════════════════════╝"
echo -e "${NC}"
echo -e "Папка бота:  ${CYAN}${INSTALL_DIR}${NC}"
echo -e "Файл данных: ${CYAN}${INSTALL_DIR}/accounts.json${NC}"
echo ""
echo -e "Команды управления:"
echo -e "  ${CYAN}sudo systemctl start ${SERVICE_NAME}${NC}    — запустить"
echo -e "  ${CYAN}sudo systemctl stop ${SERVICE_NAME}${NC}     — остановить"
echo -e "  ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}  — перезапустить"
echo -e "  ${CYAN}sudo systemctl status ${SERVICE_NAME}${NC}   — статус"
echo -e "  ${CYAN}journalctl -u ${SERVICE_NAME} -f${NC}         — логи"
echo ""
read -rp "Запустить бота сейчас? [y/N]: " START_NOW
if [[ "${START_NOW,,}" == "y" ]]; then
    sudo systemctl start ${SERVICE_NAME}
    echo -e "${GREEN}[✓] Бот запущен!${NC}"
    echo -e "Логи: ${CYAN}journalctl -u ${SERVICE_NAME} -f${NC}"
else
    echo -e "Запустите: ${CYAN}sudo systemctl start ${SERVICE_NAME}${NC}"
fi
