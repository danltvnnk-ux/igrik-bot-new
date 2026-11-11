import datetime
import logging
import os
import sqlite3
from functools import wraps
from threading import Lock, Thread
from zoneinfo import ZoneInfo
from flask import Flask, render_template
import telebot
from telebot import apihelper, types

# --- НАСТРОЙКИ И ЛОГИРОВАНИЕ ---
TOKEN = "9106912523:AAEzNfeJqGr3ZKIoi5OyTaOMpLqgDm0Idxo"
ADMINS = [6969053096, 5956780231]
BY_TZ = ZoneInfo("Europe/Minsk")
DB_FILE = "bot_webapp_data.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("IgrikWebAppBot")

apihelper.proxy = None
bot = telebot.TeleBot(TOKEN)

# --- FLASK СЕРВЕР ДЛЯ WEBAPP И RAILWAY ---
app = Flask(__name__)


@app.route("/")
def home():
    return "Игрик WebApp бот активен и работает 24/7!", 200


@app.route("/webapp")
def webapp_page():
    return render_template("index.html")


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()


try:
    bot.remove_webhook()
except Exception:
    pass

user_states = {}
db_lock = Lock()

# --- РАСПИСАНИЕ ЭЗС 12-26 ---
SCHEDULE = {
    "Понедельник": [
        [
            "1 пара (08.30-10.10): Химия",
            "2 пара (10.20-12.00): Матем",
            "3 пара (12.40-14.20): Бел. яз",
            "7 урок (14.30-15.15): Воспит. час",
        ],
        [
            "1 пара (08.30-10.10): Химия",
            "2 пара (10.20-12.00): Матем",
            "3 пара (12.40-14.20): Бел. яз",
            "7 урок (14.30-15.15): Воспит. час",
        ],
    ],
    "Вторник": [
        [
            "1 пара (08.30-10.10): Биология",
            "2 пара (10.20-12.00): Рус. лит",
            "3 пара (12.40-14.20): Физика",
            "4 пара (14.30-16.10): МП",
        ],
        [
            "1 пара (08.30-10.10): Биология",
            "2 пара (10.20-12.00): Рус. лит",
            "3 пара (12.40-14.20): Физика",
            "4 пара (14.30-16.10): МП",
        ],
    ],
    "Среда": [
        [
            "1 пара (08.30-10.10): Матем",
            "2 пара (10.20-12.00): Бел. лит",
            "3 пара (12.40-14.20): География",
        ],
        [
            "1 пара (08.30-10.10): Матем",
            "2 пара (10.20-12.00): Бел. лит",
            "3 пара (12.40-14.20): География",
            "4 пара (14.30-16.10): Информ",
        ],
    ],
    "Четверг": [
        [
            "1 пара (08.30-10.10): Матем",
            "2 пара (10.20-12.00): Биология",
            "3 пара (12.40-14.20): Физ-ра",
            "7 урок (14.30-15.15): Инф. час",
            "8,9 урок (15.25-17.05): ДП",
        ],
        [
            "1 пара (08.30-10.10): Физика",
            "2 пара (10.20-12.00): Ист. бел",
            "3 пара (12.40-14.20): Физ-ра",
            "7 урок (14.30-15.15): Инф. час",
            "8,9 урок (15.25-17.05): ДП",
        ],
    ],
    "Пятница": [
        [
            "1 пара (08.30-10.10): Рус. яз",
            "2 пара (10.20-12.00): Матем",
            "3 пара (12.40-14.20): Информ",
        ],
        [
            "1 пара (08.30-10.10): Рус. яз",
            "2 пара (10.20-12.00): Матем",
            "3 пара (12.40-14.20): Информ",
        ],
    ],
    "Суббота": [
        [
            "1 пара (08.30-10.10): Ист. бел",
            "2 пара (10.20-12.00): Химия",
            "3 пара (12.20-14.00): География",
        ],
        [
            "1 пара (08.30-10.10): Ист. бел",
            "2 пара (10.20-12.00): Химия",
            "3 пара (12.20-14.00): Физ-ра",
        ],
    ],
    "Воскресенье": [[], []],
}

DAYS_ORDER = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]


# --- РАБОТА С БД ---
def init_db():
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS homework (
                    date_key TEXT,
                    subject TEXT,
                    task TEXT,
                    PRIMARY KEY (date_key, subject)
                )
            """)
            conn.commit()


init_db()


def get_now():
    return datetime.datetime.now(BY_TZ)


def get_week_type():
    week_number = get_now().isocalendar()[1]
    return 1 if week_number % 2 != 0 else 0


def get_day_schedule_for_date(target_date):
    day_name = DAYS_ORDER[target_date.weekday()]
    today = get_now().date()
    days_diff = (target_date - today).days
    today_index = today.weekday()
    shift_weeks = (today_index + days_diff) // 7
    current_week_type = get_week_type()
    target_week_type = (current_week_type + shift_weeks) % 2
    return SCHEDULE.get(day_name, [[], []])[target_week_type]


def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    web_app = types.WebAppInfo(
        url="https://igrik-bot-new-production.up.railway.app/webapp"
    )
    markup.add(
        types.KeyboardButton("🚀 Открыть приложение ЭЗС 12-26", web_app=web_app)
    )
    markup.add(types.KeyboardButton("📅 На сегодня"), types.KeyboardButton("📅 На завтра"))
    markup.add(types.KeyboardButton("📋 Расписание на неделю"))
    return markup


def safe_handler(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        try:
            return func(message, *args, **kwargs)
        except Exception as e:
            log.error(f"Ошибка в обработчике {func.__name__}: {e}", exc_info=True)
            try:
                bot.send_message(message.chat.id, "❌ Произошла ошибка.")
            except Exception:
                pass

    return wrapper


@bot.message_handler(func=lambda message: True, content_types=["text"])
@safe_handler
def handle_messages(message):
    text = message.text.strip() if message.text else ""

    if text == "/start":
        bot.send_message(
            message.chat.id,
            (
                "Привет! Я второй бот группы ЭЗС 12-26 с поддержкой мини-приложения"
                " 🚀\nНажми кнопку ниже, чтобы открыть графический интерфейс:"
            ),
            reply_markup=get_main_keyboard(),
        )
    elif text == "📅 На сегодня":
        today = get_now().date()
        day_name = DAYS_ORDER[today.weekday()]
        lessons = get_day_schedule_for_date(today)
        lessons_str = (
            "\n".join([f"• {les}" for les in lessons])
            if lessons
            else "Пар нет 🎉"
        )
        bot.send_message(
            message.chat.id,
            (
                f"📅 **Сегодня ({day_name}, {today.strftime('%d.%m')})**\n\n"
                f"{lessons_str}"
            ),
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    elif text == "📅 На завтра":
        tomorrow = get_now().date() + datetime.timedelta(days=1)
        day_name = DAYS_ORDER[tomorrow.weekday()]
        lessons = get_day_schedule_for_date(tomorrow)
        lessons_str = (
            "\n".join([f"• {les}" for les in lessons])
            if lessons
            else "Пар нет 🎉"
        )
        bot.send_message(
            message.chat.id,
            (
                f"📅 **Завтра ({day_name}, {tomorrow.strftime('%d.%m')})**\n\n"
                f"{lessons_str}"
            ),
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
    elif text == "📋 Расписание на неделю":
        today = get_now().date()
        for i in range(7):
            check_date = today + datetime.timedelta(days=i)
            day_name = DAYS_ORDER[check_date.weekday()]
            lessons = get_day_schedule_for_date(check_date)
            lessons_str = (
                "\n".join([f"• {les}" for les in lessons])
                if lessons
                else "Выходной 🎉"
            )
            bot.send_message(
                message.chat.id,
                (
                    f"▫️ **{day_name} ({check_date.strftime('%d.%m')})**:\n"
                    f"{lessons_str}"
                ),
                parse_mode="Markdown",
            )
        bot.send_message(
            message.chat.id, "Выберите действие 👇", reply_markup=get_main_keyboard()
        )
    else:
        bot.send_message(
            message.chat.id, "Используйте меню ниже 👇", reply_markup=get_main_keyboard()
        )


if __name__ == "__main__":
    keep_alive()
    log.info("WebApp бот запущен.")
    while True:
        try:
            bot.infinity_polling(
                timeout=30, long_polling_timeout=30, skip_pending=True
            )
        except Exception as e:
            log.error(f"Ошибка polling: {e}", exc_info=True)
            import time

            time.sleep(5)
