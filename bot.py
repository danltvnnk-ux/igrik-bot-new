import datetime
import logging
import os
import sqlite3
from functools import wraps
from threading import Lock, Thread
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template_string, request
import telebot
from telebot import apihelper, types

# --- НАСТРОЙКИ И ЛОГИРОВАНИЕ ---
TOKEN = "9106912523:AAEzNfeJqGr3ZKIoi5OyTaOMpLqgDm0Idxo"
ADMINS = [6969053096, 5956780231, 7000120396]
BY_TZ = ZoneInfo("Europe/Minsk")
DB_FILE = "bot_webapp_data.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("IgrikWebAppBot")

apihelper.proxy = None
bot = telebot.TeleBot(TOKEN)

# --- HTML ИНТЕРФЕЙС МИНИ-ПРИЛОЖЕНИЯ ---
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

        <!-- Вкладки (только Расписание и Домашка) -->
        <div class="flex bg-slate-800 p-1 rounded-xl mb-6 shadow-lg">
            <button onclick="switchTab('schedule')" id="btn-schedule" class="flex-1 py-2 text-sm font-medium rounded-lg bg-sky-600 text-white transition">Расписание</button>
            <button onclick="switchTab('homework')" id="btn-homework" class="flex-1 py-2 text-sm font-medium rounded-lg text-slate-400 transition">Домашка</button>
        </div>

        <!-- РАСПИСАНИЕ -->
        <div id="tab-schedule" class="space-y-4">
            <div class="flex bg-slate-800 p-1 rounded-lg">
                <button onclick="setWeek('current')" id="week-btn-current" class="flex-1 py-1.5 text-xs font-medium rounded bg-slate-700 text-white">Эту неделю</button>
                <button onclick="setWeek('next')" id="week-btn-next" class="flex-1 py-1.5 text-xs font-medium rounded text-slate-400">Следующую</button>
            </div>

            <div id="schedule-container" class="space-y-3"></div>
        </div>

        <!-- ДОМАШКА -->
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

    <!-- МОДАЛЬНОЕ ОКНО ПРОСМОТРА ДЗ ДНЯ -->
    <div id="day-hw-modal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-slate-800 border border-slate-700 w-full max-w-sm rounded-2xl p-4 space-y-4 shadow-2xl">
            <div class="flex justify-between items-center">
                <h3 id="day-hw-modal-title" class="text-sm font-semibold text-sky-400">Домашнее задание</h3>
                <button onclick="closeDayHwModal()" class="text-slate-400 hover:text-white text-sm font-bold">✕</button>
            </div>
            <div id="day-hw-container" class="space-y-2 max-h-60 overflow-y-auto pr-1"></div>
            <button onclick="closeDayHwModal()" class="w-full bg-slate-700 hover:bg-slate-600 text-slate-200 text-xs py-2.5 rounded-xl font-medium transition">Закрыть</button>
        </div>
    </div>

    <!-- МОДАЛЬНОЕ ОКНО РЕДАКТИРОВАНИЯ ДНЯ -->
    <div id="edit-modal" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-slate-800 border border-slate-700 w-full max-w-sm rounded-2xl p-4 space-y-4 shadow-2xl">
            <div class="flex justify-between items-center">
                <h3 id="modal-title" class="text-sm font-semibold text-amber-400">Редактирование дня</h3>
                <button onclick="closeEditModal()" class="text-slate-400 hover:text-white text-sm font-bold">✕</button>
            </div>
            <div id="modal-lessons-container" class="space-y-2 max-h-60 overflow-y-auto pr-1"></div>
            <button onclick="addLessonInput()" class="w-full bg-slate-700 hover:bg-slate-600 text-slate-200 text-xs py-2 rounded-lg font-medium transition">➕ Добавить пару / урок</button>
            <div class="flex gap-2 pt-2">
                <button onclick="closeEditModal()" class="flex-1 bg-slate-700 hover:bg-slate-600 text-slate-300 text-xs py-2.5 rounded-xl font-medium transition">Отмена</button>
                <button onclick="saveDaySchedule()" class="flex-1 bg-amber-600 hover:bg-amber-500 text-white text-xs py-2.5 rounded-xl font-medium transition">Сохранить</button>
            </div>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();

        let appData = null;
        let activeWeekType = 'current';
        let editingDay = null;
        let currentEditingLessons = [];

        const userId = tg.initDataUnsafe?.user?.id || 7000120396;
        const userName = (tg.initDataUnsafe?.user?.first_name || 'Студент') + (tg.initDataUnsafe?.user?.last_name ? ' ' + tg.initDataUnsafe?.user?.last_name : '');

        window.addEventListener('DOMContentLoaded', () => {
            const todayIso = new Date().toISOString().split('T')[0];
            const dateInput = document.getElementById('hw-date');
            if (dateInput) dateInput.value = todayIso;
        });

        function loadData() {
            fetch(`/api/data?user_id=${userId}`)
                .then(res => res.json())
                .then(data => {
                    appData = data;
                    if (data.is_admin) {
                        document.getElementById('admin-badge').classList.remove('hidden');
                    }
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
                <div class="bg-slate-800 p-3.5 rounded-xl border border-slate-700 relative pb-11">
                    <div class="flex justify-between items-center mb-2">
                        <span class="font-semibold text-sky-400 text-sm">${day.day}</span>
                        <span class="text-xs text-slate-400">${day.date}</span>
                    </div>
                    <ul class="space-y-1 text-xs text-slate-300 mb-2">
                        ${day.lessons.length > 0 ? day.lessons.map(l => `<li class="py-0.5">• ${l}</li>`).join('') : '<li class="text-slate-500 italic">Пар нет 🎉</li>'}
                    </ul>
                    <div class="absolute bottom-2.5 left-3 right-3 flex justify-between items-center pt-2 border-t border-slate-700/50">
                        <button onclick="openDayHwModal('${day.full_date}', '${day.day}', '${day.date}')" class="bg-sky-500/20 hover:bg-sky-500/40 text-sky-300 px-2.5 py-1 rounded-lg text-[11px] font-medium transition flex items-center gap-1">
                            📖 Просмотреть ДЗ
                        </button>
                        ${appData.is_admin ? `
                            <button onclick="openEditModal('${day.day}')" class="bg-amber-500/20 hover:bg-amber-500/40 text-amber-400 px-2.5 py-1 rounded-lg text-[11px] font-medium transition flex items-center gap-1">
                                ✏️ Изменить
                            </button>
                        ` : ''}
                    </div>
                </div>
            `).join('');
        }

        function openDayHwModal(fullDateStr, dayName, shortDateStr) {
            const modalTitle = document.getElementById('day-hw-modal-title');
            const container = document.getElementById('day-hw-container');
            
            modalTitle.innerText = `ДЗ на ${dayName} (${shortDateStr})`;
            const filteredHw = appData.homework.filter(hw => hw.date_key === fullDateStr);

            if (filteredHw.length === 0) {
                container.innerHTML = '<p class="text-xs text-slate-500 italic text-center py-4">На этот день домашних заданий нет ✨</p>';
            } else {
                container.innerHTML = filteredHw.map(hw => `
                    <div class="bg-slate-900 p-3 rounded-xl border border-slate-700/60 space-y-1">
                        <div class="flex justify-between items-center">
                            <span class="font-semibold text-amber-400 text-xs">${hw.subject}</span>
                            <span class="text-[10px] text-slate-500">${hw.author}</span>
                        </div>
                        <p class="text-xs text-slate-200 whitespace-pre-wrap">${hw.task}</p>
                    </div>
                `).join('');
            }

            document.getElementById('day-hw-modal').classList.remove('hidden');
        }

        function closeDayHwModal() {
            document.getElementById('day-hw-modal').classList.add('hidden');
        }

        function openEditModal(dayName) {
            editingDay = dayName;
            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;
            const dayObj = schedule.find(d => d.day === dayName);
            currentEditingLessons = dayObj ? [...dayObj.lessons] : [];
            
            document.getElementById('modal-title').innerText = `${dayName} (${activeWeekType === 'current' ? 'Текущая' : 'Следующая'})`;
            renderModalLessons();
            document.getElementById('edit-modal').classList.remove('hidden');
        }

        function closeEditModal() {
            document.getElementById('edit-modal').classList.add('hidden');
            editingDay = null;
        }

        function renderModalLessons() {
            const container = document.getElementById('modal-lessons-container');
            if (currentEditingLessons.length === 0) {
                container.innerHTML = '<p class="text-xs text-slate-500 italic text-center py-4">Пар нет</p>';
                return;
            }
            container.innerHTML = currentEditingLessons.map((lesson, index) => `
                <div class="flex items-center gap-2">
                    <input type="text" value="${lesson}" onchange="updateLessonText(${index}, this.value)" class="flex-1 bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs text-slate-200 focus:outline-none focus:border-amber-500">
                    <button onclick="removeLesson(${index})" class="bg-rose-600/20 hover:bg-rose-600 text-rose-300 hover:text-white p-2 rounded-lg text-xs transition" title="Удалить пару">🗑</button>
                </div>
            `).join('');
        }

        function updateLessonText(index, val) {
            currentEditingLessons[index] = val;
        }

        function removeLesson(index) {
            currentEditingLessons.splice(index, 1);
            renderModalLessons();
        }

        function addLessonInput() {
            const num = currentEditingLessons.length + 1;
            currentEditingLessons.push(`${num} пара: Новый предмет`);
            renderModalLessons();
        }

        function saveDaySchedule() {
            if (!editingDay) return;
            const weekTypeNum = activeWeekType === 'current' ? 0 : 1;

            fetch('/api/schedule', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    user_id: userId, 
                    day: editingDay, 
                    week_type: weekTypeNum, 
                    lessons: currentEditingLessons.filter(l => l.trim().length > 0) 
                })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    closeEditModal();
                    loadData();
                } else {
                    alert('Ошибка: ' + (data.message || 'Не удалось сохранить'));
                }
            });
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

        loadData();
    </script>
</body>
</html>
"""

# --- ФЛАСК И СЕРВЕР ---
app = Flask(__name__)


@app.route("/")
def home():
    return "Игрик WebApp бот активен и работает 24/7!", 200


@app.route("/webapp")
def webapp_page():
    return render_template_string(HTML_TEMPLATE)


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


@app.route("/api/data", methods=["GET"])
def get_api_data():
    user_id = request.args.get("user_id", type=int)
    is_admin = user_id in ADMINS if user_id else False

    today = get_now().date()
    current_week_schedule = get_week_schedule_data(today, shift_weeks=0)
    next_week_schedule = get_week_schedule_data(today, shift_weeks=1)

    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM homework ORDER BY date_key DESC, id DESC"
            )
            hw_rows = cursor.fetchall()
            homework = [dict(row) for row in hw_rows]

    return jsonify({
        "is_admin": is_admin,
        "current_week": current_week_schedule,
        "next_week": next_week_schedule,
        "homework": homework,
        "today_date": today.isoformat(),
    })


@app.route("/api/schedule", methods=["POST"])
def update_schedule():
    data = request.json
    user_id = data.get("user_id")
    if user_id not in ADMINS:
        return jsonify({"status": "error", "message": "Нет прав админа"})

    day = data.get("day")
    week_type = data.get("week_type")
    lessons = data.get("lessons")

    if day in SCHEDULE and week_type in [0, 1]:
        SCHEDULE[day][week_type] = lessons
        return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "Неверные данные"})


@app.route("/api/homework", methods=["POST"])
def add_homework():
    data = request.json
    subject = data.get("subject")
    task = data.get("task")
    date_key = data.get("date_key", get_now().date().isoformat())
    author = data.get("author", "Аноним")

    if not subject or not task:
        return jsonify({"status": "error", "message": "Пустые поля"})

    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO homework (date_key, subject, task, author) VALUES"
                " (?, ?, ?, ?)",
                (date_key, subject, task, author),
            )
            conn.commit()
    return jsonify({"status": "success"})


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


def init_db():
    with db_lock:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS homework (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_key TEXT,
                    subject TEXT,
                    task TEXT,
                    author TEXT
                )
            """)
            conn.commit()


init_db()


def get_now():
    return datetime.datetime.now(BY_TZ)


def get_week_type(target_date):
    week_number = target_date.isocalendar()[1]
    return 1 if week_number % 2 != 0 else 0


def get_week_schedule_data(base_date, shift_weeks=0):
    start_of_week = base_date - datetime.timedelta(
        days=base_date.weekday()
    ) + datetime.timedelta(weeks=shift_weeks)
    week_data = []

    for i in range(6):
        day_date = start_of_week + datetime.timedelta(days=i)
        day_name = DAYS_ORDER[i]
        w_type = get_week_type(day_date)
        lessons = SCHEDULE.get(day_name, [[], []])[w_type]
        week_data.append({
            "day": day_name,
            "date": day_date.strftime("%d.%m"),
            "full_date": day_date.isoformat(),
            "lessons": lessons,
        })
    return week_data


def get_day_schedule_for_date(target_date):
    day_name = DAYS_ORDER[target_date.weekday()]
    today = get_now().date()
    days_diff = (target_date - today).days
    today_index = today.weekday()
    shift_weeks = (today_index + days_diff) // 7
    current_week_type = get_week_type(today)
    target_week_type = (current_week_type + shift_weeks) % 2
    return SCHEDULE.get(day_name, [[], []])[target_week_type]


def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    web_app = types.WebAppInfo(
        url="https://web-production-a6d39d86.up.railway.app/webapp"
    )
    markup.add(
        types.KeyboardButton("🚀 Открыть приложение ЭЗС 12-26", web_app=web_app)
    )
    markup.add(types.KeyboardButton("📅 На сегодня"), types.KeyboardButton("📅 На завтра"))
    return markup


def get_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    web_app = types.WebAppInfo(
        url="https://web-production-a6d39d86.up.railway.app/webapp"
    )
    markup.add(types.InlineKeyboardButton("🚀 Открыть приложение ЭЗС 12-26", web_app=web_app))
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
    is_group = message.chat.type in ["group", "supergroup"]

    if text == "/start":
        if is_group:
            bot.send_message(
                message.chat.id,
                "Привет! Я бот группы ЭЗС 12-26. Нажми кнопку ниже, чтобы открыть приложение:",
                reply_markup=get_inline_keyboard()
            )
        else:
            bot.send_message(
                message.chat.id,
                (
                    "Привет! Я второй бот группы ЭЗС 12-26 с поддержкой мини-приложения"
                    " 🚀\nНажми кнопку ниже, чтобы открыть графический интерфейс:"
                ),
                reply_markup=get_main_keyboard(),
            )
    elif text == "📅 На сегодня":
        if is_group:
            return
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
        if is_group:
            return
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
    else:
        if not is_group:
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
