
import os
import json
import time
import uuid
import sqlite3
import logging
from pathlib import Path
from threading import Thread
from datetime import datetime, timedelta

import requests
import pytz
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# IMPORTANT: set MAX_BOT_TOKEN in your hosting environment variables.
# Do not put the bot token directly in this source file.
TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
BASE_URL = "https://platform-api2.max.ru"
MY_CHAT_ID = os.environ.get("MY_CHAT_ID", "505757849")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app"))
DB_PATH = os.environ.get("DB_PATH", str(DATA_DIR / "data.db"))
UPLOAD_FOLDER = Path(os.environ.get("UPLOAD_FOLDER", str(DATA_DIR / "uploads")))
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB request limit
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("neprostopost")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
CORS(app, resources={r"/*": {"origins": "*"}}, methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization"])

scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
scheduler.start()


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY, title TEXT, members_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Активна')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS catalog (
            id TEXT PRIMARY KEY, title TEXT, desc TEXT, price TEXT,
            img_paths TEXT, tag TEXT, address TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, chat_id TEXT, text TEXT, time TEXT,
            is_daily INTEGER DEFAULT 1, img_paths TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY, admin_chat_id TEXT, name TEXT, phone TEXT,
            text TEXT, schedule_time TEXT, status TEXT,
            type TEXT DEFAULT 'Звонок')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY, trial_end TEXT, is_paid INTEGER DEFAULT 0)""")

        # Safe migration: add category without deleting existing catalog records.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(catalog)").fetchall()}
        if "category" not in cols:
            conn.execute("ALTER TABLE catalog ADD COLUMN category TEXT DEFAULT 'Новостройка'")
        if "created_at" not in cols:
            # No need to add created_at for functionality; intentionally leave schema minimal.
            pass
        conn.commit()


init_db()


def max_headers():
    if not TOKEN:
        raise RuntimeError("Не задан MAX_BOT_TOKEN в переменных окружения хостинга")
    return {"Authorization": TOKEN}


def max_request(method, endpoint, **kwargs):
    """Make a verified-TLS request to MAX; return requests.Response."""
    headers = kwargs.pop("headers", None) or max_headers()
    timeout = kwargs.pop("timeout", 25)
    return requests.request(method, f"{BASE_URL}{endpoint}", headers=headers, timeout=timeout, **kwargs)
