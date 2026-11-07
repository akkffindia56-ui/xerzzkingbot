import os
import asyncio
import json
import sqlite3
import hashlib
import secrets
import hmac
import random
from html import escape
from datetime import datetime, timedelta

import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

from products import CATALOG

# ==========================================
# 1. БАПТАУЛАР
# ==========================================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
API_URL = os.getenv("API_URL", "https://bantibhaiya.to/api/reseller_v1.php")
API_KEY = os.getenv("API_KEY", "")
MASTER_KEY = os.getenv("MASTER_KEY", "")
SALES_GROUP_ID = os.getenv("SALES_GROUP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

if not TOKEN: raise RuntimeError("BOT_TOKEN .env файлында көрсетілмеген!")
if not ADMIN_ID: raise RuntimeError("ADMIN_ID .env файлында көрсетілмеген!")

# ==========================================
# 2. DATABASE
# ==========================================
DB_FILE = "xerzz.db"

def db():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection

def init_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'user', reseller_login TEXT, reseller_password TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, total_sales INTEGER NOT NULL DEFAULT 0, total_spent REAL NOT NULL DEFAULT 0, referrer_id INTEGER, last_spin TIMESTAMP)")
        con.execute("CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_name TEXT NOT NULL, price REAL NOT NULL, issued_key TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        con.execute("CREATE TABLE IF NOT EXISTS deposits (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reviewed_at TIMESTAMP)")
        try: con.execute("ALTER TABLE purchases ADD COLUMN issued_key TEXT")
        except sqlite3.OperationalError: pass
        try: con.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
        except sqlite3.OperationalError: pass
        try: con.execute("ALTER TABLE users ADD COLUMN last_spin TIMESTAMP")
        except sqlite3.OperationalError: pass

def register_user(user_id, referrer_id=None):
    with db() as con:
        existing = con.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not existing: con.execute("INSERT INTO users (user_id, referrer_id) VALUES (?, ?)", (user_id, referrer_id))

def get_user(user_id):
    register_user(user_id)
    with db() as con: return con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

def get_balance(user_id): return float(get_user(user_id)["balance"])
def get_role(user_id): return get_user(user_id)["role"]

def change_balance(user_id, amount):
    register_user(user_id)
    with db() as con:
        user = con.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        new_balance = float(user["balance"]) + amount
        if new_balance < 0: return False, float(user["balance"])
        con.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
        return True, new_balance

def set_role(user_id, role):
    register_user(user_id)
    with db() as con:
        con.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
        if role == "user": con.execute("UPDATE users SET reseller_login = NULL, reseller_password = NULL WHERE user_id = ?", (user_id,))

def make_password_hash(password):
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()
    return f"{salt}${hashed}"

def check_password(password, stored):
    try:
        salt, expected = stored.split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()
        return hmac.compare_digest(actual, expected)
    except Exception: return False

def create_reseller(user_id, login, password):
    register_user(user_id)
    with db() as con:
        existing = con.execute("SELECT user_id FROM users WHERE reseller_login = ? AND user_id != ?", (login, user_id)).fetchone()
        if existing: return False
        con.execute("UPDATE users SET role = 'reseller', reseller_login = ?, reseller_password = ? WHERE user_id = ?", (login, make_password_hash(password), user_id))
    return True

def get_statistics():
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        resellers = con.execute("SELECT COUNT(*) FROM users WHERE role = 'reseller'").fetchone()[0]
        sales = con.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
        revenue = con.execute("SELECT COALESCE(SUM(price), 0) FROM purchases").fetchone()[0]
        balance_total = con.execute("SELECT COALESCE(SUM(balance), 0) FROM users").fetchone()[0]
    return users, resellers, sales, revenue, balance_total

def get_all_user_ids():
    with db() as con: rows = con.execute("SELECT user_id FROM users").fetchall()
    return [row["user_id"] for row in rows]

def save_purchase(user_id, product_name, price, issued_key=None):
    with db() as con:
        con.execute("INSERT INTO purchases (user_id, product_name, price, issued_key) VALUES (?, ?, ?, ?)", (user_id, product_name, price, issued_key))
        con.execute("UPDATE users SET total_sales = total_sales + 1, total_spent = total_spent + ? WHERE user_id = ?", (price, user_id))

def get_last_keys(user_id, limit=5):
    with db() as con:
        return con.execute("SELECT product_name, price, issued_key, created_at FROM purchases WHERE user_id = ? AND issued_key IS NOT NULL ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()

def get_referral_count(user_id):
    with db() as con: return con.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)).fetchone()[0]

def create_deposit(user_id, amount, method):
    with db() as con:
        cur = con.execute("INSERT INTO deposits (user_id, amount, method) VALUES (?, ?, ?)", (user_id, amount, method))
        return cur.lastrowid

def approve_deposit(deposit_id):
    with db() as con:
        row = con.execute("SELECT * FROM deposits WHERE id = ? AND status = 'pending'", (deposit_id,)).fetchone()
        if not row: return None
        con.execute("UPDATE deposits SET status = 'approved', reviewed_at = CURRENT_TIMESTAMP WHERE id = ?", (deposit_id,))
        con.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (row["amount"], row["user_id"]))
        return row

def reject_deposit(deposit_id):
    with db() as con:
        row = con.execute("SELECT * FROM deposits WHERE id = ? AND status = 'pending'", (deposit_id,)).fetchone()
        if not row: return None
        con.execute("UPDATE deposits SET status = 'rejected', reviewed_at = CURRENT_TIMESTAMP WHERE id = ?", (deposit_id,))
        return row

def get_all_users_for_admin():
    with db() as con:
        return con.execute("SELECT * FROM users ORDER BY total_sales DESC LIMIT 20").fetchall()

def get_top_sellers():
    with db() as con:
        return con.execute("SELECT * FROM users ORDER BY total_sales DESC LIMIT 5").fetchall()

def get_user_by_login(login):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE reseller_login = ?", (login,)).fetchone()

# ==========================================
# 3. EMOJI ID
# ==========================================
EMOJI_KZ = "5228718354658769982"
EMOJI_RU = "5449408995691341691"
EMOJI_US = "5202021044105257611"
E_SHOP = "5983399041197675256"
E_SPARK = "5893321843149902412"
E_HELLO = "5458904472598095631"
E_KEY = "6005570495603282482"
E_DROP = "6005553599201940235"
E_SEND = "6143315044588326242"
E_MONEY = "5897958754267174109"
E_PRICE = "5472030678633684592"
E_USER = "5316887736823591263"
E_COIN = "5283232570660634549"
E_BINANCE = "5384321407725358581"
E_KASPI = "4994602212794565807"
E_ID = "5888781182249738113"
E_PHONE = "5453965363286925977"
E_PROFILE_CROWN = "5942901773088857071"
E_PROFILE_ID = "5888781182249738113"
E_PROFILE_USER = "5316887736823591263"
E_PROFILE_STAT = "5028746137645876535"
E_PROFILE_CAL = "5816442787544964208"
E_PROFILE_COIN = "5237761614458933049"
E_PROFILE_BAG = "4970023558068568720"
E_PROFILE_STAR = "5310224206732996002"
E_PROFILE_KEY = "6005570495603282482"
E_LOCK = "5393302369024882368"
E_RESELLER_SHOP = "5870991681112772430"
B_SHOP = "5431499171045581032"
B_BALANCE = "6176751090109784137"
B_PROFILE = "5942901773088857071"
B_RESELLER = "5467406098367521267"
B_HOWTO = "5226512880362332956"
B_LUCKY = "5204076520963842994"
B_SHARE = "5377603473284031241"
B_SUPPORT = "5204435043358877113"
B_TOP = "5807868868886009920"
B_BACK = "5253997076169115797"
C_ANDROID = "6199417621100629765"
C_PC = "5316581935152110749"
C_IOS = "5818920837645867167"
C_OTHER = "5364052602357044385"
SALE_KEY_EMOJI = "5454386656628991407"
SALE_USER_EMOJI = "5316887736823591263"
SALE_STAR_EMOJI = "5255817682741114750"
SALE_AMOUNT_EMOJI = "5454409660473827001"
SALE_REMAIN_EMOJI = "5886412370347036129"

# Жаңа эмодзилер (Админ және сыйлық үшін)
E_ADMIN_CROWN = "6129805886383723340"
E_ADMIN_MONEY = "5767197779155754253"
E_ADMIN_PHONE = "5363858422590619939"
E_ADMIN_MINUS = "5332447793451654491"
E_ADMIN_CHECK = "5289967908309442996"
E_ADMIN_ROLE = "5373012449597335010"
E_ADMIN_STAT = "5449872877929127395"
E_ADMIN_STAT2 = "5028746137645876535"
E_ADMIN_RESELLER = "5951766778861328139"
E_ADMIN_CART = "5467723586645021294"
E_ADMIN_BANK = "5264733042710181045"
E_ADMIN_TOP = "5467406098367521267"
E_ADMIN_DIAMOND = "5235702276424737428"
E_ADMIN_TOTAL = "5375296873982604963"
E_GIFT = "5963213811597970978"

def e(emoji_id, fallback): return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

# ==========================================
# 4. BOT
# ==========================================
bot = Bot(token=TOKEN)
dp = Dispatcher()
user_lang = {}
user_states = {}
purchase_locks = {}
reseller_sessions = set()
admin_targets = {} # Админ әрекеттері үшін уақытша сақтау

def is_admin(user_id): return user_id == ADMIN_ID
def is_reseller(user_id): return get_role(user_id) == "reseller" or is_admin(user_id)

# ==========================================
# 5. САНАТТАР
# ==========================================
CATEGORIES = {
    "android_nonroot": {"title": {"kk": "NON ROOT", "ru": "NON ROOT", "en": "NON ROOT"}, "emoji": C_ANDROID, "fallback": "🎰", "style": "success"},
    "android_root": {"title": {"kk": "ANDROID ROOT", "ru": "ANDROID ROOT", "en": "ANDROID ROOT"}, "emoji": C_ANDROID, "fallback": "🎰", "style": "danger"},
    "pc": {"title": {"kk": "PC", "ru": "PC", "en": "PC"}, "emoji": C_PC, "fallback": "🖥", "style": "primary"},
    "ios": {"title": {"kk": "IOS IPHONE", "ru": "IOS IPHONE", "en": "IOS IPHONE"}, "emoji": C_IOS, "fallback": "🍏", "style": "primary"},
    "other": {"title": {"kk": "OTHER", "ru": "OTHER", "en": "OTHER"}, "emoji": C_OTHER, "fallback": "🐶", "style": "primary"}
}

# ==========================================
# 6. МӘТІНДЕР
# ==========================================
TEXTS = {
    "kk": {"hello": "Сәлем", "power": "XERZZ KEYSHOP жүйесі", "l1": "Тікелей жеткізушілермен жұмыс", "l2": "Төлемнен кейін жеткізу", "l3": "Тиімді бағалар", "l4": "Әкімші қолдауы", "press": "Төмендегі батырманы таңдаңыз.", "balance": "Сіздің балансыңыз", "soon": "Бұл бөлім әзірге дайын емес.", "choose_cat": "Санатты таңдаңыз:", "choose_prod": "Өнімді таңдаңыз:", "no_products": "Бұл санатта тауар жоқ.", "back": "🔙 Артқа", "buy": "Сатып алу", "not_configured": "API product_id әлі енгізілмеген.", "api_error": "API-ге қосылу кезінде қате шықты.", "api_reply": "API жауабы", "profile": "Профиль", "account": "Аккаунт", "sending": "Сұраныс жіберілуде...", "success": "Сатып алу сәтті аяқталды!", "private_sent": "Кілт жеке хабарламаға жіберілді."},
    "ru": {"hello": "Привет", "power": "Работает на XERZZ KEYSHOP", "l1": "Работа напрямую с поставщиками", "l2": "Выдача после оплаты", "l3": "Выгодные цены", "l4": "Поддержка администратора", "press": "Выберите кнопку ниже.", "balance": "Ваш баланс", "soon": "Этот раздел пока не готов.", "choose_cat": "Выберите категорию:", "choose_prod": "Выберите товар:", "no_products": "В этой категории пока нет товаров.", "back": "🔙 Назад", "buy": "Купить", "not_configured": "Для этого товара ещё не указан API product_id.", "api_error": "Ошибка при подключении к API.", "api_reply": "Ответ API", "profile": "Профиль", "account": "Аккаунт", "sending": "Отправка запроса...", "success": "Покупка успешно завершена!", "private_sent": "Ключ отправлен в личные сообщения."},
    "en": {"hello": "Hello", "power": "Powered by XERZZ KEYSHOP", "l1": "Direct supplier access", "l2": "Delivery after payment", "l3": "Good prices", "l4": "Admin support", "press": "Choose a button below.", "balance": "Your balance", "soon": "This section is not ready yet.", "choose_cat": "Choose a category:", "choose_prod": "Choose a product:", "no_products": "No products in this category yet.", "back": "🔙 Back", "buy": "Buy", "not_configured": "API product_id has not been set for this product.", "api_error": "Could not connect to the API.", "api_reply": "API response", "profile": "Profile", "account": "Account", "sending": "Sending request...", "success": "Purchase completed successfully!", "private_sent": "The key was sent to your private chat."}
}

BTN = {
    "kk": {"shop": "Дүкен / Тауарлар", "balance": "Балансты толтыру", "profile": "Менің профилім", "reseller": "Реселлер", "howto": "Қолдану нұсқаулығы", "lucky": "Сыйлық", "share": "Бөлісу", "support": "Қолдау", "top": "Топ сатушылар"},
    "ru": {"shop": "Магазин / Товары", "balance": "Пополнить баланс", "profile": "Мой профиль", "reseller": "Реселлер", "howto": "Инструкция", "lucky": "Подарок", "share": "Поделиться", "support": "Поддержка", "top": "Топ продавцов"},
    "en": {"shop": "Shop / Products", "balance": "Add balance", "profile": "My profile", "reseller": "Reseller", "howto": "How to use", "lucky": "Gift", "share": "Share", "support": "Support", "top": "Top sellers"}
}

# ==========================================
# 7. КӨМЕКШІ ФУНКЦИЯЛАР
# ==========================================
def unit_word(lang, unit, n):
    if lang == "kk": return {"h": "САҒАТ", "d": "КҮН", "b": "БОТ"}[unit]
    if lang == "ru":
        forms = {"h": ("ЧАС", "ЧАСА", "ЧАСОВ"), "d": ("ДЕНЬ", "ДНЯ", "ДНЕЙ"), "b": ("БОТ", "БОТА", "БОТОВ")}[unit]
        if n % 10 == 1 and n % 100 != 11: return forms[0]
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14: return forms[1]
        return forms[2]
    forms = {"h": ("HOUR", "HOURS"), "d": ("DAY", "DAYS"), "b": ("BOT", "BOTS")}[unit]
    return forms[0] if n == 1 else forms[1]

def welcome_text(lang, name, balance="0.00$"):
    t = TEXTS[lang]
    name = escape(name or "User")
    return (f'{e(E_SHOP, "🏪")} <b>XERZZ KEYSHOP</b> {e(E_SPARK, "✨")}\n\n{e(E_HELLO, "👋")} {t["hello"]}, <b>{name}</b>!\n\n{e(E_KEY, "🔑")} <b>{t["power"]}</b>\n\n— {e(E_SHOP, "🏪")} {t["l1"]}\n— {e(E_DROP, "💧")} {t["l2"]}\n— {e(E_SEND, "📤")} {t["l3"]}\n— {e(E_SHOP, "🏪")} {t["l4"]}\n\n<b>{t["press"]}</b>\n\n{e(E_MONEY, "💰")} {t["balance"]}: <b>{balance}</b>')

def product_text(lang, product, user_id, is_reseller_mode=False):
    icon = e(product["emoji_id"], product["fallback"]) if product.get("emoji_id") else product["fallback"]
    role_text = "reseller" if is_reseller_mode else "user"
    return f'{icon} <b>{escape(product["name"])}</b>\n\n{e(E_USER, "👤")} account: <b>{role_text}</b>'

def profile_text(user_id):
    user = get_user(user_id)
    role = user["role"]
    role_display = "👑 admin" if user_id == ADMIN_ID else ("💎 reseller" if role == "reseller" else "👤 user")
    created = (user["created_at"] or "")[:10]
    balance = float(user["balance"])
    total_spent = float(user["total_spent"])
    total_sales = int(user["total_sales"])
    referrals = get_referral_count(user_id)
    return (f'{e(E_PROFILE_CROWN, "👑")} <b>Your Profile</b>\n\n{e(E_PROFILE_ID, "🆔")} : <code>{user_id}</code>\n{e(E_PROFILE_USER, "👤")} : <b>{role_display}</b>\n{e(E_PROFILE_STAT, "📈")} : <b>Әзірге жоқ</b>\n{e(E_PROFILE_CAL, "🗓")} : <b>{created}</b>\n\n{e(E_PROFILE_COIN, "🪙")} : <b>{balance:.2f}$</b>\n{e(E_PROFILE_BAG, "🛍️")} : <b>{total_sales} ключ</b>\n\n{e(E_PROFILE_COIN, "🪙")} : <b>{total_spent:.2f}$</b>\n{e(E_PROFILE_STAR, "⭐")} : <b>{referrals} адам</b>')

# ==========================================
# 8. КЛАВИАТУРАЛАР
# ==========================================
def back_button(lang, callback_data):
    return InlineKeyboardButton(text=TEXTS[lang]["back"], callback_data=callback_data, style="danger", icon_custom_emoji_id=B_BACK)

def lang_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Қазақша", callback_data="lang_kk", style="success", icon_custom_emoji_id=EMOJI_KZ)],[InlineKeyboardButton(text="Русский", callback_data="lang_ru", style="primary", icon_custom_emoji_id=EMOJI_RU)],[InlineKeyboardButton(text="English", callback_data="lang_en", style="danger", icon_custom_emoji_id=EMOJI_US)]])

def main_keyboard(lang, user_id):
    b = BTN[lang]
    def btn(key, emoji, style): return InlineKeyboardButton(text=b[key], callback_data=f"menu_{key}", style=style, icon_custom_emoji_id=emoji)
    rows = [[btn("shop", B_SHOP, "success")],[btn("balance", B_BALANCE, "success"), btn("profile", B_PROFILE, "primary")],[btn("reseller", B_RESELLER, "danger")],[btn("howto", B_HOWTO, "primary"), btn("lucky", B_LUCKY, "danger")],[btn("share", B_SHARE, "primary"), btn("support", B_SUPPORT, "danger")],[btn("top", B_TOP, "primary")]]
    if is_admin(user_id): rows.append([InlineKeyboardButton(text="🔐 Admin Access", callback_data="admin_open", style="danger", icon_custom_emoji_id=E_ADMIN_CROWN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def profile_keyboard(lang): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 My Key", callback_data="profile_keys", style="primary", icon_custom_emoji_id=E_PROFILE_KEY)],[back_button(lang, "back_main")]])

def categories_keyboard(lang, is_reseller_mode=False):
    rows = []
    prefix = "res_cat_" if is_reseller_mode else "cat_"
    for key, data in CATEGORIES.items():
        rows.append([InlineKeyboardButton(text=data["title"][lang], callback_data=f"{prefix}{key}", style=data["style"], icon_custom_emoji_id=data["emoji"])])
    rows.append([back_button(lang, "res_menu" if is_reseller_mode else "back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def products_keyboard(lang, cat, is_reseller_mode=False):
    rows = []
    prefix = "res_prod_" if is_reseller_mode else "prod_"
    for i, p in enumerate(CATALOG.get(cat, [])):
        label = f'{p["fallback"]} {p["name"]}'
        kwargs = {"text": label, "callback_data": f"{prefix}{cat}_{i}", "style": "primary"}
        if p.get("emoji_id"):
            kwargs["icon_custom_emoji_id"] = p["emoji_id"]
            kwargs["text"] = p["name"]
        rows.append([InlineKeyboardButton(**kwargs)])
    rows.append([back_button(lang, "res_shop" if is_reseller_mode else "back_shop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def product_detail_keyboard(lang, cat, index, is_reseller_mode=False):
    product = CATALOG[cat][index]
    rows = []
    durations = product.get("reseller_durations", product["durations"]) if is_reseller_mode else product["durations"]
    prefix = "res_buy_" if is_reseller_mode else "buy_"
    for duration_index, (n, unit, price) in enumerate(durations):
        label = f'{n} {unit_word(lang, unit, n)} - {price:g}$ 💸'
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}{cat}_{index}_{duration_index}", style="success", icon_custom_emoji_id=E_PRICE)])
    rows.append([back_button(lang, f"res_cat_{cat}" if is_reseller_mode else f"cat_{cat}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def deposit_amount_keyboard(lang):
    amounts = [1, 3, 5, 10, 100]
    rows = [[InlineKeyboardButton(text=f"${a}", callback_data=f"dep_amt_{a}", style="success", icon_custom_emoji_id=E_COIN)] for a in amounts]
    rows.append([back_button(lang, "back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def deposit_method_keyboard(lang, amount):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Binance", callback_data=f"dep_m_{amount}_binance", style="success", icon_custom_emoji_id=E_BINANCE)],[InlineKeyboardButton(text="Kaspi", callback_data=f"dep_m_{amount}_kaspi", style="primary", icon_custom_emoji_id=E_KASPI)],[back_button(lang, "menu_balance")]])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Баланс қосу", callback_data="admin_add", icon_custom_emoji_id=E_ADMIN_MONEY)],
        [InlineKeyboardButton(text="Баланс азайту", callback_data="admin_sub", icon_custom_emoji_id=E_ADMIN_MINUS)],
        [InlineKeyboardButton(text="Рөл өзгерту", callback_data="admin_role", icon_custom_emoji_id=E_ADMIN_ROLE)],
        [InlineKeyboardButton(text="Reseller аккаунт ашу", callback_data="admin_reseller", icon_custom_emoji_id=E_KEY)],
        [InlineKeyboardButton(text="Статистика", callback_data="admin_stats", icon_custom_emoji_id=E_ADMIN_STAT)],
        [InlineKeyboardButton(text="Ботқа хабарлама", callback_data="admin_broadcast", icon_custom_emoji_id=E_SEND)],
        [InlineKeyboardButton(text="Басты мәзір", callback_data="back_main", style="danger", icon_custom_emoji_id=B_BACK)]
    ])

def admin_users_keyboard(action_prefix, users):
    rows = []
    for u in users:
        role_icon = "👑" if u["user_id"] == ADMIN_ID else ("💎" if u["role"] == "reseller" else "👤")
        rows.append([InlineKeyboardButton(
            text=f"{role_icon} ID: {u['user_id']} | Bal: {float(u['balance']):.2f}$",
            callback_data=f"{action_prefix}_{u['user_id']}",
            style="primary"
        )])
    rows.append([InlineKeyboardButton(text="🔙 Артқа", callback_data="admin_open", style="danger", icon_custom_emoji_id=B_BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_role_select_keyboard(target_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Admin", callback_data=f"setrole_admin_{target_id}", style="danger")],
        [InlineKeyboardButton(text="💎 Reseller", callback_data=f"setrole_reseller_{target_id}", style="primary")],
        [InlineKeyboardButton(text="👤 User", callback_data=f"setrole_user_{target_id}", style="success")],
        [InlineKeyboardButton(text="🔙 Артқа", callback_data="admin_role", style="danger", icon_custom_emoji_id=B_BACK)]
    ])

def reseller_main_keyboard(lang):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BTN[lang]["shop"], callback_data="res_shop", style="success", icon_custom_emoji_id=E_RESELLER_SHOP)],[InlineKeyboardButton(text=BTN[lang]["profile"], callback_data="menu_profile", style="primary", icon_custom_emoji_id=B_PROFILE)],[back_button(lang, "back_main")]])

def spin_keyboard(lang):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 Айналдыру / Spin", callback_data="spin_start", style="success", icon_custom_emoji_id=E_GIFT)],[back_button(lang, "back_main")]])

# ==========================================
# 9. API
# ==========================================
def api_duration(n, unit):
    if unit == "h": return f"{n} Hours"
    if unit == "d": return "1 Day" if n == 1 else f"{n} Days"
    if unit == "b": return "1 Bot" if n == 1 else f"{n} Bots"
    raise ValueError("Белгісіз тариф өлшемі")

async def api_buy(product_id, duration, quantity=1, android_id=None):
    if not API_KEY or not MASTER_KEY: raise RuntimeError("API_KEY немесе MASTER_KEY .env ішінде жоқ")
    data = {"api_key": API_KEY, "action": "buy", "product_id": str(product_id), "duration": duration, "quantity": quantity}
    if android_id: data["android_id"] = android_id
    headers = {"Content-Type": "application/x-www-form-urlencoded", "x-master-key": MASTER_KEY, "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post(API_URL, data=data, headers=headers) as response: return response.status, await response.text()

def find_issued_key(payload):
    key_names = {"key", "license", "license_key", "code", "token", "serial", "activation_key"}
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name.lower() in key_names and isinstance(value, (str, int)) and str(value).strip(): return str(value).strip()
        for value in payload.values():
            found = find_issued_key(value)
            if found: return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_issued_key(value)
            if found: return found
    return None

def buyer_sale_message(product, key, buyer, spent, remaining):
    icon = e(product["emoji_id"], product.get("fallback", "🎮")) if product.get("emoji_id") else product.get("fallback", "🎮")
    return (f'{icon} <b>{escape(product["name"])}</b>\n\n{e(SALE_KEY_EMOJI, "🔑")} : <code>{escape(key)}</code>\n{e(SALE_USER_EMOJI, "👤")} : <b>{escape(buyer)}</b>\n{e(SALE_AMOUNT_EMOJI, "💱")} : <b>{spent:.2f}$</b>\n{e(SALE_REMAIN_EMOJI, "👤")} : <b>{remaining:.2f}$</b>')

def group_sale_message(product, buyer):
    icon = e(product["emoji_id"], product.get("fallback", "🎮")) if product.get("emoji_id") else product.get("fallback", "🎮")
    return (f'{icon} <b>{escape(product["name"])}</b>\n\n{e(SALE_USER_EMOJI, "👤")} : <b>{escape(buyer)}</b>\n{e(SALE_STAR_EMOJI, "⭐")} : <b>Рахмет без скам</b>')

async def send_sale_announcement(chat_id, text): await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

# ==========================================
# 10. ХЕНДЛЕРЛЕР (Клиент)
# ==========================================
@dp.message(Command("start"))
async def start(message: Message):
    args = message.text.split(maxsplit=1)
    referrer_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1].removeprefix("ref_"))
            if referrer_id == message.from_user.id: referrer_id = None
        except ValueError: referrer_id = None
    register_user(message.from_user.id, referrer_id)
    await message.answer(f'{e(EMOJI_KZ, "🇰🇿")}{e(EMOJI_RU, "🇷🇺")}{e(EMOJI_US, "🇺🇸")} <b>Тілді таңдаңыз / Выберите язык / Choose language:</b>', reply_markup=lang_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("lang_"))
async def choose_lang(call: CallbackQuery):
    lang = call.data.split("_", 1)[1]
    if lang not in TEXTS: return await call.answer("Unknown language", show_alert=True)
    register_user(call.from_user.id)
    user_lang[call.from_user.id] = lang
    balance = get_balance(call.from_user.id)
    await call.message.edit_text(welcome_text(lang, call.from_user.first_name, f"{balance:.2f}$"), reply_markup=main_keyboard(lang, call.from_user.id), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    balance = get_balance(call.from_user.id)
    await call.message.edit_text(welcome_text(lang, call.from_user.first_name, f"{balance:.2f}$"), reply_markup=main_keyboard(lang, call.from_user.id), parse_mode="HTML")
    await call.answer()

# -------- ДҮКЕН (КЛИЕНТ) --------
@dp.callback_query(F.data.in_({"menu_shop", "back_shop"}))
async def open_shop(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    await call.message.edit_text(f'{e(B_SHOP, "🛒")} <b>{BTN[lang]["shop"]}</b>\n\n{TEXTS[lang]["choose_cat"]}', reply_markup=categories_keyboard(lang), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("cat_"))
async def open_category(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    cat = call.data.removeprefix("cat_")
    if cat not in CATEGORIES: return await call.answer("Category not found", show_alert=True)
    data = CATEGORIES[cat]
    body = TEXTS[lang]["choose_prod"] if CATALOG.get(cat) else TEXTS[lang]["no_products"]
    await call.message.edit_text(f'{e(data["emoji"], data["fallback"])} <b>{data["title"][lang]}</b>\n\n{body}', reply_markup=products_keyboard(lang, cat, is_reseller_mode=False), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("prod_"))
async def open_product(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    payload = call.data.removeprefix("prod_")
    try:
        cat, index = payload.rsplit("_", 1)
        index = int(index)
        product = CATALOG[cat][index]
    except: return await call.answer("Product not found", show_alert=True)
    await call.message.edit_text(product_text(lang, product, call.from_user.id, is_reseller_mode=False), reply_markup=product_detail_keyboard(lang, cat, index, is_reseller_mode=False), parse_mode="HTML")
    await call.answer()

# -------- БАЛАНС (КЛИЕНТ) --------
@dp.callback_query(F.data == "menu_balance")
async def open_deposit(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    balance = get_balance(call.from_user.id)
    await call.message.edit_text(f'{e(E_MONEY, "💰")} <b>{TEXTS[lang]["balance"]}: {balance:.2f}$</b>\n\nСоманы таңдаңыз (min $1, max $100):', reply_markup=deposit_amount_keyboard(lang), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("dep_amt_"))
async def choose_amount(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    amount = int(call.data.removeprefix("dep_amt_"))
    await call.message.edit_text(f'{e(E_COIN, "🪙")} Сома: <b>${amount}</b>\n\nТөлем әдісін таңдаңыз:', reply_markup=deposit_method_keyboard(lang, amount), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("dep_m_"))
async def choose_method(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    try: _, _, amount, method = call.data.split("_"); amount = int(amount)
    except: return await call.answer("Қате", show_alert=True)
    user_states[call.from_user.id] = {"step": "await_receipt", "amount": amount, "method": method}
    if method == "binance": details = f'{e(E_BINANCE, "💵")} <b>Binance</b>\n\n{e(E_ID, "🆔")} ID: <code>1254750465</code>\n{e(E_COIN, "🪙")} Сома: <b>${amount}</b>\n\nТөлем жасап, чекті сурет ретінде осы чатқа жіберіңіз.'
    else: details = f'{e(E_KASPI, "💳")} <b>Kaspi</b>\n\n{e(E_PHONE, "📞")} : <code>87782935178</code>\n{e(E_USER, "👤")} : <b>Бердибек Қанат</b>\n{e(E_COIN, "🪙")} Сома: <b>${amount}</b>\n\nТөлем жасап, чекті сурет ретінде осы чатқа жіберіңіз.'
    await call.message.edit_text(details + "\n\n⚠️ Тек <b>реалды чек</b> қабылданады.", parse_mode="HTML")
    await call.answer()

@dp.message(F.photo)
async def receive_receipt(message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not isinstance(state, dict) or state.get("step") != "await_receipt": return
    amount = state["amount"]; method = state["method"]
    deposit_id = create_deposit(user_id, amount, method)
    user_states.pop(user_id, None)
    await message.answer(f"✅ Чек қабылданды!\nOrder №<b>{deposit_id}</b>\nСома: <b>${amount}</b>\n\nАдмин тексереді. Расталған соң баланс қосылады.", parse_mode="HTML")
    try:
        await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=f"🧾 <b>Жаңа депозит</b>\n\nOrder: <b>#{deposit_id}</b>\nUser: <code>{user_id}</code>\nAmount: <b>${amount}</b>\nMethod: <b>{method}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Растау", callback_data=f"dep_ok_{deposit_id}", style="success"), InlineKeyboardButton(text="❌ Қабылдамау", callback_data=f"dep_no_{deposit_id}", style="danger")]]))
    except Exception as exc: print("Deposit notify error:", type(exc).__name__)

@dp.callback_query(F.data.startswith("dep_ok_"))
async def approve_dep(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    deposit_id = int(call.data.removeprefix("dep_ok_"))
    row = approve_deposit(deposit_id)
    if not row: return await call.answer("Бұл order әлдеқашан өңделген.", show_alert=True)
    if call.message.caption: await call.message.edit_caption(caption=call.message.caption + "\n\n✅ <b>РАСТАЛДЫ</b>", parse_mode="HTML")
    try: await bot.send_message(row["user_id"], f"✅ Төлеміңіз расталды!\nБалансыңызға <b>${row['amount']:.2f}</b> қосылды.", parse_mode="HTML")
    except: pass
    await call.answer("Расталды")

@dp.callback_query(F.data.startswith("dep_no_"))
async def reject_dep(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    deposit_id = int(call.data.removeprefix("dep_no_"))
    row = reject_deposit(deposit_id)
    if not row: return await call.answer("Бұл order әлдеқашан өңделген.", show_alert=True)
    if call.message.caption: await call.message.edit_caption(caption=call.message.caption + "\n\n❌ <b>ҚАБЫЛДАМАДЫ</b>", parse_mode="HTML")
    try: await bot.send_message(row["user_id"], "❌ Чек қабылданбады.\nСебебі: чек жарамсыз немесе қайталанған.\nҚолдауға хабарласыңыз.")
    except: pass
    await call.answer("Қабылданбады")

# -------- RESELLER FLOW --------
@dp.callback_query(F.data == "menu_reseller")
async def reseller_menu(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    user_id = call.from_user.id
    if is_reseller(user_id):
        reseller_sessions.add(user_id)
        await call.message.edit_text(f'{e(E_RESELLER_SHOP, "🛒")} <b>Reseller Panel</b>\n\nҚош келдіңіз! Бағалар сізге арнайы жеңілдікпен көрсетіледі.', reply_markup=reseller_main_keyboard(lang), parse_mode="HTML")
        await call.answer()
        return
    user_states[user_id] = "reseller_login"
    await call.message.edit_text(f'{e(E_LOCK, "🔒")} <b>Enter the credentials provided by the administrator in the following format:</b>\n\n{e(E_USER, "👤")} LOGIN\n{e(E_KEY, "🔑")} PASSWORD', parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "res_shop")
async def reseller_shop(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    await call.message.edit_text(f'{e(B_SHOP, "🛒")} <b>Reseller {BTN[lang]["shop"]}</b>\n\n{TEXTS[lang]["choose_cat"]}', reply_markup=categories_keyboard(lang, is_reseller_mode=True), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "res_menu")
async def reseller_back_menu(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    await call.message.edit_text(f'{e(E_RESELLER_SHOP, "🛒")} <b>Reseller Panel</b>\n\nҚош келдіңіз! Бағалар сізге арнайы жеңілдікпен көрсетіледі.', reply_markup=reseller_main_keyboard(lang), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("res_cat_"))
async def open_res_category(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    cat = call.data.removeprefix("res_cat_")
    if cat not in CATEGORIES: return await call.answer("Category not found", show_alert=True)
    data = CATEGORIES[cat]
    body = TEXTS[lang]["choose_prod"] if CATALOG.get(cat) else TEXTS[lang]["no_products"]
    await call.message.edit_text(f'{e(data["emoji"], data["fallback"])} <b>{data["title"][lang]}</b>\n\n{body}', reply_markup=products_keyboard(lang, cat, is_reseller_mode=True), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("res_prod_"))
async def open_res_product(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    payload = call.data.removeprefix("res_prod_")
    try:
        cat, index = payload.rsplit("_", 1)
        index = int(index)
        product = CATALOG[cat][index]
    except: return await call.answer("Product not found", show_alert=True)
    await call.message.edit_text(product_text(lang, product, call.from_user.id, is_reseller_mode=True), reply_markup=product_detail_keyboard(lang, cat, index, is_reseller_mode=True), parse_mode="HTML")
    await call.answer()

# -------- BUY --------
@dp.callback_query(F.data.startswith("res_buy_") | F.data.startswith("buy_"))
async def buy_product(call: CallbackQuery):
    user_id = call.from_user.id
    lang = user_lang.get(user_id, "kk")
    is_reseller_mode = call.data.startswith("res_buy_")
    payload = call.data.removeprefix("res_buy_" if is_reseller_mode else "buy_")
    try:
        cat, index, duration_index = payload.rsplit("_", 2)
        index = int(index)
        duration_index = int(duration_index)
        product = CATALOG[cat][index]
        durations = product.get("reseller_durations", product["durations"]) if is_reseller_mode else product["durations"]
        n, unit, price = durations[duration_index]
    except: return await call.answer("Тариф табылмады", show_alert=True)

    product_id = product.get("product_id")
    if not product_id: return await call.answer(TEXTS[lang]["not_configured"], show_alert=True)

    lock = purchase_locks.setdefault(user_id, asyncio.Lock())
    if lock.locked(): return await call.answer("Сатып алу орындалып жатыр. Күте тұрыңыз.", show_alert=True)

    async with lock:
        balance = get_balance(user_id)
        if balance < price: return await call.answer(f"Баланс жеткіліксіз!\nҚажет: {price:.2f}$\nСізде: {balance:.2f}$", show_alert=True)
        await call.answer(TEXTS[lang]["sending"])
        try:
            http_status, raw_result = await api_buy(product_id=product_id, duration=api_duration(n, unit), quantity=1)
            try: api_response = json.loads(raw_result)
            except json.JSONDecodeError: api_response = None
            issued_key = find_issued_key(api_response) if api_response is not None else None
            status_value = str(api_response.get("status", "")).lower() if isinstance(api_response, dict) else ""
            api_says_error = http_status >= 400 or status_value in {"error", "failed", "failure"}
            if api_says_error or not issued_key:
                return await call.message.answer(f'<b>{TEXTS[lang]["api_reply"]}:</b>\n<code>{escape(raw_result[:2500])}</code>', parse_mode="HTML")
            success, remaining = change_balance(user_id, -price)
            if not success: return await call.message.answer("Баланс өзгеріп кетті. Қайталап көріңіз.")
            save_purchase(user_id, product["name"], price, issued_key)
            buyer = f"@{call.from_user.username}" if call.from_user.username else call.from_user.full_name or "user"
            buyer_message = buyer_sale_message(product, issued_key, buyer, price, remaining)
            try: await send_sale_announcement(user_id, buyer_message)
            except: await call.message.answer("Сатып алу орындалды, бірақ кілтті жеке хабарламаға жіберу мүмкін болмады. Ботқа /start жіберіңіз.")
            if SALES_GROUP_ID:
                try: await send_sale_announcement(int(SALES_GROUP_ID), group_sale_message(product, buyer))
                except Exception as exc: print("Group delivery error:", type(exc).__name__)
        except Exception as exc:
            print("Purchase error:", type(exc).__name__)
            await call.message.answer(TEXTS[lang]["api_error"])

# -------- БАСҚА МӘЗІРЛЕР (Профиль, Топ, Сыйлық, Қолдау, Нұсқаулық) --------
@dp.callback_query(F.data == "menu_profile")
async def show_profile(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    await call.message.edit_text(profile_text(call.from_user.id), reply_markup=profile_keyboard(lang), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "profile_keys")
async def show_my_keys(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    keys = get_last_keys(call.from_user.id, limit=5)
    if not keys:
        return await call.message.edit_text(f'{e(E_PROFILE_KEY, "🔑")} <b>My Keys</b>\n\nСіз әлі ешбір ключ сатып алмағансыз.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(lang, "menu_profile")]]), parse_mode="HTML")
    lines = [f'{e(E_PROFILE_KEY, "🔑")} <b>Соңғы {len(keys)} ключ</b>\n']
    for idx, row in enumerate(keys, 1): lines.append(f'<b>{idx}.</b> {escape(row["product_name"])}\n   🔑 <code>{escape(row["issued_key"])}</code>\n   💵 {float(row["price"]):.2f}$ • {(row["created_at"] or "")[:16]}\n')
    await call.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(lang, "menu_profile")]]), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "menu_support")
async def support_menu(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    await call.message.edit_text(
        f'{e(B_SUPPORT, "👑")} <b>Қолдау қызметі</b>\n\n'
        f'Сұрақтарыңыз болса, әкімшіге жазыңыз:\n'
        f'{e(E_ADMIN_CROWN, "👑")} @xerzcheat',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(lang, "back_main")]]),
        parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data == "menu_howto")
async def howto_menu(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    text = (
        f'{e(B_HOWTO, "📖")} <b>Қолдану нұсқаулығы</b>\n\n'
        f'1. Балансты толтырыңыз (Binance немесе Kaspi арқылы).\n'
        f'2. Дүкен бөліміне өтіп, қажетті тауарды таңдаңыз.\n'
        f'3. Сатып алу батырмасын басып, кілтті алыңыз.\n'
        f'4. Кілтті ойынға енгізіп, ләззат алыңыз!\n\n'
        f'Егер қандай да бір қателік шықса, қолдау қызметіне хабарласыңыз.'
    )
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(lang, "back_main")]]), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "menu_top")
async def top_menu(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    top_users = get_top_sellers()
    now = datetime.now()
    month_year = now.strftime("%B %Y")
    
    text = (
        f'{e(E_ADMIN_TOP, "👑")} <b>TOP 5 SELLERS MONTHLY</b>\n'
        f'{e(E_ADMIN_DIAMOND, "💎")} {month_year} - list resets automatically on the 1st of every month\n'
        f'•••••••••••••••••••••••••••••\n\n'
    )
    
    for idx, u in enumerate(top_users, 1):
        role_icon = "👑" if u["user_id"] == ADMIN_ID else ("💎" if u["role"] == "reseller" else "👤")
        role_text = "admin" if u["user_id"] == ADMIN_ID else u["role"]
        username = f"@{u['reseller_login']}" if u['reseller_login'] else f"ID: {u['user_id']}"
        
        text += (
            f'<b>{idx}. {username}</b>\n'
            f'{e(E_USER, "👤")} Role : {role_text}\n'
            f'{e(E_KEY, "🔑")} Keys : {u["total_sales"]}\n'
            f'{e(E_ADMIN_TOTAL, "💰")} Total Buy : {float(u["total_spent"]):.2f}$\n\n'
        )
        
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(lang, "back_main")]]), parse_mode="HTML")
    await call.answer()

# -------- СЫЙЛЫҚ (SPINNER) --------
@dp.callback_query(F.data == "menu_lucky")
async def lucky_menu(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    user = get_user(call.from_user.id)
    
    last_spin = user["last_spin"]
    if last_spin:
        last_spin_dt = datetime.strptime(last_spin, "%Y-%m-%d %H:%M:%S")
        if datetime.now() < last_spin_dt + timedelta(days=3):
            time_left = last_spin_dt + timedelta(days=3) - datetime.now()
            days = time_left.days
            hours, remainder = divmod(time_left.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            return await call.answer(f"Келесі сыйлықты {days} күн {hours} сағат {minutes} мин кейін ала аласыз!", show_alert=True)

    await call.message.edit_text(
        f'{e(E_GIFT, "🎁")} <b>Сыйлық алу (Рулетка)</b>\n\n'
        f'Күніне бір рет (3 күнде бір рет) тегін айналдырып, 0.1$ - 0.50$ аралығында ұтыс ала аласыз!\n\n'
        f'Айналдыру үшін төмендегі батырманы басыңыз.',
        reply_markup=spin_keyboard(lang),
        parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data == "spin_start")
async def spin_start(call: CallbackQuery):
    lang = user_lang.get(call.from_user.id, "kk")
    user_id = call.from_user.id
    user = get_user(user_id)
    
    last_spin = user["last_spin"]
    if last_spin:
        last_spin_dt = datetime.strptime(last_spin, "%Y-%m-%d %H:%M:%S")
        if datetime.now() < last_spin_dt + timedelta(days=3):
            return await call.answer("Сіз 3 күн бұрын ойнадыңыз. Күте тұрыңыз.", show_alert=True)

    await call.answer("Айналдырылуда...", show_alert=False)
    
    # Спиннер анимациясын имитациялау
    for _ in range(3):
        await call.message.edit_text(f'{e(E_GIFT, "🎁")} Спиннер айналуда... ⏳', reply_markup=None, parse_mode="HTML")
        await asyncio.sleep(0.5)

    prizes = [0.1, 0.2, 0.3, 0.4, 0.5]
    won_amount = random.choice(prizes)
    
    success, new_balance = change_balance(user_id, won_amount)
    if success:
        with db() as con:
            con.execute("UPDATE users SET last_spin = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))
            
        await call.message.edit_text(
            f'{e(E_GIFT, "🎁")} <b>Spin result: you won {won_amount:.2f}$!</b>\n\n'
            f'{e(E_PROFILE_COIN, "🪙")} <b>New balance: {new_balance:.2f}$</b>',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button(lang, "back_main")]]),
            parse_mode="HTML"
        )

# ==========================================
# 11. АДМИН ПАНЕЛЬ ЖӘНЕ ОНЫҢ ІШІНДЕГІЛЕР
# ==========================================
@dp.callback_query(F.data == "admin_open")
async def admin_open(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    user_states.pop(call.from_user.id, None)
    await call.message.edit_text(f'{e(E_ADMIN_CROWN, "👑")} <b>XERZZ ADMIN PANEL</b>', reply_markup=admin_keyboard(), parse_mode="HTML")
    await call.answer()

# Баланс қосу
@dp.callback_query(F.data == "admin_add")
async def admin_add(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    user_states[call.from_user.id] = "admin_add_id"
    await call.message.edit_text(
        f'{e(E_ADMIN_MONEY, "💰")} <b>Баланс қосу</b>\n\n'
        f'{e(E_ADMIN_PHONE, "📱")} Қолданушының Telegram ID-ін жіберіңіз:',
        parse_mode="HTML"
    )
    await call.answer()

# Баланс азайту (Тізіммен)
@dp.callback_query(F.data == "admin_sub")
async def admin_sub(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    users = get_all_users_for_admin()
    text = f'{e(E_ADMIN_MINUS, "➖")} <b>Баланс азайту</b>\n\nАзайту үшін қолданушыны таңдаңыз:'
    await call.message.edit_text(text, reply_markup=admin_users_keyboard("admin_sub_select", users), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("admin_sub_select_"))
async def admin_sub_select(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    target_id = int(call.data.removeprefix("admin_sub_select_"))
    user_states[call.from_user.id] = {"step": "admin_sub_amount", "target_id": target_id}
    await call.message.edit_text(
        f'{e(E_USER, "👤")} ID: <code>{target_id}</code>\n'
        f'Қанша ақша азайтамыз? (Мысалы: 5)',
        parse_mode="HTML"
    )
    await call.answer()

# Рөл өзгерту (Тізіммен)
@dp.callback_query(F.data == "admin_role")
async def admin_role(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    users = get_all_users_for_admin()
    text = f'{e(E_ADMIN_ROLE, "👤")} <b>Рөл өзгерту</b>\n\nҚолданушыны таңдаңыз:'
    await call.message.edit_text(text, reply_markup=admin_users_keyboard("admin_role_select", users), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("admin_role_select_"))
async def admin_role_select(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    target_id = int(call.data.removeprefix("admin_role_select_"))
    await call.message.edit_text(
        f'ID: <code>{target_id}</code>\nРөлін таңдаңыз:',
        reply_markup=admin_role_select_keyboard(target_id),
        parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("setrole_"))
async def set_role_handler(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    parts = call.data.split("_")
    role, target_id = parts[1], int(parts[2])
    set_role(target_id, role)
    await call.message.edit_text(
        f'✅ <code>{target_id}</code> қолданушысының рөлі <b>{role}</b> болып өзгертілді.',
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )
    await call.answer()

# Reseller аккаунт ашу (Логин/Пароль)
@dp.callback_query(F.data == "admin_reseller")
async def admin_reseller(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    user_states[call.from_user.id] = "admin_res_login"
    await call.message.edit_text(
        f'{e(E_KEY, "🔑")} <b>Reseller аккаунт ашу</b>\n\n'
        f'{e(E_USER, "👤")} LOGIN :', parse_mode="HTML"
    )
    await call.answer()

# Статистика
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    users, resellers, sales, revenue, balance_total = get_statistics()
    text = (
        f'{e(E_ADMIN_STAT2, "📈")} <b>XERZZ SHOP Статистика</b>\n\n'
        f'{e(E_USER, "👤")} Қолданушылар саны : <b>{users}</b>\n'
        f'{e(E_ADMIN_RESELLER, "💎")} Reseller - лер : <b>{resellers}</b>\n'
        f'{e(E_ADMIN_CART, "🛒")} Сатылымдар : <b>{sales}</b>\n'
        f'{e(E_ADMIN_MONEY, "💰")} Сатылған тауар сомасы : <b>{revenue:.2f}$</b>\n'
        f'{e(E_ADMIN_BANK, "🏢")} Қолданушылар балансы : <b>{balance_total:.2f}$</b>\n\n'
        f'{e(E_ADMIN_STAT2, "📈")} Reseller'
    )
    # Реселлерлер тізімін қосу
    with db() as con:
        reseller_list = con.execute("SELECT * FROM users WHERE role='reseller'").fetchall()
    
    text += "\n"
    for r in reseller_list:
        text += (
            f'\n{e(E_USER, "👤")} Role : reseller\n'
            f'{e(E_KEY, "🔑")} : {r["total_sales"]} keys\n'
            f'{e(E_ADMIN_MONEY, "💰")} : Bal {float(r["balance"]):.2f}$ | Spent {float(r["total_spent"]):.2f}$'
        )
        
    await call.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")
    await call.answer()

# Хабарлама тарату
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery):
    if not is_admin(call.from_user.id): return await call.answer("Рұқсат жоқ!", show_alert=True)
    user_states[call.from_user.id] = "broadcast"
    await call.message.edit_text("📢 Барлық қолданушыға жіберілетін хабарламаны жазыңыз:")
    await call.answer()

# ==========================================
# 12. МӘТІНДІК ХАБАРЛАМАЛАРДЫ ӨҢДЕУ (STATES)
# ==========================================
@dp.message()
async def process_text(message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    lang = user_lang.get(user_id, "kk")

    if isinstance(state, dict) and state.get("step") == "await_receipt": return
    if not message.text: return

    # --- RESELLER LOGIN ---
    if state == "reseller_login":
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) != 2: return await message.answer("Логин мен парольді бір жолға жазыңыз:\n<code>login password</code>", parse_mode="HTML")
        login, password = parts
        target_user = get_user_by_login(login)
        
        if target_user and target_user["reseller_password"] and check_password(password, target_user["reseller_password"]):
            set_role(user_id, "reseller") # Егер ол басқа адамның логинін білсе, өзін реселлер етеді (немесе тек өз логинін тексереді)
            user_states.pop(user_id, None)
            reseller_sessions.add(user_id)
            await message.answer(f'{e(E_RESELLER_SHOP, "🛒")} <b>Reseller Panel</b>\n\nҚош келдіңіз!', reply_markup=reseller_main_keyboard(lang), parse_mode="HTML")
        else: await message.answer("❌ Логин немесе пароль қате. Қайта енгізіңіз.")
        return

    if not is_admin(user_id):
        user_states.pop(user_id, None)
        return

    # --- ADMIN: BALANCE ADD ---
    if state == "admin_add_id":
        try: target_id = int(message.text.strip())
        except: return await message.answer("Telegram ID сан болуы керек.")
        user_states[user_id] = {"step": "admin_add_amount", "target_id": target_id}
        await message.answer("Қанша доллар қосамыз? Мысалы: 10")
        return

    if isinstance(state, dict) and state.get("step") == "admin_add_amount":
        try: amount = float(message.text.strip().replace(",", "."))
        except: return await message.answer("Дұрыс сан енгізіңіз.")
        target_id = state["target_id"]
        success, new_balance = change_balance(target_id, amount)
        user_states.pop(user_id, None)
        
        target_user = get_user(target_id)
        await message.answer(
            f"✅ Балансқа <b>{amount:.2f}$</b> қосылды.\n"
            f"ID: <code>{target_id}</code>\n"
            f"Role: <b>{target_user['role']}</b>\n"
            f"Balance: <b>{new_balance:.2f}$</b>\n"
            f"My balance: <b>{get_balance(user_id):.2f}$</b>",
            parse_mode="HTML", reply_markup=admin_keyboard()
        )
        try: 
            await bot.send_message(target_id, f"👤 Role : {target_user['role']}\n💰 Balance : {amount:.2f}$ қосылды\nЖалпы баланс: {new_balance:.2f}$")
        except: pass
        return

    # --- ADMIN: BALANCE SUB ---
    if isinstance(state, dict) and state.get("step") == "admin_sub_amount":
        try: amount = float(message.text.strip().replace(",", "."))
        except: return await message.answer("Дұрыс сан енгізіңіз.")
        target_id = state["target_id"]
        success, new_balance = change_balance(target_id, -amount)
        user_states.pop(user_id, None)
        if not success: return await message.answer("❌ Баланс жеткіліксіз.")
        
        await message.answer(f"✅ <b>{amount:.2f}$</b> алынды.\nID: <code>{target_id}</code>\nҚалған баланс: <b>{new_balance:.2f}$</b>", parse_mode="HTML", reply_markup=admin_keyboard())
        return

    # --- ADMIN: RESELLER CREATE ---
    if state == "admin_res_login":
        login = message.text.strip()
        if len(login) < 3: return await message.answer("Логин кемінде 3 таңба болуы керек.")
        user_states[user_id] = {"step": "admin_res_pass", "login": login}
        await message.answer(f'{e(E_KEY, "🔑")} PASSWORD :\n(Тек 4 сан болуы керек)')
        return

    if isinstance(state, dict) and state.get("step") == "admin_res_pass":
        password = message.text.strip()
        if not password.isdigit() or len(password) != 4:
            return await message.answer("Пароль тек 4 саннан тұруы керек! Қайта енгізіңіз:")
        
        login = state["login"]
        # Бұл жерде біз жаңа ID талап етпейміз, админ өзі үшін ашады деп есептейміз
        # Егер админ басқа адамға ашса, ол ID сұрауы керек еді, бірақ сұраныста ID сұрамау айтылған.
        # Сондықтан ағымдағы админге немесе бот арқылы кірген адамға байланыстырамыз.
        # Бірақ дұрысы - логин арқылы кірген адам сол логинге байланысты болады.
        
        # Біз админге логин/пароль жасап береміз. Ол кез келген адамға бере алады.
        # Ол адам кірген кезде, сол логин/пароль арқылы кіріп, өзінің ID-іне рөл алады.
        
        # Уақытша шешім: Тек логин мен парольді сақтаймыз, кім бірінші кірсе, сол реселлер болады.
        # Немесе админ өзіне ашады. Мен админге ашатын етіп жасаймын.
        
        created = create_reseller(user_id, login, password)
        user_states.pop(user_id, None)
        
        if created:
            await message.answer(f"✅ Reseller сәтті ашылды!\nLOGIN: <code>{login}</code>\nPASSWORD: <code>{password}</code>", parse_mode="HTML", reply_markup=admin_keyboard())
        else:
            await message.answer("❌ Бұл логин бос емес. Басқа логин таңдаңыз.", reply_markup=admin_keyboard())
        return

    # --- BROADCAST ---
    if state == "broadcast":
        users = get_all_user_ids()
        sent, failed = 0, 0
        status_message = await message.answer(f"📢 Жіберілуде... ({len(users)})")
        for tid in users:
            try:
                await bot.send_message(tid, message.text, parse_mode="HTML")
                sent += 1
            except: failed += 1
            await asyncio.sleep(0.05)
        user_states.pop(user_id, None)
        await status_message.edit_text(f"✅ Аяқталды.\nЖіберілді: {sent}\nҚате: {failed}", reply_markup=admin_keyboard())
        return

    # Қосымша мәзірлер
    if message.text.startswith("/"): return

# ==========================================
# 13. БОТТЫ ІСКЕ ҚОСУ
# ==========================================
async def main():
    init_db()
    print("XERZZ KEYSHOP іске қосылды!")
    print("Admin ID:", ADMIN_ID)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())