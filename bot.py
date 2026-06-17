import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, List, Tuple, Any

import requests
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


# === Configuration ===
# Telegram bot token provided by the user (DO NOT SHARE IN PUBLIC REPOS)
TELEGRAM_TOKEN = "8322129898:AAE3PuhQwP8_ixQrGOpdvvrNLw1Esqm1YHk"

# Crypto Pay API token (create in @CryptoBot -> Crypto Pay). Read from env var.
# Example: setx CRYPTO_PAY_TOKEN "YOUR:CRYPTO:PAY:TOKEN"
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "475075:AAWLkbI6x6heGhmhfNHRmRjettxbbcPB1fF")

# Admin username who can use the admin panel
ADMIN_USERNAME = "doxplay"
# Admin Telegram numeric ID
ADMIN_ID = 7796528949

# Database file path
DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")

# Reusable constants
REPORT_STARTED_TEXT = (
    "Бот начал запуск репортов, дальнейшие действия зависят уже от модерации телеграмма"
)


# === Data layer ===
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_vip INTEGER NOT NULL DEFAULT 0,
            vip_until INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            asset TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """
    )
    # Bans table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY,
            reason TEXT,
            banned_at INTEGER NOT NULL
        )
        """
    )
    # Logs table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def upsert_user(user_id: int, username: Optional[str]) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users(user_id, username) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (user_id, username),
    )
    conn.commit()
    conn.close()


def set_vip(user_id: int, days: int) -> int:
    now = int(time.time())
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT vip_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    current_until = row[0] if row and row[0] else 0
    base = max(now, int(current_until))
    new_until = base + days * 24 * 3600
    cur.execute(
        "UPDATE users SET is_vip=1, vip_until=? WHERE user_id=?",
        (new_until, user_id),
    )
    conn.commit()
    conn.close()
    return new_until


def revoke_vip(user_id: int) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_vip=0, vip_until=NULL WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def list_vips() -> List[sqlite3.Row]:
    now = int(time.time())
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE is_vip=1 AND (vip_until IS NULL OR vip_until>?) ORDER BY vip_until DESC",
        (now,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def list_users(limit: Optional[int] = None) -> List[sqlite3.Row]:
    """Return users with basic fields for listing."""
    conn = get_db()
    cur = conn.cursor()
    base_sql = "SELECT user_id, username, is_vip, vip_until FROM users ORDER BY user_id DESC"
    if limit is not None:
        cur.execute(f"{base_sql} LIMIT ?", (int(limit),))
    else:
        cur.execute(base_sql)
    rows = cur.fetchall()
    conn.close()
    return rows

def count_users() -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    count = cur.fetchone()[0]
    conn.close()
    return int(count)

def get_all_user_ids(exclude_banned: bool = True) -> List[int]:
    conn = get_db()
    cur = conn.cursor()
    if exclude_banned:
        cur.execute(
            """
            SELECT u.user_id
            FROM users u
            WHERE NOT EXISTS (SELECT 1 FROM bans b WHERE b.user_id = u.user_id)
            ORDER BY u.user_id ASC
            """
        )
    else:
        cur.execute("SELECT user_id FROM users ORDER BY user_id ASC")
    ids = [int(r[0]) for r in cur.fetchall()]
    conn.close()
    return ids


def save_invoice(invoice_id: str, user_id: int, status: str, asset: str, amount: float) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO invoices(invoice_id, user_id, status, asset, amount, created_at) VALUES(?,?,?,?,?,?)",
        (invoice_id, user_id, status, asset, amount, int(time.time())),
    )
    conn.commit()
    conn.close()


def update_invoice_status(invoice_id: str, status: str) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE invoices SET status=? WHERE invoice_id=?", (status, invoice_id))
    conn.commit()
    conn.close()


def is_vip(user_id: int) -> bool:
    row = get_user(user_id)
    if not row:
        return False
    if not row["is_vip"]:
        return False
    vip_until = row["vip_until"]
    if vip_until is None:
        return True
    return int(vip_until) > int(time.time())


# === Bans & Logs helpers ===
def ban_user(user_id: int, reason: str | None = None) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bans(user_id, reason, banned_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, banned_at=excluded.banned_at",
        (user_id, reason or "", int(time.time())),
    )
    conn.commit()
    conn.close()


def unban_user(user_id: int) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def is_banned(user_id: int) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def count_banned() -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bans")
    count = cur.fetchone()[0]
    conn.close()
    return int(count)


def log_event(user_id: Optional[int], action: str, details: Optional[str] = None) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO logs(user_id, action, details, created_at) VALUES(?,?,?,?)",
        (user_id, action, details, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_logs(user_id: Optional[int] = None, limit: int = 20) -> List[sqlite3.Row]:
    conn = get_db()
    cur = conn.cursor()
    if user_id is None:
        cur.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    else:
        cur.execute(
            "SELECT * FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
    rows = cur.fetchall()
    conn.close()
    return rows


def resolve_user_id_from_token(token: str) -> Optional[int]:
    token = token.strip()
    if not token:
        return None
    # If token is numeric user_id
    if token.lstrip("-+").isdigit():
        try:
            return int(token)
        except Exception:
            return None
    # If token is @username
    if token.startswith("@"):
        token = token[1:]
    uname = token.lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (uname,))
    row = cur.fetchone()
    conn.close()
    if row:
        return int(row[0])
    return None


# === CryptoBot API client ===
class CryptoPayClient:
    def __init__(self, api_token: str) -> None:
        self.api_token = api_token
        self.base = "https://pay.crypt.bot/api"

    def _headers(self) -> dict:
        return {"Crypto-Pay-API-Token": self.api_token}

    def create_invoice(self, amount: float, asset: str, description: str, payload: str) -> Tuple[str, str]:
        url = f"{self.base}/createInvoice"
        data = {
            "amount": amount,
            "asset": asset,
            "description": description,
            "payload": payload,
        }
        resp = requests.post(url, headers=self._headers(), data=data, timeout=20)
        resp.raise_for_status()
        j = resp.json()
        if not j.get("ok"):
            raise RuntimeError(f"CryptoPay error: {j}")
        result = j["result"]
        return result["invoice_id"], result["pay_url"]

    def get_invoice(self, invoice_id: str) -> dict:
        url = f"{self.base}/getInvoices"
        params = {"invoice_ids": invoice_id}
        resp = requests.get(url, headers=self._headers(), params=params, timeout=20)
        resp.raise_for_status()
        j = resp.json()
        if not j.get("ok"):
            raise RuntimeError(f"CryptoPay error: {j}")
        items = j["result"]["items"]
        if not items:
            raise RuntimeError("Invoice not found")
        return items[0]


# === Bot setup ===
dp = Dispatcher(storage=MemoryStorage())
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
crypto = CryptoPayClient(CRYPTO_PAY_TOKEN) if CRYPTO_PAY_TOKEN else None


# === Helpers ===
def is_admin_id(user_id: int, username: Optional[str] = None) -> bool:
    if user_id == ADMIN_ID:
        return True
    if username:
        return (username or "").lower() == ADMIN_USERNAME.lower()
    return False


def is_admin(message: Message | CallbackQuery) -> bool:
    u = message.from_user if isinstance(message, (Message, CallbackQuery)) else None
    if not u:
        return False
    return is_admin_id(u.id, u.username)


def vip_required_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Купить VIP", callback_data="shop")
    return kb.as_markup()


PLANS = [
    ("🗓️ 1 день — $2.90", 1, 2.90),
    ("🗓️ 3 дня — $5.90", 3, 5.90),
    ("🗓️ 7 дней — $7.90", 7, 7.90),
    ("🗓️ 15 дней — $11.99", 15, 11.99),
    ("🗓️ 30 дней — $14.90", 30, 14.90),
    ("💎 Навсегда — $29.90", 36500, 29.90),
]

ASSETS = ["💵 USDT", "💠 TON"]


def shop_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, days, price in PLANS:
        kb.button(text=label, callback_data=f"plan:{days}:{price}")
    kb.adjust(2)
    return kb.as_markup()


def assets_kb(days: int, price: float) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for asset in ASSETS:
        asset_code = "USDT" if "USDT" in asset else "TON"
        kb.button(text=asset, callback_data=f"asset:{asset_code}:{days}:{price}")
    kb.button(text="◀️ Назад", callback_data="shop")
    kb.adjust(1)
    return kb.as_markup()


def pay_kb(pay_url: str, invoice_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Оплатить", url=pay_url)
    kb.button(text="✅ Проверить оплату", callback_data=f"check:{invoice_id}")
    kb.button(text="◀️ Назад", callback_data="shop")
    kb.adjust(1)
    return kb.as_markup()


def methods_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Метод Группа", callback_data="m:group")
    kb.button(text="📣 Метод Канал", callback_data="m:channel")
    kb.button(text="🤖 Метод BOT", callback_data="m:bot")
    kb.button(text="✉️ Метод Email", callback_data="m:email")
    kb.button(text="🌐 Метод Web", callback_data="m:web")
    kb.button(text="👤 Метод Username", callback_data="m:username")
    kb.button(text="⚡ ATK запрос", callback_data="m:atk")
    kb.adjust(2)
    return kb.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Бан", callback_data="admin:ban")
    kb.button(text="✅ Разбан", callback_data="admin:unban")
    kb.button(text="ℹ️ Инфо пользователя", callback_data="admin:userinfo")
    kb.button(text="📜 Логи", callback_data="admin:logs")
    kb.button(text="👑 Снять VIP", callback_data="admin:revokevip")
    kb.button(text="👑 Выдать VIP", callback_data="admin:grantvip")
    kb.button(text="📢 Рассылка", callback_data="admin:broadcast")
    kb.button(text="👥 Пользователи", callback_data="admin:users")
    kb.adjust(2)
    return kb.as_markup()


# === FSM States ===
class ReportStates(StatesGroup):
    waiting_link = State()


class UsernameReportStates(StatesGroup):
    waiting_username = State()
    waiting_violation_link = State()


class AdminStates(StatesGroup):
    ban_target = State()
    unban_target = State()
    user_info_target = State()
    revoke_vip_target = State()
    grant_vip_target = State()
    logs_target = State()
    broadcast_message = State()


# === Middleware ===
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id: Optional[int] = None
        username: Optional[str] = None
        if isinstance(event, Message):
            if event.from_user:
                user_id = event.from_user.id
                username = event.from_user.username
        elif isinstance(event, CallbackQuery):
            if event.from_user:
                user_id = event.from_user.id
                username = event.from_user.username
        # Track interacting users so we can broadcast later
        if user_id is not None:
            try:
                upsert_user(int(user_id), username)
            except Exception:
                pass
        # Allow admin always
        if user_id is not None and is_admin_id(user_id, username):
            return await handler(event, data)
        # Block banned
        if user_id is not None and is_banned(user_id):
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ Вы заблокированы.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⛔ Вы заблокированы.")
            except Exception:
                pass
            return None
        return await handler(event, data)


# Register middleware for messages and callbacks
dp.message.middleware(BanMiddleware())
dp.callback_query.middleware(BanMiddleware())


# === Handlers ===
@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    upsert_user(message.from_user.id, message.from_user.username)
    log_event(message.from_user.id, "start", None)
    text = (
        "<b>🔥 Добро пожаловать в VIP-центр!</b>\n\n"
        "Оформите подписку и получите доступ к мощным инструментам сноса.\n"
        "<i>Будьте аккуратны и используйте функционал ответственно.</i>\n\n"
        "🛍️ <b>/shop</b> — магазин подписок\n"
        "🛠️ <b>/methods</b> — функции VIP\n"
        "👑 <b>/status</b> — статус подписки\n\n"
        "🛡️ <b>/admin</b> — админ-панель (для администратора)"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍️ Магазин", callback_data="shop")
    kb.button(text="🛠️ Функции", callback_data="methods")
    if is_admin(message):
        kb.button(text="🛡️ Админ", callback_data="admin:open")
    await message.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data == "shop")
@dp.message(Command("shop"))
async def shop(entry: Message | CallbackQuery) -> None:
    target = entry.message if isinstance(entry, CallbackQuery) else entry
    log_event(target.from_user.id if target.from_user else None, "shop_open", None)
    await target.answer(
        "<b>💳 Выберите срок подписки:</b>", reply_markup=shop_kb()
    )


@dp.callback_query(F.data.startswith("plan:"))
async def choose_plan(callback: CallbackQuery) -> None:
    _, days, price = callback.data.split(":", 2)
    log_event(
        callback.from_user.id,
        "choose_plan",
        f"days={days}; price={price}",
    )
    await callback.message.edit_text(
        "<b>💱 Выберите валюту для оплаты:</b>",
        reply_markup=assets_kb(int(days), float(price)),
    )


@dp.callback_query(F.data.startswith("asset:"))
async def choose_asset(callback: CallbackQuery) -> None:
    if not crypto:
        await callback.answer("Crypto Pay токен не настроен", show_alert=True)
        return
    _, asset, days, price = callback.data.split(":", 3)
    days_i = int(days)
    price_f = float(price)

    description = f"VIP {days_i}d for @{callback.from_user.username or callback.from_user.id}"
    payload = f"{callback.from_user.id}|{days_i}"
    try:
        invoice_id, pay_url = crypto.create_invoice(price_f, asset, description, payload)
        log_event(callback.from_user.id, "invoice_created", f"{invoice_id}; {asset}; {price_f}")
    except Exception as e:
        log_event(callback.from_user.id, "invoice_error", str(e))
        await callback.answer(f"Ошибка создания счёта: {e}", show_alert=True)
        return

    save_invoice(invoice_id, callback.from_user.id, "active", asset, price_f)
    await callback.message.edit_text(
        f"<b>🧾 Счёт ({asset})</b>:\nОтправляете: <b>{price_f} {asset}</b>\n\n"
        f"🔗 Ссылка на оплату:\n{pay_url}",
        reply_markup=pay_kb(pay_url, invoice_id),
        disable_web_page_preview=True,
    )


@dp.callback_query(F.data.startswith("check:"))
async def check_payment(callback: CallbackQuery) -> None:
    if not crypto:
        await callback.answer("Crypto Pay токен не настроен", show_alert=True)
        return
    _, invoice_id = callback.data.split(":", 1)
    try:
        inv = crypto.get_invoice(invoice_id)
    except Exception as e:
        log_event(callback.from_user.id, "invoice_check_error", str(e))
        await callback.answer(f"Ошибка запроса: {e}", show_alert=True)
        return

    status = inv.get("status")
    update_invoice_status(invoice_id, status)
    log_event(callback.from_user.id, "invoice_status", f"{invoice_id}; {status}")
    if status == "paid":
        payload = inv.get("payload", "")
        try:
            user_id_str, days_str = (payload or "").split("|", 1)
            days = int(days_str)
        except Exception:
            days = 1
        new_until = set_vip(callback.from_user.id, days)
        until_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_until))
        log_event(callback.from_user.id, "vip_activated", f"until={until_str}")
        await callback.message.edit_text(
            f"Платёж подтверждён. VIP активен до: <b>{until_str}</b>",
            reply_markup=methods_kb(),
        )
    elif status in {"active", "pending"}:
        await callback.answer("Оплата не найдена. Попробуйте позже.", show_alert=True)
    else:
        await callback.answer(f"Статус счёта: {status}", show_alert=True)


@dp.message(Command("status"))
async def status(message: Message) -> None:
    log_event(message.from_user.id, "status_checked", None)
    if is_vip(message.from_user.id):
        row = get_user(message.from_user.id)
        until = row["vip_until"]
        until_str = "бессрочно" if not until else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(until)))
        await message.answer(f"👑 Ваш статус: <b>VIP</b>\n⏳ Действует до: <b>{until_str}</b>")
    else:
        await message.answer("❌ VIP не активен.", reply_markup=vip_required_kb())


@dp.callback_query(F.data == "methods")
@dp.message(Command("methods"))
async def methods(entry: Message | CallbackQuery) -> None:
    target = entry.message if isinstance(entry, CallbackQuery) else entry
    user_id = target.chat.id
    if not is_vip(user_id):
        await target.answer("Требуется VIP.")
        return
    log_event(user_id, "methods_open", None)
    await target.answer("<b>🧰 Доступные методы:</b>", reply_markup=methods_kb())


@dp.callback_query(F.data.startswith("m:"))
async def method_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_vip(callback.from_user.id):
        await callback.answer("Требуется VIP", show_alert=True)
        return
    _, method = callback.data.split(":", 1)
    log_event(callback.from_user.id, "method_selected", method)
    await callback.answer()
    if method == "username":
        await state.set_state(UsernameReportStates.waiting_username)
        await state.update_data(method=method)
        await callback.message.answer("Введите username для сноса")
    else:
        await state.set_state(ReportStates.waiting_link)
        await state.update_data(method=method)
        await callback.message.answer("Отправьте ссылку на сообщение с нарушением")


@dp.message(StateFilter(ReportStates.waiting_link))
async def handle_report_link(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    method = data.get("method", "")
    link = (message.text or "").strip()
    if not link:
        await message.answer("Нужна ссылка на сообщение с нарушением")
        return
    # Notify user and admin
    await message.answer(REPORT_STARTED_TEXT)
    user = message.from_user
    admin_text = (
        f"⚠️ Новый репорт от <a href=\"tg://user?id={user.id}\">@{user.username or user.id}</a>\n"
        f"Метод: <b>{method}</b>\n"
        f"Ссылка: {link}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, disable_web_page_preview=True)
    except Exception:
        pass
    log_event(message.from_user.id, "report_submitted", f"{method}; {link}")
    await state.clear()


@dp.message(StateFilter(UsernameReportStates.waiting_username))
async def handle_username_input(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("Введите username для сноса")
        return
    await state.update_data(username=username)
    await state.set_state(UsernameReportStates.waiting_violation_link)
    await message.answer("Теперь отправьте ссылку на сообщение с нарушением или банводом")


@dp.message(StateFilter(UsernameReportStates.waiting_violation_link))
async def handle_username_violation_link(message: Message, state: FSMContext) -> None:
    link = (message.text or "").strip()
    if not link:
        await message.answer("Нужна ссылка на сообщение с нарушением или банводом")
        return
    data = await state.get_data()
    method = data.get("method", "username")
    username = data.get("username", "")
    await message.answer(REPORT_STARTED_TEXT)
    user = message.from_user
    admin_text = (
        f"⚠️ Новый репорт (username) от <a href=\"tg://user?id={user.id}\">@{user.username or user.id}</a>\n"
        f"Метод: <b>{method}</b>\n"
        f"Username: <code>{username}</code>\n"
        f"Ссылка: {link}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, disable_web_page_preview=True)
    except Exception:
        pass
    log_event(message.from_user.id, "report_submitted_username", f"{username}; {link}")
    await state.clear()


# === Admin ===
@dp.message(Command("admin"))
async def admin(message: Message) -> None:
    if not is_admin(message):
        return
    total_vips = len(list_vips())
    total_banned = count_banned()
    total_users = count_users()
    await message.answer(
        "<b>🛡️ Админ-панель</b>\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"👑 VIP пользователей: <b>{total_vips}</b>\n"
        f"🚫 Забанено: <b>{total_banned}</b>\n\n"
        "Команды:\n"
        "• /grant_vip <code>user_id</code> <code>days</code> — выдать VIP\n"
        "• /revoke_vip <code>user_id</code> — снять VIP\n"
        "• /ban <code>user_id|@username</code> [reason] — бан\n"
        "• /unban <code>user_id|@username</code> — разбан\n"
        "• /user_info <code>user_id|@username</code> — информация\n"
        "• /logs [user_id|@username] [limit] — логи\n"
        "• /broadcast <code>text</code> — рассылка всем\n"
        "• /users [limit] — список пользователей\n",
        reply_markup=admin_kb(),
    )
    log_event(message.from_user.id, "admin_open", None)


@dp.callback_query(F.data == "admin:open")
async def admin_open_cb(cb: CallbackQuery) -> None:
    if not is_admin(cb):
        return
    total_vips = len(list_vips())
    total_banned = count_banned()
    total_users = count_users()
    await cb.message.answer(
        "<b>🛡️ Админ-панель</b>\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"👑 VIP пользователей: <b>{total_vips}</b>\n"
        f"🚫 Забанено: <b>{total_banned}</b>",
        reply_markup=admin_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def admin_panel_actions(cb: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(cb):
        return
    action = cb.data.split(":", 1)[1]
    if action == "ban":
        await state.set_state(AdminStates.ban_target)
        await cb.message.answer("Введите user_id или @username и причину (необязательно)\nНапример: <code>123456 Спам</code> или <code>@user нарушение</code>")
    elif action == "unban":
        await state.set_state(AdminStates.unban_target)
        await cb.message.answer("Введите user_id или @username для разбана")
    elif action == "userinfo":
        await state.set_state(AdminStates.user_info_target)
        await cb.message.answer("Введите user_id или @username для просмотра информации")
    elif action == "revokevip":
        await state.set_state(AdminStates.revoke_vip_target)
        await cb.message.answer("Введите user_id для снятия VIP")
    elif action == "grantvip":
        await state.set_state(AdminStates.grant_vip_target)
        await cb.message.answer("Введите: <code>user_id days</code> для выдачи VIP")
    elif action == "logs":
        await state.set_state(AdminStates.logs_target)
        await cb.message.answer("Введите user_id или @username и лимит (необязательно). Пример: <code>@user 30</code> или <code>all 50</code>")
    elif action == "broadcast":
        await state.set_state(AdminStates.broadcast_message)
        await cb.message.answer("Введите текст рассылки. Будет отправлен всем, кто писал боту.\nМожно использовать HTML разметку.")
    elif action == "users":
        total = count_users()
        vips = len(list_vips())
        await cb.message.answer(f"Всего пользователей: <b>{total}</b> (VIP: <b>{vips}</b>)\nОтправьте <code>/users [limit]</code> чтобы получить список или нажмите /users без параметров.")
    await cb.answer()


def _parse_user_and_reason(text: str) -> tuple[Optional[int], Optional[str]]:
    text = (text or "").strip()
    if not text:
        return None, None
    parts = text.split()
    if not parts:
        return None, None
    uid = resolve_user_id_from_token(parts[0])
    reason = " ".join(parts[1:]) if len(parts) > 1 else None
    return uid, (reason or None)


@dp.message(StateFilter(AdminStates.ban_target))
async def admin_ban_process(message: Message, state: FSMContext) -> None:
    uid, reason = _parse_user_and_reason(message.text or "")
    if not uid:
        await message.answer("Не удалось определить пользователя. Укажите user_id или @username")
        return
    ban_user(uid, reason)
    log_event(message.from_user.id, "admin_ban", f"{uid}; {reason or ''}")
    await message.answer(f"✅ Пользователь <code>{uid}</code> забанен. Причина: {reason or '—'}")
    await state.clear()


@dp.message(StateFilter(AdminStates.unban_target))
async def admin_unban_process(message: Message, state: FSMContext) -> None:
    uid = resolve_user_id_from_token((message.text or "").strip())
    if not uid:
        await message.answer("Не удалось определить пользователя. Укажите user_id или @username")
        return
    unban_user(uid)
    log_event(message.from_user.id, "admin_unban", str(uid))
    await message.answer(f"✅ Пользователь <code>{uid}</code> разбанен")
    await state.clear()


@dp.message(StateFilter(AdminStates.user_info_target))
async def admin_user_info_process(message: Message, state: FSMContext) -> None:
    uid = resolve_user_id_from_token((message.text or "").strip())
    if not uid:
        await message.answer("Не удалось определить пользователя. Укажите user_id или @username")
        return
    row = get_user(uid)
    banned = is_banned(uid)
    vip = is_vip(uid)
    vip_until = None
    uname = None
    if row:
        vip_until = row["vip_until"]
        uname = row["username"]
    until_str = (
        "—" if not vip_until else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(vip_until)))
    )
    await message.answer(
        "<b>Информация о пользователе</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Username: <code>{uname or '—'}</code>\n"
        f"VIP: <b>{'да' if vip else 'нет'}</b> (до: <code>{until_str}</code>)\n"
        f"Бан: <b>{'да' if banned else 'нет'}</b>"
    )
    log_event(message.from_user.id, "admin_user_info", str(uid))
    await state.clear()


@dp.message(StateFilter(AdminStates.revoke_vip_target))
async def admin_revoke_vip_process(message: Message, state: FSMContext) -> None:
    txt = (message.text or "").strip()
    if not txt.lstrip("-+").isdigit():
        await message.answer("Введите числовой user_id")
        return
    uid = int(txt)
    revoke_vip(uid)
    log_event(message.from_user.id, "admin_revoke_vip", str(uid))
    await message.answer(f"⛔ VIP снят у пользователя <code>{uid}</code>")
    await state.clear()


@dp.message(StateFilter(AdminStates.grant_vip_target))
async def admin_grant_vip_process(message: Message, state: FSMContext) -> None:
    try:
        uid_str, days_str = (message.text or "").split(maxsplit=1)
        uid = int(uid_str)
        days = int(days_str)
    except Exception:
        await message.answer("Формат: <code>user_id days</code>")
        return
    upsert_user(uid, None)
    new_until = set_vip(uid, days)
    until_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_until))
    log_event(message.from_user.id, "admin_grant_vip", f"{uid}; {days}")
    await message.answer(f"✅ VIP выдан до <b>{until_str}</b> пользователю <code>{uid}</code>")
    await state.clear()


@dp.message(StateFilter(AdminStates.logs_target))
async def admin_logs_process(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    parts = text.split()
    uid: Optional[int] = None
    limit = 20
    if parts:
        if parts[0].lower() != "all":
            uid = resolve_user_id_from_token(parts[0])
        if len(parts) > 1 and parts[1].isdigit():
            limit = int(parts[1])
    rows = get_logs(uid, limit)
    if not rows:
        await message.answer("Логи не найдены")
        await state.clear()
        return
    lines: List[str] = []
    for r in rows:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(int(r["created_at"])))
        lines.append(f"<code>{ts}</code> | <code>{r['user_id'] or '—'}</code> | <b>{r['action']}</b> | {r['details'] or ''}")
    # Telegram message limit; chunk if needed
    result = "\n".join(lines)
    if len(result) > 3500:
        result = result[:3500] + "\n…"
    await message.answer(result)
    log_event(message.from_user.id, "admin_logs", f"uid={uid}; limit={limit}")
    await state.clear()


@dp.message(StateFilter(AdminStates.broadcast_message))
async def admin_broadcast_process(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пуст. Отправьте сообщение для рассылки или /cancel")
        return
    user_ids = get_all_user_ids(exclude_banned=True)
    sent = 0
    failed = 0
    await message.answer(f"Начинаю рассылку {len(user_ids)} пользователям…")
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
            # avoid hitting flood limits
            await asyncio.sleep(0.03)
        except Exception as e:
            failed += 1
            await asyncio.sleep(0.03)
            continue
    log_event(message.from_user.id, "admin_broadcast", f"sent={sent}; failed={failed}")
    await message.answer(f"Рассылка завершена. Успешно: <b>{sent}</b>, Ошибок: <b>{failed}</b>.")
    await state.clear()


# === Admin commands (slash) ===
@dp.message(Command("grant_vip"))
async def grant_vip_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    try:
        user_id_str, days_str = (command.args or "").split()
        user_id = int(user_id_str)
        days = int(days_str)
    except Exception:
        await message.answer("Формат: /grant_vip <code>user_id</code> <code>days</code>")
        return
    upsert_user(user_id, None)
    new_until = set_vip(user_id, days)
    until_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_until))
    log_event(message.from_user.id, "admin_grant_vip_cmd", f"{user_id}; {days}")
    await message.answer(f"✅ VIP выдан до <b>{until_str}</b> пользователю <code>{user_id}</code>")


@dp.message(Command("revoke_vip"))
async def revoke_vip_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    try:
        user_id = int((command.args or "").strip())
    except Exception:
        await message.answer("Формат: /revoke_vip <code>user_id</code>")
        return
    revoke_vip(user_id)
    log_event(message.from_user.id, "admin_revoke_vip_cmd", str(user_id))
    await message.answer(f"⛔ VIP снят у пользователя <code>{user_id}</code>")


@dp.message(Command("ban"))
async def ban_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    text = (command.args or "").strip()
    uid, reason = _parse_user_and_reason(text)
    if not uid:
        await message.answer("Формат: /ban <code>user_id|@username</code> [reason]")
        return
    ban_user(uid, reason)
    log_event(message.from_user.id, "admin_ban_cmd", f"{uid}; {reason or ''}")
    await message.answer(f"✅ Пользователь <code>{uid}</code> забанен. Причина: {reason or '—'}")


@dp.message(Command("unban"))
async def unban_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    token = (command.args or "").strip()
    uid = resolve_user_id_from_token(token)
    if not uid:
        await message.answer("Формат: /unban <code>user_id|@username</code>")
        return
    unban_user(uid)
    log_event(message.from_user.id, "admin_unban_cmd", str(uid))
    await message.answer(f"✅ Пользователь <code>{uid}</code> разбанен")


@dp.message(Command("user_info"))
async def user_info_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    token = (command.args or "").strip()
    uid = resolve_user_id_from_token(token)
    if not uid:
        await message.answer("Формат: /user_info <code>user_id|@username</code>")
        return
    row = get_user(uid)
    banned = is_banned(uid)
    vip = is_vip(uid)
    vip_until = None
    uname = None
    if row:
        vip_until = row["vip_until"]
        uname = row["username"]
    until_str = (
        "—" if not vip_until else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(vip_until)))
    )
    await message.answer(
        "<b>Информация о пользователе</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Username: <code>{uname or '—'}</code>\n"
        f"VIP: <b>{'да' if vip else 'нет'}</b> (до: <code>{until_str}</code>)\n"
        f"Бан: <b>{'да' if banned else 'нет'}</b>"
    )
    log_event(message.from_user.id, "admin_user_info_cmd", str(uid))


@dp.message(Command("logs"))
async def logs_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    text = (command.args or "").strip()
    parts = text.split()
    uid: Optional[int] = None
    limit = 20
    if parts:
        if parts[0].lower() != "all":
            uid = resolve_user_id_from_token(parts[0])
        if len(parts) > 1 and parts[1].isdigit():
            limit = int(parts[1])
    rows = get_logs(uid, limit)
    if not rows:
        await message.answer("Логи не найдены")
        return
    lines: List[str] = []
    for r in rows:
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(int(r["created_at"])))
        lines.append(f"<code>{ts}</code> | <code>{r['user_id'] or '—'}</code> | <b>{r['action']}</b> | {r['details'] or ''}")
    result = "\n".join(lines)
    if len(result) > 3500:
        result = result[:3500] + "\n…"
    await message.answer(result)
    log_event(message.from_user.id, "admin_logs_cmd", f"uid={uid}; limit={limit}")


# === New admin commands ===
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /broadcast <code>text</code>")
        return
    user_ids = get_all_user_ids(exclude_banned=True)
    sent = 0
    failed = 0
    await message.answer(f"Начинаю рассылку {len(user_ids)} пользователям…")
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
            await asyncio.sleep(0.03)
        except Exception:
            failed += 1
            await asyncio.sleep(0.03)
    log_event(message.from_user.id, "admin_broadcast_cmd", f"sent={sent}; failed={failed}")
    await message.answer(f"Готово. Успешно: <b>{sent}</b>, Ошибок: <b>{failed}</b>.")


@dp.message(Command("users"))
async def users_cmd(message: Message, command: CommandObject) -> None:
    if not is_admin(message):
        return
    limit: Optional[int] = None
    args = (command.args or "").strip()
    if args and args.isdigit():
        limit = int(args)
    rows = list_users(limit=limit)
    if not rows:
        await message.answer("Пользователи не найдены")
        return
    lines: List[str] = []
    now_ts = int(time.time())
    for r in rows:
        uid = r["user_id"]
        uname = r["username"] or "—"
        isvip = bool(r["is_vip"]) and (
            r["vip_until"] is None or int(r["vip_until"]) > now_ts
        )
        status = "VIP" if isvip else "—"
        lines.append(f"<code>{uid}</code> | @{uname} | {status}")
    result = "\n".join(lines)
    if len(result) > 3500:
        result = result[:3500] + "\n…"
    await message.answer(result)


# === Main ===
async def main() -> None:
    init_db()
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
