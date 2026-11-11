import datetime
import logging
import os
import sqlite3
from threading import Thread, Lock
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template_string, request
import telebot
from telebot import apihelper, types

# --- НАСТРОЙКИ И ЛОГИРОВАНИЕ ---
TOKEN = "9106912523:AAFVKhZC2epgoaVMPk4ZhPKVssZ1V1erma0"
ADMINS = [6969053096, 5956780231, 7000120396]
BY_TZ = ZoneInfo("Europe/Minsk")
DB_FILE = "bot_webapp_data.db"

WEBAPP_URL = "https://web-production-a6d39d86.up.railway.app/webapp"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("IgrikWebAppBot")

apihelper.proxy = None
bot = telebot.TeleBot(TOKEN)

# --- ИНТЕРФЕЙС МИНИ-ПРИЛОЖЕНИЯ ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ЭЗС 12-26</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen pb-20">
    <div class="max-w-md mx-auto p-4">
        <header class="flex justify-between items-center mb-6 pt-2">
            <h1 class="text-xl font-bold text-sky-400">⚡ ЭЗС 12-26</h1>
            <span id="admin-badge" class="hidden bg-amber-500/20 text-amber-400 text-xs px-2 py-1 rounded-full font-medium">Админ</span>
        </header>
        <div class="flex bg-slate-800 p-1 rounded-xl mb-6 shadow-lg">
            <button onclick="switchTab('schedule')" id="btn-schedule" class="flex-1 py-2 text-sm font-medium rounded-lg bg-sky-600 text-white transition">Расписание</button>
            <button onclick="switchTab('homework')" id="btn-homework" class="flex-1 py-2 text-sm font-medium rounded-lg text-slate-400 transition">Домашка</button>
        </div>
        <div id="tab-schedule" class="space-y-4">
            <div class="flex bg-slate-800 p-1 rounded-lg">
                <button onclick="setWeek('current')" id="week-btn-current" class="flex-1 py-1.5 text-xs font-medium rounded bg-slate-700 text-white">Эту неделю</button>
                <button onclick="setWeek('next')" id="week-btn-next" class="flex-1 py-1.5 text-xs font-medium rounded text-slate-400">Следующую</button>
            </div>
            <div id="schedule-container" class="space-y-3"></div>
        </div>
        <div id="tab-homework" class="hidden space-y-4">
            <div class="bg-slate-800 p-4 rounded-xl border border-slate-700">
                <h2 class="text-sm font-semibold mb-3 text-sky-300">➕ Добавить домашнее задание</h2>
                <form id="hw-form" onsubmit="submitHomework(event)" class="space-y-3">
                    <div>
                        <label class="block text-[11px] text-slate-400 mb-1">Дата на которую задано:</label>
                        <input type="date" id="hw-date" required class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2.5 text-sm text-slate-200 focus:outline-none focus:border-sky-500">
                    </div>
                    <input type="text" id="hw-subject" placeholder="Предмет (например, Физика)" required class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2.5 text-sm focus:outline-none focus:border-sky-500">
                    <textarea id="hw-task" placeholder="Что задали..." rows="2" required class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2.5 text-sm focus:outline-none focus:border-sky-500"></textarea>
                    <button type="submit" class="w-full bg-sky-600 hover:bg-sky-500 text-white py-2.5 rounded-lg text-sm font-medium transition">Опубликовать</button>
                </form>
            </div>
            <div id="homework-container" class="space-y-3"></div>
        </div>
    </div>
    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        let appData = null;
        let activeWeekType = 'current';
        const userId = tg.initDataUnsafe?.user?.id || 7000120396;
        const userName = (tg.initDataUnsafe?.user?.first_name || 'Студент');
        window.addEventListener('DOMContentLoaded', () => {
            const todayIso = new Date().toISOString().split('T')[0];
            const dateInput = document.getElementById('hw-date');
            if (dateInput) dateInput.value = todayIso;
            loadData();
        });
        function loadData() {
            fetch(`/api/data?user_id=${userId}`)
                .then(res => res.json())
                .then(data => {
                    appData = data;
                    if (data.is_admin) document.getElementById('admin-badge').classList.remove('hidden');
                    renderSchedule();
                    renderHomework();
                });
        }
        function switchTab(tab) {
            ['schedule', 'homework'].forEach(t => {
                document.getElementById(`tab-${t}`).classList.add('hidden');
                document.getElementById(`btn-${t}`).className = "flex-1 py-2 text-sm font-medium rounded-lg text-slate-400 transition";
            });
            document.getElementById(`tab-${tab}`).classList.remove('hidden');
            document.getElementById(`btn-${tab}`).className = "flex-1 py-2 text-sm font-medium rounded-lg bg-sky-600 text-white transition";
        }
        function setWeek(type) {
            activeWeekType = type;
            document.getElementById('week-btn-current').className = type === 'current' ? "flex-1 py-1.5 text-xs font-medium rounded bg-slate-700 text-white" : "flex-1 py-1.5 text-xs font-medium rounded text-slate-400";
            document.getElementById('week-btn-next').className = type === 'next' ? "flex-1 py-1.5 text-xs font-medium rounded bg-slate-700 text-white" : "flex-1 py-1.5 text-xs font-medium rounded text-slate-400";
            renderSchedule();
        }
        function renderSchedule() {
            if (!appData) return;
            const container = document.getElementById('schedule-container');
            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;
            container.innerHTML = schedule.map(day => `
                <div class="bg-slate-800 p-3.5 rounded-xl border border-slate-700 relative">
                    <div class="flex justify-between items-center mb-2">
                        <span class="font-semibold text-sky-400 text-sm">${day.day}</span>
                        <span class="text-xs text-slate-400">${day.date}</span>
                    </div>
                    <ul class="space-y-1 text-xs text-slate-300">
                        ${day.lessons.length > 0 ? day.lessons.map(l => `<li class="py-0.5">• ${l}</li>`).join('') : '<li class="text-slate-500 italic">Пар нет 🎉</li>'}
                    </ul>
                </div>
            `).join('');
        }
        function renderHomework() {
            if (!appData) return;
            const container = document.getElementById('homework-container');
            if (appData.homework.length === 0) {
                container.innerHTML = '<div class="text-center py-6 text-slate-500 text-sm">Домашки пока нет ✨</div>';
                return;
            }
            container.innerHTML = appData.homework.map(hw => `
                <div class="bg-slate-800 p-3.5 rounded-xl border border-slate-700 space-y-1">
                    <div class="flex justify-between items-center">
                        <span class="font-semibold text-amber-400 text-sm">${hw.subject}</span>
                        <span class="text-[11px] bg-slate-700/50 text-slate-300 px-2 py-0.5 rounded">${hw.date_key}</span>
                    </div>
                    <p class="text-xs text-slate-200 whitespace-pre-wrap">${hw.task}</p>
                    <div class="text-[10px] text-slate-500 text-right">Автор: ${hw.author}</div>
                </div>
            `).join('');
        }
        function submitHomework(e) {
            e.preventDefault();
            const dateKey = document.getElementById('hw-date').value;
            const subject = document.getElementById('hw-subject').value;
            const task = document.getElementById('hw-task').value;
            fetch('/api/homework', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ date_key: dateKey, subject, task, author: userName })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    document.getElementById('hw-task').value = '';
                    document.getElementById('hw-subject').value = '';
                    loadData();
                }
            });
        }
    </script>
</body>
</html>
"""

# --- ФЛАСК И СЕРВЕР ---
app = Flask(__name__)
db_lock = Lock()

@app.route("/")
def home():
    return "Игрик WebApp бот активен и работает 24/7!", 200

@app.route("/webapp")
def webapp_page():
    return render_template_string(HTML_TEMPLATE)

SCHEDULE = {
    "Понедельник": [["1 пара: Химия", "2 пара: Матем"], ["1 пара: Химия", "2 пара: Матем"]],
    "Вторник": [["1 пара: Биология", "2 пара: Физика"], ["1 пара: Биология", "2 пара: Физика"]],
    "Среда": [["1 пара: Матем", "2 пара: География"], ["1 пара: Матем", "2 пара: Информ"]],
    "Четверг": [["1 пара: Матем", "2 пара: Физика"], ["1 пара: Физика", "2 пара: История"]],
    "Пятница": [["1 пара: Рус. яз", "2 пара: Матем"], ["1 пара: Рус. яз", "2 пара: Матем"]],
    "Суббота": [["1 пара: История", "2 пара: Химия"], ["1 пара: История", "2 пара: Химия"]],
    "Воскресенье": [[], []],
}
DAYS_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

@app.route("/api/data", methods=["GET"])
def get_api_data():
    user_id = request.args.get("user_id", type=int)
    is_admin = user_id in ADMINS if user_id else False
    today = get_now().date()
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM homework ORDER BY date_key DESC, id DESC")
            homework = [dict(row) for row in cursor.fetchall()]
    return jsonify({
        "is_admin": is_admin,
        "current_week": get_week_data_safe(today, 0),
        "next_week": get_week_data_safe(today, 1),
        "homework": homework,
        "today_date": today.isoformat(),
    })

@app.route("/api/homework", methods=["POST"])
def add_homework():
    data = request.json
    if not data or not data.get("subject") or not data.get("task"):
        return jsonify({"status": "error"})
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO homework (date_key, subject, task, author) VALUES (?, ?, ?, ?)",
                           (data.get("date_key"), data.get("subject"), data.get("task"), data.get("author", "Студент")))
            conn.commit()
    return jsonify({"status": "success"})

def init_db():
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS homework (id INTEGER PRIMARY KEY AUTOINCREMENT, date_key TEXT, subject TEXT, task TEXT, author TEXT)")
            conn.commit()

init_db()

def get_now():
    return datetime.datetime.now(BY_TZ)

def get_week_data_safe(base_date, shift=0):
    start = base_date - datetime.timedelta(days=base_date.weekday()) + datetime.timedelta(weeks=shift)
    res = []
    for i in range(6):
        d = start + datetime.timedelta(days=i)
        w_type = 1 if d.isocalendar()[1] % 2 != 0 else 0
        res.append({"day": DAYS_ORDER[i], "date": d.strftime("%d.%m"), "full_date": d.isoformat(), "lessons": SCHEDULE.get(DAYS_ORDER[i], [[],[]])[w_type]})
    return res

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    web_app = types.WebAppInfo(url=WEBAPP_URL)
    markup.add(types.KeyboardButton("🚀 Открыть приложение ЭЗС 12-26", web_app=web_app))
    markup.add(types.KeyboardButton("📅 На сегодня"), types.KeyboardButton("📅 На завтра"))
    markup.add(types.KeyboardButton("📋 Расписание на неделю"))
    return markup

@bot.message_handler(func=lambda message: True, content_types=['text', 'photo', 'document', 'video', 'audio'])
def handle_all(message):
    try:
        text = (message.text or "").strip().lower()
        if "@" in text: text = text.split("@")[0]
        
        if text.startswith("/start") or "приложение" in text:
            bot.send_message(message.chat.id, "Привет! Используй кнопки ниже:", reply_markup=get_main_keyboard())
        elif "сегодня" in text:
            today = get_now().date()
            les = SCHEDULE.get(DAYS_ORDER[today.weekday()], [[],[]])[0]
            bot.send_message(message.chat.id, f"📅 Сегодня:\n" + "\n".join([f"• {l}" for l in les]), reply_markup=get_main_keyboard())
        elif "завтра" in text:
            tom = get_now().date() + datetime.timedelta(days=1)
            les = SCHEDULE.get(DAYS_ORDER[tom.weekday()], [[],[]])[0]
            bot.send_message(message.chat.id, f"📅 Завтра:\n" + "\n".join([f"• {l}" for l in les]), reply_markup=get_main_keyboard())
        else:
            bot.send_message(message.chat.id, "Меню:", reply_markup=get_main_keyboard())
    except Exception as e:
        log.error(e, exc_info=True)

if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке с защитой от падений
    def run_server():
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = Thread(target=run_server, daemon=True)
    t.start()
    log.info("Flask запущен.")

    try:
        bot.remove_webhook(remove_pending=True)
        bot.stop_polling()
    except: pass

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            log.error(e)
            import time; time.sleep(3)
