import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatJoinRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
import sqlite3
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GROUP_ID = int(os.getenv("GROUP_ID"))
PAYMENT_RECIPIENT = os.getenv("PAYMENT_RECIPIENT")
PAYMENT_IBAN = os.getenv("PAYMENT_IBAN")
PAYMENT_BANK = os.getenv("PAYMENT_BANK")

# Webhook налаштування (додаємо з .env або Render variables)
WEBHOOK_PATH = "/webhook"
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")  # наприклад https://your-bot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-super-secret-2026")  # обов'язково зміни на свій!

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

waiting_for_proof = {}

main_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Обрати тариф", callback_data="choose_tariff")],
    [InlineKeyboardButton(text="Мій статус / до якої дати", callback_data="my_status")]
])

tariffs_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="14 днів — 500 грн", callback_data="tariff_14days")],
    [InlineKeyboardButton(text="1 місяць — 800 грн", callback_data="tariff_1month")],
    [InlineKeyboardButton(text="← Назад", callback_data="back")]
])

DB_FILE = "users.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            tariff TEXT,
            start_date TEXT,
            end_date TEXT,
            status TEXT DEFAULT 'pending',  -- pending / active / grace / expired / blocked
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def save_subscription(user_id: int, username: str, tariff: str, days: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    now = datetime.utcnow()

    # Перевіряємо, чи є вже запис
    cur.execute("SELECT end_date, status FROM users WHERE user_id = ?", (user_id,))
    existing = cur.fetchone()

    if existing:
        old_end_str, status = existing
        old_end = datetime.fromisoformat(old_end_str)

        # Беремо дату, від якої продовжуємо: max(зараз, стара end_date)
        base_date = max(now, old_end)

        new_end = base_date + timedelta(days=days)

        # Оновлюємо тільки дати + статус на active
        cur.execute("""
            UPDATE users 
            SET tariff = ?, 
                start_date = ?, 
                end_date = ?, 
                status = 'active',
                username = ?
            WHERE user_id = ?
        """, (tariff, now.isoformat(), new_end.isoformat(), username, user_id))

        logger.info(f"Продовжено підписку для {user_id}: +{days} днів")
    else:
        # Новий користувач
        new_end = now + timedelta(days=days)
        cur.execute('''
            INSERT INTO users 
            (user_id, username, tariff, start_date, end_date, status)
            VALUES (?, ?, ?, ?, ?, 'active')
        ''', (user_id, username, tariff, now.isoformat(), new_end.isoformat()))

        logger.info(f"Нова підписка для {user_id}: {days} днів")

    conn.commit()
    conn.close()

async def check_subscriptions():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, tariff, end_date, status 
        FROM users 
        WHERE status IN ('active', 'grace')
    """)
    users = cur.fetchall()
    conn.close()

    now = datetime.utcnow()

    for user_id, username, tariff, end_date_str, status in users:
        end_date = datetime.fromisoformat(end_date_str)
        days_past_end = (now - end_date).days

        if status == 'active' and days_past_end >= 0:
            # Початок grace period
            new_end = end_date + timedelta(days=2)  # grace до цього
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("UPDATE users SET status = 'grace', end_date = ? WHERE user_id = ?",
                        (new_end.isoformat(), user_id))
            conn.commit()
            conn.close()

            await bot.send_message(
                user_id,
                f"Привіт! Твоя підписка ({tariff}) закінчилася вчора.\n"
                f"У тебе є ще 2 дні grace-періоду, щоб продовжити без втрати доступу! 💪\n"
                "Обери тариф у меню і оплати, щоб залишитися з нами ❤️"
            )
            logger.info(f"Grace почався для {user_id}")

        elif status == 'grace':
            if days_past_end == 1:
                # День 1 grace — нагадування
                await bot.send_message(
                    user_id,
                    f"Залишився 1 день grace-періоду!\n"
                    f"Продовж підписку сьогодні, щоб не втратити доступ до тренувань 💙\n"
                    "Натисни /start і обери тариф!"
                )
            elif days_past_end >= 2:
                # Кік + expired
                try:
                    await bot.ban_chat_member(chat_id=GROUP_ID, user_id=user_id)
                    await bot.unban_chat_member(chat_id=GROUP_ID, user_id=user_id)  # щоб можна було повернутися пізніше
                    logger.info(f"Кік користувача {user_id} після grace")

                    conn = sqlite3.connect(DB_FILE)
                    cur = conn.cursor()
                    cur.execute("UPDATE users SET status = 'expired' WHERE user_id = ?", (user_id,))
                    conn.commit()
                    conn.close()

                    await bot.send_message(
                        user_id,
                        "На жаль, grace-період закінчився 😔\n"
                        "Твій доступ до групи закрито.\n"
                        "Щоб повернутися — обери тариф, оплати і напиши мені знову! 🚀"
                    )
                except Exception as e:
                    logger.error(f"Помилка кику {user_id}: {e}")

def get_user_status(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT tariff, start_date, end_date, status FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()

    if row:
        return {
            "tariff": row[0],
            "start_date": row[1],
            "end_date": row[2],
            "status": row[3]
        }
    return None

@dp.message(Command("checksubs"))
async def cmd_checksubs(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await check_subscriptions()
    await message.answer("Перевірку закінчення підписок виконано вручну!")

def get_payment_kb(user_id: int, tariff: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я оплатив", callback_data=f"paid_{user_id}_{tariff}")],
        [InlineKeyboardButton(text="← Назад до меню", callback_data="back")]
    ])


@dp.message(F.photo | F.document | F.video)
async def handle_proof(message: Message):
    user_id = message.from_user.id
    logger.info(f"Отримано медіа від {user_id} (тип: {message.content_type})")

    if user_id in waiting_for_proof:
        data = waiting_for_proof[user_id]
        username = data["username"]
        tariff_name = data["tariff"]

        logger.info(f"Пересилання медіа адміну від {user_id}")

        await bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )

        await bot.send_message(
            ADMIN_ID,
            f"Ось скрін/чек від @{username} (ID: {user_id})\n"
            f"Тариф: {tariff_name}\n"
            "Перевірте, будь ласка!"
        )

        await message.answer(
            "Скрін/чек успішно надіслано адміністратору! ❤️\n"
            "Зачекайте на підтвердження."
        )

        del waiting_for_proof[user_id]
    else:
        await message.answer("Якщо це оплата — спочатку натисніть «Я оплатив» після вибору тарифу 🙏")


@dp.message(Command("approve"))
async def cmd_approve(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Ця команда тільки для адміністратора.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Використання: /approve [user_id]")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await message.answer("user_id має бути числом.")
        return

    # Отримуємо дані з waiting_for_proof (якщо є)
    tariff_period = None
    if target_id in waiting_for_proof:
        data = waiting_for_proof[target_id]
        tariff_name = data["tariff"]
        tariff_period = "14days" if "14" in tariff_name else "1month"
        del waiting_for_proof[target_id]  # чистимо після апруву
    else:
        tariff_name = "невідомо"
        tariff_period = "14days"  # дефолт, або можна зробити помилку

    days = 14 if tariff_period == "14days" else 30

    try:
        expire_date = datetime.utcnow() + timedelta(hours=24)
        invite = await bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            creates_join_request=True,
            name=f"Доступ для {target_id}",
            expire_date=expire_date
        )
        link = invite.invite_link

        # Зберігаємо підписку в БД
        username = (await bot.get_chat(target_id)).username or f"id{target_id}"
        save_subscription(target_id, username, tariff_name, days)

        await message.answer(f"Посилання створено (24 год):\n{link}\nПідписка збережена в БД.")

        await bot.send_message(
            target_id,
            "Вітаємо в нашій дружній спільноті! 🎉\n"
            "Доступ активовано!\n\n"
            f"Натисни посилання (діє 24 години):\n{link}\n\n"
            "Після натискання бот автоматично схвалить твій запит за кілька секунд 💪"
        )

        logger.info(f"Апрув + збереження підписки для {target_id} ({tariff_name})")

    except Exception as e:
        logger.error(f"Помилка в /approve: {e}")
        await message.answer(f"Помилка: {str(e)}")


@dp.chat_join_request()
async def auto_approve_join(request: ChatJoinRequest):
    if request.chat.id != GROUP_ID:
        return

    user_id = request.from_user.id

    # Перевіряємо, чи цей користувач має активну/грас підписку в БД
    data = get_user_status(user_id)

    if data and data['status'] in ['active', 'grace']:
        await bot.approve_chat_join_request(
            chat_id=request.chat.id,
            user_id=user_id
        )
        logger.info(f"Автосхвалено вступ {user_id} (має підписку)")

        await bot.send_message(
            user_id,
            "Вітаємо в групі! 🎉\nТепер ти в нашій дружній спільноті з тренуваннями Ірини 💪"
        )
    else:
        # Якщо немає підписки — відхиляємо або ігноруємо
        await bot.decline_chat_join_request(
            chat_id=request.chat.id,
            user_id=user_id
        )
        logger.warning(f"Відхилено вступ {user_id} — немає активної підписки")

        # Опціонально: повідомити адміну
        await bot.send_message(
            ADMIN_ID,
            f"Хтось ({user_id} / @{request.from_user.username or 'без імені'}) спробував вступити без підписки!"
        )


@dp.message(F.chat.type == "private")
async def welcome(message: Message):
    await message.answer(
        "Привіт! 👋 Дякую, що звернувся до мене!\n"
        "Я — бот для платних тренувань Ірини: відео, чат, підтримка та мотивація 💙\n\n"
        "Обери тариф і почнемо твій шлях до результатів! 🚀",
        reply_markup=main_menu
    )


@dp.callback_query(F.data == "choose_tariff")
async def show_tariffs(callback: CallbackQuery):
    logger.info("Натиснуто 'Обрати тариф'")
    await callback.message.edit_text(
        "Обери тариф для доступу до тренувань Ірини 💪\n\n"
        "• 14 днів — 500 грн\n"
        "• 1 місяць — 800 грн",
        reply_markup=tariffs_menu
    )
    await callback.answer("Тарифи відкрито!")


@dp.callback_query(F.data == "back")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "Привіт! 👋 Дякую, що звернувся до мене!\n"
        "Я — бот для платних тренувань Ірини: відео, чат, підтримка та мотивація 💙\n\n"
        "Обери тариф і почнемо твій шлях до результатів! 🚀",
        reply_markup=main_menu
    )
    await callback.answer()


@dp.callback_query(F.data == "my_status")
async def my_status(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_status(user_id)

    if not data or data["status"] not in ["active", "grace"]:
        text = "Твій статус підписки поки що не активовано.\nОбери тариф, щоб отримати доступ! 💪"
    else:
        end_date = datetime.fromisoformat(data["end_date"])
        days_left = (end_date - datetime.utcnow()).days
        text = (
            f"Твоя підписка: **{data['tariff']}**\n"
            f"Активна до: **{end_date.strftime('%d.%m.%Y')}**\n"
            f"Залишилось приблизно {max(0, days_left)} днів\n\n"
            "Продовжуй рухатись до мети! 🚀"
        )

    await callback.message.edit_text(text, reply_markup=main_menu, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("tariff_"))
async def tariff_chosen(callback: CallbackQuery):
    period = callback.data.split("_")[1]
    tariff_name = "14 днів" if period == "14days" else "1 місяць"
    price = "500 грн" if period == "14days" else "800 грн"
    user_id = callback.from_user.id

    payment_code = f"Підписка {user_id}-{period}"

    text = (
        f"Ти обрав(ла) тариф: **{tariff_name} — {price}** ✅\n\n"
        f"Перекажіть **{price}** на рахунок:\n"
        f"Отримувач: {PAYMENT_RECIPIENT}\n"
        f"IBAN: {PAYMENT_IBAN}\n"
        f"Банк: {PAYMENT_BANK}\n\n"
        f"**Обов’язково в призначенні платежу вкажіть код:**\n"
        f"`{payment_code}`\n\n"
        "Після оплати натисніть кнопку нижче і надішліть скрін або чек оплати."
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_payment_kb(user_id, period),
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("paid_"))
async def user_paid(callback: CallbackQuery):
    _, user_id_str, period = callback.data.split("_")
    user_id = int(user_id_str)
    username = callback.from_user.username or "без @username"
    tariff_name = "14 днів" if period == "14days" else "1 місяць"

    logger.info(f"Користувач {user_id} (@{username}) натиснув 'Я оплатив'")

    await callback.message.edit_text(
        "Дякуємо! Тепер надішліть скрін або чек оплати прямо сюди.\n"
        "Адміністратор перевірить і активує доступ!",
        reply_markup=main_menu
    )
    await callback.answer("Дякуємо!")

    waiting_for_proof[user_id] = {
        "tariff": tariff_name,
        "username": username,
        "period": period
    }

    await bot.send_message(
        ADMIN_ID,
        f"Новий запит на перевірку!\n"
        f"Користувач: @{username} (ID: {user_id})\n"
        f"Тариф: {tariff_name}\n"
        "Чекаємо скрін/чек..."
    )

# Startup: встановлюємо webhook
async def on_startup(bot: Bot):
    if not BASE_WEBHOOK_URL:
        logger.error("BASE_WEBHOOK_URL не встановлено в змінних середовища!")
        sys.exit(1)

    webhook_url = f"{BASE_WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True  # ігноруємо старі оновлення після рестарту
    )
    logger.info(f"Webhook встановлено на {webhook_url}")

# Shutdown: видаляємо webhook (опціонально, але корисно)
async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logger.info("Webhook видалено")

# ... весь твій код до кінця хендлерів без змін ...

# Startup і shutdown залишаються async
# ... весь твій код до кінця хендлерів без змін ...

# Startup і shutdown залишаються async
async def on_startup(bot: Bot):
    if not BASE_WEBHOOK_URL:
        logger.error("BASE_WEBHOOK_URL не встановлено в змінних середовища!")
        sys.exit(1)

    webhook_url = f"{BASE_WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )
    logger.info(f"Webhook встановлено на {webhook_url}")

    # Запускаємо планувальник тут — event loop вже існує!
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_subscriptions,
        CronTrigger(hour=9, minute=0),
        id='daily_subscription_check'
    )
    scheduler.start()
    logger.info("Планувальник запущено (перевірка щодня о 9:00)")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logger.info("Webhook видалено")

def main():
    print("Бот запускається...")
    print(f"ADMIN_ID: {ADMIN_ID}")
    print(f"GROUP_ID: {GROUP_ID}")
    port = int(os.getenv("PORT", 8080))
    print(f"Запуск сервера на порту: {port}")
    print(f"BASE_WEBHOOK_URL: {BASE_WEBHOOK_URL}")
    print(f"WEBHOOK_SECRET: {WEBHOOK_SECRET[:5]}... (скрито)")

    init_db()
    print("База даних ініціалізована")

    # aiohttp додаток
    app = web.Application()

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
        handle_in_background=True
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    # Реєструємо startup/shutdown
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    port = int(os.getenv("PORT", 8080))

    # Запускаємо сервер — це створює event loop
    # Всередині startup ми запустимо scheduler
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()