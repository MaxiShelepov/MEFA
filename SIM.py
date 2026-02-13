import os
import json
import uuid
import logging
import asyncio
import aiohttp
import re
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Set
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = "7824171390:AAFz_d5CXRkiLVaJgwM9nH6FEhnGV3hqgCQ"
ADMIN_ID = 8133517773
ADMIN_USERNAME = "@WBSpaceT"

# NUMVERIFY API
NUMVERIFY_API_KEY = "5c7f11db4450bc3938a0f0013658f17b"
NUMVERIFY_URL = "http://apilayer.net/api/validate"

# CryptoBot API
CRYPTO_BOT_TOKEN = "528185:AAxnCLhKJKxLQgsPxsK0xPkm3pQ61kdwRL3"
CRYPTO_BOT_API_URL = "https://pay.crypt.bot/api"
CRYPTO_BOT_TEST_MODE = False

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== РАБОТА С JSON-ФАЙЛАМИ ==========
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, "users.json")
CATALOG_FILE = os.path.join(DATA_DIR, "catalog.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
INVOICES_FILE = os.path.join(DATA_DIR, "invoices.json")
PURCHASES_FILE = os.path.join(DATA_DIR, "purchases.json")

# Глобальные переменные
users: Dict[str, Any] = {}
catalog: List[Dict] = []
orders: List[Dict] = []
invoices: Dict[str, Any] = {}
purchases: Dict[str, List] = {}

def atomic_json_dump(data, filepath):
    temp = f"{filepath}.tmp"
    try:
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp, filepath)
        return True
    except Exception as e:
        logger.error(f"Ошибка записи {filepath}: {e}")
        try:
            os.remove(temp)
        except:
            pass
        return False

def load_data():
    global users, catalog, orders, invoices, purchases
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users = json.load(f)
        else:
            users = {}
            save_users()

        if os.path.exists(CATALOG_FILE):
            with open(CATALOG_FILE, 'r', encoding='utf-8') as f:
                catalog = json.load(f)
        else:
            catalog = []
            default_packages = [
                {"id": "pkg_10", "name": "🔥 10 проверок", "price": 1.24, "checks": 10, "type": "sim_package"},
                {"id": "pkg_25", "name": "🚀 25 проверок", "price": 2.49, "checks": 25, "type": "sim_package"},
                {"id": "pkg_50", "name": "⚡ 50 проверок", "price": 4.36, "checks": 50, "type": "sim_package"},
                {"id": "pkg_100", "name": "💎 100 проверок", "price": 7.49, "checks": 100, "type": "sim_package"},
                {"id": "pkg_250", "name": "👑 250 проверок", "price": 16.24, "checks": 250, "type": "sim_package"}
            ]
            catalog.extend(default_packages)
            save_catalog()

        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, 'r', encoding='utf-8') as f:
                orders = json.load(f)
        else:
            orders = []
            save_orders()

        if os.path.exists(INVOICES_FILE):
            with open(INVOICES_FILE, 'r', encoding='utf-8') as f:
                invoices = json.load(f)
        else:
            invoices = {}
            save_invoices()

        if os.path.exists(PURCHASES_FILE):
            with open(PURCHASES_FILE, 'r', encoding='utf-8') as f:
                purchases = json.load(f)
        else:
            purchases = {}
            save_purchases()

        logger.info("✅ Данные загружены")
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        users, catalog, orders, invoices, purchases = {}, [], [], {}, {}

def save_users():
    atomic_json_dump(users, USERS_FILE)

def save_catalog():
    atomic_json_dump(catalog, CATALOG_FILE)

def save_orders():
    atomic_json_dump(orders, ORDERS_FILE)

def save_invoices():
    atomic_json_dump(invoices, INVOICES_FILE)

def save_purchases():
    atomic_json_dump(purchases, PURCHASES_FILE)

# ========== РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ==========
def ensure_user(user_id: int, username: str = None, first_name: str = None) -> dict:
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "id": user_id,
            "username": username or "",
            "first_name": first_name or "",
            "balance_usdt": 0.0,
            "sim_checks": 0,
            "sim_used": 0,
            "is_admin": False,
            "joined": datetime.now().isoformat(),
            "orders": [],
            "purchases": []
        }
        if user_id == ADMIN_ID:
            users[uid]["is_admin"] = True
        save_users()
    else:
        if username:
            users[uid]["username"] = username
        if first_name:
            users[uid]["first_name"] = first_name
        if user_id == ADMIN_ID:
            users[uid]["is_admin"] = True
        save_users()
    return users[uid]

def get_user(user_id: int) -> dict:
    uid = str(user_id)
    return users.get(uid)

def update_user_balance(user_id: int, amount: float):
    uid = str(user_id)
    if uid in users:
        users[uid]["balance_usdt"] = users[uid].get("balance_usdt", 0) + amount
        save_users()

def add_sim_checks(user_id: int, checks: int):
    uid = str(user_id)
    if uid in users:
        users[uid]["sim_checks"] = users[uid].get("sim_checks", 0) + checks
        save_users()

def use_sim_check(user_id: int) -> bool:
    uid = str(user_id)
    if uid in users and users[uid].get("sim_checks", 0) > users[uid].get("sim_used", 0):
        users[uid]["sim_used"] = users[uid].get("sim_used", 0) + 1
        save_users()
        return True
    return False

def get_sim_checks_left(user_id: int) -> int:
    uid = str(user_id)
    if uid in users:
        return users[uid].get("sim_checks", 0) - users[uid].get("sim_used", 0)
    return 0

def is_admin(user_id: int) -> bool:
    uid = str(user_id)
    return uid in users and users[uid].get("is_admin", False)

# ========== CRYPTOBOT API ==========
class CryptoBotAPI:
    def __init__(self, token: str, test_mode: bool = False):
        self.token = token
        self.test_mode = test_mode
        self.base_url = CRYPTO_BOT_API_URL
        self.session: Optional[aiohttp.ClientSession] = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict:
        try:
            session = await self.get_session()
            url = f"{self.base_url}/{endpoint}"
            headers = {
                "Crypto-Pay-API-Token": self.token,
                "Content-Type": "application/json"
            }
            params = {}
            if self.test_mode:
                params['test'] = 'true'

            if method.upper() == "GET":
                async with session.get(url, headers=headers, params=params) as resp:
                    return await resp.json()
            else:
                async with session.post(url, headers=headers, json=data, params=params) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"CryptoBot API error: {e}")
            return {"ok": False, "error": str(e)}

    async def create_invoice(self, amount: float, description: str) -> Optional[Dict]:
        data = {
            "asset": "USDT",
            "amount": str(amount),
            "description": description,
            "hidden_message": "Спасибо за оплату!",
            "paid_btn_name": "openBot",
            "paid_btn_url": "https://t.me/your_bot",
            "payload": str(uuid.uuid4())[:16],
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 3600
        }
        result = await self._make_request("POST", "createInvoice", data)
        if result.get("ok") and result.get("result"):
            inv = result["result"]
            return {
                "invoice_id": inv["invoice_id"],
                "status": inv["status"],
                "hash": inv.get("hash"),
                "asset": inv["asset"],
                "amount": float(inv["amount"]),
                "pay_url": inv["pay_url"],
                "description": inv["description"],
                "created_at": inv["created_at"],
                "payload": data["payload"]
            }
        logger.error(f"Ошибка создания счёта: {result}")
        return None

    async def check_invoice_status(self, invoice_id: str) -> Optional[Dict]:
        result = await self._make_request("GET", f"getInvoices?invoice_ids={invoice_id}")
        if result.get("ok") and result.get("result") and result["result"].get("items"):
            inv = result["result"]["items"][0]
            return {
                "invoice_id": inv["invoice_id"],
                "status": inv["status"],
                "paid_at": inv.get("paid_at"),
                "amount": float(inv["amount"]),
                "asset": inv["asset"]
            }
        return None

crypto_api = CryptoBotAPI(CRYPTO_BOT_TOKEN, CRYPTO_BOT_TEST_MODE)

# ========== ОПЕРАТОРЫ РОССИИ ==========
OPERATORS = {
    'MTS': {
        'codes': ['910', '911', '912', '913', '914', '915', '916', '917', '918', '919',
                  '980', '981', '982', '983', '984', '985', '986', '987', '988', '989'],
        'name': 'МТС',
        'icon': '🔵'
    },
    'MEGAFON': {
        'codes': ['920', '921', '922', '923', '924', '925', '926', '927', '928', '929',
                  '930', '931', '932', '933', '937', '938', '939'],
        'name': 'МегаФон',
        'icon': '🟢'
    },
    'BEELINE': {
        'codes': ['900', '901', '902', '903', '904', '905', '906', '907', '908', '909',
                  '960', '961', '962', '963', '964', '965', '966', '967', '968', '969'],
        'name': 'Билайн',
        'icon': '🟡'
    },
    'TELE2': {
        'codes': ['950', '951', '952', '953', '958', '977', '978', '979',
                  '991', '992', '993', '994', '995', '996', '999'],
        'name': 'Tele2',
        'icon': '🟣'
    },
    'YOTA': {
        'codes': ['995', '996', '999'],
        'name': 'Yota',
        'icon': '🔴'
    }
}

# ========== HLR ПРОВЕРЩИК ==========
class HLRChecker:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    async def check_number(self, phone: str) -> Dict:
        clean = re.sub(r'\D', '', phone)
        if clean.startswith('8'):
            clean = '7' + clean[1:]
        elif not clean.startswith('7') and len(clean) == 10:
            clean = '7' + clean
        elif not clean.startswith('7') and len(clean) == 11:
            clean = '7' + clean[1:]

        params = {
            'access_key': NUMVERIFY_API_KEY,
            'number': clean,
            'country_code': '',
            'format': 1
        }
        try:
            session = await self.get_session()
            start = time.time()
            async with session.get(NUMVERIFY_URL, params=params) as resp:
                rtt = round((time.time() - start) * 1000, 2)
                if resp.status == 200:
                    data = await resp.json()
                    return self._parse_response(data, rtt)
                else:
                    return {'error': True, 'message': f'HTTP {resp.status}'}
        except asyncio.TimeoutError:
            return {'error': True, 'message': 'Таймаут'}
        except Exception as e:
            logger.error(f"HLR error: {e}")
            return {'error': True, 'message': str(e)}

    def _parse_response(self, data: Dict, rtt: float) -> Dict:
        result = {'error': False, 'response_time': rtt, 'valid': data.get('valid', False)}
        if result['valid']:
            result.update({
                'phone': data.get('international_format', ''),
                'local_format': data.get('local_format', ''),
                'country': data.get('country_name', 'Неизвестно'),
                'country_code': data.get('country_code', ''),
                'operator': data.get('carrier', 'Неизвестно'),
                'line_type': data.get('line_type', 'Неизвестно'),
                'location': data.get('location', 'Неизвестно'),
                'status': 'active' if data.get('line_type') else 'unknown',
                'status_text': 'Активен' if data.get('line_type') else 'Неизвестно'
            })
        else:
            result['message'] = 'Номер недействителен'
        return result

    async def close(self):
        if self.session:
            await self.session.close()

# ========== ВСПОМОГАТЕЛЬНОЕ ==========
def detect_operator(phone: str) -> Optional[Dict]:
    m = re.search(r'(?:\+7|8)?(\d{3})', phone)
    if not m:
        return None
    code = m.group(1)
    for op in OPERATORS.values():
        if code in op['codes']:
            return op
    return None

def validate_phone(phone: str) -> Optional[str]:
    cleaned = re.sub(r'\D', '', phone)
    if cleaned.startswith('7') and len(cleaned) == 11:
        return f"+{cleaned}"
    if cleaned.startswith('8') and len(cleaned) == 11:
        return f"+7{cleaned[1:]}"
    if len(cleaned) == 10:
        return f"+7{cleaned}"
    return None

def get_unique_prices() -> List[float]:
    """Возвращает уникальные цены пакетов из каталога (для кнопок пополнения)"""
    prices: Set[float] = set()
    for item in catalog:
        if item.get("type") == "sim_package":
            prices.add(item["price"])
    return sorted(prices)

# ========== КЛАВИАТУРЫ ==========
def main_keyboard(user_id: int = None) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="📱 Проверить SIM", callback_data="check_sim"),
         InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🛍️ Каталог", callback_data="catalog"),
         InlineKeyboardButton(text="💰 Пополнить", callback_data="deposit")],
    ]
    if user_id and is_admin(user_id):
        kb.append([InlineKeyboardButton(text="👑 Админка", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def profile_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="🛍️ Каталог", callback_data="catalog"),
         InlineKeyboardButton(text="💰 Пополнить", callback_data="deposit")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
         InlineKeyboardButton(text="📦 Мои покупки", callback_data="my_purchases")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]])

def deposit_methods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Криптовалюта (CryptoBot)", callback_data="deposit_crypto")],
        [InlineKeyboardButton(text="💳 Через администратора", callback_data="deposit_admin")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ])

def crypto_amount_keyboard() -> InlineKeyboardMarkup:
    prices = get_unique_prices()
    if not prices:
        prices = [10, 25, 50, 100]
    kb = []
    row = []
    for p in prices:
        row.append(InlineKeyboardButton(text=f"{p:.2f} USDT", callback_data=f"crypto:{p}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton(text="Другая сумма", callback_data="crypto_custom")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="deposit")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def catalog_keyboard() -> InlineKeyboardMarkup:
    if not catalog:
        return back_to_main()
    kb = []
    for item in catalog:
        if item.get("type") == "sim_package":
            kb.append([InlineKeyboardButton(
                text=f"{item['name']} - {item['price']:.2f} USDT",
                callback_data=f"buy:{item['id']}"
            )])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def after_purchase_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новая проверка", callback_data="check_sim"),
         InlineKeyboardButton(text="🛍️ Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ])

# ========== СООБЩЕНИЯ ==========
def welcome_text(user_id: int) -> str:
    user = ensure_user(user_id)
    left = get_sim_checks_left(user_id)
    # Используем username, если он есть, иначе first_name
    name = user.get('username')
    if not name:
        name = user.get('first_name', 'Пользователь')
    # Если всё ещё пусто — подставляем ID
    if not name:
        name = f"User{user_id}"
    # Добавляем @ перед username, если это username (без пробелов)
    if user.get('username'):
        display_name = f"@{name}"
    else:
        display_name = name
    return (
        "✨ <b>HLR SIM CHECKER PRO</b> ✨\n\n"
        f"👋 Привет, {display_name}!\n"
        f"💰 Баланс: {user['balance_usdt']:.2f} USDT\n"
        f"📱 Осталось проверок: {left}\n\n"
        "🔍 Реальная HLR проверка через Numverify\n"
        "💎 Пополнение через CryptoBot (USDT)\n\n"
        "👇 Выберите действие:"
    )

def profile_text(user_id: int) -> str:
    user = ensure_user(user_id)
    left = get_sim_checks_left(user_id)
    used = user.get("sim_used", 0)
    total = user.get("sim_checks", 0)
    admin_status = "👑 Администратор" if user.get("is_admin") else "👤 Пользователь"
    return (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📝 Имя: {user.get('first_name', '—')}\n"
        f"📧 Username: @{user.get('username', '—')}\n"
        f"{admin_status}\n"
        f"💰 Баланс: {user['balance_usdt']:.2f} USDT\n"
        f"📱 Проверок: {left} (всего {total}, использовано {used})\n"
        f"📅 Регистрация: {user.get('joined', '—')[:10]}"
    )

def stats_text(user_id: int) -> str:
    left = get_sim_checks_left(user_id)
    return f"📊 У вас осталось <b>{left}</b> проверок SIM."

# ========== ОБРАБОТЧИКИ КОМАНД ==========
load_data()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
active_states = {}

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id, message.from_user.username, message.from_user.first_name)
    await message.answer(welcome_text(user_id), reply_markup=main_keyboard(user_id))

@dp.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(welcome_text(user_id), reply_markup=main_keyboard(user_id))
    await callback.answer()

@dp.callback_query(F.data == "profile")
async def profile_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(profile_text(user_id), reply_markup=profile_keyboard(user_id))
    await callback.answer()

@dp.callback_query(F.data == "stats")
async def stats_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(stats_text(user_id), reply_markup=profile_keyboard(user_id))
    await callback.answer()

@dp.callback_query(F.data == "catalog")
async def catalog_cb(callback: CallbackQuery):
    await callback.message.edit_text(
        "🛍️ <b>Доступные пакеты проверок</b>\n\nВыберите пакет:",
        reply_markup=catalog_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def buy_package_cb(callback: CallbackQuery):
    product_id = callback.data.split(":", 1)[1]
    product = next((p for p in catalog if p["id"] == product_id), None)
    if not product:
        await callback.answer("❌ Товар не найден", show_alert=True)
        return
    user_id = callback.from_user.id
    user = ensure_user(user_id)
    if user["balance_usdt"] < product["price"]:
        await callback.answer("❌ Недостаточно средств", show_alert=True)
        return
    user["balance_usdt"] -= product["price"]
    add_sim_checks(user_id, product["checks"])
    order_id = str(uuid.uuid4())[:8]
    orders.append({
        "order_id": order_id,
        "user_id": user_id,
        "product_id": product_id,
        "product_name": product["name"],
        "price": product["price"],
        "checks": product["checks"],
        "date": datetime.now().isoformat()
    })
    save_orders()
    save_users()
    await callback.message.edit_text(
        f"✅ <b>Покупка успешна!</b>\n\n"
        f"📦 {product['name']}\n"
        f"💰 {product['price']:.2f} USDT\n"
        f"📱 +{product['checks']} проверок\n\n"
        f"Остаток баланса: {user['balance_usdt']:.2f} USDT",
        reply_markup=after_purchase_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "deposit")
async def deposit_cb(callback: CallbackQuery):
    await callback.message.edit_text(
        "💰 <b>Пополнение баланса</b>\n\nВыберите способ:",
        reply_markup=deposit_methods_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "deposit_admin")
async def deposit_admin_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(
        f"💳 <b>Пополнение через администратора</b>\n\n"
        f"Напишите {ADMIN_USERNAME} и укажите ваш ID: <code>{user_id}</code>\n"
        "Сумму указывайте в USDT. После проверки баланс будет зачислен.",
        reply_markup=back_to_main()
    )
    await callback.answer()

@dp.callback_query(F.data == "deposit_crypto")
async def deposit_crypto_cb(callback: CallbackQuery):
    await callback.message.edit_text(
        "💎 <b>Пополнение через CryptoBot</b>\n\n"
        "Выберите сумму в USDT (минимум 1, максимум 1000):",
        reply_markup=crypto_amount_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("crypto:"))
async def crypto_amount_cb(callback: CallbackQuery):
    amount = float(callback.data.split(":", 1)[1])
    await create_invoice(callback, amount)

@dp.callback_query(F.data == "crypto_custom")
async def crypto_custom_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(
        "💎 <b>Введите сумму в USDT</b>\n\n"
        "Например: 15.5\nМинимум 1, максимум 1000.\n\n"
        "Отправьте число в чат."
    )
    active_states[user_id] = "awaiting_crypto_amount"
    await callback.answer()

async def create_invoice(callback_or_message, amount: float):
    if isinstance(callback_or_message, CallbackQuery):
        user_id = callback_or_message.from_user.id
        is_callback = True
        chat_id = callback_or_message.message.chat.id
    else:
        user_id = callback_or_message.from_user.id
        is_callback = False
        chat_id = callback_or_message.chat.id

    desc = f"Пополнение баланса SIM Checker. Пользователь {user_id}"
    inv_data = await crypto_api.create_invoice(amount, desc)
    if not inv_data:
        text = "❌ Ошибка создания счёта. Попробуйте позже."
        if is_callback:
            await callback_or_message.message.edit_text(text, reply_markup=deposit_methods_keyboard())
        else:
            await callback_or_message.answer(text, reply_markup=deposit_methods_keyboard())
        return

    inv_id = inv_data["invoice_id"]
    invoices[str(inv_id)] = {
        "invoice_id": inv_id,
        "user_id": user_id,
        "amount": amount,
        "currency": "USDT",
        "status": "active",
        "created_at": inv_data["created_at"],
        "expires_at": (datetime.now() + timedelta(hours=1)).isoformat(),
        "pay_url": inv_data["pay_url"],
        "payload": inv_data["payload"]
    }
    save_invoices()

    text = (
        f"✅ <b>Счёт на {amount:.2f} USDT создан!</b>\n\n"
        f"💳 <a href='{inv_data['pay_url']}'>Перейти к оплате</a>\n\n"
        f"⏳ Счёт действителен 1 час. После оплаты баланс пополнится автоматически."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=inv_data['pay_url'])],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="deposit")]
    ])
    if is_callback:
        await callback_or_message.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await callback_or_message.answer(text, reply_markup=kb, disable_web_page_preview=True)

    asyncio.create_task(check_invoice_loop(inv_id))

async def check_invoice_loop(invoice_id: str):
    max_checks = 360
    for _ in range(max_checks):
        await asyncio.sleep(10)
        inv = invoices.get(str(invoice_id))
        if not inv or inv["status"] != "active":
            break
        status_data = await crypto_api.check_invoice_status(invoice_id)
        if status_data and status_data["status"] == "paid":
            user_id = inv["user_id"]
            amount = inv["amount"]
            update_user_balance(user_id, amount)
            inv["status"] = "paid"
            inv["paid_at"] = datetime.now().isoformat()
            save_invoices()
            try:
                await bot.send_message(
                    user_id,
                    f"✅ <b>Оплата получена!</b>\n\n"
                    f"💰 Баланс пополнен на {amount:.2f} USDT."
                )
            except:
                pass
            logger.info(f"Счёт {invoice_id} оплачен, пользователю {user_id} начислено {amount} USDT")
            return
        elif status_data and status_data["status"] == "expired":
            inv["status"] = "expired"
            save_invoices()
            return
    invoices[str(invoice_id)]["status"] = "expired"
    save_invoices()

@dp.callback_query(F.data == "check_sim")
async def check_sim_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    left = get_sim_checks_left(user_id)
    if left <= 0:
        await callback.message.edit_text(
            "❌ <b>Нет доступных проверок!</b>\n\nКупите пакет в каталоге.",
            reply_markup=main_keyboard(user_id)
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "📱 <b>Отправьте номер телефона</b>\n\n"
        "Примеры:\n89161234567, +79161234567, 9161234567"
    )
    active_states[user_id] = "waiting_phone"
    await callback.answer()

@dp.message()
async def handle_messages(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if user_id in active_states:
        state = active_states[user_id]

        if state == "awaiting_crypto_amount":
            try:
                amount = float(text.replace(',', '.'))
                if amount < 1 or amount > 1000:
                    await message.answer("❌ Сумма от 1 до 1000 USDT")
                    return
                await create_invoice(message, amount)
                del active_states[user_id]
            except ValueError:
                await message.answer("❌ Введите число, например 15.5")
            return

        if state == "waiting_phone":
            phone = validate_phone(text)
            if not phone:
                await message.answer(
                    "❌ Неверный формат. Попробуйте:\n89161234567, +79161234567"
                )
                return

            left = get_sim_checks_left(user_id)
            if left <= 0:
                await message.answer("❌ Нет проверок!", reply_markup=main_keyboard(user_id))
                del active_states[user_id]
                return

            status_msg = await message.answer("🔍 Выполняется HLR проверка...")
            checker = HLRChecker()
            result = await checker.check_number(phone)
            await checker.close()
            await status_msg.delete()

            use_sim_check(user_id)

            if result.get('error'):
                await message.answer(
                    f"❌ Ошибка: {result.get('message')}",
                    reply_markup=after_purchase_keyboard()
                )
            else:
                await message.answer(
                    format_hlr_result(result),
                    reply_markup=after_purchase_keyboard()
                )
            del active_states[user_id]
            return

        if state == "awaiting_admin_add_balance":
            if not is_admin(user_id):
                await message.answer("❌ Нет доступа")
                del active_states[user_id]
                return
            parts = text.split()
            if len(parts) != 2:
                await message.answer("❌ Неверный формат. Нужно: ID СУММА")
                return
            try:
                target_id = int(parts[0])
                amount = float(parts[1])
                if amount <= 0:
                    await message.answer("❌ Сумма должна быть > 0")
                    return
                ensure_user(target_id)
                update_user_balance(target_id, amount)
                await message.answer(f"✅ Баланс пользователя {target_id} пополнен на {amount:.2f} USDT")
                try:
                    await bot.send_message(target_id, f"💰 Ваш баланс пополнен администратором на {amount:.2f} USDT")
                except:
                    pass
            except ValueError:
                await message.answer("❌ Неверные числа")
            del active_states[user_id]
            return

    await cmd_start(message)

def format_hlr_result(res: Dict) -> str:
    if res.get('error'):
        return f"❌ Ошибка: {res['message']}"
    if not res.get('valid'):
        return "❌ Номер недействителен"
    status = "🟢 Активен" if res['status'] == 'active' else "⚪ Неизвестно"
    op = res.get('operator', 'Неизвестно')
    op_icon = "📡"
    for o in OPERATORS.values():
        if o['name'].lower() in op.lower() or op.lower() in o['name'].lower():
            op_icon = o['icon']
            break
    return (
        f"📱 <b>HLR результат</b>\n\n"
        f"📞 Номер: <code>{res.get('phone')}</code>\n"
        f"{status}\n"
        f"{op_icon} Оператор: {op}\n"
        f"🌍 Страна: {res.get('country')}\n"
        f"📞 Тип линии: {res.get('line_type')}\n"
        f"📍 Регион: {res.get('location')}\n"
        f"⏱ Время: {res.get('response_time')} мс"
    )

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.callback_query(F.data == "admin")
async def admin_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    text = "👑 Админ-панель\n\n"
    text += f"Пользователей: {len(users)}\n"
    text += f"Активных счетов: {sum(1 for i in invoices.values() if i['status']=='active')}\n"
    text += f"Оплаченных: {sum(1 for i in invoices.values() if i['status']=='paid')}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_users")],
        [InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="admin_add_balance")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    lines = []
    for uid_str, data in users.items():
        uid = int(uid_str)
        # Формируем отображаемое имя: username или first_name
        name = data.get('username')
        if not name:
            name = data.get('first_name', f"User{uid}")
        balance = data.get('balance_usdt', 0)
        checks_left = data.get('sim_checks', 0) - data.get('sim_used', 0)
        link = f"tg://user?id={uid}"
        lines.append(f"• <a href='{link}'>{name}</a> (ID: <code>{uid}</code>) | Баланс: {balance:.2f} USDT | Проверок: {checks_left}")
    if not lines:
        text = "👥 Нет пользователей."
    else:
        text = "👥 <b>Список пользователей</b>\n\n" + "\n".join(lines[:50])
        if len(lines) > 50:
            text += f"\n\n... и ещё {len(lines)-50} пользователей."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="admin")]
    ])
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "💎 <b>Пополнение баланса пользователя</b>\n\n"
        "Введите ID пользователя и сумму через пробел, например:\n"
        "<code>123456789 10.5</code>"
    )
    active_states[user_id] = "awaiting_admin_add_balance"
    await callback.answer()

# ========== ЗАПУСК ==========
async def main():
    logger.info("🚀 Запуск бота...")
    print("="*50)
    print("HLR SIM CHECKER + CryptoBot (JSON)")
    print("="*50)
    test = HLRChecker()
    t = await test.check_number("79001234567")
    await test.close()
    if t.get('error'):
        print("❌ Numverify не отвечает")
    else:
        print("✅ Numverify OK")
    print("="*50)
    try:
        await dp.start_polling(bot)
    finally:
        await crypto_api.close_session()

if __name__ == "__main__":
    asyncio.run(main())
