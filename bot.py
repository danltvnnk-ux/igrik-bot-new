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
AUTH_MAX_AGE = 86400            # срок действия initData, секунд
ADMIN_LINK_TTL = 86400          # срок действия личной админ-ссылки, секунд
ALERT_COOLDOWN = 600            # одинаковое уведомление не чаще раза в 10 минут, секунд
SATURDAY_SWITCH_HOUR = 14       # с субботы после этого часа текущей становится следующая неделя
SUBJECT_HORIZON_DAYS = 56       # на сколько дней вперёд ищем пары по предметам
HOMEWORK_CLEAN_INTERVAL = 1800  # как часто чистить прошедшие ДЗ, секунд

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>ЭЗС 12-26</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --bg: #0A0807;
            --text: #FFF6EC;
            --text-2: #B7A593;
            --text-3: #7D6D5E;
            --latte: #E9C9A4;
            --caramel-1: #E0A06A;
            --caramel-2: #A8683A;
            --ease: cubic-bezier(0.16, 1, 0.3, 1);
        }

        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }

        html, body {
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Roboto, system-ui, sans-serif;
            -webkit-font-smoothing: antialiased;
            overflow-x: hidden;
        }

        /* --- Амбиент-фон --- */
        .mesh {
            position: fixed;
            inset: -20%;
            z-index: 0;
            pointer-events: none;
            will-change: transform;
            transition: transform 0.6s var(--ease);
        }
        .blob {
            position: absolute;
            border-radius: 50%;
            filter: blur(90px);
            opacity: 0.3;
            animation: drift 22s ease-in-out infinite alternate;
        }
        .b1 { width: 55vw; height: 55vw; background: #C77B43; top: -6%; left: -10%; }
        .b2 { width: 50vw; height: 50vw; background: #6B3F22; top: 34%; right: -16%; animation-duration: 28s; }
        .b3 { width: 40vw; height: 40vw; background: #E9A86A; bottom: -4%; left: 18%; opacity: 0.16; animation-duration: 25s; }
        @keyframes drift {
            from { transform: translate3d(0, 0, 0) scale(1); }
            to { transform: translate3d(6vw, -4vh, 0) scale(1.12); }
        }

        /* --- Контейнер приложения --- */
        #app {
            position: relative;
            z-index: 1;
            max-width: 460px;
            margin: 0 auto;
            padding: 14px 16px 120px;
            transform-origin: 50% 0;
            transition: transform 0.55s var(--ease), filter 0.45s var(--ease);
        }
        body.sheet-open #app {
            transform: scale(0.97);
            filter: brightness(0.6);
        }

        /* --- Стекло --- */
        .glass {
            position: relative;
            background: linear-gradient(160deg, rgba(44, 33, 26, 0.62), rgba(20, 15, 12, 0.6));
            -webkit-backdrop-filter: blur(16px) saturate(150%);
            backdrop-filter: blur(16px) saturate(150%);
            border-radius: 22px;
            box-shadow:
                inset 0 1px 0 rgba(255, 255, 255, 0.06),
                0 1px 2px rgba(0, 0, 0, 0.45),
                0 18px 40px -22px rgba(0, 0, 0, 0.95);
        }
        .glass::before {
            content: '';
            position: absolute;
            inset: 0;
            border-radius: inherit;
            padding: 1px;
            background: linear-gradient(135deg, rgba(233, 201, 164, 0.38), rgba(233, 201, 164, 0.04) 42%, rgba(224, 160, 106, 0.28));
            -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
            -webkit-mask-composite: xor;
            mask-composite: exclude;
            pointer-events: none;
        }

        /* --- Шапка --- */
        .hdr {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin: 6px 4px 4px;
        }
        .brand {
            font-size: 22px;
            font-weight: 800;
            letter-spacing: -0.02em;
        }
        .greet {
            font-size: 14px;
            color: var(--text-2);
            margin: 2px 4px 14px;
        }
        .badge {
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.06em;
            padding: 6px 11px;
            border-radius: 999px;
            background: linear-gradient(135deg, var(--caramel-1), var(--caramel-2));
            color: #fff;
            box-shadow: 0 8px 18px -10px rgba(224, 160, 106, 0.9);
        }

        /* --- Виджет каникул --- */
        .countdown {
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 14px 16px;
            margin-bottom: 14px;
            border-radius: 20px;
        }
        .cd-num {
            font-size: 28px;
            font-weight: 800;
            color: var(--latte);
            font-variant-numeric: tabular-nums;
            min-width: 56px;
            text-align: center;
        }
        .cd-txt { font-size: 13px; color: var(--text-2); line-height: 1.35; }
        .cd-txt b { color: var(--text); font-weight: 700; display: block; font-size: 14px; }

        /* --- Сегмент-контрол --- */
        .seg {
            position: relative;
            display: flex;
            padding: 4px;
            border-radius: 18px;
            margin-bottom: 14px;
        }
        .seg-ind {
            position: absolute;
            left: 0;
            top: 4px;
            bottom: 4px;
            width: 0;
            border-radius: 14px;
            background: linear-gradient(135deg, var(--caramel-1), var(--caramel-2));
            box-shadow: 0 10px 24px -10px rgba(224, 160, 106, 0.8);
            transition: transform 0.55s var(--ease), width 0.55s var(--ease);
        }
        .seg button {
            position: relative;
            z-index: 1;
            flex: 1;
            border: 0;
            background: none;
            color: var(--text-2);
            font: inherit;
            font-size: 14px;
            font-weight: 700;
            padding: 11px 8px;
            border-radius: 14px;
            cursor: pointer;
            transition: color 0.35s var(--ease);
        }
        .seg button.on { color: #fff; }
        .seg-sm { padding: 3px; border-radius: 14px; margin-bottom: 16px; }
        .seg-sm .seg-ind { top: 3px; bottom: 3px; border-radius: 11px; }
        .seg-sm button { padding: 9px 6px; font-size: 12.5px; border-radius: 11px; }

        .hidden { display: none !important; }
        .enter { animation: enter 0.6s var(--ease) both; }
        @keyframes enter {
            from { opacity: 0; transform: translateY(12px) scale(0.985); }
            to { opacity: 1; transform: none; }
        }

        .stack { display: flex; flex-direction: column; gap: 14px; }
        .stack-sm { display: flex; flex-direction: column; gap: 12px; }

        /* --- Карточки расписания с подписями пар/уроков --- */
        .card {
            padding: 18px 18px 16px;
            border-radius: 24px;
            transform-style: preserve-3d;
            transition: transform 0.35s var(--ease), box-shadow 0.35s var(--ease);
            will-change: transform;
        }
        .card.today {
            box-shadow:
                0 0 0 1px rgba(224, 160, 106, 0.55),
                0 0 30px -6px rgba(224, 160, 106, 0.45),
                0 18px 40px -22px rgba(0, 0, 0, 0.95);
        }
        .card.today::before {
            background: linear-gradient(135deg, #F3C48F, rgba(224, 160, 106, 0.55) 45%, rgba(168, 104, 58, 0.5));
        }
        .shine {
            position: absolute;
            inset: 0;
            border-radius: inherit;
            background: radial-gradient(circle at var(--mx, 50%) var(--my, 50%), rgba(255, 240, 220, 0.16), transparent 55%);
            opacity: 0;
            transition: opacity 0.3s;
            pointer-events: none;
        }
        .card.tilt.active .shine { opacity: 1; }

        .dhead {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .dname { font-size: 17px; font-weight: 800; letter-spacing: -0.01em; }
        .tag {
            font-size: 11px;
            font-weight: 700;
            padding: 5px 10px;
            border-radius: 999px;
            background: rgba(224, 160, 106, 0.14);
            color: var(--latte);
            border: 1px solid rgba(224, 160, 106, 0.35);
            white-space: nowrap;
        }
        .today-tag {
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.14em;
            color: #F3C48F;
            margin-right: 8px;
        }
        
        /* Стили для красивого отображения подписей пар/уроков и времени */
        .lesson-item {
            padding: 6px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
        }
        .lesson-item:last-child { border-bottom: none; }
        .lesson-meta {
            display: flex;
            justify-content: space-between;
            font-size: 11px;
            font-weight: 700;
            color: var(--caramel-1);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 2px;
        }
        .lesson-sub {
            font-size: 14.5px;
            font-weight: 600;
            color: #EFE3D3;
        }

        .empty { color: var(--text-3); font-style: italic; font-size: 14px; padding: 4px 0; }
        .dact {
            display: flex;
            justify-content: space-between;
            gap: 8px;
            margin-top: 14px;
            padding-top: 12px;
            border-top: 1px solid rgba(255, 255, 255, 0.06);
        }

        /* --- Карточки домашки / объявлений --- */
        .hw-item { padding: 16px 16px 14px; }
        .hw-subj { font-size: 16px; font-weight: 800; letter-spacing: -0.01em; }
        .hw-task { font-size: 14.5px; color: #D9C8B5; white-space: pre-wrap; line-height: 1.5; margin: 8px 0 10px; }
        .hw-author { font-size: 11px; color: var(--text-3); text-align: right; }

        .sec-title { font-size: 15px; font-weight: 800; margin-bottom: 12px; }
        .row2 { display: flex; gap: 8px; }
        .muted { font-size: 13px; color: var(--text-2); }
        .small { font-size: 12px; }
        .acc { color: #F3C48F; font-weight: 700; }
        .label {
            font-size: 11px;
            color: var(--text-3);
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        /* --- Кнопки --- */
        .btn {
            border: 0;
            cursor: pointer;
            font: inherit;
            font-weight: 700;
            font-size: 14px;
            border-radius: 15px;
            padding: 13px 16px;
            color: #fff;
            background: linear-gradient(135deg, var(--caramel-1), var(--caramel-2));
            box-shadow: 0 12px 26px -14px rgba(224, 160, 106, 0.9);
            transition: transform 0.2s var(--ease), filter 0.2s;
        }
        .btn:hover { filter: brightness(1.06); }
        .btn:active { transform: scale(0.97); }
        .btn-sm { padding: 8px 12px; font-size: 12.5px; border-radius: 12px; }
        .btn-ghost {
            background: rgba(255, 255, 255, 0.06);
            color: var(--latte);
            border: 1px solid rgba(255, 255, 255, 0.09);
            box-shadow: none;
        }
        .btn-danger {
            background: rgba(120, 30, 30, 0.35);
            color: #F2A09A;
            border: 1px solid rgba(242, 160, 154, 0.22);
            box-shadow: none;
        }
        .btn-block { width: 100%; }

        /* --- Шторки (bottom sheets) --- */
        .sheet-bd {
            position: fixed;
            inset: 0;
            z-index: 55;
            background: rgba(0, 0, 0, 0.45);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.45s var(--ease);
        }
        .sheet-bd.open { opacity: 1; pointer-events: auto; }
        .sheet {
            position: fixed;
            left: 0;
            right: 0;
            bottom: 0;
            z-index: 60;
            max-height: 88vh;
            overflow-y: auto;
            padding: 10px 18px calc(20px + env(safe-area-inset-bottom, 0px));
            border-radius: 28px 28px 0 0;
            background: linear-gradient(180deg, rgba(44, 34, 27, 0.86), rgba(18, 14, 11, 0.94));
            -webkit-backdrop-filter: blur(22px) saturate(160%);
            backdrop-filter: blur(22px) saturate(160%);
            border: 1px solid rgba(233, 201, 164, 0.14);
            border-bottom: 0;
            box-shadow: 0 -24px 60px -20px rgba(0, 0, 0, 0.9);
            transform: translateY(104%);
            transition: transform 0.6s var(--ease);
            max-width: 460px;
            margin: 0 auto;
        }
        .sheet.open { transform: translateY(0); }
        .handle {
            width: 40px;
            height: 5px;
            border-radius: 9px;
            background: rgba(255, 255, 255, 0.2);
            margin: 0 auto 14px;
        }
        .sh-title {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 18px;
            font-weight: 800;
            margin-bottom: 14px;
            gap: 8px;
        }
        .sh-left { display: flex; align-items: center; gap: 8px; }
        .sh-close, .sh-back {
            width: 34px;
            height: 34px;
            border-radius: 50%;
            border: 0;
            background: rgba(255, 255, 255, 0.08);
            color: var(--text-2);
            font-size: 14px;
            cursor: pointer;
            flex-shrink: 0;
        }
        .sh-back { border-radius: 12px; color: var(--latte); }

        /* --- Поля ввода --- */
        .input {
            width: 100%;
            background: rgba(10, 8, 7, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.09);
            border-radius: 14px;
            padding: 12px 14px;
            color: var(--text);
            font: inherit;
            font-size: 15px;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        .input:focus {
            outline: none;
            border-color: rgba(224, 160, 106, 0.7);
            box-shadow: 0 0 0 4px rgba(224, 160, 106, 0.12);
        }
        textarea.input { resize: vertical; }
        .input-sm { padding: 10px 12px; font-size: 14px; }

        /* --- Предметы --- */
        .subj-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; max-height: 60vh; overflow-y: auto; padding-right: 2px; }
        .subj {
            padding: 14px 8px;
            border-radius: 16px;
            border: 1px solid rgba(255, 255, 255, 0.09);
            background: rgba(255, 255, 255, 0.04);
            color: var(--text);
            font: inherit;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.2s var(--ease), border-color 0.25s, background 0.25s;
        }
        .subj:active { transform: scale(0.95); }
        .subj:hover { border-color: rgba(224, 160, 106, 0.6); background: rgba(224, 160, 106, 0.08); }

        /* --- Календарь --- */
        .cal {
            padding: 14px;
            border-radius: 20px;
            background: rgba(10, 8, 7, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.06);
        }
        .cal-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }
        .cal-month { font-size: 15px; font-weight: 800; }
        .cal-nav {
            width: 34px;
            height: 34px;
            border-radius: 11px;
            border: 1px solid rgba(255, 255, 255, 0.08);
            background: rgba(255, 255, 255, 0.05);
            color: var(--latte);
            font-size: 18px;
            cursor: pointer;
        }
        .cal-nav:disabled { opacity: 0.25; }
        .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
        .cal-wd {
            text-align: center;
            font-size: 11px;
            color: var(--text-3);
            font-weight: 700;
            padding: 4px 0;
        }
        .cal-day {
            position: relative;
            aspect-ratio: 1 / 1;
            border-radius: 12px;
            border: 1px solid transparent;
            background: transparent;
            color: var(--text);
            font: inherit;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s, transform 0.2s var(--ease);
        }
        .cal-day:active:not(:disabled) { transform: scale(0.88); }
        .cal-day:hover:not(:disabled) { background: rgba(255, 255, 255, 0.06); }
        .cal-past { opacity: 0.25; cursor: default; }
        .cal-today { border-color: rgba(224, 160, 106, 0.75); color: #F3C48F; }
        .cal-selected {
            background: linear-gradient(135deg, var(--caramel-1), var(--caramel-2));
            color: #fff;
            box-shadow: 0 8px 18px -10px rgba(224, 160, 106, 0.95);
        }
        .cal-dot {
            position: absolute;
            bottom: 4px;
            left: 50%;
            transform: translateX(-50%);
            width: 4px;
            height: 4px;
            border-radius: 50%;
            background: var(--latte);
        }

        /* --- Редактор расписания --- */
        .lesson-row {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px;
            border-radius: 16px;
            background: rgba(10, 8, 7, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.06);
        }
        .lesson-row .lbl {
            font-size: 10px;
            color: var(--text-3);
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .del-row {
            width: 36px;
            height: 36px;
            flex-shrink: 0;
            border-radius: 12px;
            border: 0;
            background: rgba(120, 30, 30, 0.35);
            color: #F2A09A;
            font-size: 13px;
            cursor: pointer;
        }
        .add-toggle {
            width: 100%;
            background: transparent;
            border: 1px dashed rgba(224, 160, 106, 0.4);
            color: var(--latte);
            font: inherit;
            font-size: 14px;
            font-weight: 700;
            padding: 13px;
            border-radius: 15px;
            cursor: pointer;
        }
        .add-panel {
            display: none;
            flex-direction: column;
            gap: 12px;
            padding: 14px;
            border-radius: 18px;
            background: rgba(10, 8, 7, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.07);
        }
        .add-panel.open { display: flex; }
        .mini-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .slot {
            padding: 11px 6px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.08);
            background: rgba(255, 255, 255, 0.04);
            color: var(--text-2);
            font: inherit;
            font-size: 13px;
            font-weight: 700;
            line-height: 1.25;
            cursor: pointer;
            transition: all 0.25s var(--ease);
        }
        .slot small { display: block; font-size: 10.5px; font-weight: 600; opacity: 0.8; margin-top: 3px; }
        .slot.on {
            background: linear-gradient(135deg, var(--caramel-1), var(--caramel-2));
            border-color: transparent;
            color: #fff;
            box-shadow: 0 8px 18px -10px rgba(224, 160, 106, 0.9);
        }

        /* --- Скелетоны загрузки --- */
        .skel {
            position: relative;
            overflow: hidden;
            height: 150px;
            border-radius: 24px;
            background: rgba(255, 255, 255, 0.035);
            border: 1px solid rgba(255, 255, 255, 0.05);
        }
        .skel::after {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(100deg, transparent 30%, rgba(255, 240, 220, 0.09) 50%, transparent 70%);
            transform: translateX(-100%);
            animation: shimmer 1.4s infinite;
        }
        @keyframes shimmer { to { transform: translateX(100%); } }

        .toast-empty {
            text-align: center;
            padding: 36px 12px;
            color: var(--text-3);
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="mesh" id="mesh">
        <span class="blob b1"></span>
        <span class="blob b2"></span>
        <span class="blob b3"></span>
    </div>

    <div id="app">
        <header class="hdr">
            <div class="brand">🤎 ЭЗС 12-26</div>
            <span id="admin-badge" class="badge hidden">Админ</span>
        </header>
        <div id="greeting" class="greet"></div>

        <div id="countdown" class="glass countdown hidden"></div>

        <div id="seg-main" class="seg glass">
            <span class="seg-ind"></span>
            <button class="on" data-tab="schedule" onclick="switchTab('schedule')">Расписание</button>
            <button data-tab="homework" onclick="switchTab('homework')">Домашка</button>
            <button data-tab="news" onclick="switchTab('news')">Объявления</button>
        </div>

        <section id="tab-schedule">
            <div id="seg-week" class="seg seg-sm glass">
                <span class="seg-ind"></span>
                <button class="on" data-week="current" onclick="setWeek('current')">Эту неделю</button>
                <button data-week="next" onclick="setWeek('next')">Следующую</button>
            </div>
            <div id="schedule-container" class="stack"></div>
        </section>

        <section id="tab-homework" class="hidden">
            <div class="glass card">
                <div class="sec-title">Домашнее задание</div>
                <div class="row2">
                    <button class="btn btn-block" onclick="openHwWizard()">Записать ДЗ</button>
                    <button class="btn btn-danger hidden" id="btn-del-hw" onclick="openDeleteHwModal()">Удалить</button>
                </div>
            </div>
            <div id="homework-container" class="stack" style="margin-top:14px"></div>
        </section>

        <section id="tab-news" class="hidden">
            <div class="glass card">
                <div class="sec-title">Объявления</div>
                <button id="news-admin-btn" class="btn btn-block hidden" onclick="openNewsComposer()">Написать объявление</button>
            </div>
            <div id="news-container" class="stack" style="margin-top:14px"></div>
        </section>
    </div>

    <div id="bd" class="sheet-bd" onclick="closeAllSheets()"></div>

    <!-- Шторка: запись ДЗ -->
    <div id="hw-sheet" class="sheet glass">
        <div class="handle"></div>
        <div class="sh-title">
            <div class="sh-left">
                <button id="hw-back" class="sh-back hidden" onclick="showHwStage1()">←</button>
                <span id="hw-title">Выберите предмет</span>
            </div>
            <button class="sh-close" onclick="closeHwWizard()">✕</button>
        </div>
        <div id="hw-stage-subjects">
            <div id="subject-list" class="subj-grid"></div>
        </div>
        <div id="hw-stage-form" class="hidden stack-sm">
            <div class="muted">Предмет: <span id="hw-subject-label" class="acc"></span></div>
            <div class="label">Дата</div>
            <div id="hw-calendar" class="cal"></div>
            <div id="hw-date-hint" class="muted small"></div>
            <textarea id="hw-task" class="input" rows="3" maxlength="2000" placeholder="Что задали..."></textarea>
            <button class="btn btn-block" onclick="submitHomework()">Опубликовать</button>
        </div>
    </div>

    <!-- Шторка: новое объявление (админ) -->
    <div id="news-sheet" class="sheet glass">
        <div class="handle"></div>
        <div class="sh-title">
            <span>Новое объявление</span>
            <button class="sh-close" onclick="closeNewsComposer()">✕</button>
        </div>
        <div class="stack-sm">
            <div class="label">Дата события / показа</div>
            <input id="news-date" type="date" class="input input-sm">
            <textarea id="news-text" class="input" rows="4" maxlength="1000" placeholder="Текст объявления..."></textarea>
            <button class="btn btn-block" onclick="publishNews()">Опубликовать</button>
        </div>
    </div>

    <!-- Шторка: удаление ДЗ (админ) -->
    <div id="del-sheet" class="sheet glass">
        <div class="handle"></div>
        <div class="sh-title">
            <span>Удалить ДЗ</span>
            <button class="sh-close" onclick="closeDeleteHwModal()">✕</button>
        </div>
        <div id="del-hw-list" class="stack-sm"></div>
    </div>

    <!-- Шторка: ДЗ на день -->
    <div id="day-sheet" class="sheet glass">
        <div class="handle"></div>
        <div class="sh-title">
            <span id="day-hw-modal-title">Домашнее задание</span>
            <button class="sh-close" onclick="closeDayHwModal()">✕</button>
        </div>
        <div id="day-hw-container" class="stack-sm"></div>
        <button class="btn btn-block" style="margin-top:14px" onclick="closeDayHwModal()">Закрыть</button>
    </div>

    <!-- Шторка: редактор дня (админ) -->
    <div id="edit-sheet" class="sheet glass">
        <div class="handle"></div>
        <div class="sh-title">
            <span id="modal-title">Редактирование дня</span>
            <button class="sh-close" onclick="closeEditModal()">✕</button>
        </div>
        <div class="stack-sm">
            <div id="modal-lessons-container" class="stack-sm"></div>

            <button type="button" id="add-toggle" class="add-toggle" onclick="toggleAddPanel()">+ Добавить пару / урок</button>

            <div id="add-panel" class="add-panel">
                <div class="mini-grid">
                    <button type="button" id="kind-pair" class="slot" onclick="setLessonKind('pair')">Пара</button>
                    <button type="button" id="kind-lesson" class="slot" onclick="setLessonKind('lesson')">Урок</button>
                </div>
                <div id="slot-list" class="mini-grid"></div>
                <div id="custom-lesson" class="hidden stack-sm">
                    <input id="custom-label" type="text" placeholder="Название, например: 8 урок" class="input input-sm">
                    <input id="custom-time" type="text" placeholder="Время, например: 15.25-16.10" class="input input-sm">
                </div>
                <input id="new-subject" list="subject-options" type="text" placeholder="Предмет" class="input input-sm">
                <datalist id="subject-options"></datalist>
                <button type="button" class="btn btn-block btn-sm" onclick="confirmAddLesson()">Добавить в расписание</button>
            </div>

            <div class="row2" style="margin-top:6px">
                <button class="btn btn-ghost btn-block" onclick="closeEditModal()">Отмена</button>
                <button class="btn btn-block" onclick="saveDaySchedule()">Сохранить</button>
            </div>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.ready();
        tg.expand();

        const authToken = new URLSearchParams(window.location.search).get('t') || '';

        let appData = null;
        let activeTab = 'schedule';
        let activeWeekType = 'current';
        let editingDay = null;
        let currentEditingLessons = [];
        let addState = { kind: 'pair', slot: 0 };
        let hw = { subject: null, date: null, view: null };
        let scrolledToToday = false;
        let lastTilt = null;

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

        // Разбор строки расписания
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

        function haptic(kind) {
            try {
                const h = tg.HapticFeedback;
                if (!h) return;
                if (kind === 'success' || kind === 'error' || kind === 'warning') {
                    h.notificationOccurred(kind);
                } else if (kind === 'selection') {
                    h.selectionChanged();
                } else {
                    h.impactOccurred(kind || 'light');
                }
            } catch (e) {}
        }

        function openSheet(id) {
            document.getElementById(id).classList.add('open');
            document.getElementById('bd').classList.add('open');
            document.body.classList.add('sheet-open');
        }
        function closeSheet(id) {
            document.getElementById(id).classList.remove('open');
            if (!document.querySelector('.sheet.open')) {
                document.getElementById('bd').classList.remove('open');
                document.body.classList.remove('sheet-open');
            }
        }
        function closeAllSheets() {
            document.querySelectorAll('.sheet.open').forEach(s => s.classList.remove('open'));
            document.getElementById('bd').classList.remove('open');
            document.body.classList.remove('sheet-open');
            editingDay = null;
        }

        function moveInd(segId) {
            const seg = document.getElementById(segId);
            if (!seg) return;
            const on = seg.querySelector('button.on');
            const ind = seg.querySelector('.seg-ind');
            if (!on || !ind || !on.offsetWidth) return;
            ind.style.width = on.offsetWidth + 'px';
            ind.style.transform = `translateX(${on.offsetLeft}px)`;
        }

        function setOn(segId, attr, value) {
            document.querySelectorAll(`#${segId} button`).forEach(b => {
                b.classList.toggle('on', b.dataset[attr] === value);
            });
            moveInd(segId);
        }

        function animateIn(el) {
            el.classList.remove('enter');
            void el.offsetWidth;
            el.classList.add('enter');
        }

        function switchTab(tab) {
            if (tab === activeTab) return;
            activeTab = tab;
            setOn('seg-main', 'tab', tab);
            const sections = { schedule: 'tab-schedule', homework: 'tab-homework', news: 'tab-news' };
            Object.entries(sections).forEach(([k, id]) => {
                document.getElementById(id).classList.toggle('hidden', k !== tab);
            });
            animateIn(document.getElementById(sections[tab]));
            if (tab === 'schedule') moveInd('seg-week');
            haptic('light');
        }

        function setWeek(type) {
            if (type === activeWeekType) return;
            activeWeekType = type;
            setOn('seg-week', 'week', type);
            renderSchedule(true);
            haptic('light');
        }

        function initSwipe() {
            const el = document.getElementById('tab-schedule');
            let sx = 0, sy = 0, active = false;
            el.addEventListener('touchstart', e => {
                if (document.querySelector('.sheet.open')) return;
                sx = e.touches[0].clientX;
                sy = e.touches[0].clientY;
                active = true;
            }, { passive: true });
            el.addEventListener('touchend', e => {
                if (!active) return;
                active = false;
                const t = e.changedTouches[0];
                const dx = t.clientX - sx;
                const dy = t.clientY - sy;
                if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
                    if (dx < 0) setWeek('next');
                    else setWeek('current');
                }
            }, { passive: true });
        }

        function initTilt() {
            const reset = el => {
                if (!el) return;
                el.style.transform = '';
                el.classList.remove('active');
            };
            document.addEventListener('pointermove', e => {
                const el = e.target.closest && e.target.closest('.tilt');
                if (lastTilt && lastTilt !== el) reset(lastTilt);
                if (!el) { lastTilt = null; return; }
                lastTilt = el;
                const r = el.getBoundingClientRect();
                const x = (e.clientX - r.left) / r.width;
                const y = (e.clientY - r.top) / r.height;
                const rx = (0.5 - y) * 7;
                const ry = (x - 0.5) * 9;
                el.style.transform = `perspective(900px) rotateX(${rx}deg) rotateY(${ry}deg) scale(1.01)`;
                el.style.setProperty('--mx', (x * 100) + '%');
                el.style.setProperty('--my', (y * 100) + '%');
                el.classList.add('active');
            }, { passive: true });
            document.addEventListener('pointerleave', e => {
                if (e.target && e.target.classList && e.target.classList.contains('tilt')) reset(e.target);
            }, true);
            document.addEventListener('pointerup', () => { if (lastTilt) { reset(lastTilt); lastTilt = null; } });
            document.addEventListener('pointercancel', () => { if (lastTilt) { reset(lastTilt); lastTilt = null; } });
        }

        function initParallax() {
            const mesh = document.getElementById('mesh');
            let raf = null;
            window.addEventListener('scroll', () => {
                if (raf) return;
                raf = requestAnimationFrame(() => {
                    const y = window.scrollY || 0;
                    mesh.style.transform = `translate3d(0, ${-y * 0.12}px, 0)`;
                    raf = null;
                });
            }, { passive: true });
        }

        function initHaptics() {
            document.addEventListener('click', e => {
                const b = e.target.closest && e.target.closest('button');
                if (b) haptic('light');
            }, true);
        }

        function renderGreeting() {
            const u = (tg.initDataUnsafe && tg.initDataUnsafe.user) || {};
            const name = u.first_name || '';
            const h = new Date().getHours();
            let g = 'Добрый вечер';
            if (h < 5) g = 'Доброй ночи';
            else if (h < 12) g = 'Доброе утро';
            else if (h < 18) g = 'Добрый день';
            document.getElementById('greeting').textContent = name ? `${g}, ${name} 👋` : `${g} 👋`;
        }

        function plural(n, one, few, many) {
            const a = Math.abs(n) % 100;
            const b = a % 10;
            if (a > 10 && a < 20) return many;
            if (b > 1 && b < 5) return few;
            if (b === 1) return one;
            return many;
        }
        function dayWord(n) { return plural(n, 'день', 'дня', 'дней'); }

        function renderCountdown() {
            const el = document.getElementById('countdown');
            if (!appData || !appData.today_date) { el.classList.add('hidden'); return; }
            const p = parseIso(appData.today_date);
            const today = new Date(p.y, p.m, p.d);
            const y = p.y;
            const diff = (to) => Math.round((to - today) / 86400000);
            const mkDate = (yy, mm, dd) => new Date(yy, mm, dd);

            const mo = p.m;
            const d = p.d;
            let num = null, title = '', sub = '';

            if (mo === 0 && d <= 18) {
                num = diff(mkDate(y, 0, 19));
                title = 'До зимних каникул';
                sub = 'Старт 19 января';
            } else if ((mo === 0 && d >= 19) || (mo === 1 && d <= 1)) {
                num = diff(mkDate(y, 1, 2));
                title = 'Зимние каникулы';
                sub = 'До учёбы 2 февраля';
            } else if ((mo === 1 && d >= 2) || (mo >= 2 && mo <= 4) || (mo === 5)) {
                num = diff(mkDate(y, 6, 1));
                title = 'До летних каникул';
                sub = 'Старт 1 июля';
            } else if (mo === 11) {
                num = diff(mkDate(y + 1, 0, 19));
                title = 'До зимних каникул';
                sub = 'Старт 19 января';
            } else {
                el.classList.add('hidden');
                return;
            }

            const isVacation = title === 'Зимние каникулы';
            const value = isVacation ? diff(mkDate(y, 1, 2)) : num;
            el.innerHTML = `
                <div class="cd-num">${value}</div>
                <div class="cd-txt"><b>${title}</b>${sub} · ${dayWord(value)}</div>
            `;
            el.classList.remove('hidden');
        }

        function skeletons(n) {
            return Array.from({ length: n }, () => '<div class="skel"></div>').join('');
        }

        function loadData() {
            if (!appData) {
                document.getElementById('schedule-container').innerHTML = skeletons(3);
                document.getElementById('homework-container').innerHTML = skeletons(2);
            }
            return fetch('/api/data', { headers: authHeaders(false) })
                .then(res => res.json())
                .then(data => {
                    const first = appData === null;
                    appData = data;
                    if (data.is_admin) {
                        document.getElementById('admin-badge').classList.remove('hidden');
                        document.getElementById('btn-del-hw').classList.remove('hidden');
                        document.getElementById('news-admin-btn').classList.remove('hidden');
                    }
                    renderGreeting();
                    renderCountdown();
                    renderSchedule(first);
                    renderHomework(first);
                    renderNews(first);
                    if (first) requestAnimationFrame(() => { moveInd('seg-main'); moveInd('seg-week'); });
                })
                .catch(() => {
                    document.getElementById('schedule-container').innerHTML =
                        '<div class="toast-empty">Нет связи с сервером. Закройте и откройте приложение снова.</div>';
                });
        }

        // --- РАСПИСАНИЕ С ПОДПИСЯМИ ПАР И УРОКОВ ---
        function renderSchedule(animate) {
            if (!appData) return;
            const container = document.getElementById('schedule-container');
            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;

            container.innerHTML = schedule.map((day, i) => {
                const isToday = day.full_date === appData.today_date;
                const delay = animate ? `animation-delay:${i * 45}ms` : '';
                const todayTag = isToday ? '<span class="today-tag">СЕГОДНЯ</span>' : '';
                
                const lessons = day.lessons.length
                    ? day.lessons.map(l => {
                        const m = l.match(/^(.*?)\s*\(([^)]*)\)\s*:\s*(.*)$/);
                        if (m) {
                            return `
                            <div class="lesson-item">
                                <div class="lesson-meta"><span>${esc(m[1])}</span><span>${esc(m[2])}</span></div>
                                <div class="lesson-sub">${esc(m[3])}</div>
                            </div>`;
                        }
                        const m2 = l.match(/^(.*?):\s*(.*)$/);
                        if (m2) {
                            return `
                            <div class="lesson-item">
                                <div class="lesson-meta"><span>${esc(m2[1])}</span></div>
                                <div class="lesson-sub">${esc(m2[2])}</div>
                            </div>`;
                        }
                        return `<div class="lesson-item"><div class="lesson-sub">${esc(l)}</div></div>`;
                    }).join('')
                    : '<div class="empty">Пар нет</div>';

                return `
                <div class="glass card tilt ${isToday ? 'today' : ''} ${animate ? 'enter' : ''}" style="${delay}">
                    <div class="shine"></div>
                    <div class="dhead">
                        <span class="dname">${esc(day.day)}</span>
                        <span style="display:flex;align-items:center">${todayTag}<span class="tag">${esc(day.date)}</span></span>
                    </div>
                    <div style="display:flex;flex-direction:column;gap:6px">${lessons}</div>
                    <div class="dact">
                        <button class="btn btn-sm" onclick="openDayHwModal('${esc(day.full_date)}', '${esc(day.day)}', '${esc(day.date)}')">Домашка</button>
                        ${appData.is_admin ? `<button class="btn btn-ghost btn-sm" onclick="openEditModal('${esc(day.day)}')">Изменить</button>` : ''}
                    </div>
                </div>`;
            }).join('');

            if (!scrolledToToday && activeWeekType === 'current' && activeTab === 'schedule') {
                scrolledToToday = true;
                const el = container.querySelector('.card.today');
                if (el) setTimeout(() => el.scrollIntoView({ behavior: 'smooth', block: 'center' }), 500);
            }
        }

        function openDayHwModal(fullDateStr, dayName, shortDateStr) {
            document.getElementById('day-hw-modal-title').innerText = `ДЗ · ${dayName} ${shortDateStr}`;
            const list = appData.homework.filter(h => h.date_key === fullDateStr);
            const container = document.getElementById('day-hw-container');
            container.innerHTML = list.length
                ? list.map(h => `
                    <div class="glass hw-item">
                        <div class="dhead" style="margin-bottom:0">
                            <span class="hw-subj">${esc(h.subject)}</span>
                            <span class="muted small">${esc(h.author)}</span>
                        </div>
                        <div class="hw-task">${esc(h.task)}</div>
                    </div>`).join('')
                : '<div class="empty" style="text-align:center;padding:24px 0">На этот день домашних заданий нет ✨</div>';
            openSheet('day-sheet');
        }

        function closeDayHwModal() { closeSheet('day-sheet'); }

        // --- РЕДАКТОР РАСПИСАНИЯ ---
        function openEditModal(dayName) {
            editingDay = dayName;
            const schedule = activeWeekType === 'current' ? appData.current_week : appData.next_week;
            const dayObj = schedule.find(d => d.day === dayName);
            currentEditingLessons = dayObj ? dayObj.lessons.map(parseLesson) : [];

            document.getElementById('modal-title').innerText = `${dayName} · ${activeWeekType === 'current' ? 'Текущая' : 'Следующая'}`;
            document.getElementById('subject-options').innerHTML =
                (appData.subjects || []).map(s => `<option value="${esc(s)}"></option>`).join('');

            closeAddPanel();
            renderModalLessons();
            openSheet('edit-sheet');
        }

        function closeEditModal() {
            closeSheet('edit-sheet');
            editingDay = null;
        }

        function syncFromInputs() {
            document.querySelectorAll('#modal-lessons-container input[data-index]').forEach(inp => {
                const i = Number(inp.dataset.index);
                if (currentEditingLessons[i]) currentEditingLessons[i].subject = inp.value;
            });
        }

        function renderModalLessons() {
            const container = document.getElementById('modal-lessons-container');
            if (currentEditingLessons.length === 0) {
                container.innerHTML = '<div class="empty" style="text-align:center;padding:14px 0">Пар нет</div>';
                return;
            }
            container.innerHTML = currentEditingLessons.map((l, i) => `
                <div class="lesson-row">
                    <div style="flex:1;min-width:0">
                        <div class="lbl">${esc(l.label)}${l.time ? ' · ' + esc(l.time) : ''}</div>
                        <input type="text" data-index="${i}" value="${esc(l.subject)}" class="input input-sm" style="margin-top:6px">
                    </div>
                    <button type="button" class="del-row" onclick="removeLesson(${i})" title="Удалить">✕</button>
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
            if (!panel.classList.contains('open')) {
                addState = { kind: 'pair', slot: 0 };
                panel.classList.add('open');
                renderAddPanel();
            } else {
                closeAddPanel();
            }
        }

        function closeAddPanel() {
            document.getElementById('add-panel').classList.remove('open');
        }

        function setLessonKind(kind) {
            addState = { kind, slot: 0 };
            renderAddPanel();
        }

        function renderAddPanel() {
            document.getElementById('kind-pair').classList.toggle('on', addState.kind === 'pair');
            document.getElementById('kind-lesson').classList.toggle('on', addState.kind === 'lesson');

            const slots = addState.kind === 'pair' ? PAIR_SLOTS : LESSON_SLOTS;
            document.getElementById('slot-list').innerHTML = slots.map((s, i) => `
                <button type="button" data-slot="${i}" class="slot ${i === addState.slot ? 'on' : ''}" onclick="pickSlot(${i})">
                    ${esc(s.label)}${s.time ? '<small>' + esc(s.time) + '</small>' : ''}
                </button>
            `).join('');

            const current = slots[addState.slot];
            document.getElementById('custom-lesson').classList.toggle('hidden', !(current && current.custom));
        }

        function pickSlot(i) {
            addState.slot = i;
            renderAddPanel();
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
                    haptic('warning');
                    alert('Введите название урока, например: 8 урок');
                    return;
                }
            }

            const subject = document.getElementById('new-subject').value.trim();
            if (!subject) {
                haptic('warning');
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
                    haptic('success');
                    closeEditModal();
                    loadData();
                } else {
                    haptic('error');
                    alert('Ошибка: ' + (data.message || 'Не удалось сохранить'));
                }
            });
        }

        // --- ДОМАШКА: СПИСОК ---
        function renderHomework(animate) {
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
                container.innerHTML = '<div class="toast-empty">Домашки пока нет ✨</div>';
                return;
            }
            container.innerHTML = list.map((h, i) => `
                <div class="glass card hw-item tilt ${animate ? 'enter' : ''}" style="${animate ? `animation-delay:${i * 40}ms` : ''}">
                    <div class="shine"></div>
                    <div class="dhead" style="margin-bottom:0">
                        <span class="hw-subj">${esc(h.subject)}</span>
                        <span class="tag">${esc(h.date_key)}</span>
                    </div>
                    <div class="hw-task">${esc(h.task)}</div>
                    <div class="hw-author">${esc(h.author)}</div>
                </div>
            `).join('');
        }

        function openDeleteHwModal() {
            if (!appData) return;
            const list = document.getElementById('del-hw-list');
            list.innerHTML = appData.homework.length
                ? appData.homework.map(h => `
                    <div class="lesson-row">
                        <div style="flex:1;min-width:0">
                            <div style="font-size:14px;font-weight:700">${esc(h.subject)} <span class="muted small">· ${esc(h.date_key)}</span></div>
                            <div class="muted small" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(h.task)}</div>
                        </div>
                        <button type="button" data-del="${h.id}" class="btn btn-danger btn-sm">Удалить</button>
                    </div>
                `).join('')
                : '<div class="empty" style="text-align:center;padding:20px 0">Домашки пока нет</div>';
            openSheet('del-sheet');
        }

        function closeDeleteHwModal() { closeSheet('del-sheet'); }

        function deleteHomework(id) {
            if (!confirm('Удалить это ДЗ?')) return;
            fetch('/api/homework/delete', {
                method: 'POST',
                headers: authHeaders(true),
                body: JSON.stringify({ id })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    haptic('success');
                    loadData().then(openDeleteHwModal);
                } else {
                    haptic('error');
                    alert('Ошибка: ' + (data.message || 'Не удалось удалить'));
                }
            });
        }

        // --- ДОМАШКА: ПОШАГОВАЯ ЗАПИСЬ ---
        function openHwWizard() {
            if (!appData) return;
            hw = { subject: null, date: null, view: null };
            showHwStage1();
            openSheet('hw-sheet');
        }

        function closeHwWizard() { closeSheet('hw-sheet'); }

        function showHwStage1() {
            document.getElementById('hw-title').textContent = 'Выберите предмет';
            document.getElementById('hw-back').classList.add('hidden');
            document.getElementById('hw-stage-subjects').classList.remove('hidden');
            document.getElementById('hw-stage-form').classList.add('hidden');

            const subjects = appData.subjects || [];
            document.getElementById('subject-list').innerHTML = subjects.length
                ? subjects.map(s => `<button type="button" data-subject="${esc(s)}" class="subj">${esc(s)}</button>`).join('')
                : '<div class="muted" style="grid-column:1/-1;text-align:center;padding:20px 0">Предметов пока нет</div>';
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
                <div class="cal-head">
                    <button type="button" data-nav="-1" class="cal-nav" ${prevAllowed ? '' : 'disabled'}>‹</button>
                    <div class="cal-month">${MONTHS[m]} ${y}</div>
                    <button type="button" data-nav="1" class="cal-nav">›</button>
                </div>
                <div class="cal-grid" style="margin-bottom:4px">${WD_SHORT.map(w => `<span class="cal-wd">${w}</span>`).join('')}</div>
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
                haptic('warning');
                alert('Выберите предмет');
                return;
            }
            if (!hw.date || hw.date < appData.today_date) {
                haptic('warning');
                alert('Нельзя записать ДЗ на прошедший день');
                return;
            }
            if (!task) {
                haptic('warning');
                alert('Напишите, что задали');
                return;
            }

            fetch('/api/homework', {
                method: 'POST',
                headers: authHeaders(true),
                body: JSON.stringify({ date_key: hw.date, subject: hw.subject, task })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    haptic('success');
                    document.getElementById('hw-task').value = '';
                    closeHwWizard();
                    loadData();
                } else {
                    haptic('error');
                    alert('Ошибка: ' + (data.message || 'Не удалось сохранить'));
                }
            });
        }

        // --- ОБЪЯВЛЕНИЯ С ДАТОЙ И УДАЛЕНИЕМ ---
        function renderNews(animate) {
            if (!appData) return;
            const list = appData.announcements || [];
            const container = document.getElementById('news-container');
            if (list.length === 0) {
                container.innerHTML = '<div class="toast-empty">Объявлений пока нет</div>';
                return;
            }
            container.innerHTML = list.map((n, i) => `
                <div class="glass card hw-item tilt ${animate ? 'enter' : ''}" style="${animate ? `animation-delay:${i * 40}ms` : ''}">
                    <div class="shine"></div>
                    <div class="dhead" style="margin-bottom:8px">
                        <span class="tag">${esc(n.date_key || n.created_at)}</span>
                        ${appData.is_admin ? `<button class="btn btn-danger btn-sm" onclick="deleteNews(${Number(n.id)})">Удалить</button>` : ''}
                    </div>
                    <div class="hw-task">${esc(n.text)}</div>
                    <div class="hw-author">Опубликовал: ${esc(n.author)}</div>
                </div>
            `).join('');
        }

        function openNewsComposer() {
            document.getElementById('news-text').value = '';
            if (appData && appData.today_date) {
                document.getElementById('news-date').value = appData.today_date;
            }
            openSheet('news-sheet');
        }

        function closeNewsComposer() { closeSheet('news-sheet'); }

        function publishNews() {
            const text = document.getElementById('news-text').value.trim();
            const dateKey = document.getElementById('news-date').value;
            if (!dateKey) {
                haptic('warning');
                alert('Укажите дату объявления');
                return;
            }
            if (!text) {
                haptic('warning');
                alert('Введите текст объявления');
                return;
            }
            fetch('/api/announcements', {
                method: 'POST',
                headers: authHeaders(true),
                body: JSON.stringify({ text, date_key: dateKey })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    haptic('success');
                    closeNewsComposer();
                    loadData();
                } else {
                    haptic('error');
                    alert('Ошибка: ' + (data.message || 'Не удалось опубликовать'));
                }
            });
        }

        function deleteNews(id) {
            if (!confirm('Удалить объявление?')) return;
            fetch('/api/announcements/delete', {
                method: 'POST',
                headers: authHeaders(true),
                body: JSON.stringify({ id })
            }).then(res => res.json()).then(data => {
                if (data.status === 'success') {
                    haptic('success');
                    loadData();
                } else {
                    haptic('error');
                    alert('Ошибка: ' + (data.message || 'Не удалось удалить'));
                }
            });
        }

        window.addEventListener('DOMContentLoaded', () => {
            document.getElementById('subject-list').addEventListener('click', e => {
                const btn = e.target.closest('button[data-subject]');
                if (btn) chooseSubject(btn.dataset.subject);
            });
            document.getElementById('del-hw-list').addEventListener('click', e => {
                const btn = e.target.closest('button[data-del]');
                if (btn) deleteHomework(Number(btn.dataset.del));
            });
            document.getElementById('hw-calendar').addEventListener('click', e => {
                const nav = e.target.closest('[data-nav]');
                if (nav) {
                    if (!nav.disabled) shiftMonth(Number(nav.dataset.nav));
                    return;
                }
                const day = e.target.closest('[data-iso]');
                if (day && !day.disabled) {
                    haptic('selection');
                    hw.date = day.dataset.iso;
                    renderCalendar();
                }
            });

            initSwipe();
            initTilt();
            initParallax();
            initHaptics();
            window.addEventListener('resize', () => {
                moveInd('seg-main');
                moveInd('seg-week');
            });
            loadData();
            setInterval(renderCountdown, 60 * 60 * 1000);
        });
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
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                author TEXT,
                created_at TEXT
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(announcements)")]
        if "date_key" not in cols:
            conn.execute("ALTER TABLE announcements ADD COLUMN date_key TEXT")
        if "notified" not in cols:
            conn.execute("ALTER TABLE announcements ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
        conn.execute("DELETE FROM announcements WHERE date_key IS NULL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
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
    if now.weekday() == 6 or (now.weekday() == 5 and now.hour >= SATURDAY_SWITCH_HOUR):
        return now.date() + timedelta(days=7)
    return now.date()


def purge_old_homework():
    today_iso = get_now().date().isoformat()
    with db_lock, get_db() as conn:
        conn.execute("DELETE FROM homework WHERE date_key < ?", (today_iso,))
        conn.execute("DELETE FROM announcements WHERE date_key < ?", (today_iso,))


def homework_cleaner():
    while True:
        try:
            purge_old_homework()
        except Exception as e:
            log.error("Ошибка очистки ДЗ/объявлений: %s", e)
        time.sleep(HOMEWORK_CLEAN_INTERVAL)


SUBJECT_RE = re.compile(r":\s*(.+)$")
PLACEHOLDER_SUBJECT = "Новый предмет"


def parse_subject(lesson):
    m = SUBJECT_RE.search(lesson)
    if not m:
        return None
    name = m.group(1).strip()
    if not name or name == PLACEHOLDER_SUBJECT:
        return None
    return name


def get_subjects():
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

    purge_old_homework()

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
        cursor.execute("SELECT * FROM announcements ORDER BY date_key ASC, id DESC")
        announcements = [dict(row) for row in cursor.fetchall()]

    return jsonify({
        "is_admin": is_admin,
        "announcements": announcements,
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


@app.route("/api/homework/delete", methods=["POST"])
def delete_homework():
    user_id, _, _ = get_request_user()
    if user_id not in ADMINS:
        return jsonify({"status": "error", "message": "Нет прав админа"}), 403

    data = request.get_json(silent=True) or {}
    hw_id = data.get("id")
    if not isinstance(hw_id, int) or isinstance(hw_id, bool):
        return jsonify({"status": "error", "message": "Неверный идентификатор"}), 400

    with db_lock, get_db() as conn:
        cur = conn.execute("DELETE FROM homework WHERE id = ?", (hw_id,))
        deleted = cur.rowcount

    if not deleted:
        return jsonify({"status": "error", "message": "ДЗ не найдено"}), 404
    return jsonify({"status": "success"})


@app.route("/api/announcements", methods=["POST"])
def add_announcement():
    user_id, first_name, last_name = get_request_user()
    if user_id not in ADMINS:
        return jsonify({"status": "error", "message": "Нет прав админа"}), 403

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"status": "error", "message": "Пустой текст"}), 400
    if len(text) > 1000:
        return jsonify({"status": "error", "message": "Слишком длинный текст"}), 400

    date_key_str = data.get("date_key")
    try:
        parsed_date = date.fromisoformat(str(date_key_str))
    except ValueError:
        return jsonify({"status": "error", "message": "Укажите дату объявления"}), 400
    if parsed_date < get_now().date():
        return jsonify({"status": "error", "message": "Дата уже прошла"}), 400

    author = first_name + (f" {last_name}" if last_name else "")
    created = get_now().strftime("%d.%m.%Y %H:%M")
    with db_lock, get_db() as conn:
        conn.execute(
            "INSERT INTO announcements (text, author, created_at, date_key, notified) VALUES (?, ?, ?, ?, 0)",
            (text, author, created, parsed_date.isoformat()),
        )
    try:
        notify_due_announcements()
    except Exception as e:
        log.error("Ошибка немедленной рассылки объявления: %s", e)
    return jsonify({"status": "success"})


@app.route("/api/announcements/delete", methods=["POST"])
def delete_announcement():
    user_id, _, _ = get_request_user()
    if user_id not in ADMINS:
        return jsonify({"status": "error", "message": "Нет прав админа"}), 403

    data = request.get_json(silent=True) or {}
    ann_id = data.get("id")
    if not isinstance(ann_id, int) or isinstance(ann_id, bool):
        return jsonify({"status": "error", "message": "Неверный идентификатор"}), 400

    with db_lock, get_db() as conn:
        cur = conn.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
        deleted = cur.rowcount

    if not deleted:
        return jsonify({"status": "error", "message": "Объявление не найдено"}), 404
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


# --- РАССЫЛКИ И ОПОВЕЩЕНИЯ ---
DAILY_SEND_HOUR = 7
DAILY_SEND_MINUTE = 30
DAILY_WINDOW_END_HOUR = 9


def register_chat(chat_id, title):
    with db_lock, get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chats (chat_id, title) VALUES (?, ?)",
            (chat_id, title or ""),
        )


def unregister_chat(chat_id):
    with db_lock, get_db() as conn:
        conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))


def get_chats():
    with db_lock, get_db() as conn:
        return [row["chat_id"] for row in conn.execute("SELECT chat_id FROM chats")]


def get_meta(key):
    with db_lock, get_db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key, value):
    with db_lock, get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def send_schedule_to_groups():
    text = format_day(get_now().date(), "Сегодня")
    sent = 0
    for chat_id in get_chats():
        try:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=get_group_keyboard())
            sent += 1
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code in (400, 403):
                unregister_chat(chat_id)
            else:
                log.error("Ошибка отправки в группу %s: %s", chat_id, e)
        except Exception as e:
            log.error("Ошибка отправки в группу %s: %s", chat_id, e)
    return sent


def send_announcement_to_groups(text, date_key):
    shown = date.fromisoformat(date_key).strftime("%d.%m")
    msg = f"📢 <b>Объявление на {shown}</b>\n\n{html.escape(text)}"
    sent = 0
    for chat_id in get_chats():
        try:
            bot.send_message(chat_id, msg, parse_mode="HTML")
            sent += 1
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code in (400, 403):
                unregister_chat(chat_id)
            else:
                log.error("Ошибка отправки объявления в группу %s: %s", chat_id, e)
        except Exception as e:
            log.error("Ошибка отправки объявления в группу %s: %s", chat_id, e)
    return sent


def notify_due_announcements():
    """Рассылка объявлений за день до даты или в день даты."""
    now = get_now()
    today_iso = now.date().isoformat()
    tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()
    with db_lock, get_db() as conn:
        rows = conn.execute(
            "SELECT id, text, date_key FROM announcements WHERE notified = 0 AND date_key <= ?",
            (tomorrow_iso,),
        ).fetchall()
    for row in rows:
        send_announcement_to_groups(row["text"], row["date_key"])
        with db_lock, get_db() as conn:
            conn.execute("UPDATE announcements SET notified = 1 WHERE id = ?", (row["id"],))


def daily_scheduler():
    while True:
        try:
            now = get_now()
            today_iso = now.date().isoformat()
            in_window = (
                (now.hour, now.minute) >= (DAILY_SEND_HOUR, DAILY_SEND_MINUTE)
                and now.hour < DAILY_WINDOW_END_HOUR
            )
            notify_due_announcements()
            if in_window and now.weekday() != 6 and get_meta("daily_sent") != today_iso:
                sent = send_schedule_to_groups()
                set_meta("daily_sent", today_iso)
                log.info("Ежедневная рассылка расписания отправлена в групп: %s", sent)
        except Exception as e:
            log.error("Ошибка ежедневной рассылки: %s", e)
        time.sleep(30)


# --- ОБРАБОТЧИКИ БОТА ---
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if message.chat.type == "private":
        send_safe(message.chat.id, "Привет! Используйте кнопки ниже:", get_private_keyboard())
    else:
        register_chat(message.chat.id, message.chat.title)
        send_safe(message.chat.id, "Привет! Кнопки ниже:", get_group_keyboard())
        send_safe(message.chat.id, "Мини-приложение группы:", get_group_inline())


@bot.my_chat_member_handler()
def on_my_chat_member(update):
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    status = update.new_chat_member.status
    if status in ("member", "administrator"):
        register_chat(chat.id, chat.title)
    elif status in ("left", "kicked"):
        unregister_chat(chat.id)


@bot.message_handler(commands=["sendtoday"])
def cmd_sendtoday(message):
    if message.from_user.id not in ADMINS:
        send_safe(message.chat.id, "У вас нет прав администратора.")
        return
    sent = send_schedule_to_groups()
    send_safe(message.chat.id, f"Расписание отправлено в групп: {sent}")


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


# --- ЗАПУСК ---
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

    try:
        purge_old_homework()
    except Exception as e:
        log.error("Первичная очистка ДЗ не удалась: %s", e)

    Thread(target=run_bot, daemon=True).start()
    Thread(target=homework_cleaner, daemon=True).start()
    Thread(target=daily_scheduler, daemon=True).start()
    log.info("Telegram бот запущен в фоне.")

    port = int(os.environ.get("PORT", 5000))
    log.info("Запуск Flask сервера на порту %s...", port)
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.exception("Веб-сервер остановился")
        notify_admins(f"🔥 Веб-сервер остановился: {type(e).__name__}: {e}", key="flask-stopped")
