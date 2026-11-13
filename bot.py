import hashlib
import hmac
import html
import json
import logging
import os
import re
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
from werkzeug.exceptions import HTTPException

# --- НАСТРОЙКИ И ОКРУЖЕНИЕ ---
TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
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
WEBAPP_URL = "https://igrik-bot-new-production.up.railway.app/webapp"
AUTH_MAX_AGE = 86400          # срок действия initData, секунд
ADMIN_LINK_TTL = 86400        # срок действия личной админ-ссылки, секунд
ALERT_COOLDOWN = 600          # одинаковое уведомление не чаще раза в 10 минут, секунд
SATURDAY_SWITCH_HOUR = 14     # с субботы после этого часа текущей становится следующая неделя
SUBJECT_HORIZON_DAYS = 56     # на сколько дней вперёд ищем пары по предметам

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("IgrikWebAppBot")

bot = telebot.TeleBot(TOKEN)

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


# --- УВЕДОМЛЕНИЯ АДМИНИСТРАТОРАМ ---
_alert_last_sent = {}
_alert_lock = Lock()


def notify_admins(text, key=None):
    key = key or text[:80]
    now = time.time()
    with _alert_lock:
        if now - _alert_last_sent.get(key, 0) < ALERT_COOLDOWN:
            return
        _alert_last_sent[key] = now

    for admin_id in ADMINS:
        try:
            bot.send_message(admin_id, text[:3900])
        except Exception as e:
            log.warning("Не удалось отправить уведомление админу %s: %s", admin_id, e)


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
    if not init_data:
        return None, None, None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None, None, None

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


# --- ЛИЧНАЯ АДМИН-ССЫЛКА ---
def _admin_link_key():
    return hashlib.sha256(b"admin-link|" + TOKEN.encode()).digest()


def make_admin_token(user_id):
    exp = int(time.time()) + ADMIN_LINK_TTL
    payload = f"{user_id}.{exp}"
    sig = hmac.new(_admin_link_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_admin_token(token):
    if not token:
        return None
    try:
        uid_s, exp_s, sig = token.split(".")
        payload = f"{uid_s}.{exp_s}"
        calc = hmac.new(_admin_link_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, sig):
            return None
        if int(exp_s) < time.time():
            return None
        uid = int(uid_s)
        return uid if uid in ADMINS else None
    except Exception:
        return None


def get_request_user():
    user_id, first_name, last_name = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user_id is not None:
        return user_id, first_name, last_name
    uid = verify_admin_token(request.headers.get("X-Auth-Token", ""))
    if uid is not None:
        return uid, "Админ", ""
    return None, None, None


# --- ИНТЕРФЕЙС МИНИ-ПРИЛОЖЕНИЯ ---
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

        .card-violet { border: 1px solid #a855f7; box-shadow: 0 0 8px rgba(168, 85, 247, 0.5); }
        .card-red-neon { border: 1px solid #ff1744; box-shadow: 0 0 12px rgba(255, 23, 68, 0.65); }

        /* Неоново-фиолетовая рамка модальных окон */
        .modal-neon {
            border: 1px solid #a855f7;
            box-shadow: 0 0 14px rgba(168, 85, 247, 0.65), 0 0 26px rgba(168, 85, 247, 0.25);
        }

        .tag-outline {
            background: rgba(139, 90, 43, 0.25);
            border: 1px solid #8B5A2B;
            color: #e8c39e;
            border-radius: 0.5rem;
        }

        /* Рамки переключателей */
        .neon-frame { padding: 2px; border-radius: 0.75rem; }
        .neon-inner { border-radius: 0.6rem; }
        .neon-shift {
            background: linear-gradient(90deg, #a855f7, #ff1744, #a855f7, #ff1744, #a855f7);
            background-size: 200% 100%;
            animation: neonShift 4s linear infinite;
            box-shadow: 0 0 10px rgba(168, 85, 247, 0.6), 0 0 16px rgba(255, 23, 68, 0.35);
        }
        @keyframes neonShift {
            from { background-position: 0% 50%; }
            to { background-position: 100% 50%; }
        }
        .neon-violet {
            background: #a855f7;
            box-shadow: 0 0 10px rgba(168, 85, 247, 0.75), 0 0 18px rgba(168, 85, 247, 0.4);
        }

        .subject-btn {
            background: #120c1c;
            border: 1px solid #a855f7;
            color: #f3e8ff;
            border-radius: 0.75rem;
            padding: 0.625rem 0.5rem;
            font-size: 0.8rem;
            box-shadow: 0 0 8px rgba(168, 85, 247, 0.6), inset 0 0 6px rgba(168, 85, 247, 0.15);
            transition: box-shadow 0.15s;
        }
        .subject-btn:hover { box-shadow: 0 0 14px rgba(168, 85, 247, 0.95); }

        .input-dark {
            width: 100%;
            background: #121212;
            border: 1px solid #333333;
            border-radius: 0.5rem;
            padding: 0.5rem 0.6rem;
            font-size: 0.8rem;
            color: #e4e4e7;
        }
        .input-dark:focus { outline: none; border-color: #71717a; }

        .seg-btn {
            background: #27272a;
            color: #d4d4d8;
            border: 1px solid #333333;
            border-radius: 0.5rem;
            padding: 0.45rem 0.4rem;
            font-size: 0.75rem;
            line-height: 1.2;
        }
        .seg-btn.active {
            background: #8B5A2B;
            color: #ffffff;
            border-color: #8B5A2B;
            box-shadow: 0 0 8px rgba(139, 90, 43, 0.6);
        }

        .cal-wrap {
            background: linear-gradient(160deg, #211b16, #141210);
            border: 1px solid #5a3d24;
            border-radius: 1rem;
            padding: 0.75rem;
            box-shadow: 0 0 18px rgba(139, 90, 43, 0.25);
        }
        .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
        .cal-wd { text-align: center; font-size: 0.65rem; color: #71717a; padding: 2px 0; }
        .cal-day {
            position: relative;
            aspect-ratio: 1 / 1;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 0.6rem;
            font-size: 0.8rem;
            color: #e4e4e7;
            background: #1c1816;
            border: 1px solid transparent;
        }
        .cal-day:hover:not(:disabled) { border-color: #8B5A2B; }
        .cal-past { opacity: 0.25; cursor: not-allowed; }
        .cal-today { border-color: #39ff14 !important; box-shadow: 0 0 8px rgba(57, 255, 20, 0.6); color: #39ff14; }
        .cal-selected { background: #8B5A2B; color: #ffffff; box-shadow: 0 0 12px rgba(139, 90, 43, 0.85); }
        .cal-dot {
            position: absolute;
            bottom: 3px;
            left: 50%;
            transform: translateX(-50%);
            width: 4px;
            height: 4px;
            border-radius: 9999px;
            background: #a855f7;
            box-shadow: 0 0 4px #a855f7;
        }
        .cal-nav {
            width: 2rem;
            height: 2rem;
            border-radius: 0.5rem;
            background: #2a2019;
            color: #e8c39e;
            border: 1px solid #5a3d24;
            font-size: 1.1rem;
        }
        .cal-nav:disabled { opacity: 0.25; }
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

        <!-- Вкладки: переливающаяся фиолетово-красная рамка -->
        <div class="neon-frame neon-shift mb-6">
            <div class="flex bg-custom-card p-1 neon-inner">
                <button onclick="switchTab('schedule')" id="btn-schedule" class="flex-1 py-2 text-sm font-medium rounded-lg btn-chocolate text-white transition">Расписание</button>
                <button onclick="switchTab('homework')" id="btn-homework" class="flex-1 py-2 text-sm font-medium rounded-lg text-zinc-400 transition">Домашка</button>
            </div>
        </div>

        <!-- РАСПИСАНИЕ -->
        <div id="tab-schedule" class="space-y-4">
            <!-- Переключатель недель: неоново-фиолетовая рамка -->
            <div class="neon-frame neon-violet">
                <div class="flex bg-custom-card p-1 neon-inner">
                    <button onclick="setWeek('current')" id="week-btn-current" class="flex-1 py-1.5 text-xs font-medium rounded btn-chocolate text-white shadow">Эту неделю</button>
                    <button onclick="setWeek('next')" id="week-btn-next" class="flex-1 py-1.5 text-xs font-medium rounded text-zinc-400">Следующую</button>
                </div>
            </div>

            <div id="schedule-container" class="space-y-3"></div>
        </div>

        <!-- ДОМАШКА -->
        <div id="tab-homework" class="hidden space-y-4">
            <div class="bg-custom-card p-4 rounded-xl card-red-neon">
                <h2 class="text-sm font-semibold mb-3 text-zinc-200">➕ Добавить домашнее задание</h2>
                <button type="button" onclick="openHwWizard()" class="w-full btn-chocolate text-white py-2.5 rounded-lg text-sm font-medium transition shadow">📝 Записать ДЗ</button>
            </div>
            <div id="homework-container" class="space-y-3"></div>
        </div>
    </div>

    <!-- ПОШАГОВОЕ ОКНО ЗАПИСИ ДЗ -->
    <div id="hw-modal" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-custom-card border border-custom-border w-full max-w-sm rounded-2xl p-4 space-y-4 shadow-2xl max-h-[90vh] overflow-y-auto">
            <div class="flex justify-between items-center">
                <div class="flex items-center gap-2">
                    <button type="button" id="hw-back" onclick="showHwStage1()" class="hidden text-zinc-400 hover:text-white text-sm px-1">←</button>
                    <h3 id="hw-title" class="text-sm font-semibold text-zinc-200">Выберите предмет</h3>
                </div>
                <button type="button" onclick="closeHwWizard()" class="text-zinc-400 hover:text-white text-sm font-bold">✕</button>
            </div>

            <!-- Шаг 1: выбор предмета -->
            <div id="hw-stage-subjects">
                <div id="subject-list" class="grid grid-cols-2 gap-2 max-h-72 overflow-y-auto pr-1"></div>
            </div>

            <!-- Шаг 2: дата и текст -->
            <div id="hw-stage-form" class="hidden space-y-3">
                <div class="text-xs text-zinc-400">Предмет: <span id="hw-subject-label" class="text-[#c4b5fd] font-semibold"></span></div>
                <div class="text-[11px] text-zinc-400">Дата на которую задано:</div>
                <div id="hw-calendar" class="cal-wrap"></div>
                <div id="hw-date-hint" class="text-[11px] text-zinc-400"></div>
                <textarea id="hw-task" placeholder="Что задали..." rows="3" maxlength="2000" class="w-full bg-custom-dark border border-custom-border rounded-lg p-2.5 text-sm text-zinc-200 focus:outline-none focus:border-zinc-500"></textarea>
                <button type="button" onclick="submitHomework()" class="w-full btn-chocolate text-white py-2.5 rounded-lg text-sm font-medium transition shadow">Опубликовать</button>
            </div>
        </div>
    </div>

    <!-- МОДАЛЬНОЕ ОКНО ПРОСМОТРА ДЗ ДНЯ (неоново-фиолетовая рамка) -->
    <div id="day-hw-modal" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-custom-card modal-neon w-full max-w-sm rounded-2xl p-4 space-y-4">
            <div class="flex justify-between items-center">
                <h3 id="day-hw-modal-title" class="text-sm font-semibold text-zinc-200">Домашнее задание</h3>
                <button onclick="closeDayHwModal()" class="text-zinc-400 hover:text-white text-sm font-bold">✕</button>
            </div>
            <div id="day-hw-container" class="space-y-2 max-h-60 overflow-y-auto pr-1"></div>
            <button onclick="closeDayHwModal()" class="w-full btn-chocolate text-white text-xs py-2.5 rounded-xl font-medium transition shadow">Закрыть</button>
        </div>
    </div>

    <!-- МОДАЛЬНОЕ ОКНО РЕДАКТИРОВАНИЯ ДНЯ (неоново-фиолетовая рамка) -->
    <div id="edit-modal" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
        <div class="bg-custom-card modal-neon w-full max-w-sm rounded-2xl p-4 space-y-3 max-h-[90vh] overflow-y-auto">
            <div class="flex justify-between items-center">
                <h3 id="modal-title" class="text-sm font-semibold text-zinc-200">Редактирование дня</h3>
                <button onclick="closeEditModal()" class="text-zinc-400 hover:text-white text-sm font-bold">✕</button>
            </div>

            <div id="modal-lessons-container" class="space-y-2"></div>

            <button type="button" id="add-toggle" onclick="toggleAddPanel()" class="w-full bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-xs py-2 rounded-lg font-medium transition border border-custom-border">➕ Добавить пару / урок</button>

            <div id="add-panel" class="hidden space-y-2 border border-custom-border rounded-xl p-3">
                <div class="grid grid-cols-2 gap-2">
                    <button type="button" id="kind-pair" onclick="setLessonKind('pair')" class="seg-btn">Пара</button>
                    <button type="button" id="kind-lesson" onclick="setLessonKind('lesson')" class="seg-btn">Урок</button>
                </div>
                <div id="slot-list" class="grid grid-cols-2 gap-2"></div>
                <div id="custom-lesson" class="hidden space-y-2">
                    <input id="custom-label" type="text" placeholder="Название, например: 8 урок" class="input-dark">
                    <input id="custom-time" type="text" placeholder="Время, например: 15.25-16.10" class="input-dark">
                </div>
                <input id="new-subject" list="subject-options" type="text" placeholder="Предмет" class="input-dark">
                <datalist id="subject-options"></datalist>
                <button type="button" onclick="confirmAddLesson()" class="w-full btn-chocolate text-white text-xs py-2 rounded-lg font-medium transition shadow">Добавить в расписание</button>
            </div>

            <div class="flex gap-2 pt-2">
                <button onclick="closeEditModal()" class="flex-1 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs py-2.5 rounded-xl font-medium transition">Отмена</button>
                <button onclick="saveDaySchedule()" class="flex-1 btn-chocolate text-white text-xs py-2.5 rounded-xl font-medium transition shadow">Сохранить</button>
            </div>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();

        const authToken = new URLSearchParams(window.location.search).get('t') || '';

        let appData = null;
        let activeWeekType = 'current';
        let editingDay = null;
        let currentEditingLessons = [];
        let addState = { kind: 'pair', slot: 0 };
        let hw = { subject: null, date: null, view: null };

        const PAIR_SLOTS = [
            { label: '1 пара', time: '08.30-10.10' },
            { label: '2 пара', time: '10.20-12.00' },
            { label: '3 пара', time: '12.40-14.20' },
            { label: '4 пара', time: '14.30-16.10' }
        ];
        const LESSON_SLOTS = [
            { label: '7 урок', time: '14.30-15.15' },
            { label: '8,9 урок', time: '15.25-17.05' },
            { label: 'Другой урок', time: '', custom: true }
        ];

        const MONTHS = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
        const MONTHS_GEN = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
        const WD_SHORT = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
        const WD_FULL = ['Воскресенье','Понедельник','Вторник','Среда','Четверг','Пятница','Суббота'];

        const esc = s => String(s ?? '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

        const pad = n => String(n).padStart(2, '0');
        function parseIso(iso) {
            const [y, m, d] = iso.split('-').map(Number);
            return { y, m: m - 1, d };
        }
        function isoOf(y, m, d) { return `${y}-${pad(m + 1)}-${pad(d)}`; }
        function fmtLong(iso) {
            const p = parseIso(iso);
            const wd = new Date(p.y, p.m, p.d).getDay();
            return `${p.d} ${MONTHS_GEN[p.m]}, ${WD_FULL[wd]}`;
        }
        function fmtShort(iso) {
            const p = parseIso(iso);
            return `${pad(p.d)}.${pad(p.m + 1)}`;
        }
        function subjectLessonDates(subject) {
            return (appData && appData.subject_dates && appData.subject_dates[subject]) || [];
        }

        // Разбор строки расписания: "1 пара (08.30-10.10): Химия"
        function parseLesson(s) {
            const m = s.match(/^(.*?)\s*\(([^)]*)\)\s*:\s*(.*)$/);
            if (m) return { label: m[1].trim(), time: m[2].trim(), subject: m[3].trim() };
            const m2 = s.match(/^(.*?):\s*(.*)$/);
            if (m2) return { label: m2[1].trim(), time: '', subject: m2[2].trim() };
            return { label: '', time: '', subject: s.trim() };
        }
        function formatLesson(l) {
            const label = l.label || 'Урок';
            return l.time ? `${label} (${l.time}): ${l.subject}` : `${label}: ${l.subject}`;
        }
        function startMin(time) {
            const m = (time || '').match(/(\d{1,2})\.(\d{2})/);
            return m ? Number(m[1]) * 60 + Number(m[2]) : 9999;
        }

        function authHeaders(withJson) {
            const h = {};
            if (withJson) h['Content-Type'] = 'application/json';
            if (tg.initData) h['X-Telegram-Init-Data'] = tg.initData;
            if (authToken) h['X-Auth-Token'] = authToken;
            return h;
        }

        window.addEventListener('DOMContentLoaded', () => {
            document.getElementById('subject-list').addEventListener('click', e => {
                const btn = e.target.closest('button[data-subject]');
                if (btn) chooseSubject(btn.dataset.subject);
            });
            document.getElementById('slot-list').addEventListener('click', e => {
                const btn = e.target.closest('button[data-slot]');
                if (btn) {
                    addState.slot = Number(btn.dataset.slot);
                    renderAddPanel();
                }
            });
            document.getElementById('hw-calendar').addEventListener('click', e => {
                const nav = e.target.closest('[data-nav]');
                if (nav) {
                    if (!nav.disabled) shiftMonth(Number(nav.dataset.nav));
                    return;
                }
                const day = e.target.closest('[data-iso]');
                if (day && !day.disabled) {
                    hw.date = day.dataset.iso;
                    renderCalendar();
                }
            });
            loadData();
        });

        function loadData() {
            fetch('/api/data', { headers: authHeaders(false) })
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

            container.innerHTML = schedule.map(day => {
                const isToday = day.full_date === appData.today_date;
                const cardClass = isToday
                    ? 'bg-custom-card p-3.5 rounded-xl relative pb-11'
                    : 'bg-custom-card p-3.5 rounded-xl relative pb-11 card-violet';
                const cardStyle = isToday
                    ? 'border:2px solid #39ff14;box-shadow:0 0 14px rgba(57,255,20,0.75)'
                    : '';
                const todayTag = isToday
                    ? '<span class="text-[10px] font-bold text-[#39ff14]">СЕГОДНЯ</span>'
                    : '';

                return `
                <div class="${cardClass}" style="${cardStyle}">
                    <div class="flex justify-between items-center mb-2">
                        <span class="font-semibold text-zinc-200 text-sm">${esc(day.day)}</span>
                        <span class="flex items-center gap-1.5">${todayTag}<span class="tag-outline text-[11px] px-2 py-0.5">${esc(day.date)}</span></span>
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
                `;
            }).join('');
        }

        function openDayHwModal(fullDateStr, dayName, shortDateStr) {
            const modalTitle = document.getElementById('day-hw-modal-title');
            const container = document.getElementById('day-hw-container');

            modalTitle.innerText = `ДЗ на ${dayName} (${shortDateStr})`;
            const filteredHw = appData.homework.filter(h => h.date_key === fullDateStr);

            if (filteredHw.length === 0) {
                container.innerHTML = '<p class="text-xs text-zinc-500 italic text-center py-4">На этот день домашних заданий нет ✨</p>';
            } else {
                container.innerHTML = filteredHw.map(h => `
                    <div class="bg-custom-dark p-3 rounded-xl border border-custom-border space-y-1">
                        <div class="flex justify-between items-center">
                            <span class="font-semibold text-zinc-200 text-xs">${esc(h.subject)}</span>
                            <span class="text-[10px] text-zinc-500">${esc(h.author)}</span>
                        </div>
                        <p class="text-xs text-zinc-300 whitespace-pre-wrap">${esc(h.task)}</p>
                    </div>
                `).join('');
            }

            document.getElementById('day-hw-modal').classList.remove('hidden');
        }

        function closeDayHwModal() {
            document.getElementById('day-hw-modal').classList.add('hidden');
        }

        // --- РЕДАКТОР РАСПИСАНИЯ ---
        function openEditModal(dayName) {
            editingDay = dayName;
            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;
            const dayObj = schedule.find(d => d.day === dayName);
            currentEditingLessons = dayObj ? dayObj.lessons.map(parseLesson) : [];

            document.getElementById('modal-title').innerText = `${dayName} (${activeWeekType === 'current' ? 'Текущая' : 'Следующая'})`;
            document.getElementById('subject-options').innerHTML =
                (appData.subjects || []).map(s => `<option value="${esc(s)}"></option>`).join('');

            closeAddPanel();
            renderModalLessons();
            document.getElementById('edit-modal').classList.remove('hidden');
        }

        function closeEditModal() {
            document.getElementById('edit-modal').classList.add('hidden');
            editingDay = null;
        }

        // Переносит текст из полей в массив (чтобы не потерять правки)
        function syncFromInputs() {
            document.querySelectorAll('#modal-lessons-container input[data-index]').forEach(inp => {
                const i = Number(inp.dataset.index);
                if (currentEditingLessons[i]) currentEditingLessons[i].subject = inp.value;
            });
        }

        function renderModalLessons() {
            const container = document.getElementById('modal-lessons-container');
            if (currentEditingLessons.length === 0) {
                container.innerHTML = '<p class="text-xs text-zinc-500 italic text-center py-4">Пар нет</p>';
                return;
            }
            container.innerHTML = currentEditingLessons.map((l, i) => `
                <div class="flex items-center gap-2">
                    <div class="flex-1">
                        <div class="text-[10px] text-zinc-500">${esc(l.label)}${l.time ? ' (' + esc(l.time) + ')' : ''}</div>
                        <input type="text" data-index="${i}" value="${esc(l.subject)}" class="input-dark">
                    </div>
                    <button type="button" onclick="removeLesson(${i})" class="bg-red-950/40 hover:bg-red-900/60 text-red-300 border border-red-900/50 p-2 rounded-lg text-xs transition" title="Удалить">🗑</button>
                </div>
            `).join('');
        }

        function removeLesson(index) {
            syncFromInputs();
            currentEditingLessons.splice(index, 1);
            renderModalLessons();
        }

        function toggleAddPanel() {
            const panel = document.getElementById('add-panel');
            if (panel.classList.contains('hidden')) {
                addState = { kind: 'pair', slot: 0 };
                panel.classList.remove('hidden');
                renderAddPanel();
            } else {
                closeAddPanel();
            }
        }

        function closeAddPanel() {
            document.getElementById('add-panel').classList.add('hidden');
        }

        function setLessonKind(kind) {
            addState = { kind, slot: 0 };
            renderAddPanel();
        }

        function renderAddPanel() {
            document.getElementById('kind-pair').classList.toggle('active', addState.kind === 'pair');
            document.getElementById('kind-lesson').classList.toggle('active', addState.kind === 'lesson');

            const slots = addState.kind === 'pair' ? PAIR_SLOTS : LESSON_SLOTS;
            document.getElementById('slot-list').innerHTML = slots.map((s, i) => `
                <button type="button" data-slot="${i}" class="seg-btn ${i === addState.slot ? 'active' : ''}">
                    ${esc(s.label)}${s.time ? '<br><span class="text-[10px] opacity-80">' + esc(s.time) + '</span>' : ''}
                </button>
            `).join('');

            const current = slots[addState.slot];
            document.getElementById('custom-lesson').classList.toggle('hidden', !(current && current.custom));
        }

        function confirmAddLesson() {
            syncFromInputs();
            const slots = addState.kind === 'pair' ? PAIR_SLOTS : LESSON_SLOTS;
            const slot = slots[addState.slot];

            let label = slot.label;
            let time = slot.time || '';
            if (slot.custom) {
                label = document.getElementById('custom-label').value.trim();
                time = document.getElementById('custom-time').value.trim();
                if (!label) {
                    alert('Введите название урока, например: 8 урок');
                    return;
                }
            }

            const subject = document.getElementById('new-subject').value.trim();
            if (!subject) {
                alert('Введите название предмета');
                return;
            }

            currentEditingLessons.push({ label, time, subject });
            document.getElementById('new-subject').value = '';
            document.getElementById('custom-label').value = '';
            document.getElementById('custom-time').value = '';
            closeAddPanel();
            renderModalLessons();
        }

        function saveDaySchedule() {
            if (!editingDay) return;
            syncFromInputs();

            const items = currentEditingLessons
                .filter(l => l.subject && l.subject.trim())
                .map(l => ({ ...l, subject: l.subject.trim() }));
            items.sort((a, b) => startMin(a.time) - startMin(b.time));
            const formattedLessons = items.map(formatLesson);

            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;
            const weekTypeNum = schedule[0].week_type;

            fetch('/api/schedule', {
                method: 'POST',
                headers: authHeaders(true),
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

        // --- ДОМАШКА: СПИСОК ---
        function renderHomework() {
            if (!appData) return;
            const container = document.getElementById('homework-container');
            const today = appData.today_date;

            const upcoming = appData.homework
                .filter(h => h.date_key >= today)
                .sort((a, b) => a.date_key.localeCompare(b.date_key) || a.id - b.id);
            const past = appData.homework
                .filter(h => h.date_key < today)
                .sort((a, b) => b.date_key.localeCompare(a.date_key) || b.id - a.id);
            const list = [...upcoming, ...past];

            if (list.length === 0) {
                container.innerHTML = '<div class="text-center py-6 text-zinc-500 text-sm">Домашки пока нет ✨</div>';
                return;
            }
            container.innerHTML = list.map(h => `
                <div class="bg-custom-card p-3.5 rounded-xl border border-custom-border space-y-1 shadow-md">
                    <div class="flex justify-between items-center">
                        <span class="font-semibold text-zinc-200 text-sm">${esc(h.subject)}</span>
                        <span class="tag-outline text-[11px] px-2 py-0.5">${esc(h.date_key)}</span>
                    </div>
                    <p class="text-xs text-zinc-300 whitespace-pre-wrap">${esc(h.task)}</p>
                    <div class="text-[10px] text-zinc-500 text-right">Автор: ${esc(h.author)}</div>
                </div>
            `).join('');
        }

        // --- ДОМАШКА: ПОШАГОВАЯ ЗАПИСЬ ---
        function openHwWizard() {
            if (!appData) return;
            hw = { subject: null, date: null, view: null };
            showHwStage1();
            document.getElementById('hw-modal').classList.remove('hidden');
        }

        function closeHwWizard() {
            document.getElementById('hw-modal').classList.add('hidden');
        }

        function showHwStage1() {
            document.getElementById('hw-title').textContent = 'Выберите предмет';
            document.getElementById('hw-back').classList.add('hidden');
            document.getElementById('hw-stage-subjects').classList.remove('hidden');
            document.getElementById('hw-stage-form').classList.add('hidden');

            const subjects = appData.subjects || [];
            document.getElementById('subject-list').innerHTML = subjects.length
                ? subjects.map(s => `<button type="button" data-subject="${esc(s)}" class="subject-btn">${esc(s)}</button>`).join('')
                : '<p class="col-span-2 text-xs text-zinc-500 text-center py-4">Предметов пока нет</p>';
        }

        function chooseSubject(name) {
            const dates = subjectLessonDates(name);
            hw.subject = name;
            hw.date = dates[0] || appData.today_date;
            const p = parseIso(hw.date);
            hw.view = { y: p.y, m: p.m };

            document.getElementById('hw-title').textContent = 'Новое ДЗ';
            document.getElementById('hw-back').classList.remove('hidden');
            document.getElementById('hw-stage-subjects').classList.add('hidden');
            document.getElementById('hw-stage-form').classList.remove('hidden');
            document.getElementById('hw-subject-label').textContent = name;
            renderCalendar();
        }

        function shiftMonth(delta) {
            let { y, m } = hw.view;
            m += delta;
            if (m < 0) { m = 11; y--; }
            if (m > 11) { m = 0; y++; }
            hw.view = { y, m };
            renderCalendar();
        }

        function renderCalendar() {
            const todayParts = parseIso(appData.today_date);
            const { y, m } = hw.view;
            const lessonDates = subjectLessonDates(hw.subject);
            const lessonSet = new Set(lessonDates);
            const prevAllowed = (y * 12 + m) > (todayParts.y * 12 + todayParts.m);
            const firstDow = (new Date(y, m, 1).getDay() + 6) % 7;
            const daysInMonth = new Date(y, m + 1, 0).getDate();

            let cells = '';
            for (let i = 0; i < firstDow; i++) cells += '<span></span>';
            for (let d = 1; d <= daysInMonth; d++) {
                const iso = isoOf(y, m, d);
                const past = iso < appData.today_date;
                const cls = ['cal-day'];
                if (past) cls.push('cal-past');
                if (iso === appData.today_date) cls.push('cal-today');
                if (iso === hw.date) cls.push('cal-selected');
                const dot = lessonSet.has(iso) ? '<i class="cal-dot"></i>' : '';
                cells += `<button type="button" data-iso="${iso}" class="${cls.join(' ')}" ${past ? 'disabled' : ''}>${d}${dot}</button>`;
            }

            document.getElementById('hw-calendar').innerHTML = `
                <div class="flex items-center justify-between mb-2">
                    <button type="button" data-nav="-1" class="cal-nav" ${prevAllowed ? '' : 'disabled'}>‹</button>
                    <div class="text-sm font-semibold text-zinc-100">${MONTHS[m]} ${y}</div>
                    <button type="button" data-nav="1" class="cal-nav">›</button>
                </div>
                <div class="cal-grid mb-1">${WD_SHORT.map(w => `<span class="cal-wd">${w}</span>`).join('')}</div>
                <div class="cal-grid">${cells}</div>
            `;

            const nearest = lessonDates[0];
            let hint = `Выбрано: ${fmtLong(hw.date)}`;
            if (nearest && hw.date === nearest) {
                hint += ' · ближайшая пара по этому предмету';
            } else if (nearest) {
                hint += ` · ближайшая пара: ${fmtShort(nearest)}`;
            } else {
                hint += ' · пар по этому предмету в расписании не найдено';
            }
            document.getElementById('hw-date-hint').textContent = hint;
        }

        function submitHomework() {
            const task = document.getElementById('hw-task').value.trim();

            if (!hw.subject) {
                alert('Выберите предмет');
                return;
            }
            if (!hw.date || hw.date < appData.today_date) {
                alert('Нельзя записать ДЗ на прошедший день');
                return;
            }
            if (!task) {
                alert('Напишите, что задали');
                return;
            }

            fetch('/api/homework', {
                method: 'POST',
                headers: authHeaders(true),
                body: JSON.stringify({ date_key: hw.date, subject: hw.subject, task })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    document.getElementById('hw-task').value = '';
                    closeHwWizard();
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


@app.errorhandler(Exception)
def handle_web_exception(e):
    if isinstance(e, HTTPException):
        return e
    log.exception("Ошибка веб-сервера на %s", request.path)
    notify_admins(
        f"🔥 Ошибка в веб-части бота\nМаршрут: {request.path}\n{type(e).__name__}: {e}",
        key=f"web:{type(e).__name__}:{request.path}",
    )
    return jsonify({"status": "error", "message": "Внутренняя ошибка сервера"}), 500


@app.route("/")
def home():
    return "Игрик WebApp бот активен и работает 24/7!", 200


@app.route("/webapp")
def webapp_page():
    return HTML_TEMPLATE, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }


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


def schedule_anchor(now):
    """Дата, от которой считается «текущая» неделя.
    С субботы после SATURDAY_SWITCH_HOUR и в воскресенье текущей становится следующая неделя."""
    if now.weekday() == 6 or (now.weekday() == 5 and now.hour >= SATURDAY_SWITCH_HOUR):
        return now.date() + timedelta(days=7)
    return now.date()


SUBJECT_RE = re.compile(r":\s*(.+)$")
PLACEHOLDER_SUBJECT = "Новый предмет"


def parse_subject(lesson):
    """Достаёт название предмета из строки вида '1 пара (08.30-10.10): Химия'."""
    m = SUBJECT_RE.search(lesson)
    if not m:
        return None
    name = m.group(1).strip()
    if not name or name == PLACEHOLDER_SUBJECT:
        return None
    return name


def get_subjects():
    """Список всех предметов из расписания (включая правки админов)."""
    names = set()
    with db_lock:
        for weeks in SCHEDULE.values():
            for lessons in weeks:
                for lesson in lessons:
                    name = parse_subject(lesson)
                    if name:
                        names.add(name)
    return sorted(names)


def build_subject_dates(start, days=SUBJECT_HORIZON_DAYS):
    """Для каждого предмета — список дат (ISO), на которые у него есть пара, начиная с start."""
    found = {}
    for i in range(days):
        d = start + timedelta(days=i)
        for lesson in get_day_schedule_for_date(d):
            name = parse_subject(lesson)
            if name:
                found.setdefault(name, set()).add(d.isoformat())
    return {name: sorted(dates) for name, dates in found.items()}


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
    user_id, _, _ = get_request_user()
    is_admin = user_id in ADMINS if user_id else False

    now = get_now()
    today = now.date()
    anchor = schedule_anchor(now)
    current_week_schedule = get_week_schedule_data(anchor, shift_weeks=0)
    next_week_schedule = get_week_schedule_data(anchor, shift_weeks=1)
    subjects = get_subjects()
    subject_dates = build_subject_dates(today)

    with db_lock, get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM homework ORDER BY date_key DESC, id DESC")
        homework = [dict(row) for row in cursor.fetchall()]

    return jsonify({
        "is_admin": is_admin,
        "current_week": current_week_schedule,
        "next_week": next_week_schedule,
        "homework": homework,
        "subjects": subjects,
        "subject_dates": subject_dates,
        "today_date": today.isoformat(),
    })


@app.route("/api/schedule", methods=["POST"])
def update_schedule():
    user_id, _, _ = get_request_user()
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
    user_id, first_name, last_name = get_request_user()
    if user_id is None:
        return jsonify({"status": "error", "message": "Нет доступа"}), 403

    author = first_name + (f" {last_name}" if last_name else "")
    data = request.get_json(silent=True) or {}

    date_key_str = data.get("date_key")
    if not date_key_str:
        return jsonify({"status": "error", "message": "Отсутствует дата"}), 400
    try:
        parsed_date = date.fromisoformat(str(date_key_str))
    except ValueError:
        return jsonify({"status": "error", "message": "Неверный формат даты"}), 400

    if parsed_date < get_now().date():
        return jsonify({"status": "error", "message": "Нельзя записать ДЗ на прошедший день"}), 400

    subject = (data.get("subject") or "").strip()
    task = (data.get("task") or "").strip()

    if not subject or not task:
        return jsonify({"status": "error", "message": "Пустые поля"}), 400
    if subject not in get_subjects():
        return jsonify({"status": "error", "message": "Выберите предмет из списка"}), 400
    if len(subject) > 100 or len(task) > 2000:
        return jsonify({"status": "error", "message": "Превышена максимальная длина полей"}), 400

    with db_lock, get_db() as conn:
        conn.execute(
            "INSERT INTO homework (date_key, subject, task, author) VALUES (?, ?, ?, ?)",
            (parsed_date.isoformat(), subject, task, author),
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
    now = get_now()
    for d in get_week_schedule_data(schedule_anchor(now), shift_weeks=0):
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


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.chat.type != "private":
        send_safe(message.chat.id, "Команда /admin работает только в личке с ботом.")
        return
    if message.from_user.id not in ADMINS:
        send_safe(message.chat.id, "У вас нет прав администратора.")
        return
    token = make_admin_token(message.from_user.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "🛠 Открыть как админ",
            web_app=types.WebAppInfo(url=f"{WEBAPP_URL}?t={token}"),
        )
    )
    send_safe(message.chat.id, f"Личная ссылка администратора, действует {ADMIN_LINK_TTL // 3600} ч. Не пересылайте её:", kb)


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    uid = message.from_user.id
    status = "да" if uid in ADMINS else "нет"
    send_safe(message.chat.id, f"Ваш ID: <code>{uid}</code>\nАдмин: {status}")


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


# --- ЗАПУСК (Flask на порту PORT от Railway в главном потоке) ---
if __name__ == "__main__":
    def run_bot():
        bot.set_update_listener(log_updates)
        bot.remove_webhook()
        notify_admins("✅ Бот запущен и работает.", key="startup")
        while True:
            try:
                bot.infinity_polling(
                    timeout=60,
                    long_polling_timeout=60,
                    skip_pending=True,
                    allowed_updates=telebot.util.update_types,
                )
            except Exception as e:
                log.exception("Опрос Telegram прервался")
                notify_admins(
                    f"🔥 Бот споткнулся, перезапускаю опрос: {type(e).__name__}: {e}",
                    key=f"poll:{type(e).__name__}",
                )
                time.sleep(10)

    Thread(target=run_bot, daemon=True).start()
    log.info("Telegram бот запущен в фоне.")

    port = int(os.environ.get("PORT", 5000))
    log.info("Запуск Flask сервера на порту %s...", port)
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.exception("Веб-сервер остановился")
        notify_admins(f"🔥 Веб-сервер остановился: {type(e).__name__}: {e}", key="flask-stopped")
