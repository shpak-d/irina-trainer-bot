import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatJoinRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
import sqlite3
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram.types import FSInputFile

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GROUP_ID = int(os.getenv("GROUP_ID"))
PAYMENT_RECIPIENT = os.getenv("PAYMENT_RECIPIENT")
PAYMENT_IBAN = os.getenv("PAYMENT_IBAN")
PAYMENT_BANK = os.getenv("PAYMENT_BANK")
WEBHOOK_PATH = "/webhook"
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

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
DB_FILE = "/data/users.db"


def init_db():
    with sqlite3.connect(DB_FILE) as conn:  # Додано context manager для DB
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                tariff TEXT,
                start_date TEXT,
                end_date TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

def save_subscription(user_id: int, username: str, tariff: str, days: int):
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT end_date, status FROM users WHERE user_id = ?",
            (user_id,)
        )
        existing = cur.fetchone()
        if existing:
            old_end_str, status = existing
            old_end = datetime.fromisoformat(old_end_str)
            # Визначаємо точку відліку для нових днів
            if status == 'active' and old_end > now:
                # ще активна підписка → продовжуємо від кінця старої
                base_date = old_end
                action = "Продовжено активну підписку"
            else:
                # grace, expired, або інший статус → починаємо з моменту оплати
                base_date = now
                action = "Активовано нову підписку (grace/expired)"

            new_end = base_date + timedelta(days=days)
            cur.execute("""
                UPDATE users
                SET 
                    tariff      = ?,
                    start_date  = ?,
                    end_date    = ?,
                    status      = 'active',
                    username    = ?
                WHERE user_id = ?
            """, (
                tariff,
                now.isoformat(),
                new_end.isoformat(),
                username,
                user_id
            ))
            logger.info(f"{action} для {user_id}: +{days} днів, нова дата закінчення: {new_end.isoformat()}")
        else:
            # Новий користувач — просто додаємо від зараз
            new_end = now + timedelta(days=days)
            cur.execute('''
                INSERT INTO users
                (user_id, username, tariff, start_date, end_date, status)
                VALUES (?, ?, ?, ?, ?, 'active')
            ''', (
                user_id,
                username,
                tariff,
                now.isoformat(),
                new_end.isoformat()
            ))
            logger.info(f"Нова підписка для {user_id}: {days} днів, закінчення: {new_end.isoformat()}")
        conn.commit()

async def check_subscriptions():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, username, tariff, end_date, status
            FROM users
            WHERE status IN ('active', 'grace')
        """)
        users = cur.fetchall()
    now = datetime.now(timezone.utc)
    for user_id, username, tariff, end_date_str, status in users:
        end_date = datetime.fromisoformat(end_date_str)
        days_before_end = (end_date - now).days
        if status == 'active' and days_before_end < 1:
            new_end = end_date + timedelta(days=2)
            with sqlite3.connect(DB_FILE) as conn:
                cur = conn.cursor()
                cur.execute("UPDATE users SET status = 'grace', end_date = ? WHERE user_id = ?",
                            (new_end.isoformat(), user_id))
                conn.commit()
            await bot.send_message(user_id,
                                   f"Привіт! Твоя підписка ({tariff}) закінчується сьогодні.\nНе хвилюйся, у тебе буде ще 2 дні grace-періоду, щоб продовжити без втрати доступу! 💪\nОбери тариф у меню і оплати, щоб залишитися з нами ❤️")
            logger.info(f"Grace почався для {user_id}")
        elif status == 'grace':
            days_left_in_grace = (end_date - now).days

            if days_left_in_grace == 1:
                # сьогодні — перед останній день grace (перший)
                await bot.send_message(user_id,
                                       f"Це перший з двох днів grace-періоду!\n"
                                       f"Підписка закінчиться післязавтра зранку.\n"
                                       "Продовж, щоб не втратити доступ до тренувань 💙")
            if days_left_in_grace == 0:
                # сьогодні — останній день grace (другий)
                await bot.send_message(user_id,
                                       f"Це останній день grace-періоду!\n"
                                       f"Підписка закінчиться завтра зранку.\n"
                                       "Продовж сьогодні, щоб не втратити доступ до тренувань 💙")

            elif days_left_in_grace < 0:
                # grace закінчився (сьогодні або раніше)
                try:
                    await bot.ban_chat_member(chat_id=GROUP_ID, user_id=user_id)
                    await bot.unban_chat_member(chat_id=GROUP_ID, user_id=user_id)
                    logger.info(f"Кік користувача {user_id} після grace")
                    with sqlite3.connect(DB_FILE) as conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE users SET status = 'expired' WHERE user_id = ?", (user_id,))
                        conn.commit()
                    await bot.send_message(user_id,
                                           "На жаль, grace-період закінчився 😔\n"
                                           "Твій доступ до групи закрито.\n"
                                           "Щоб повернутися — напиши мені знову та обери тариф. 🚀")
                except Exception as e:
                    logger.error(f"Помилка кіку {user_id}: {e}")


async def daily_backup():
    try:
        await bot.send_document(chat_id=ADMIN_ID, document=FSInputFile(DB_FILE),
                                caption=f"Щоденний бекап бази даних {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        logger.info("Щоденний бекап бази надіслано адміну")
    except Exception as e:
        logger.error(f"Помилка щоденного бекапу: {e}")


def get_user_status(user_id: int) -> dict | None:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT tariff, start_date, end_date, status FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    if row:
        return {"tariff": row[0], "start_date": row[1], "end_date": row[2], "status": row[3]}
    return None


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ заборонено. Це тільки для адміністратора.")
        return
    admin_menu = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Список підписників", callback_data="admin_listusers")],
        [InlineKeyboardButton(text="Додати підписку", callback_data="admin_addsub")],
        [InlineKeyboardButton(text="Видалити підписку", callback_data="admin_removesub")],
        [InlineKeyboardButton(text="Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="Перевірити закінчення підписок", callback_data="admin_checksubs")],
        [InlineKeyboardButton(text="Зробити бекап бази", callback_data="admin_backupdb")],
        [InlineKeyboardButton(text="Розіслати запрошення з БД", callback_data="admin_sendinvites")],
        [InlineKeyboardButton(text="Перевірка зайців", callback_data="admin_checkzaycev")],
        [InlineKeyboardButton(text="Очистити expired записи (спитай Дена перед натисканням)", callback_data="admin_clean_expired")],
        [InlineKeyboardButton(text="Закрити меню", callback_data="admin_close")]
    ])
    await message.answer("Вітаю в адмін-панелі! 💻\nЩо хочеш зробити?", reply_markup=admin_menu)


@dp.callback_query(F.data.startswith("admin_"))
async def admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ заборонено!", show_alert=True)
        return
    data = callback.data
    if data == "admin_listusers":
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id, username, tariff, end_date, status FROM users ORDER BY end_date DESC")
            users = cur.fetchall()
        if not users:
            text = "Підписників поки що немає."
        else:
            text = "Список підписників:\n\n"
            for uid, uname, tar, edate, stat in users:
                text += f"ID: {uid} | @{uname or 'немає'} | {tar} | До: {edate} | {stat}\n"
        await callback.message.edit_text(text)
    elif data == "admin_addsub":
        example = "/addsub 123456789 14days 14"
        await callback.message.edit_text(
            "Формат: /addsub [user_id] [tariff] [days]\n\n"
            f"Приклад (натисни, щоб скопіювати):\n"
            f"`{example}`",
            parse_mode="Markdown"
        )
        await callback.answer()

    elif data == "admin_removesub":
        example = "/removesub 123456789"
        await callback.message.edit_text(
            "Формат: /removesub [user_id]\n\n"
            f"Приклад (натисни, щоб скопіювати):\n"
            f"`{example}`",
            parse_mode="Markdown"
        )
        await callback.answer()
    elif data == "admin_stats":
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users WHERE status = 'active'")
            active = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users")
            total = cur.fetchone()[0]
        text = f"Статистика:\nАктивних підписників: {active}\nВсього записів: {total}"
        await callback.message.edit_text(text)
    elif data == "admin_checksubs":
        await check_subscriptions()
        await callback.message.edit_text(
            "Перевірку закінчення підписок виконано вручну!\nНагадування/кіки відправлено, якщо потрібно.")
        await callback.answer("Перевірку виконано!")
    elif data == "admin_checkzaycev":
        try:
            total_members = await bot.get_chat_member_count(GROUP_ID)
            admins = await bot.get_chat_administrators(GROUP_ID)
            total_admins = len(admins) - 1
            with sqlite3.connect(DB_FILE) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM users WHERE status IN ('active', 'grace')")
                db_active = cur.fetchone()[0]
            potential_zaycev = total_members - total_admins - db_active
            if potential_zaycev <= 0:
                text = f"Зайців не виявлено! 😊\nВ групі {total_members} учасників (з них {total_admins} адмінів).\nВ БД {db_active} активних підписок."
            else:
                text = f"Увага! Виявлено {potential_zaycev} можливих зайців! 🚨\nВ групі {total_members} учасників (з них {total_admins} адмінів).\nВ БД {db_active} активних підписок.\nПеревірте учасників групи вручну."
            await callback.message.edit_text(text)
            await callback.answer("Перевірку завершено!")
        except Exception as e:
            await callback.message.edit_text(f"Помилка перевірки: {str(e)}")
            await callback.answer("Помилка!", show_alert=True)
    elif data == "admin_clean_expired":
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM users WHERE status = 'expired'")
            deleted_count = cur.rowcount
            conn.commit()

        await callback.message.edit_text(
            f"Очищено {deleted_count} записів зі статусом 'expired'.\nБаза чиста! 🧹")
        await callback.answer("База почищена!")
    elif data == "admin_backupdb":
        try:
            await callback.message.answer_document(FSInputFile(DB_FILE),
                                                   caption=f"Ручний бекап бази даних {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
            await callback.message.edit_text("Бекап бази надіслано тобі як документ!")
        except Exception as e:
            await callback.message.edit_text(f"Помилка бекапу: {str(e)}")
        await callback.answer("Бекап надіслано!")
    elif data == "admin_sendinvites":
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users WHERE status IN ('active', 'grace')")
            users = [row[0] for row in cur.fetchall()]
        sent = 0
        errors = 0
        for uid in users:
            try:
                expire_date = datetime.now(timezone.utc) + timedelta(hours=24)
                invite = await bot.create_chat_invite_link(GROUP_ID, creates_join_request=True,
                                                           name=f"Відновлення доступу для {uid}",
                                                           expire_date=expire_date)
                link = invite.invite_link
                await bot.send_message(uid,
                                       f"Доступ відновлено! 🎉\nПриєднуйся назад до групи:\n{link}\nПосилання діє 24 години. Бот схвалить запит автоматично 💪")
                sent += 1
            except Exception as e:
                logger.error(f"Помилка розсилки запрошення {uid}: {e}")
                errors += 1
        await callback.message.edit_text(
            f"Розсилку завершено. Надіслано {sent} запрошень з {len(users)}. Помилок: {errors}")
        await callback.answer("Розсилка запрошень завершено")
    elif data == "admin_close":
        await callback.message.delete()
    await callback.answer()

@dp.message(Command("addsub"))
async def cmd_addsub(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    args = message.text.split()[1:]
    if len(args) < 3:
        example = "/addsub 123456789 14days 14"
        await message.answer(
            "Формат: /addsub [user_id] [tariff] [days]\n\n"
            f"Приклад (натисни, щоб скопіювати):\n"
            f"`{example}`",
            parse_mode="Markdown"
        )
        return

    try:
        user_id = int(args[0])
        tariff = args[1]
        days = int(args[2])
    except ValueError:
        await message.answer("Неправильний формат.")
        return

    username = (await bot.get_chat(user_id)).username or f"id{user_id}"
    save_subscription(user_id, username, tariff, days)
    # Автоматичне надсилання запрошення
    try:
        expire_date = datetime.now(timezone.utc) + timedelta(hours=24)
        invite = await bot.create_chat_invite_link(
            GROUP_ID,
            creates_join_request=True,
            name=f"Доступ для {user_id} після addsub",
            expire_date=expire_date
        )
        link = invite.invite_link
        await bot.send_message(
            user_id,
            f"Підписка активована вручну адміном! 🎉\n"
            f"Приєднуйся до групи (посилання діє 24 години):\n"
            f"{link}\n"
            "Бот автоматично схвалить запит 💪"
        )
        await message.answer(
            f"Підписка додана/продовжена для {user_id} ({tariff}, {days} днів)\n"
            f"Запрошення надіслано користувачу: `{link}`"
        )
    except Exception as e:
        logger.error(f"Помилка надсилання запрошення після addsub {user_id}: {e}")
        await message.answer(f"Підписка додана, але помилка надсилання запрошення: {str(e)}")

@dp.message(Command("removesub"))
async def cmd_removesub(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    args = message.text.split()
    if len(args) < 2:
        example = "/removesub 123456789"
        await message.answer(
            "Формат: /removesub [user_id]\n\n"
            f"Приклад (натисни, щоб скопіювати):\n"
            f"`{example}`",
            parse_mode="Markdown"
        )
        return

    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("user_id має бути числом.")
        return

    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()

    try:
        await bot.ban_chat_member(chat_id=GROUP_ID, user_id=user_id)
        await bot.unban_chat_member(chat_id=GROUP_ID, user_id=user_id)
        logger.info(f"Користувач {user_id} видалений з групи після removesub")
        await message.answer(f"Підписка для {user_id} видалена з БД і користувач видалений з групи.")
    except Exception as e:
        logger.error(f"Помилка кіку після removesub: {e}")
        await message.answer(f"Підписка видалена з БД, але помилка видалення з групи: {str(e)}")

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


@dp.message(F.photo | F.document | F.video, F.chat.type == "private")
async def handle_proof(message: Message):
    user_id = message.from_user.id
    logger.info(f"Отримано медіа від {user_id} (тип: {message.content_type})")
    if user_id in waiting_for_proof:
        data = waiting_for_proof[user_id]
        username = data["username"]
        tariff_name = data["tariff"]
        period = data["period"]
        await message.answer("Скрін/чек успішно надіслано адміністратору! ❤️\nЗачекайте на підтвердження.")
        forwarded = await bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
        approve_button = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Апрув цього платежу", callback_data=f"approve_{user_id}_{period}")]
        ])
        await bot.send_message(ADMIN_ID,
                               f"Ось скрін/чек від @{username} (ID: {user_id})\nТариф: {tariff_name}\nПеревірте, будь ласка!",
                               reply_markup=approve_button, reply_to_message_id=forwarded.message_id)
        del waiting_for_proof[user_id]
    else:
        await message.answer("Якщо це оплата — спочатку натисніть «Я оплатив» після вибору тарифу 🙏")


async def approve_user(user_id: int, period: str,
                       message_or_callback):  # Нова функція для консолідації апрув-логіки (видалено дублювання з cmd_approve і callback)
    tariff_name = "14 днів" if period == "14days" else "1 місяць"
    days = 14 if period == "14days" else 30
    try:
        expire_date = datetime.now(timezone.utc) + timedelta(hours=24)
        invite = await bot.create_chat_invite_link(GROUP_ID, creates_join_request=True, name=f"Доступ для {user_id}",
                                                   expire_date=expire_date)
        link = invite.invite_link
        username = (await bot.get_chat(user_id)).username or f"id{user_id}"
        save_subscription(user_id, username, tariff_name, days)
        await bot.send_message(user_id,
                               f"Вітаємо в нашій дружній спільноті! 🎉\nДоступ активовано!\n\nНатисни посилання (діє 24 години):\n{link}\n\nПісля натискання бот автоматично схвалить твій запит 💪")
        logger.info(f"Апрув + збереження підписки для {user_id} ({tariff_name})")
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(f"Посилання створено (24 год):\n{link}\nПідписка збережена в БД.")
        else:  # CallbackQuery
            await message_or_callback.message.edit_text(
                f"Апрув виконано для {user_id} ({tariff_name})!\nПосилання створено (24 год):\n{link}\nПідписка збережена.")
            await message_or_callback.answer("Апрув успішний!")
    except Exception as e:
        logger.error(f"Помилка в апруві: {e}")
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(f"Помилка: {str(e)}")
        else:
            await message_or_callback.answer(f"Помилка: {str(e)}", show_alert=True)


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
        user_id = int(args[1])
    except ValueError:
        await message.answer("user_id має бути числом.")
        return
    period = "14days"  # Дефолт, якщо не з waiting_for_proof (спрощено, бо ручний апрув не залежить від стану)
    if user_id in waiting_for_proof:
        period = waiting_for_proof[user_id]["period"]
        del waiting_for_proof[user_id]
    await approve_user(user_id, period, message)


@dp.message(Command("backupdb"))
async def cmd_backupdb(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        await message.answer_document(FSInputFile(DB_FILE),
                                      caption=f"Ручний бекап бази даних {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        logger.info(f"Ручний бекап бази надіслано адміну {ADMIN_ID}")
    except Exception as e:
        await message.answer(f"Помилка надсилання бази: {str(e)}")
        logger.error(f"Помилка ручного бекапу: {e}")


@dp.chat_join_request()
async def auto_approve_join(request: ChatJoinRequest):
    if request.chat.id != GROUP_ID:
        return
    user_id = request.from_user.id
    data = get_user_status(user_id)
    if data and data['status'] in ['active', 'grace']:
        await bot.approve_chat_join_request(request.chat.id, user_id)
        logger.info(f"Автосхвалено вступ {user_id} (має підписку)")
        await bot.send_message(user_id,
                               "Вітаємо в групі! 🎉\nТепер ти в нашій дружній спільноті з тренуваннями Ірини 💪")
    else:
        await bot.decline_chat_join_request(request.chat.id, user_id)
        logger.warning(f"Відхилено вступ {user_id} — немає активної підписки")
        await bot.send_message(ADMIN_ID,
                               f"Хтось ({user_id} / @{request.from_user.username or 'без імені'}) спробував вступити без підписки!")


@dp.message(F.chat.type == "private")
async def welcome(message: Message):
    if message.from_user.id == ADMIN_ID and not message.text.startswith('/'):
        await cmd_admin(message)
        return
    await message.answer(
        "Привіт! 👋 Дякую, що звернувся до мене!\nЯ — бот для платних тренувань Ірини: відео, чат, підтримка та мотивація 💙\n\nОбери тариф і почнемо твій шлях до результатів! 🚀",
        reply_markup=main_menu)


@dp.callback_query(F.data == "choose_tariff")
async def show_tariffs(callback: CallbackQuery):
    logger.info("Натиснуто 'Обрати тариф'")
    await callback.message.edit_text(
        "Обери тариф для доступу до тренувань Ірини 💪\n\n• 14 днів — 500 грн\n• 1 місяць — 800 грн",
        reply_markup=tariffs_menu)
    await callback.answer("Тарифи відкрито!")


@dp.callback_query(F.data == "back")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "Привіт! 👋 Дякую, що звернувся до мене!\nЯ — бот для платних тренувань Ірини: відео, чат, підтримка та мотивація 💙\n\nОбери тариф і почнемо твій шлях до результатів! 🚀",
        reply_markup=main_menu)
    await callback.answer()


@dp.callback_query(F.data == "my_status")
async def my_status(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_status(user_id)
    if not data or data["status"] not in ["active", "grace"]:
        text = "Твій статус підписки поки що не активовано.\nОбери тариф, щоб отримати доступ! 💪"
    else:
        end_date = datetime.fromisoformat(data["end_date"])
        days_left = (end_date - datetime.now(timezone.utc)).days
        text = f"Твоя підписка в статусі: **{data['status']}**\nАктивна до: **{end_date.strftime('%d.%m.%Y')}**\nЗалишилось приблизно {max(0, days_left)} днів\n\nПродовжуй рухатись до мети! 🚀"
    await callback.message.edit_text(text, reply_markup=main_menu, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("tariff_"))
async def tariff_chosen(callback: CallbackQuery):
    period = callback.data.split("_")[1]
    tariff_name = "14 днів" if period == "14days" else "1 місяць"
    price = "500 грн" if period == "14days" else "800 грн"
    user_id = callback.from_user.id
    payment_code = f"Підписка {user_id}"
    text = f"Ти обрав(ла) тариф: **{tariff_name} — {price}** ✅\n\nПерекажіть **{price}** на рахунок (просто натисни на IBAN та призначення — вони скопіюються):\n\nОтримувач: {PAYMENT_RECIPIENT}\nIBAN: `{PAYMENT_IBAN}`\nБанк: {PAYMENT_BANK}\n\n**Призначення платежу (обов’язково!):** `{payment_code}`\n\nПісля оплати натисни кнопку нижче і надішли скрін або чек оплати."
    await callback.message.edit_text(text, reply_markup=get_payment_kb(user_id, period), parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("approve_"))
async def admin_approve_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Тільки адмін може апрувати!", show_alert=True)
        return
    _, user_id_str, period = callback.data.split("_")
    user_id = int(user_id_str)
    await callback.answer("Апрув прийнято, обробляю…")
    asyncio.create_task(approve_user(user_id, period, callback))


@dp.callback_query(F.data.startswith("paid_"))
async def user_paid(callback: CallbackQuery):
    _, user_id_str, period = callback.data.split("_")
    user_id = int(user_id_str)
    username = callback.from_user.username or "без @username"
    tariff_name = "14 днів" if period == "14days" else "1 місяць"
    logger.info(f"Користувач {user_id} (@{username}) натиснув 'Я оплатив'")
    await callback.message.edit_text(
        "Дякуємо! Тепер надішліть скрін або чек оплати прямо сюди.\nАдміністратор перевірить і активує доступ!",
        reply_markup=main_menu)
    await callback.answer("Дякуємо!")
    waiting_for_proof[user_id] = {"tariff": tariff_name, "username": username, "period": period}
    await bot.send_message(ADMIN_ID,
                           f"Новий запит на перевірку!\nКористувач: @{username} (ID: {user_id})\nТариф: {tariff_name}\nЧекаємо скрін/чек...")


async def on_startup(bot: Bot):  # Об'єднано дублювання: webhook + scheduler
    if not BASE_WEBHOOK_URL:
        logger.error("BASE_WEBHOOK_URL не встановлено в змінних середовища!")
        raise SystemExit(1)  # Замість sys.exit для asyncio
    webhook_url = f"{BASE_WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    await bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logger.info(f"Webhook встановлено на {webhook_url}")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions, CronTrigger(hour=9, minute=0), id='daily_subscription_check')
    scheduler.add_job(daily_backup, CronTrigger(hour=21, minute=0), id='daily_backup')
    scheduler.start()
    logger.info("Планувальник запущено (перевірка щодня о 9:00 + бекап о 23:00)")


async def on_shutdown(bot: Bot):
    logger.warning("Shutdown detected, webhook not removed (Render safe)")


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
    app = web.Application()

    async def healthcheck(request):
        return web.Response(text="ok")

    app.router.add_get("/", healthcheck)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET,
                                           handle_in_background=True)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()