import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatJoinRequest
from dotenv import load_dotenv
import os
import sqlite3

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GROUP_ID = int(os.getenv("GROUP_ID"))

PAYMENT_RECIPIENT = os.getenv("PAYMENT_RECIPIENT")
PAYMENT_IBAN = os.getenv("PAYMENT_IBAN")
PAYMENT_BANK = os.getenv("PAYMENT_BANK")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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

    start = datetime.utcnow()
    end = start + timedelta(days=days)

    cur.execute('''
        INSERT OR REPLACE INTO users 
        (user_id, username, tariff, start_date, end_date, status)
        VALUES (?, ?, ?, ?, ?, 'active')
    ''', (
        user_id,
        username,
        tariff,
        start.isoformat(),
        end.isoformat()
    ))

    conn.commit()
    conn.close()


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
    if request.chat.id == GROUP_ID:
        await bot.approve_chat_join_request(
            chat_id=request.chat.id,
            user_id=request.from_user.id
        )
        logger.info(f"Автоматично схвалено вступ {request.from_user.id}")
        await bot.send_message(
            request.from_user.id,
            "Вітаємо в групі! 🎉\nТепер ти в нашій дружній спільноті з тренуваннями Ірини 💪"
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


async def main():
    print("Бот запускається...")
    print(f"ADMIN_ID: {ADMIN_ID}")
    print(f"GROUP_ID: {GROUP_ID}")

    init_db()  # ← додаємо тут
    print("База даних ініціалізована")

    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query", "chat_join_request"]
    )


if __name__ == "__main__":
    asyncio.run(main())