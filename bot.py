import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatJoinRequest
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 5387819554)
GROUP_ID = int(os.getenv("GROUP_ID") or -1003660114914)

PAYMENT_RECIPIENT = os.getenv("PAYMENT_RECIPIENT", "Тренер Ірина")
PAYMENT_IBAN = os.getenv("PAYMENT_IBAN", "UA12345678900000000000000000")
PAYMENT_BANK = os.getenv("PAYMENT_BANK", "Монобанк")

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
        await message.answer("Використання: /approve [user_id]\nПриклад: /approve 377139113")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await message.answer("Вкажіть правильний user_id (число).")
        return

    try:
        expire_date = datetime.utcnow() + timedelta(hours=24)  # 24 години

        invite = await bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            creates_join_request=True,
            name=f"Доступ для {target_id}",
            expire_date=expire_date
        )
        link = invite.invite_link

        await message.answer(
            f"Одноразове посилання створено (діє 24 години):\n{link}"
        )

        await bot.send_message(
            target_id,
            "Вітаємо в нашій дружній спільноті! 🎉\n"
            "Доступ активовано!\n\n"
            f"Натисни це посилання (діє 24 години):\n{link}\n\n"
            "Після натискання ти надішлеш запит на вступ — бот автоматично схвалить тебе за кілька секунд 💪"
        )

        await message.answer(f"Посилання надіслано користувачу {target_id}.")
        logger.info(f"Адмін створив одноразове посилання для {target_id}")

    except Exception as e:
        logger.error(f"Помилка створення запрошення: {e}")
        await message.answer(f"Помилка: {str(e)}\nПеревірте GROUP_ID та права бота.")


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
    await callback.message.edit_text(
        "Твій статус підписки поки що не активовано.\n"
        "Обери тариф, щоб отримати доступ! 💪",
        reply_markup=main_menu
    )
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
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query", "chat_join_request"]
    )


if __name__ == "__main__":
    asyncio.run(main())
