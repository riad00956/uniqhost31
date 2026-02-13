#!/usr/bin/env python3
"""
Simple Yet Powerful Telegram Voting Board with User Local VPS Mode
Lightweight, beginner-friendly, modular, secure.
Run: python bot_server.py
"""

import os
import sys
import json
import uuid
import socket
import logging
import threading
import sqlite3
from datetime import datetime, timezone
from functools import wraps

# Required libraries
from dotenv import load_dotenv, set_key
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, render_template_string, session, redirect, url_for, abort

# Optional: system monitoring
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# ==================== CONFIGURATION ====================

ENV_FILE = ".env"
load_dotenv(ENV_FILE)

# Core constants
DEFAULT_CORE_KEY = "CORE-TWDHREXC288"
DEFAULT_PORT = 5000

# Instance identity – auto‑generate if missing
INSTANCE_ID = os.getenv("INSTANCE_ID")
if not INSTANCE_ID:
    INSTANCE_ID = str(uuid.uuid4())
    set_key(ENV_FILE, "INSTANCE_ID", INSTANCE_ID)

INSTANCE_SECRET = os.getenv("INSTANCE_SECRET")
if not INSTANCE_SECRET:
    INSTANCE_SECRET = str(uuid.uuid4())
    set_key(ENV_FILE, "INSTANCE_SECRET", INSTANCE_SECRET)

# User settings from .env (must be provided by user)
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", DEFAULT_PORT))
OWNER_ID = os.getenv("OWNER_ID")
CORE_KEY = os.getenv("CORE_KEY", DEFAULT_CORE_KEY)

# Validate essential settings
if not BOT_TOKEN or not OWNER_ID:
    logging.error("BOT_TOKEN and OWNER_ID must be set in .env file.")
    sys.exit(1)

try:
    OWNER_ID = int(OWNER_ID)
except ValueError:
    logging.error("OWNER_ID must be an integer (Telegram user ID).")
    sys.exit(1)

# Database per instance
DB_FILE = f"instance_{INSTANCE_ID}.db"

# Flask secret key (generate random, used for sessions)
FLASK_SECRET = uuid.uuid4().hex

# ==================== LOGGING ====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("VotingBoard")

# ==================== PORT AVAILABILITY CHECK ====================

def is_port_available(port):
    """Return True if the given TCP port is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except socket.error:
            return False

if not is_port_available(PORT):
    logger.error(f"Port {PORT} is already in use. Please choose another port in .env")
    sys.exit(1)

# ==================== DATABASE LAYER ====================
# Simple, synchronous SQLite. One connection per thread (bot + Flask).

def get_db_connection():
    """Return a new SQLite connection for the current thread."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    """Create tables if they don't exist."""
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT UNIQUE NOT NULL,
                question TEXT NOT NULL,
                options TEXT NOT NULL,          -- JSON array of strings
                created_at INTEGER NOT NULL,    -- Unix timestamp
                close_at INTEGER,              -- optional Unix timestamp
                is_active BOOLEAN NOT NULL DEFAULT 1,
                anonymous BOOLEAN NOT NULL DEFAULT 1,
                allow_vote_change BOOLEAN NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                voted_at INTEGER NOT NULL,
                FOREIGN KEY(poll_id) REFERENCES polls(poll_id) ON DELETE CASCADE,
                UNIQUE(poll_id, user_id)       -- one vote per user per poll
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                first_name TEXT,
                username TEXT,
                last_interaction INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                action TEXT NOT NULL,
                user_id INTEGER,
                details TEXT
            )
        """)
        conn.commit()
    logger.info(f"Database initialized: {DB_FILE}")

# -------------------- Poll operations --------------------
def create_poll(poll_id, question, options, close_at, anonymous, allow_vote_change, created_by):
    """Insert a new poll into the database."""
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO polls (poll_id, question, options, created_at, close_at, anonymous, allow_vote_change, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (poll_id, question, json.dumps(options), int(datetime.now().timestamp()), close_at, anonymous, allow_vote_change, created_by)
        )
        conn.commit()

def get_poll(poll_id):
    """Return a poll as dict, or None."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM polls WHERE poll_id = ?", (poll_id,)).fetchone()
    return dict(row) if row else None

def get_all_polls(active_only=True):
    """Return list of polls, optionally only active ones."""
    with get_db_connection() as conn:
        if active_only:
            rows = conn.execute("SELECT * FROM polls WHERE is_active = 1 AND (close_at IS NULL OR close_at > ?) ORDER BY created_at DESC", (int(datetime.now().timestamp()),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM polls ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

def update_poll_status(poll_id, active):
    """Activate or deactivate a poll."""
    with get_db_connection() as conn:
        conn.execute("UPDATE polls SET is_active = ? WHERE poll_id = ?", (1 if active else 0, poll_id))
        conn.commit()

def delete_poll(poll_id):
    """Remove a poll and all its votes (cascade)."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM polls WHERE poll_id = ?", (poll_id,))
        conn.commit()

def reset_votes(poll_id):
    """Delete all votes for a given poll."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM votes WHERE poll_id = ?", (poll_id,))
        conn.commit()

# -------------------- Vote operations --------------------
def add_vote(poll_id, user_id, option_index):
    """Record a vote. If vote exists and change is allowed, update it."""
    with get_db_connection() as conn:
        poll = conn.execute("SELECT allow_vote_change FROM polls WHERE poll_id = ?", (poll_id,)).fetchone()
        if not poll:
            return False, "Poll not found."
        allow_change = poll["allow_vote_change"]
        existing = conn.execute("SELECT id FROM votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)).fetchone()
        if existing:
            if not allow_change:
                return False, "You have already voted and vote change is not allowed."
            conn.execute("UPDATE votes SET option_index = ?, voted_at = ? WHERE poll_id = ? AND user_id = ?",
                         (option_index, int(datetime.now().timestamp()), poll_id, user_id))
        else:
            conn.execute("INSERT INTO votes (poll_id, user_id, option_index, voted_at) VALUES (?, ?, ?, ?)",
                         (poll_id, user_id, option_index, int(datetime.now().timestamp())))
        conn.commit()
    return True, "Vote recorded."

def get_user_vote(poll_id, user_id):
    """Return the option index voted by user, or None."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT option_index FROM votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)).fetchone()
    return row["option_index"] if row else None

def get_vote_counts(poll_id):
    """Return a dict: {option_index: count} and total votes."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT option_index, COUNT(*) as cnt FROM votes WHERE poll_id = ? GROUP BY option_index", (poll_id,)).fetchall()
    counts = {r["option_index"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    return counts, total

def get_all_votes_with_timestamps(poll_id):
    """Return list of votes with user info and timestamp (for owner)."""
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, option_index, voted_at FROM votes WHERE poll_id = ? ORDER BY voted_at DESC",
            (poll_id,)
        ).fetchall()
    return [dict(r) for r in rows]

# -------------------- User operations --------------------
def update_user_info(user_id, first_name, username):
    """Store or update user information."""
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, first_name, username, last_interaction) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username, last_interaction=excluded.last_interaction",
            (user_id, first_name, username, int(datetime.now().timestamp()))
        )
        conn.commit()

def get_active_users_count(since_hours=24):
    """Number of distinct users who interacted in the last N hours."""
    cutoff = int(datetime.now().timestamp()) - (since_hours * 3600)
    with get_db_connection() as conn:
        row = conn.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM users WHERE last_interaction > ?", (cutoff,)).fetchone()
    return row["cnt"] if row else 0

# -------------------- Logging --------------------
def log_action(action, user_id=None, details=None):
    """Insert an entry into the logs table."""
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO logs (timestamp, action, user_id, details) VALUES (?, ?, ?, ?)",
            (int(datetime.now().timestamp()), action, user_id, details)
        )
        conn.commit()

# ==================== SECURITY HELPERS ====================

def is_owner(user_id):
    """Check if the Telegram user is the configured owner."""
    return user_id == OWNER_ID

def verify_admin_credentials(core_key, instance_secret):
    """Verify credentials for web admin access."""
    return core_key == CORE_KEY and instance_secret == INSTANCE_SECRET

# ==================== TELEGRAM BOT ====================

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)  # no async

# Temporary storage for poll creation (per user)
poll_creation_data = {}

# -------------------- Keyboards --------------------
def user_main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("🗳 Vote Now"), KeyboardButton("📊 Live Polls"))
    markup.add(KeyboardButton("📈 Results"), KeyboardButton("ℹ️ Poll Info"))
    return markup

def owner_main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("➕ Create Poll"), KeyboardButton("⚙️ Manage Polls"))
    markup.add(KeyboardButton("🗑 Delete Poll"), KeyboardButton("🔄 Reset Votes"))
    markup.add(KeyboardButton("📤 Export Data"), KeyboardButton("📟 System Info"))
    markup.add(KeyboardButton("🛑 Shutdown"))
    return markup

def poll_list_keyboard(polls, action_prefix):
    """Generate inline keyboard with poll buttons."""
    markup = InlineKeyboardMarkup()
    for p in polls:
        markup.add(InlineKeyboardButton(p["question"][:30], callback_data=f"{action_prefix}:{p['poll_id']}"))
    return markup

# -------------------- Handlers --------------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_id = message.from_user.id
    update_user_info(user_id, message.from_user.first_name, message.from_user.username)
    bot.send_message(message.chat.id,
                     f"Welcome to the Voting Board (Instance: {INSTANCE_ID[:8]}).\n"
                     "Use the buttons below to navigate.",
                     reply_markup=owner_main_keyboard() if is_owner(user_id) else user_main_keyboard())
    log_action("/start", user_id)

# ----- User actions -----
@bot.message_handler(func=lambda m: m.text == "🗳 Vote Now")
def vote_now(message):
    user_id = message.from_user.id
    polls = get_all_polls(active_only=True)
    if not polls:
        bot.reply_to(message, "No active polls at the moment.")
        return
    markup = InlineKeyboardMarkup()
    for p in polls:
        # check if user already voted
        voted = get_user_vote(p["poll_id"], user_id)
        status = "✅" if voted else "⭕"
        markup.add(InlineKeyboardButton(f"{status} {p['question'][:30]}", callback_data=f"vote:{p['poll_id']}"))
    bot.send_message(message.chat.id, "Select a poll to vote:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "📊 Live Polls")
def live_polls(message):
    """Show all active polls with live counts (public)."""
    polls = get_all_polls(active_only=True)
    if not polls:
        bot.reply_to(message, "No active polls.")
        return
    text = "📊 **Live Polls**\n\n"
    for p in polls:
        counts, total = get_vote_counts(p["poll_id"])
        text += f"**{p['question']}**\n"
        options = json.loads(p["options"])
        for idx, opt in enumerate(options):
            count = counts.get(idx, 0)
            text += f"• {opt}: {count} votes\n"
        text += f"Total: {total} vote(s)\n"
        if p["close_at"]:
            close_time = datetime.fromtimestamp(p["close_at"]).strftime("%Y-%m-%d %H:%M")
            text += f"Closes: {close_time}\n"
        text += "\n"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "📈 Results")
def results(message):
    """Show closed polls results or any poll final results."""
    polls = get_all_polls(active_only=False)
    if not polls:
        bot.reply_to(message, "No polls available.")
        return
    markup = poll_list_keyboard(polls, "results")
    bot.send_message(message.chat.id, "Select a poll to see results:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "ℹ️ Poll Info")
def poll_info(message):
    polls = get_all_polls(active_only=False)
    if not polls:
        bot.reply_to(message, "No polls.")
        return
    markup = poll_list_keyboard(polls, "info")
    bot.send_message(message.chat.id, "Select a poll for details:", reply_markup=markup)

# ----- Owner actions -----
@bot.message_handler(func=lambda m: is_owner(m.from_user.id) and m.text == "➕ Create Poll")
def create_poll_start(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    poll_creation_data[user_id] = {}
    bot.send_message(chat_id, "Enter the poll question:")
    bot.register_next_step_handler(message, process_poll_question)

def process_poll_question(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if user_id not in poll_creation_data:
        return
    poll_creation_data[user_id]['question'] = message.text
    bot.send_message(chat_id, "Enter poll options, one per line:\nExample:\nYes\nNo\nMaybe")
    bot.register_next_step_handler(message, process_poll_options)

def process_poll_options(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if user_id not in poll_creation_data:
        return
    options = [line.strip() for line in message.text.split('\n') if line.strip()]
    if len(options) < 2:
        bot.send_message(chat_id, "At least two options required. Try again.")
        bot.register_next_step_handler(message, process_poll_options)
        return
    poll_creation_data[user_id]['options'] = options
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Skip (no close time)", callback_data="close:skip"))
    bot.send_message(chat_id, "Enter close time in format YYYY-MM-DD HH:MM (24h) or skip:", reply_markup=markup)
    bot.register_next_step_handler(message, process_poll_close)

def process_poll_close(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if user_id not in poll_creation_data:
        return
    text = message.text.strip()
    close_ts = None
    if text.lower() != "skip":
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
            close_ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            bot.send_message(chat_id, "Invalid format. Use YYYY-MM-DD HH:MM or Skip.")
            bot.register_next_step_handler(message, process_poll_close)
            return
    poll_creation_data[user_id]['close_at'] = close_ts
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Anonymous", callback_data="anon:1"),
               InlineKeyboardButton("Public", callback_data="anon:0"))
    bot.send_message(chat_id, "Vote anonymity?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("anon:"))
def set_anon(call):
    user_id = call.from_user.id
    if user_id not in poll_creation_data:
        bot.answer_callback_query(call.id, "Session expired.")
        return
    anon = int(call.data.split(":")[1]) == 1
    poll_creation_data[user_id]['anonymous'] = anon
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Yes", callback_data="change:1"),
               InlineKeyboardButton("No", callback_data="change:0"))
    bot.edit_message_text("Allow users to change their vote?", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("change:"))
def set_change(call):
    user_id = call.from_user.id
    if user_id not in poll_creation_data:
        bot.answer_callback_query(call.id, "Session expired.")
        return
    allow_change = int(call.data.split(":")[1]) == 1
    poll_creation_data[user_id]['allow_vote_change'] = allow_change
    # generate unique poll id
    poll_id = str(uuid.uuid4())[:8]
    data = poll_creation_data.pop(user_id)
    create_poll(
        poll_id=poll_id,
        question=data['question'],
        options=data['options'],
        close_at=data.get('close_at'),
        anonymous=data.get('anonymous', True),
        allow_vote_change=allow_change,
        created_by=user_id
    )
    log_action("create_poll", user_id, f"poll_id={poll_id}")
    bot.edit_message_text(f"✅ Poll created successfully!\nPoll ID: `{poll_id}`\nShare this ID for voting via board.",
                          call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: is_owner(m.from_user.id) and m.text == "⚙️ Manage Polls")
def manage_polls(message):
    polls = get_all_polls(active_only=False)
    if not polls:
        bot.reply_to(message, "No polls.")
        return
    markup = poll_list_keyboard(polls, "manage")
    bot.send_message(message.chat.id, "Select a poll to manage:", reply_markup=markup)

@bot.message_handler(func=lambda m: is_owner(m.from_user.id) and m.text == "🗑 Delete Poll")
def delete_poll_prompt(message):
    polls = get_all_polls(active_only=False)
    if not polls:
        bot.reply_to(message, "No polls.")
        return
    markup = poll_list_keyboard(polls, "delete")
    bot.send_message(message.chat.id, "Select poll to DELETE:", reply_markup=markup)

@bot.message_handler(func=lambda m: is_owner(m.from_user.id) and m.text == "🔄 Reset Votes")
def reset_votes_prompt(message):
    polls = get_all_polls(active_only=False)
    if not polls:
        bot.reply_to(message, "No polls.")
        return
    markup = poll_list_keyboard(polls, "reset")
    bot.send_message(message.chat.id, "Select poll to reset votes:", reply_markup=markup)

@bot.message_handler(func=lambda m: is_owner(m.from_user.id) and m.text == "📤 Export Data")
def export_data(message):
    """Send the SQLite database file to the owner."""
    try:
        with open(DB_FILE, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"Instance {INSTANCE_ID} database export.")
        log_action("export_db", message.from_user.id)
    except Exception as e:
        bot.reply_to(message, f"Export failed: {e}")

@bot.message_handler(func=lambda m: is_owner(m.from_user.id) and m.text == "📟 System Info")
def system_info(message):
    """Show instance info, active users, resource usage."""
    info = f"🖥 **Instance Info**\n"
    info += f"ID: `{INSTANCE_ID}`\n"
    info += f"Secret: `{INSTANCE_SECRET[:8]}...`\n"
    info += f"Port: {PORT}\n"
    info += f"DB: {DB_FILE}\n\n"
    info += f"📊 **Statistics**\n"
    with get_db_connection() as conn:
        total_polls = conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0]
        total_votes = conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
        total_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM users").fetchone()[0]
    info += f"Total polls: {total_polls}\n"
    info += f"Total votes: {total_votes}\n"
    info += f"Total users: {total_users}\n"
    info += f"Active users (24h): {get_active_users_count()}\n"
    if PSUTIL_AVAILABLE:
        info += f"\n🖥 **System**\n"
        info += f"CPU: {psutil.cpu_percent()}%\n"
        info += f"RAM: {psutil.virtual_memory().percent}%\n"
    bot.send_message(message.chat.id, info, parse_mode="Markdown")

@bot.message_handler(func=lambda m: is_owner(m.from_user.id) and m.text == "🛑 Shutdown")
def shutdown(message):
    bot.reply_to(message, "Shutting down bot and web server...")
    log_action("shutdown", message.from_user.id)
    # Force exit after a short delay
    threading.Timer(2.0, lambda: os._exit(0)).start()
    bot.stop_polling()

# ----- Inline callback handlers -----
@bot.callback_query_handler(func=lambda call: True)
def handle_inline(call):
    user_id = call.from_user.id
    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data.startswith("vote:"):
        poll_id = data.split(":")[1]
        poll = get_poll(poll_id)
        if not poll or not poll["is_active"]:
            bot.answer_callback_query(call.id, "Poll is not active.", show_alert=True)
            return
        options = json.loads(poll["options"])
        markup = InlineKeyboardMarkup()
        for idx, opt in enumerate(options):
            markup.add(InlineKeyboardButton(opt, callback_data=f"select:{poll_id}:{idx}"))
        bot.edit_message_text(f"🗳 {poll['question']}\nChoose your option:", chat_id, msg_id, reply_markup=markup)

    elif data.startswith("select:"):
        _, poll_id, opt_idx = data.split(":")
        opt_idx = int(opt_idx)
        poll = get_poll(poll_id)
        if not poll or not poll["is_active"]:
            bot.answer_callback_query(call.id, "Poll closed.", show_alert=True)
            return
        success, msg = add_vote(poll_id, user_id, opt_idx)
        bot.answer_callback_query(call.id, msg, show_alert=not success)
        if success:
            log_action("vote", user_id, f"poll={poll_id}, option={opt_idx}")
            # Show updated results if anonymous; else show nothing
            if poll["anonymous"]:
                counts, total = get_vote_counts(poll_id)
                options = json.loads(poll["options"])
                text = f"📊 {poll['question']}\n"
                for i, opt in enumerate(options):
                    text += f"• {opt}: {counts.get(i, 0)} votes\n"
                text += f"Total: {total} votes"
                bot.edit_message_text(text, chat_id, msg_id)

    elif data.startswith("results:"):
        poll_id = data.split(":")[1]
        poll = get_poll(poll_id)
        if not poll:
            bot.answer_callback_query(call.id, "Poll not found.")
            return
        counts, total = get_vote_counts(poll_id)
        options = json.loads(poll["options"])
        text = f"📈 **{poll['question']}**\n"
        for i, opt in enumerate(options):
            text += f"• {opt}: {counts.get(i, 0)} votes\n"
        text += f"**Total:** {total} votes\n"
        if poll["close_at"]:
            text += f"Closed: {datetime.fromtimestamp(poll['close_at']).strftime('%Y-%m-%d %H:%M')}"
        bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown")

    elif data.startswith("info:"):
        poll_id = data.split(":")[1]
        poll = get_poll(poll_id)
        if not poll:
            bot.answer_callback_query(call.id, "Not found.")
            return
        status = "✅ Active" if poll["is_active"] else "❌ Closed"
        anon = "Anonymous" if poll["anonymous"] else "Public"
        change = "Allowed" if poll["allow_vote_change"] else "Not allowed"
        text = f"ℹ️ **Poll Info**\n"
        text += f"ID: `{poll['poll_id']}`\n"
        text += f"Question: {poll['question']}\n"
        text += f"Status: {status}\n"
        text += f"Anonymity: {anon}\n"
        text += f"Vote change: {change}\n"
        if poll["close_at"]:
            text += f"Close time: {datetime.fromtimestamp(poll['close_at']).strftime('%Y-%m-%d %H:%M')}"
        bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown")

    # Owner management actions
    elif data.startswith("manage:"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "Owner only.")
            return
        poll_id = data.split(":")[1]
        poll = get_poll(poll_id)
        if not poll:
            bot.answer_callback_query(call.id, "No poll.")
            return
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("❌ Suspend/Activate", callback_data=f"toggle:{poll_id}"))
        markup.add(InlineKeyboardButton("🔄 Reset Votes", callback_data=f"reset:{poll_id}"))
        markup.add(InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{poll_id}"))
        bot.edit_message_text(f"Manage: {poll['question']}", chat_id, msg_id, reply_markup=markup)

    elif data.startswith("toggle:"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "Owner only.")
            return
        poll_id = data.split(":")[1]
        poll = get_poll(poll_id)
        if poll:
            new_state = not poll["is_active"]
            update_poll_status(poll_id, new_state)
            log_action("toggle_poll", user_id, f"poll={poll_id}, active={new_state}")
            bot.answer_callback_query(call.id, f"Poll {'activated' if new_state else 'suspended'}.", show_alert=True)
        bot.delete_message(chat_id, msg_id)

    elif data.startswith("reset:"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "Owner only.")
            return
        poll_id = data.split(":")[1]
        reset_votes(poll_id)
        log_action("reset_votes", user_id, f"poll={poll_id}")
        bot.answer_callback_query(call.id, "All votes reset.", show_alert=True)

    elif data.startswith("delete:"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "Owner only.")
            return
        poll_id = data.split(":")[1]
        delete_poll(poll_id)
        log_action("delete_poll", user_id, f"poll={poll_id}")
        bot.answer_callback_query(call.id, "Poll deleted.", show_alert=True)
        bot.delete_message(chat_id, msg_id)

    elif data == "close:skip":
        bot.edit_message_text("No close time set. Now choose anonymity:", chat_id, msg_id)
        # Re-prompt anonymity using same logic
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Anonymous", callback_data="anon:1"),
                   InlineKeyboardButton("Public", callback_data="anon:0"))
        bot.send_message(chat_id, "Vote anonymity?", reply_markup=markup)

# ==================== FLASK WEB BOARD ====================

app = Flask(__name__)
app.secret_key = FLASK_SECRET

# Simple HTML templates (inline for zero dependencies)
BASE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Voting Board - Instance {instance}</title>
    <meta charset="utf-8">
    <style>
        body { font-family: Arial, sans-serif; max-width: 800px; margin: auto; padding: 20px; }
        .poll { border: 1px solid #ddd; padding: 15px; margin-bottom: 20px; border-radius: 5px; }
        .vote-bar { background-color: #4CAF50; height: 20px; color: white; padding: 2px 5px; }
        .admin { background: #f9f9f9; padding: 10px; border-left: 3px solid #f44336; }
        .button { background: #008CBA; color: white; padding: 8px 12px; text-decoration: none; border-radius: 3px; }
    </style>
</head>
<body>
    <h1>🗳 Voting Board (Instance: {instance})</h1>
    {content}
</body>
</html>
"""

@app.route('/')
def index():
    """Public board: list all active polls with current results."""
    polls = get_all_polls(active_only=True)
    html = "<h2>Active Polls</h2>"
    if not polls:
        html += "<p>No active polls.</p>"
    else:
        for p in polls:
            html += f"<div class='poll'><h3>{p['question']}</h3>"
            counts, total = get_vote_counts(p['poll_id'])
            options = json.loads(p['options'])
            for idx, opt in enumerate(options):
                count = counts.get(idx, 0)
                percent = (count / total * 100) if total > 0 else 0
                html += f"<p><strong>{opt}</strong>: {count} votes</p>"
                html += f"<div class='vote-bar' style='width: {percent}%;'>{percent:.1f}%</div>"
            html += f"<p>Total votes: {total}</p>"
            if p['close_at']:
                html += f"<p>⏳ Closes: {datetime.fromtimestamp(p['close_at']).strftime('%Y-%m-%d %H:%M')}</p>"
            html += "</div>"
    return BASE_HTML.format(instance=INSTANCE_ID[:8], content=html)

@app.route('/poll/<poll_id>')
def poll_detail(poll_id):
    """Detailed results for a specific poll (even if closed)."""
    poll = get_poll(poll_id)
    if not poll:
        return "Poll not found", 404
    counts, total = get_vote_counts(poll_id)
    options = json.loads(poll['options'])
    html = f"<h2>{poll['question']}</h2>"
    html += f"<p>Status: {'✅ Active' if poll['is_active'] else '❌ Closed'}</p>"
    html += f"<p>Type: {'Anonymous' if poll['anonymous'] else 'Public'}</p>"
    for idx, opt in enumerate(options):
        count = counts.get(idx, 0)
        percent = (count / total * 100) if total > 0 else 0
        html += f"<p><strong>{opt}</strong>: {count} votes</p>"
        html += f"<div class='vote-bar' style='width: {percent}%;'>{percent:.1f}%</div>"
    html += f"<p>Total votes: {total}</p>"
    if poll['close_at']:
        html += f"<p>Closes: {datetime.fromtimestamp(poll['close_at']).strftime('%Y-%m-%d %H:%M')}</p>"
    return BASE_HTML.format(instance=INSTANCE_ID[:8], content=html)

# -------------------- Admin Panel --------------------
@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    """Simple login form asking for Core Key and Instance Secret."""
    if request.method == 'POST':
        core = request.form.get('core_key')
        secret = request.form.get('instance_secret')
        if verify_admin_credentials(core, secret):
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            return "<h2>Invalid credentials</h2><a href='/admin'>Try again</a>"
    html = """
    <h2>Admin Login</h2>
    <form method="post">
        <label>Core Key:</label><br>
        <input type="password" name="core_key"><br>
        <label>Instance Secret:</label><br>
        <input type="password" name="instance_secret"><br><br>
        <input type="submit" value="Login" class="button">
    </form>
    """
    return BASE_HTML.format(instance=INSTANCE_ID[:8], content=html)

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    """Admin overview with management options."""
    polls = get_all_polls(active_only=False)
    html = "<h2>Admin Dashboard</h2>"
    html += "<div class='admin'>"
    html += f"<p>Instance ID: {INSTANCE_ID}</p>"
    html += f"<p>Total polls: {len(polls)}</p>"
    html += "<h3>Polls</h3><ul>"
    for p in polls:
        html += f"<li>{p['question']} - "
        html += f"<a href='/admin/poll/{p['poll_id']}/delete'>Delete</a> | "
        html += f"<a href='/admin/poll/{p['poll_id']}/reset'>Reset votes</a> | "
        html += f"<a href='/admin/poll/{p['poll_id']}/toggle'>{'Suspend' if p['is_active'] else 'Activate'}</a></li>"
    html += "</ul>"
    html += "<hr>"
    html += f"<p><a href='/admin/export' class='button'>📥 Export Database</a></p>"
    html += f"<p><a href='/admin/shutdown' class='button' style='background:#f44336;' onclick='return confirm(\"Shutdown entire system?\")'>🛑 Shutdown Server</a></p>"
    html += "</div>"
    return BASE_HTML.format(instance=INSTANCE_ID[:8], content=html)

@app.route('/admin/poll/<poll_id>/delete')
@admin_required
def admin_delete_poll(poll_id):
    delete_poll(poll_id)
    log_action("admin_delete_poll", details=f"poll={poll_id}")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/poll/<poll_id>/reset')
@admin_required
def admin_reset_poll(poll_id):
    reset_votes(poll_id)
    log_action("admin_reset_votes", details=f"poll={poll_id}")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/poll/<poll_id>/toggle')
@admin_required
def admin_toggle_poll(poll_id):
    poll = get_poll(poll_id)
    if poll:
        update_poll_status(poll_id, not poll["is_active"])
        log_action("admin_toggle_poll", details=f"poll={poll_id}")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/export')
@admin_required
def admin_export():
    """Send the SQLite file as download."""
    from flask import send_file
    log_action("admin_export_db")
    return send_file(DB_FILE, as_attachment=True, download_name=f"instance_{INSTANCE_ID}.db")

@app.route('/admin/shutdown')
@admin_required
def admin_shutdown():
    log_action("admin_shutdown")
    # Shutdown after short delay
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return "<h2>Shutting down...</h2><p>You can close this window.</p>"

# ==================== MAIN LAUNCHER ====================

def run_flask():
    """Start Flask development server on the specified port."""
    logger.info(f"Starting Flask board on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)

def run_bot():
    """Start Telegram bot polling (blocking)."""
    logger.info("Starting Telegram bot...")
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.error(f"Bot polling error: {e}")

def main():
    """Initialize everything and run both services concurrently."""
    init_database()
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    # Run Flask in main thread
    run_flask()

if __name__ == "__main__":
    main()
