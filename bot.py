import hashlib
import hmac
import html
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from threading import Lock, Thread
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import telebot
from flask import Flask, jsonify, request
from telebot import types

# --- НАСТРОЙКИ И ОКРУЖЕНИЕ ---
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

_admins_raw = os.environ.get("ADMINS", "6969053096,5956780231,7000120396")
try:
    ADMINS = [int(x.strip()) for x in _admins_raw.split(",") if x.strip()]
except ValueError:
    raise RuntimeError("Переменная ADMINS должна содержать ID через запятую")

BY_TZ = ZoneInfo("Europe/Minsk")
DB_FILE = os.environ.get("DB_PATH", "bot_webapp_data.db")
MINIAPP_SHORT_NAME = "ezs"
WEBAPP_URL = "https://web-production-a6d39d86.up.railway.app/webapp"
AUTH_MAX_AGE = 86400  # срок действия initData, секунд

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("IgrikWebAppBot")

bot = telebot.TeleBot(TOKEN)

# Имя бота нужно для ссылки на мини-приложение. Без него ссылка будет неверной,
# поэтому при ошибке процесс падает сразу, а не работает с неправильной ссылкой.
BOT_USERNAME = None
for _attempt in range(3):
    try:
        BOT_USERNAME = bot.get_me().username
        break
    except Exception as _e:
        log.warning("get_me не удался (попытка %s): %s", _attempt + 1, _e)
        time.sleep(2)
if not BOT_USERNAME:
    raise RuntimeError("Не удалось получить username бота через get_me()")

db_lock = Lock()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- ПРОВЕРКА ПОДПИСИ TELEGRAM INIT DATA ---
def verify_init_data(init_data):
    """Возвращает (user_id, first_name, last_name), если подпись и срок действия верны, иначе (None, None, None)."""
    if not init_data:
        return None, None, None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None, None, None

        # auth_date обязателен: Telegram всегда его присылает
        auth_date_str = pairs.get("auth_date")
        if not auth_date_str or time.time() - int(auth_date_str) > AUTH_MAX_AGE:
            return None, None, None

        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None, None, None

        user = json.loads(pairs.get("user", "{}"))
        user_id = user.get("id")
        if not isinstance(user_id, int):
            return None, None, None
        return user_id, user.get("first_name") or "Студент", user.get("last_name") or ""
    except Exception as e:
        log.error("Ошибка проверки initData: %s", e)
        return None, None, None


# --- ИНТЕРФЕЙС МИНИ-ПРИЛОЖЕНИЯ ---
# raw-строка: обратные слеши в JS (например, \s в регулярке) сохраняются как есть
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ЭЗС 12-26</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .bg-custom-dark { background-color: #121212; }
        .bg-custom-card { background-color: #1a1a1a; }
        .bg-custom-border { border-color: #333333; }
        .btn-chocolate { background-color: #8B5A2B; }
        .btn-chocolate:hover { background-color: #A06835; }
    </style>
</head>
<body class="bg-custom-dark text-zinc-100 min-h-screen pb-20">
    <div class="max-w-md mx-auto p-4">
        <header class="flex justify-between items-center mb-6 pt-2">
            <h1 class="text-xl font-bold text-zinc-100 flex items-center gap-2">
                <span>🤎</span> ЭЗС 12-26
            </h1>
            <span id="admin-badge" class="hidden btn-chocolate text-white text-xs px-2.5 py-1 rounded-full font-medium shadow">Админ</span>
        </header>

        <!-- Вкладки -->
        <div class="flex bg-custom-card border border-custom-border p-1 rounded-xl mb-6 shadow-lg">
            <button onclick="switchTab('schedule')" id="btn-schedule" class="flex-1 py-2 text-sm font-medium rounded-lg btn-chocolate text-white transition">Расписание</button>
            <button onclick="switchTab('homework')" id="btn-homework" class="flex-1 py-2 text-sm font-medium rounded-lg text-zinc-400 transition">Домашка</button>
        </div>

        <!-- РАСПИСАНИЕ -->
        <div id="tab-schedule" class="space-y-4">
            <div class="flex bg-custom-card border border-custom-border p-1 rounded-lg">
                <button onclick="setWeek('current')" id="week-btn-current" class="flex-1 py-1.5 text-xs font-medium rounded btn-chocolate text-white shadow">Эту неделю</button>
                <button onclick="setWeek('next')" id="week-btn-next" class="flex-1 py-1.5 text-xs font-medium rounded text-zinc-400">Следующую</button>
            </div>

            <div id="schedule-container" class="space-y-3"></div>
        </div>

        <!-- ДОМАШКА -->
        <div id="tab-homework" class="hidden space-y-4">
            <div class="bg-custom-card p-4 rounded-xl border border-custom-border shadow-md">
                <h2 class="text-sm font-semibold mb-3 text-zinc-200">➕ Добавить домашнее задание</h2>
                <form id="hw-form" onsubmit="submitHomework(event)" class="space-y-3">
                    <div>
                        <label class="block text-[11px] text-zinc-400 mb-1">Дата на которую задано:</label>
                        <input type="date" id="hw-date" required class="w-full bg-custom-dark border border-custom-border rounded-lg p-2.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-500">
                    </div>
                    <input type="text" id="hw-subject" placeholder="Предмет (например, Физика)" maxlength="100" required class="w-full bg-custom-dark border border-custom-border rounded-lg p-2.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-500">
                    <textarea id="hw-task" placeholder="Что задали..." rows="2" maxlength="2000" required class="w-full bg-custom-dark border border-custom-border rounded-lg p-2.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-500"></textarea>
                    <button type="submit" class="w-full btn-chocolate text-white py-2.5 rounded-lg text-sm font-medium transition shadow">Опубликовать</button>
                </form>
            </div>
            <div id="homework-container" class="space-y-3"></div>
        </div>
    </div>

    <!-- МОДАЛЬНОЕ ОКНО ПРОСМОТРА ДЗ ДНЯ -->
    <div id="day-hw-modal" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-custom-card border border-custom-border w-full max-w-sm rounded-2xl p-4 space-y-4 shadow-2xl">
            <div class="flex justify-between items-center">
                <h3 id="day-hw-modal-title" class="text-sm font-semibold text-zinc-200">Домашнее задание</h3>
                <button onclick="closeDayHwModal()" class="text-zinc-400 hover:text-white text-sm font-bold">✕</button>
            </div>
            <div id="day-hw-container" class="space-y-2 max-h-60 overflow-y-auto pr-1"></div>
            <button onclick="closeDayHwModal()" class="w-full btn-chocolate text-white text-xs py-2.5 rounded-xl font-medium transition shadow">Закрыть</button>
        </div>
    </div>

    <!-- МОДАЛЬНОЕ ОКНО РЕДАКТИРОВАНИЯ ДНЯ -->
    <div id="edit-modal" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-custom-card border border-custom-border w-full max-w-sm rounded-2xl p-4 space-y-4 shadow-2xl">
            <div class="flex justify-between items-center">
                <h3 id="modal-title" class="text-sm font-semibold text-zinc-200">Редактирование дня</h3>
                <button onclick="closeEditModal()" class="text-zinc-400 hover:text-white text-sm font-bold">✕</button>
            </div>
            <div id="modal-lessons-container" class="space-y-2 max-h-60 overflow-y-auto pr-1"></div>
            <button onclick="addLessonInput()" class="w-full bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-xs py-2 rounded-lg font-medium transition border border-custom-border">➕ Добавить пару / урок</button>
            <div class="flex gap-2 pt-2">
                <button onclick="closeEditModal()" class="flex-1 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs py-2.5 rounded-xl font-medium transition">Отмена</button>
                <button onclick="saveDaySchedule()" class="flex-1 btn-chocolate text-white text-xs py-2.5 rounded-xl font-medium transition shadow">Сохранить</button>
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

        const esc = s => String(s ?? '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

        window.addEventListener('DOMContentLoaded', () => {
            loadData();
        });

        function loadData() {
            fetch('/api/data', {
                headers: { 'X-Telegram-Init-Data': tg.initData || '' }
            })
            .then(res => res.json())
            .then(data => {
                appData = data;
                if (data.is_admin) {
                    document.getElementById('admin-badge').classList.remove('hidden');
                }
                const dateInput = document.getElementById('hw-date');
                if (dateInput && data.today_date) {
                    dateInput.value = data.today_date;
                }
                renderSchedule();
                renderHomework();
            });
        }

        function switchTab(tab) {
            ['schedule', 'homework'].forEach(t => {
                document.getElementById(`tab-${t}`).classList.add('hidden');
                document.getElementById(`btn-${t}`).className = "flex-1 py-2 text-sm font-medium rounded-lg text-zinc-400 transition";
            });
            document.getElementById(`tab-${tab}`).classList.remove('hidden');
            document.getElementById(`btn-${tab}`).className = "flex-1 py-2 text-sm font-medium rounded-lg btn-chocolate text-white transition";
        }

        function setWeek(type) {
            activeWeekType = type;
            document.getElementById('week-btn-current').className = type === 'current' ? "flex-1 py-1.5 text-xs font-medium rounded btn-chocolate text-white shadow" : "flex-1 py-1.5 text-xs font-medium rounded text-zinc-400";
            document.getElementById('week-btn-next').className = type === 'next' ? "flex-1 py-1.5 text-xs font-medium rounded btn-chocolate text-white shadow" : "flex-1 py-1.5 text-xs font-medium rounded text-zinc-400";
            renderSchedule();
        }

        function renderSchedule() {
            if (!appData) return;
            const container = document.getElementById('schedule-container');
            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;

            container.innerHTML = schedule.map(day => `
                <div class="bg-custom-card p-3.5 rounded-xl border border-custom-border relative pb-11 shadow-md">
                    <div class="flex justify-between items-center mb-2">
                        <span class="font-semibold text-zinc-200 text-sm">${esc(day.day)}</span>
                        <span class="text-xs text-zinc-400">${esc(day.date)}</span>
                    </div>
                    <ul class="space-y-1 text-xs text-zinc-300 mb-2">
                        ${day.lessons.length > 0 ? day.lessons.map(l => `<li class="py-0.5">• ${esc(l)}</li>`).join('') : '<li class="text-zinc-500 italic">Пар нет 🎉</li>'}
                    </ul>
                    <div class="absolute bottom-2.5 left-3 right-3 flex justify-between items-center pt-2 border-t border-custom-border/50">
                        <button onclick="openDayHwModal('${esc(day.full_date)}', '${esc(day.day)}', '${esc(day.date)}')" class="btn-chocolate text-white px-2.5 py-1 rounded-lg text-[11px] font-medium transition flex items-center gap-1 shadow">
                            📖 Просмотреть ДЗ
                        </button>
                        ${appData.is_admin ? `
                            <button onclick="openEditModal('${esc(day.day)}')" class="btn-chocolate text-white px-2.5 py-1 rounded-lg text-[11px] font-medium transition flex items-center gap-1 shadow">
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
                container.innerHTML = '<p class="text-xs text-zinc-500 italic text-center py-4">На этот день домашних заданий нет ✨</p>';
            } else {
                container.innerHTML = filteredHw.map(hw => `
                    <div class="bg-custom-dark p-3 rounded-xl border border-custom-border space-y-1">
                        <div class="flex justify-between items-center">
                            <span class="font-semibold text-zinc-200 text-xs">${esc(hw.subject)}</span>
                            <span class="text-[10px] text-zinc-500">${esc(hw.author)}</span>
                        </div>
                        <p class="text-xs text-zinc-300 whitespace-pre-wrap">${esc(hw.task)}</p>
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

        const PAIR_TIMES = [
            "08.30-10.10",
            "10.20-12.00",
            "12.40-14.20"
        ];

        function renderModalLessons() {
            const container = document.getElementById('modal-lessons-container');
            if (currentEditingLessons.length === 0) {
                container.innerHTML = '<p class="text-xs text-zinc-500 italic text-center py-4">Пар нет</p>';
                return;
            }
            container.innerHTML = currentEditingLessons.map((lesson, index) => {
                let cleanSubj = lesson.replace(/^.*?(?:пара|урок).*?:\s*/i, '');
                return `
                    <div class="flex items-center gap-2">
                        <input type="text" data-index="${index}" value="${esc(cleanSubj)}" onchange="updateLessonSubject(${index}, this.value)" class="flex-1 bg-custom-dark border border-custom-border rounded-lg p-2 text-xs text-zinc-200 focus:outline-none focus:border-zinc-500">
                        <button onclick="removeLesson(${index})" class="bg-red-950/40 hover:bg-red-900/60 text-red-300 border border-red-900/50 p-2 rounded-lg text-xs transition" title="Удалить пару">🗑</button>
                    </div>
                `;
            }).join('');
        }

        function updateLessonSubject(index, val) {
            currentEditingLessons[index] = val;
        }

        function removeLesson(index) {
            const container = document.getElementById('modal-lessons-container');
            const inputs = container.querySelectorAll('input');
            inputs.forEach((inp, idx) => {
                currentEditingLessons[idx] = inp.value;
            });

            currentEditingLessons.splice(index, 1);
            renderModalLessons();
        }

        function addLessonInput() {
            const container = document.getElementById('modal-lessons-container');
            const inputs = container.querySelectorAll('input');
            inputs.forEach((inp, idx) => {
                currentEditingLessons[idx] = inp.value;
            });

            currentEditingLessons.push('Новый предмет');
            renderModalLessons();
        }

        function saveDaySchedule() {
            if (!editingDay) return;

            const container = document.getElementById('modal-lessons-container');
            const inputs = container.querySelectorAll('input');

            const formattedLessons = [];
            inputs.forEach((inp, idx) => {
                let subjName = inp.value.trim();
                if (!subjName) return;

                let positionNum = idx + 1;
                if (positionNum <= 3) {
                    let timeStr = PAIR_TIMES[idx];
                    formattedLessons.push(`${positionNum} пара (${timeStr}): ${subjName}`);
                } else if (positionNum === 4) {
                    formattedLessons.push(`7 урок (14.30-15.15): ${subjName}`);
                } else if (positionNum === 5) {
                    formattedLessons.push(`8,9 урок (15.25-17.05): ${subjName}`);
                } else {
                    formattedLessons.push(`${positionNum} урок: ${subjName}`);
                }
            });

            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;
            const weekTypeNum = schedule[0].week_type;

            fetch('/api/schedule', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Telegram-Init-Data': tg.initData || ''
                },
                body: JSON.stringify({
                    day: editingDay,
                    week_type: weekTypeNum,
                    lessons: formattedLessons
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
                container.innerHTML = '<div class="text-center py-6 text-zinc-500 text-sm">Домашки пока нет ✨</div>';
                return;
            }
            container.innerHTML = appData.homework.map(hw => `
                <div class="bg-custom-card p-3.5 rounded-xl border border-custom-border space-y-1 shadow-md">
                    <div class="flex justify-between items-center">
                        <span class="font-semibold text-zinc-200 text-sm">${esc(hw.subject)}</span>
                        <span class="text-[11px] btn-chocolate text-white px-2 py-0.5 rounded">${esc(hw.date_key)}</span>
                    </div>
                    <p class="text-xs text-zinc-300 whitespace-pre-wrap">${esc(hw.task)}</p>
                    <div class="text-[10px] text-zinc-500 text-right">Автор: ${esc(hw.author)}</div>
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
                headers: {
                    'Content-Type': 'application/json',
                    'X-Telegram-Init-Data': tg.initData || ''
                },
                body: JSON.stringify({ date_key: dateKey, subject, task })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    document.getElementById('hw-task').value = '';
                    document.getElementById('hw-subject').value = '';
                    loadData();
                } else {
                    alert('Ошибка: ' + (data.message || 'Не удалось сохранить'));
                }
            });
        }
    </script>
</body>
</html>
"""

# --- FLASK ---
app = Flask(__name__)


@app.route("/")
def home():
    return "Игрик WebApp бот активен и работает 24/7!", 200


@app.route("/webapp")
def webapp_page():
    return HTML_TEMPLATE, 200, {"Content-Type": "text/html; charset=utf-8"}


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


# --- ДАННЫЕ ---
def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS homework (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_key TEXT, subject TEXT, task TEXT, author TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedule_overrides (
                day TEXT NOT NULL,
                week_type INTEGER NOT NULL,
                lessons TEXT NOT NULL,
                PRIMARY KEY (day, week_type)
            )
        """)
        with db_lock:
            for day, wt, lessons in conn.execute(
                "SELECT day, week_type, lessons FROM schedule_overrides"
            ):
                if day in SCHEDULE:
                    try:
                        SCHEDULE[day][wt] = json.loads(lessons)
                    except (ValueError, TypeError) as e:
                        log.error("Не удалось загрузить правку расписания %s/%s: %s", day, wt, e)


init_db()


def get_now():
    return datetime.now(BY_TZ)


def get_week_type(target_date):
    week_number = target_date.isocalendar()[1]
    return 1 if week_number % 2 != 0 else 0


def get_week_schedule_data(base_date, shift_weeks=0):
    start_of_week = base_date - timedelta(days=base_date.weekday()) + timedelta(weeks=shift_weeks)
    week_data = []

    for i in range(6):
        day_date = start_of_week + timedelta(days=i)
        day_name = DAYS_ORDER[i]
        w_type = get_week_type(day_date)
        with db_lock:
            lessons = list(SCHEDULE.get(day_name, [[], []])[w_type])
        week_data.append({
            "day": day_name,
            "date": day_date.strftime("%d.%m"),
            "full_date": day_date.isoformat(),
            "lessons": lessons,
            "week_type": w_type,
        })
    return week_data


def get_day_schedule_for_date(target_date):
    day_name = DAYS_ORDER[target_date.weekday()]
    with db_lock:
        return list(SCHEDULE.get(day_name, [[], []])[get_week_type(target_date)])


# --- API ---
@app.route("/api/data", methods=["GET"])
def get_api_data():
    user_id, _, _ = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    is_admin = user_id in ADMINS if user_id else False

    today = get_now().date()
    current_week_schedule = get_week_schedule_data(today, shift_weeks=0)
    next_week_schedule = get_week_schedule_data(today, shift_weeks=1)

    with db_lock, get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM homework ORDER BY date_key DESC, id DESC")
        homework = [dict(row) for row in cursor.fetchall()]

    return jsonify({
        "is_admin": is_admin,
        "current_week": current_week_schedule,
        "next_week": next_week_schedule,
        "homework": homework,
        "today_date": today.isoformat(),
    })


@app.route("/api/schedule", methods=["POST"])
def update_schedule():
    user_id, _, _ = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user_id not in ADMINS:
        return jsonify({"status": "error", "message": "Нет прав админа"}), 403

    data = request.get_json(silent=True) or {}
    day = data.get("day")
    week_type = data.get("week_type")
    lessons = data.get("lessons")

    if not isinstance(day, str) or day not in SCHEDULE:
        return jsonify({"status": "error", "message": "Неверный день"}), 400
    if not isinstance(week_type, int) or isinstance(week_type, bool) or week_type not in (0, 1):
        return jsonify({"status": "error", "message": "Неверный тип недели"}), 400
    if (
        not isinstance(lessons, list)
        or len(lessons) > 50
        or not all(isinstance(x, str) and len(x) <= 200 for x in lessons)
    ):
        return jsonify({"status": "error", "message": "Неверные данные"}), 400

    with db_lock:
        SCHEDULE[day][week_type] = lessons
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schedule_overrides (day, week_type, lessons) VALUES (?, ?, ?)",
                (day, week_type, json.dumps(lessons, ensure_ascii=False)),
            )
    return jsonify({"status": "success"})


@app.route("/api/homework", methods=["POST"])
def add_homework():
    user_id, first_name, last_name = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user_id is None:
        return jsonify({"status": "error", "message": "Нет доступа"}), 403

    author = first_name + (f" {last_name}" if last_name else "")
    data = request.get_json(silent=True) or {}

    date_key_str = data.get("date_key")
    if not date_key_str:
        return jsonify({"status": "error", "message": "Отсутствует дата"}), 400
    try:
        date_key = date.fromisoformat(str(date_key_str)).isoformat()
    except ValueError:
        return jsonify({"status": "error", "message": "Неверный формат даты"}), 400

    subject = (data.get("subject") or "").strip()
    task = (data.get("task") or "").strip()

    if not subject or not task:
        return jsonify({"status": "error", "message": "Пустые поля"}), 400
    if len(subject) > 100 or len(task) > 2000:
        return jsonify({"status": "error", "message": "Превышена максимальная длина полей"}), 400

    with db_lock, get_db() as conn:
        conn.execute(
            "INSERT INTO homework (date_key, subject, task, author) VALUES (?, ?, ?, ?)",
            (date_key, subject, task, author),
        )
    return jsonify({"status": "success"})


# --- КЛАВИАТУРЫ ---
APP_BUTTON_TEXT = "🚀 Открыть приложение ЭЗС 12-26"


def get_private_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    markup.add(types.KeyboardButton("📅 На сегодня"), types.KeyboardButton("📅 На завтра"))
    markup.add(
        types.KeyboardButton(
            APP_BUTTON_TEXT,
            web_app=types.WebAppInfo(url=WEBAPP_URL),
        )
    )
    return markup


def get_group_keyboard():
    # В группах кнопка с web_app не открывает приложение, поэтому
    # эта кнопка отправляет текст, а бот отвечает ссылкой на приложение
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    markup.add(types.KeyboardButton("📅 На сегодня"), types.KeyboardButton("📅 На завтра"))
    markup.add(types.KeyboardButton(APP_BUTTON_TEXT))
    return markup


def get_group_inline():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "🚀 Открыть приложение ЭЗС 12-26",
            url=f"https://t.me/{BOT_USERNAME}/{MINIAPP_SHORT_NAME}",
        )
    )
    return kb


def keyboard_for(message):
    return get_private_keyboard() if message.chat.type == "private" else get_group_keyboard()


BUTTON_ACTIONS = {
    "📅 на сегодня": "today",
    "📅 на завтра": "tomorrow",
    APP_BUTTON_TEXT.lower(): "app",
}


def format_day(day_date, title):
    day_name = DAYS_ORDER[day_date.weekday()]
    lessons = get_day_schedule_for_date(day_date)
    body = "\n".join(f"• {html.escape(l)}" for l in lessons) if lessons else "Пар нет 🎉"
    return f"📅 <b>{title} ({day_name}, {day_date.strftime('%d.%m')})</b>\n\n{body}"


def format_week():
    lines = ["📋 <b>Расписание на неделю</b>", ""]
    for d in get_week_schedule_data(get_now().date(), shift_weeks=0):
        lines.append(f"<b>{d['day']} ({d['date']})</b>")
        if d["lessons"]:
            lines += [f"• {html.escape(l)}" for l in d["lessons"]]
        else:
            lines.append("Пар нет 🎉")
        lines.append("")
    return "\n".join(lines)


def send_safe(chat_id, text, markup=None):
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        return True
    except Exception as e:
        log.error("Не удалось отправить сообщение в чат %s: %s", chat_id, e, exc_info=True)
        return False


# --- ОБРАБОТЧИКИ БОТА ---
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if message.chat.type == "private":
        send_safe(message.chat.id, "Привет! Используйте кнопки ниже:", get_private_keyboard())
    else:
        send_safe(message.chat.id, "Привет! Кнопки ниже:", get_group_keyboard())
        send_safe(message.chat.id, "Мини-приложение группы:", get_group_inline())


@bot.message_handler(commands=["week"])
def cmd_week(message):
    send_safe(message.chat.id, format_week(), keyboard_for(message))


@bot.message_handler(content_types=["text"])
def on_text(message):
    text = (message.text or "").strip().lower()
    action = BUTTON_ACTIONS.get(text)

    if action is None:
        if message.chat.type == "private" and text in ("меню", "приложение"):
            send_safe(message.chat.id, "Используйте кнопки меню ниже:", get_private_keyboard())
        return

    try:
        markup = keyboard_for(message)
        today = get_now().date()
        if action == "today":
            send_safe(message.chat.id, format_day(today, "Сегодня"), markup)
        elif action == "tomorrow":
            send_safe(message.chat.id, format_day(today + timedelta(days=1), "Завтра"), markup)
        elif action == "app":
            send_safe(message.chat.id, "Мини-приложение группы:", get_group_inline())
    except Exception as e:
        log.error("Ошибка обработки кнопки: %s", e, exc_info=True)


def log_updates(messages):
    for m in messages:
        log.info("update: chat_id=%s type=%s", m.chat.id, m.chat.type)


# --- ЗАПУСК ---
if __name__ == "__main__":
    def run_server():
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    Thread(target=run_server, daemon=True).start()
    log.info("Flask сервер запущен.")

    bot.set_update_listener(log_updates)
    bot.remove_webhook()
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        skip_pending=True,
        allowed_updates=telebot.util.update_types,
    )
