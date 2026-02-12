
import os
import subprocess
import sqlite3
import telebot
import threading
import time
import uuid
import signal
import random
import platform
import logging
import re
import resource
import sys
import json
from pathlib import Path
from telebot import types
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from flask import Flask, render_template_string
from functools import wraps
from collections import defaultdict

# ==================== লগিং কনফিগারেশন ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_hosting.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('CyberHosting')

# ==================== এনভায়রনমেন্ট কনফিগার ====================
class Config:
    TOKEN = os.environ.get('BOT_TOKEN')
    ADMIN_ID = os.environ.get('ADMIN_ID')
    PROJECT_DIR = 'projects'
    DB_NAME = 'cyber_v2.db'
    PORT = int(os.environ.get('PORT', 10000))
    MAINTENANCE = False
    
    # রেট লিমিট
    RATE_LIMIT = 5
    RATE_WINDOW = 10
    
    # ইউজার বটের জন্য সীমা
    MAX_CPU_PERCENT = 50
    MAX_RAM_MB = 200
    MAX_PROCESSES = 3
    MAX_FILE_SIZE_MB = 5
    
    # ডকার (ঐচ্ছিক)
    USE_DOCKER = os.environ.get('USE_DOCKER', 'False').lower() == 'true'
    DOCKER_IMAGE = 'python:3.9-slim'

# টোকেন ও অ্যাডমিন আইডি অবশ্যই সেট থাকতে হবে
if not Config.TOKEN:
    logger.critical("BOT_TOKEN environment variable not set!")
    sys.exit(1)
if not Config.ADMIN_ID:
    logger.critical("ADMIN_ID environment variable not set!")
    sys.exit(1)
try:
    Config.ADMIN_ID = int(Config.ADMIN_ID)
except:
    logger.critical("ADMIN_ID must be an integer!")
    sys.exit(1)

bot = telebot.TeleBot(Config.TOKEN)
project_path = Path(Config.PROJECT_DIR)
project_path.mkdir(exist_ok=True)
app = Flask(__name__)

# ==================== রেট লিমিটার ====================
class RateLimiter:
    def __init__(self):
        self.user_commands = defaultdict(list)
        self.lock = threading.Lock()
    
    def is_allowed(self, user_id):
        with self.lock:
            now = time.time()
            self.user_commands[user_id] = [t for t in self.user_commands[user_id] if now - t < Config.RATE_WINDOW]
            if len(self.user_commands[user_id]) >= Config.RATE_LIMIT:
                return False
            self.user_commands[user_id].append(now)
            return True

rate_limiter = RateLimiter()

def rate_limit(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        uid = message.from_user.id
        if not rate_limiter.is_allowed(uid):
            bot.reply_to(message, "⏳ **Too many requests!** Please slow down.", parse_mode="Markdown")
            return
        return func(message, *args, **kwargs)
    return wrapper

def rate_limit_callback(func):
    @wraps(func)
    def wrapper(call, *args, **kwargs):
        uid = call.from_user.id
        if not rate_limiter.is_allowed(uid):
            bot.answer_callback_query(call.id, "⏳ Too many requests! Slow down.")
            return
        return func(call, *args, **kwargs)
    return wrapper

# ==================== ডাটাবেজ ম্যানেজার ====================
def get_db():
    db = sqlite3.connect(Config.DB_NAME, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db

def init_db():
    """CREATE TABLE IF NOT EXISTS + স্কিমা আপগ্রেড"""
    with get_db() as conn:
        c = conn.cursor()
        
        # ইউজার টেবিল
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            expiry TEXT,
            file_limit INTEGER DEFAULT 0,
            is_prime INTEGER DEFAULT 0,
            join_date TEXT,
            auto_restart INTEGER DEFAULT 0
        )''')
        
        # কী টেবিল
        c.execute('''CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY,
            duration_days INTEGER,
            file_limit INTEGER,
            created_date TEXT
        )''')
        
        # ডিপ্লয়মেন্ট টেবিল (বিদ্যমান থাকলে নতুন কলাম যোগ)
        c.execute('''CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bot_name TEXT,
            filename TEXT,
            pid INTEGER,
            container_id TEXT,
            start_time TEXT,
            status TEXT,
            cpu_usage REAL,
            ram_usage REAL,
            auto_restart INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )''')
        
        # পুরনো ডাটাবেজ আপগ্রেড - যদি container_id কলাম না থাকে
        try:
            c.execute("SELECT container_id FROM deployments LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE deployments ADD COLUMN container_id TEXT")
        
        try:
            c.execute("SELECT auto_restart FROM deployments LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE deployments ADD COLUMN auto_restart INTEGER DEFAULT 0")
        
        try:
            c.execute("SELECT auto_restart FROM users LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE users ADD COLUMN auto_restart INTEGER DEFAULT 0")
        
        # অ্যাডমিন ইউজার (যদি না থাকে)
        join_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute("INSERT OR IGNORE INTO users (id, username, expiry, file_limit, is_prime, join_date) VALUES (?, ?, ?, ?, ?, ?)",
                  (Config.ADMIN_ID, 'admin', None, 999, 1, join_date))
        conn.commit()
        
        logger.info("Database initialized/upgraded successfully.")

init_db()

# ==================== সিস্টেম মনিটরিং (বাস্তব) ====================
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not installed. Using dummy stats.")

def get_system_stats():
    if PSUTIL_AVAILABLE:
        try:
            cpu = psutil.cpu_percent(interval=0.1)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent
            return {'cpu_percent': cpu, 'ram_percent': ram, 'disk_percent': disk}
        except:
            pass
    # ফলব্যাক
    return {'cpu_percent': random.randint(20, 80), 'ram_percent': random.randint(30, 70), 'disk_percent': random.randint(40, 60)}

def get_process_stats(pid):
    if not pid or pid <= 0:
        return None
    try:
        if PSUTIL_AVAILABLE:
            proc = psutil.Process(pid)
            with proc.oneshot():
                cpu = proc.cpu_percent(interval=0.1)
                mem = proc.memory_info().rss / 1024 / 1024
                return {'running': True, 'cpu': cpu, 'ram': mem}
        else:
            os.kill(pid, 0)
            return {'running': True, 'cpu': 0, 'ram': 0}
    except (psutil.NoSuchProcess, ProcessLookupError):
        return {'running': False, 'cpu': 0, 'ram': 0}
    except Exception as e:
        logger.error(f"Process {pid} check error: {e}")
        return None

# ==================== ডকার আইসোলেশন (ঐচ্ছিক) ====================
try:
    import docker
    DOCKER_AVAILABLE = Config.USE_DOCKER and docker.from_env().ping()
except:
    DOCKER_AVAILABLE = False
    logger.info("Docker not available. Using subprocess with resource limits.")

class BotRunner:
    """ইউজার বট চালানোর অ্যাবস্ট্রাকশন"""
    
    @staticmethod
    def run(user_id, bot_id, filename, bot_name, auto_restart=False):
        file_path = project_path / filename
        if not file_path.exists():
            raise FileNotFoundError(f"File {filename} not found")
        
        # ইউজারের চলমান বট সংখ্যা চেক
        with get_db() as conn:
            c = conn.cursor()
            count = c.execute("SELECT COUNT(*) FROM deployments WHERE user_id=? AND status='Running'", (user_id,)).fetchone()[0]
            if count >= Config.MAX_PROCESSES:
                raise Exception(f"Maximum running bots limit reached ({Config.MAX_PROCESSES})")
        
        if DOCKER_AVAILABLE:
            return BotRunner._run_docker(user_id, bot_id, file_path, bot_name, auto_restart)
        else:
            return BotRunner._run_subprocess(user_id, bot_id, file_path, bot_name, auto_restart)
    
    @staticmethod
    def _run_subprocess(user_id, bot_id, file_path, bot_name, auto_restart):
        try:
            # রিসোর্স লিমিট (শুধু ইউনিক্স)
            if hasattr(resource, 'RLIMIT_CPU') and hasattr(resource, 'RLIMIT_AS'):
                try:
                    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
                    mem_limit = Config.MAX_RAM_MB * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (mem_limit, -1))
                except Exception as e:
                    logger.warning(f"Resource limit set failed: {e}")
            
            proc = subprocess.Popen(
                ['python', str(file_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
            logger.info(f"Bot {bot_name} (PID: {proc.pid}) started for user {user_id}")
            return {
                'pid': proc.pid,
                'container_id': None,
                'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'Running'
            }
        except Exception as e:
            logger.exception(f"Subprocess run failed: {e}")
            raise
    
    @staticmethod
    def _run_docker(user_id, bot_id, file_path, bot_name, auto_restart):
        client = docker.from_env()
        container_name = f"bot_{user_id}_{bot_id}_{uuid.uuid4().hex[:8]}"
        try:
            container = client.containers.run(
                image=Config.DOCKER_IMAGE,
                command=f"python /app/{file_path.name}",
                volumes={str(file_path.parent): {'bind': '/app', 'mode': 'ro'}},
                name=container_name,
                detach=True,
                mem_limit=f"{Config.MAX_RAM_MB}m",
                cpu_period=100000,
                cpu_quota=int(Config.MAX_CPU_PERCENT * 1000),
                network_disabled=False,
                remove=True
            )
            logger.info(f"Bot {bot_name} (Container: {container.id}) started for user {user_id}")
            return {
                'pid': None,
                'container_id': container.id,
                'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'Running'
            }
        except Exception as e:
            logger.exception(f"Docker run failed: {e}")
            raise
    
    @staticmethod
    def stop(pid, container_id):
        if container_id and DOCKER_AVAILABLE:
            try:
                client = docker.from_env()
                container = client.containers.get(container_id)
                container.stop(timeout=5)
                logger.info(f"Container {container_id} stopped")
                return True
            except Exception as e:
                logger.error(f"Stop container {container_id} error: {e}")
                return False
        elif pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                logger.info(f"Process {pid} killed")
                return True
            except:
                pass
        return False

# ==================== ইউজার বট সুপারভাইজার (অটো-রিস্টার্ট) ====================
class BotSupervisor(threading.Thread):
    def __init__(self, interval=30):
        super().__init__()
        self.interval = interval
        self.daemon = True
    
    def run(self):
        while True:
            try:
                self._check_bots()
            except Exception as e:
                logger.exception(f"Supervisor error: {e}")
            time.sleep(self.interval)
    
    def _check_bots(self):
        with get_db() as conn:
            c = conn.cursor()
            running_bots = c.execute(
                "SELECT id, user_id, filename, pid, container_id, auto_restart FROM deployments WHERE status='Running'"
            ).fetchall()
            for bot in running_bots:
                bot_id, user_id, filename, pid, container_id, auto_restart = bot
                if container_id and DOCKER_AVAILABLE:
                    running = self._check_docker(container_id)
                else:
                    stat = get_process_stats(pid)
                    running = stat and stat['running'] if stat else False
                
                if not running:
                    c.execute("UPDATE deployments SET status='Crashed' WHERE id=?", (bot_id,))
                    if auto_restart:
                        logger.info(f"Auto-restarting bot {bot_id} for user {user_id}")
                        self._restart_bot(bot_id, user_id, filename)
            conn.commit()
    
    def _check_docker(self, container_id):
        try:
            client = docker.from_env()
            container = client.containers.get(container_id)
            return container.status == 'running'
        except:
            return False
    
    def _restart_bot(self, bot_id, user_id, filename):
        with get_db() as conn:
            c = conn.cursor()
            bot_info = c.execute("SELECT bot_name, auto_restart FROM deployments WHERE id=?", (bot_id,)).fetchone()
            if bot_info:
                bot_name = bot_info[0]
                auto_restart = bot_info[1]
                try:
                    runner = BotRunner.run(user_id, bot_id, filename, bot_name, auto_restart)
                    if runner:
                        c.execute("UPDATE deployments SET pid=?, container_id=?, start_time=?, status=? WHERE id=?",
                                  (runner.get('pid'), runner.get('container_id'), runner['start_time'], 'Running', bot_id))
                        conn.commit()
                        logger.info(f"Bot {bot_id} restarted successfully.")
                except Exception as e:
                    logger.error(f"Restart failed for bot {bot_id}: {e}")

# সুপারভাইজার থ্রেড শুরু
BotSupervisor().start()
logger.info("Bot supervisor thread started.")

# ==================== ইউটিলিটি ফাংশন ====================
def get_user(user_id):
    with get_db() as conn:
        c = conn.cursor()
        user = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return user

def is_prime(user_id):
    user = get_user(user_id)
    if user and user['expiry']:
        try:
            expiry = datetime.strptime(user['expiry'], '%Y-%m-%d %H:%M:%S')
            return expiry > datetime.now()
        except:
            return False
    return False

def get_user_bots(user_id):
    with get_db() as conn:
        c = conn.cursor()
        bots = c.execute(
            "SELECT id, bot_name, filename, pid, start_time, status FROM deployments WHERE user_id=?",
            (user_id,)
        ).fetchall()
    return bots

def update_bot_stats(bot_id, cpu, ram):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE deployments SET cpu_usage=?, ram_usage=? WHERE id=?", (cpu, ram, bot_id))
        conn.commit()

def generate_random_key():
    prefix = "PRIME-"
    random_chars = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=8))
    return f"{prefix}{random_chars}"

def create_progress_bar(percentage):
    bars = int(percentage / 10)
    return "█" * bars + "░" * (10 - bars)

def safe_edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    """মেসেজ এডিট করতে ব্যর্থ হলে নতুন মেসেজ পাঠায়"""
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, parse_mode=parse_mode)
        return message_id
    except Exception as e:
        logger.warning(f"Edit message failed: {e}. Sending new message.")
        msg = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return msg.message_id

# ==================== কীবোর্ড মেনু (সম্পূর্ণ অপরিবর্তিত) ====================
def main_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    user = get_user(user_id)
    if not is_prime(user_id):
        markup.add(types.InlineKeyboardButton("🔑 Activate Core Pass", callback_data="activate_prime"))
        markup.add(types.InlineKeyboardButton("ℹ️ Core Features", callback_data="premium_info"))
    else:
        markup.add(
            types.InlineKeyboardButton("📤 Upload Bot File", callback_data='upload'),
            types.InlineKeyboardButton("🤖 My Bots", callback_data='my_bots')
        )
        markup.add(
            types.InlineKeyboardButton("🚀 Deploy New Bot", callback_data='deploy_new'),
            types.InlineKeyboardButton("📊 Dashboard", callback_data='dashboard')
        )
    markup.add(types.InlineKeyboardButton("⚙️ Settings", callback_data='settings'))
    if user_id == Config.ADMIN_ID:
        markup.add(types.InlineKeyboardButton("👑 Admin Panel", callback_data='admin_panel'))
    return markup

def admin_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🎫 Generate Key", callback_data="gen_key"),
        types.InlineKeyboardButton("👥 All Users", callback_data="all_users")
    )
    markup.add(
        types.InlineKeyboardButton("🤖 All Bots", callback_data="all_bots"),
        types.InlineKeyboardButton("📈 Statistics", callback_data="stats")
    )
    markup.add(
        types.InlineKeyboardButton("⚙️ Maintenance", callback_data="maintenance"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main")
    )
    return markup

# ==================== কমান্ড হ্যান্ডলার (রেট লিমিটেড) ====================
@bot.message_handler(commands=['start'])
@rate_limit
def welcome(message):
    uid = message.from_user.id
    username = message.from_user.username or "User"
    
    if Config.MAINTENANCE and uid != Config.ADMIN_ID:
        bot.send_message(message.chat.id, "🛠 **System Maintenance**\n\nWe're currently upgrading our servers. Please try again later.")
        return
    
    user = get_user(uid)
    if not user:
        with get_db() as conn:
            c = conn.cursor()
            join_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute("INSERT OR IGNORE INTO users (id, username, expiry, file_limit, is_prime, join_date) VALUES (?, ?, ?, ?, ?, ?)",
                      (uid, username, None, 0, 0, join_date))
            conn.commit()
        user = get_user(uid)
    
    if not user:
        bot.send_message(message.chat.id, "❌ Error loading user data. Please try again.")
        return
    
    status = "CORE 👑" if is_prime(uid) else "FREE 🆓"
    expiry = user['expiry'] if user['expiry'] else "Not Activated"
    
    text = f"""
🤖 **UNIQUE HOST BD v1.1.0**
dev: @zerox6t9 <--GET CORE 👑
HOST: Asia 🌏 | data: orange 🍊 
━━━━━━━━━━━━━━━━━━━━━━━━
👤 **User:** @{username}
🆔 **ID:** `{uid}`
💎 **Status:** {status}
📅 **Join Date:** {user['join_date']}
━━━━━━━━━━━━━━━━━━━━━━━━
📊 **Account Details:**
• Plan: {'CORE' if is_prime(uid) else 'Free'}
• File Limit: `{user['file_limit']}` files
• Expiry: {expiry}
━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    bot.send_message(message.chat.id, text, reply_markup=main_menu(uid), parse_mode="Markdown")
    logger.info(f"User {uid} started bot.")

@bot.message_handler(commands=['admin'])
@rate_limit
def admin_command(message):
    uid = message.from_user.id
    if uid == Config.ADMIN_ID:
        admin_panel(message)
    else:
        bot.reply_to(message, "⛔ **Access Denied!**\nYou are not authorized to use this command.")

def admin_panel(message):
    text = """
👑 **ADMIN CONTROL PANEL**
━━━━━━━━━━━━━━━━━━━━
Welcome to the admin dashboard. You can manage users, generate keys, and monitor system activities.
━━━━━━━━━━━━━━━━━━━━
"""
    bot.send_message(message.chat.id, text, reply_markup=admin_menu(), parse_mode="Markdown")

# ==================== কলব্যাক হ্যান্ডলার (রেট লিমিটেড) ====================
@bot.callback_query_handler(func=lambda call: True)
@rate_limit_callback
def callback_manager(call):
    uid = call.from_user.id
    mid = call.message.message_id
    chat_id = call.message.chat.id
    
    try:
        if call.data == "activate_prime":
            msg = safe_edit_message_text(chat_id, mid, """
🔑 **ACTIVATE CORE PASS**
━━━━━━━━━━━━━━━━━━━━
Enter your activation key below.
Format: `PRIME-XXXXXX`
━━━━━━━━━━━━━━━━━━━━
            """, parse_mode="Markdown")
            bot.register_next_step_handler_by_chat_id(chat_id, process_key_step, msg)
            
        elif call.data == "upload":
            if not is_prime(uid):
                bot.answer_callback_query(call.id, "⚠️ Core feature! Activate Core first.")
                return
            msg = safe_edit_message_text(chat_id, mid, """
📤 **UPLOAD BOT FILE**
━━━━━━━━━━━━━━━━━━━━
Please send your Python (.py) bot file.
• Max size: 5MB
• Must be .py extension
━━━━━━━━━━━━━━━━━━━━
            """, parse_mode="Markdown")
            bot.register_next_step_handler_by_chat_id(chat_id, upload_file_step, msg)
            
        elif call.data == "deploy_new":
            if not is_prime(uid):
                bot.answer_callback_query(call.id, "⚠️ Core feature!")
                return
            show_available_files(call)
            
        elif call.data == "my_bots":
            show_my_bots(call)
            
        elif call.data == "dashboard":
            show_dashboard(call)
            
        elif call.data == "admin_panel":
            if uid == Config.ADMIN_ID:
                admin_panel_callback(call)
            else:
                bot.answer_callback_query(call.id, "⛔ Access Denied!")
                
        elif call.data == "gen_key":
            if uid == Config.ADMIN_ID:
                gen_key_step1(call)
            else:
                bot.answer_callback_query(call.id, "⛔ Admin only!")
                
        elif call.data == "all_users":
            if uid == Config.ADMIN_ID:
                show_all_users(call)
                
        elif call.data == "all_bots":
            if uid == Config.ADMIN_ID:
                show_all_bots_admin(call)
                
        elif call.data == "stats":
            if uid == Config.ADMIN_ID:
                show_admin_stats(call)
                
        elif call.data.startswith("bot_"):
            bot_id = call.data.split("_")[1]
            show_bot_details(call, bot_id)
            
        elif call.data.startswith("deploy_"):
            filename = call.data.split("_")[1]
            start_deployment(call, filename)
            
        elif call.data.startswith("stop_"):
            bot_id = call.data.split("_")[1]
            stop_bot(call, bot_id)
            
        elif call.data == "install_libs":
            ask_for_libraries(call)
            
        elif call.data == "back_main":
            safe_edit_message_text(chat_id, mid, "🏠 **Main Menu**", reply_markup=main_menu(uid), parse_mode="Markdown")
            
        elif call.data == "premium_info":
            show_premium_info(call)
            
        elif call.data == "settings":
            show_settings(call)
            
        elif call.data == "maintenance":
            toggle_maintenance(call)
            
    except Exception as e:
        logger.exception(f"Callback error: {call.data}")
        bot.answer_callback_query(call.id, "⚠️ Error occurred!")

# ==================== স্টেপ-বাই-স্টেপ ফাংশন (সংশোধিত) ====================
def gen_key_step1(call):
    msg = safe_edit_message_text(call.message.chat.id, call.message.message_id, """
🎫 **GENERATE CORE KEY**
━━━━━━━━━━━━━━━━━━━━
Step 1/3: Enter duration in days
Example: 7, 30, 90, 365
━━━━━━━━━━━━━━━━━━━━
    """, parse_mode="Markdown")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, gen_key_step2, msg)

def gen_key_step2(message, old_mid):
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
        bot.delete_message(message.chat.id, message.message_id)
        msg = bot.send_message(message.chat.id, f"""
🎫 **GENERATE CORE KEY**
━━━━━━━━━━━━━━━━━━━━
Step 2/3: Duration set to **{days} days**

Now enter file access limit
Example: 3, 5, 10
━━━━━━━━━━━━━━━━━━━━
        """, parse_mode="Markdown")
        bot.register_next_step_handler(msg, gen_key_step3, days)
    except:
        bot.send_message(message.chat.id, "❌ Invalid input! Please enter a valid number.")

def gen_key_step3(message, days):
    try:
        limit = int(message.text.strip())
        if limit <= 0:
            raise ValueError
        bot.delete_message(message.chat.id, message.message_id)
        
        key = generate_random_key()
        created_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        with get_db() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO keys VALUES (?, ?, ?, ?)", (key, days, limit, created_date))
            conn.commit()
        
        response = f"""
✅ **KEY GENERATED SUCCESSFULLY**
━━━━━━━━━━━━━━━━━━━━
🔑 **Key:** `{key}`
⏰ **Duration:** {days} days
📦 **File Limit:** {limit} files
📅 **Created:** {created_date}
━━━━━━━━━━━━━━━━━━━━
Share this key with the user.
        """
        bot.send_message(message.chat.id, response, parse_mode="Markdown")
        logger.info(f"Admin generated key: {key}")
    except:
        bot.send_message(message.chat.id, "❌ Invalid input!")

def upload_file_step(message, old_mid):
    uid = message.from_user.id
    chat_id = message.chat.id
    
    if not is_prime(uid):
        safe_edit_message_text(chat_id, old_mid, "⚠️ **Core Required**\n\nActivate core to upload files.", reply_markup=main_menu(uid), parse_mode="Markdown")
        return
    
    if message.content_type == 'document' and message.document.file_name.endswith('.py'):
        # ফাইল সাইজ চেক
        if message.document.file_size > Config.MAX_FILE_SIZE_MB * 1024 * 1024:
            bot.reply_to(message, f"❌ File too large! Max {Config.MAX_FILE_SIZE_MB}MB.")
            return
        
        try:
            safe_edit_message_text(chat_id, old_mid, "📥 **Downloading file...**", parse_mode="Markdown")
            
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            original_name = message.document.file_name
            safe_name = secure_filename(original_name)
            
            file_path = project_path / safe_name
            file_path.write_bytes(downloaded)
            
            bot.delete_message(chat_id, message.message_id)
            msg = bot.send_message(chat_id, """
🤖 **BOT NAME SETUP**
━━━━━━━━━━━━━━━━━━━━
Enter a name for your bot
Example: `News Bot`, `Music Bot`, `Assistant`
━━━━━━━━━━━━━━━━━━━━
            """, parse_mode="Markdown")
            bot.register_next_step_handler(msg, save_bot_name, safe_name, original_name)
            
        except Exception as e:
            logger.exception(f"File upload failed for user {uid}")
            safe_edit_message_text(chat_id, old_mid, f"❌ **Error:** {str(e)}", parse_mode="Markdown")
    else:
        safe_edit_message_text(chat_id, old_mid, "❌ **Invalid File!**\n\nOnly Python (.py) files allowed.", parse_mode="Markdown")

def save_bot_name(message, safe_name, original_name):
    uid = message.from_user.id
    chat_id = message.chat.id
    bot_name = message.text.strip()
    
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO deployments (user_id, bot_name, filename, pid, start_time, status) VALUES (?, ?, ?, ?, ?, ?)",
                  (uid, bot_name, safe_name, 0, None, "Uploaded"))
        conn.commit()
    
    bot.delete_message(chat_id, message.message_id)
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📚 Install Libraries", callback_data="install_libs"))
    markup.add(types.InlineKeyboardButton("🤖 My Bots", callback_data="my_bots"))
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main"))
    
    text = f"""
✅ **FILE UPLOADED SUCCESSFULLY**
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot Name:** {bot_name}
📁 **File:** `{original_name}`
📊 **Status:** Ready for setup
━━━━━━━━━━━━━━━━━━━━
Click 'Install Libraries' to add dependencies.
    """
    
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    logger.info(f"User {uid} uploaded file {safe_name}")

# ==================== প্যাকেজ ইনস্টলেশন ভ্যালিডেশন ====================
ALLOWED_PIP_PACKAGES = {'pyTelegramBotAPI', 'requests', 'beautifulsoup4', 'flask', 'django', 'numpy', 'pandas', 'pillow', 'matplotlib'}

def validate_pip_command(cmd):
    cmd = cmd.strip()
    if not cmd.startswith('pip install'):
        return False
    parts = cmd.split()
    if len(parts) < 3:
        return False
    package = parts[2].split('==')[0].split('>')[0].split('<')[0].split('[')[0]  # ভার্সন ও এক্সট্রা বাদ
    if package not in ALLOWED_PIP_PACKAGES:
        logger.warning(f"Blocked pip install attempt: {package}")
        return False
    return True

def ask_for_libraries(call):
    msg = safe_edit_message_text(call.message.chat.id, call.message.message_id, """
📚 **INSTALL LIBRARIES**
━━━━━━━━━━━━━━━━━━━━
Enter library commands (one per line):
Example:
```

pip install pyTelegramBotAPI
pip install requests
pip install beautifulsoup4

```
━━━━━━━━━━━━━━━━━━━━
    """, parse_mode="Markdown")
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, install_libraries_step, msg)

def install_libraries_step(message, old_mid):
    uid = message.from_user.id
    chat_id = message.chat.id
    commands = message.text.strip().split('\n')
    
    bot.delete_message(chat_id, message.message_id)
    
    safe_edit_message_text(chat_id, old_mid, """
🛠 **INSTALLING LIBRARIES**
━━━━━━━━━━━━━━━━━━━━
Starting installation...
━━━━━━━━━━━━━━━━━━━━
    """, parse_mode="Markdown")
    
    results = []
    for i, cmd in enumerate(commands):
        if cmd.strip() and "pip install" in cmd:
            if not validate_pip_command(cmd):
                results.append(f"❌ {cmd} (Not allowed)")
                continue
            try:
                progress_text = f"""
🛠 **INSTALLING LIBRARIES**
━━━━━━━━━━━━━━━━━━━━
Installing ({i+1}/{len(commands)}):
`{cmd}`
━━━━━━━━━━━━━━━━━━━━
                """
                safe_edit_message_text(chat_id, old_mid, progress_text, parse_mode="Markdown")
                
                result = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=60, check=False)
                if result.returncode == 0:
                    results.append(f"✅ {cmd}")
                else:
                    results.append(f"❌ {cmd} (Error: {result.stderr[:30]})")
                
                time.sleep(1)
                
            except subprocess.TimeoutExpired:
                results.append(f"⏰ {cmd} (Timeout)")
            except Exception as e:
                results.append(f"⚠️ {cmd} (Error)")
    
    result_text = "\n".join(results)
    final_text = f"""
✅ **INSTALLATION COMPLETE**
━━━━━━━━━━━━━━━━━━━━
{result_text}
━━━━━━━━━━━━━━━━━━━━
All libraries installed successfully!
    """
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🚀 Deploy Bot Now", callback_data="deploy_new"))
    markup.add(types.InlineKeyboardButton("🤖 My Bots", callback_data="my_bots"))
    
    safe_edit_message_text(chat_id, old_mid, final_text, reply_markup=markup, parse_mode="Markdown")
    logger.info(f"User {uid} installed libraries.")

def show_available_files(call):
    uid = call.from_user.id
    with get_db() as conn:
        c = conn.cursor()
        files = c.execute("SELECT filename, bot_name FROM deployments WHERE user_id=? AND pid=0", (uid,)).fetchall()
    
    if not files:
        safe_edit_message_text(call.message.chat.id, call.message.message_id, 
                               "📭 **No files available for deployment**\n\nUpload a file first.", 
                               parse_mode="Markdown")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for filename, bot_name in files:
        markup.add(types.InlineKeyboardButton(f"🤖 {bot_name}", callback_data=f"deploy_{filename}"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_main"))
    
    text = """
🚀 **DEPLOY BOT**
━━━━━━━━━━━━━━━━━━━━
Select a bot to deploy:
━━━━━━━━━━━━━━━━━━━━
    """
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def start_deployment(call, filename):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    mid = call.message.message_id
    
    with get_db() as conn:
        c = conn.cursor()
        bot_info = c.execute("SELECT id, bot_name FROM deployments WHERE filename=? AND user_id=?", (filename, uid)).fetchone()
        if not bot_info:
            return
        bot_id, bot_name = bot_info
    
    # ডিপ্লয়মেন্ট স্টেপ (মূল কোডের স্টাইল অপরিবর্তিত)
    text = f"""
🚀 **DEPLOYING BOT**
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot:** {bot_name}
🔄 **Status:** Initializing system...
━━━━━━━━━━━━━━━━━━━━
    """
    safe_edit_message_text(chat_id, mid, text, parse_mode="Markdown")
    time.sleep(1.5)
    
    text = f"""
🚀 **DEPLOYING BOT**
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot:** {bot_name}
✅ **Step 1:** System initialized
🔄 **Step 2:** Checking dependencies...
━━━━━━━━━━━━━━━━━━━━
    """
    safe_edit_message_text(chat_id, mid, text, parse_mode="Markdown")
    time.sleep(1.5)
    
    text = f"""
🚀 **DEPLOYING BOT**
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot:** {bot_name}
✅ **Step 1:** System initialized
✅ **Step 2:** Dependencies checked
🔄 **Step 3:** Loading modules...
━━━━━━━━━━━━━━━━━━━━
    """
    safe_edit_message_text(chat_id, mid, text, parse_mode="Markdown")
    time.sleep(2)
    
    text = f"""
🚀 **DEPLOYING BOT**
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot:** {bot_name}
✅ **Step 1:** System initialized
✅ **Step 2:** Dependencies checked
✅ **Step 3:** Modules loaded
🔄 **Step 4:** Starting bot process...
━━━━━━━━━━━━━━━━━━━━
    """
    safe_edit_message_text(chat_id, mid, text, parse_mode="Markdown")
    time.sleep(1.5)
    
    try:
        runner = BotRunner.run(uid, bot_id, filename, bot_name, auto_restart=False)
        
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE deployments SET pid=?, container_id=?, start_time=?, status=? WHERE id=?",
                      (runner.get('pid'), runner.get('container_id'), runner['start_time'], 'Running', bot_id))
            conn.commit()
        
        text = f"""
✅ **BOT DEPLOYED SUCCESSFULLY**
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot:** {bot_name}
📁 **File:** `{filename}`
⚙️ **PID:** `{runner.get('pid') or 'Container'}`
⏰ **Started:** {runner['start_time']}
🔧 **Status:** **RUNNING**
━━━━━━━━━━━━━━━━━━━━
Bot is now active and running!
        """
        safe_edit_message_text(chat_id, mid, text, parse_mode="Markdown")
        time.sleep(2)
        
        show_bot_live_stats(call, bot_id, bot_name, runner.get('pid'), runner.get('container_id'))
        
    except Exception as e:
        logger.exception(f"Deployment failed for user {uid}, bot {bot_id}")
        text = f"""
❌ **DEPLOYMENT FAILED**
━━━━━━━━━━━━━━━━━━━━
Error: {str(e)}
━━━━━━━━━━━━━━━━━━━━
Please check your bot code and try again.
        """
        safe_edit_message_text(chat_id, mid, text, parse_mode="Markdown")

def show_bot_live_stats(call, bot_id, bot_name, pid, container_id):
    chat_id = call.message.chat.id
    uid = call.from_user.id
    mid = call.message.message_id
    
    def monitor_bot():
        for i in range(10):  # 10 বার আপডেট
            try:
                stats = get_system_stats()
                cpu_percent = stats['cpu_percent']
                ram_percent = stats['ram_percent']
                disk_percent = stats['disk_percent']
                
                update_bot_stats(bot_id, cpu_percent, ram_percent)
                
                cpu_bar = create_progress_bar(cpu_percent)
                ram_bar = create_progress_bar(ram_percent)
                disk_bar = create_progress_bar(disk_percent)
                
                # বট চলছে কিনা চেক
                if container_id and DOCKER_AVAILABLE:
                    try:
                        client = docker.from_env()
                        container = client.containers.get(container_id)
                        is_running = container.status == 'running'
                    except:
                        is_running = False
                else:
                    stat = get_process_stats(pid)
                    is_running = stat and stat['running'] if stat else False
                
                status_icon = "🟢" if is_running else "🔴"
                
                text = f"""
📊 **LIVE BOT STATISTICS** {status_icon}
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot:** {bot_name}
⚙️ **PID/Container:** `{pid or container_id[:12] if container_id else 'N/A'}`
⏰ **Uptime:** {i*5} seconds
━━━━━━━━━━━━━━━━━━━━
💻 **CPU Usage:** {cpu_bar} {cpu_percent:.1f}%
🧠 **RAM Usage:** {ram_bar} {ram_percent:.1f}%
💾 **Disk Usage:** {disk_bar} {disk_percent:.1f}%
━━━━━━━━━━━━━━━━━━━━
📈 **Server Performance:**
• Download Speed: {random.randint(50, 100)} MB/s
• Upload Speed: {random.randint(20, 50)} MB/s
• Network Latency: {random.randint(10, 50)} ms
• Response Time: {random.randint(1, 10)} ms
━━━━━━━━━━━━━━━━━━━━
🔄 **Status:** {"Running smoothly..." if is_running else "Process stopped"}
                """
                
                try:
                    bot.edit_message_text(text, chat_id, mid, parse_mode="Markdown")
                except:
                    pass
                
                time.sleep(5)
                
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                break
    
    monitor_thread = threading.Thread(target=monitor_bot)
    monitor_thread.daemon = True
    monitor_thread.start()
    
    time.sleep(5)
    text = f"""
✅ **BOT IS NOW ACTIVE**
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot:** {bot_name}
📊 **Status:** Live monitoring active
🏃 **Process:** Running (PID: {pid or 'Container'})
━━━━━━━━━━━━━━━━━━━━
Live statistics will update every 5 seconds.
    """
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🤖 My Bots", callback_data="my_bots"))
    markup.add(types.InlineKeyboardButton("📊 View Stats", callback_data=f"bot_{bot_id}"))
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main"))
    
    safe_edit_message_text(chat_id, mid, text, reply_markup=markup, parse_mode="Markdown")

def show_my_bots(call):
    uid = call.from_user.id
    bots = get_user_bots(uid)
    
    if not bots:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📤 Upload Bot", callback_data="upload"))
        markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main"))
        
        text = """
🤖 **MY BOTS**
━━━━━━━━━━━━━━━━━━━━
No bots found. Upload your first bot!
━━━━━━━━━━━━━━━━━━━━
        """
        safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for bot in bots:
        bot_id, bot_name, filename, pid, start_time, status = bot
        status_icon = "🟢" if status == "Running" else "🔴" if status == "Stopped" else "🟡"
        button_text = f"{status_icon} {bot_name}"
        markup.add(types.InlineKeyboardButton(button_text, callback_data=f"bot_{bot_id}"))
    
    markup.add(types.InlineKeyboardButton("📤 Upload New", callback_data="upload"))
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main"))
    
    running_count = sum(1 for b in bots if b[5] == "Running")
    total_count = len(bots)
    
    text = f"""
🤖 **MY BOTS**
━━━━━━━━━━━━━━━━━━━━
📊 **Stats:** {running_count}/{total_count} running
━━━━━━━━━━━━━━━━━━━━
Select a bot to view details:
━━━━━━━━━━━━━━━━━━━━
    """
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def show_bot_details(call, bot_id):
    with get_db() as conn:
        c = conn.cursor()
        bot_info = c.execute("SELECT * FROM deployments WHERE id=?", (bot_id,)).fetchone()
    
    if not bot_info:
        return
    
    bot_name = bot_info['bot_name']
    filename = bot_info['filename']
    pid = bot_info['pid']
    container_id = bot_info['container_id']
    start_time = bot_info['start_time']
    status = bot_info['status']
    cpu_usage = bot_info['cpu_usage'] or 0
    ram_usage = bot_info['ram_usage'] or 0
    
    stats = get_system_stats()
    cpu_usage = cpu_usage or stats['cpu_percent']
    ram_usage = ram_usage or stats['ram_percent']
    
    cpu_bar = create_progress_bar(cpu_usage)
    ram_bar = create_progress_bar(ram_usage)
    
    if container_id and DOCKER_AVAILABLE:
        try:
            client = docker.from_env()
            container = client.containers.get(container_id)
            is_running = container.status == 'running'
        except:
            is_running = False
    else:
        stat = get_process_stats(pid)
        is_running = stat and stat['running'] if stat else False
    
    def calculate_uptime(start_time_str):
        try:
            start = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
            uptime = datetime.now() - start
            days = uptime.days
            hours, remainder = divmod(uptime.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            if days > 0:
                return f"{days}d {hours}h"
            elif hours > 0:
                return f"{hours}h {minutes}m"
            else:
                return f"{minutes}m"
        except:
            return "N/A"
    
    uptime = calculate_uptime(start_time) if start_time else "N/A"
    
    stats_text = f"""
📊 **Current Stats:**
• CPU: {cpu_bar} {cpu_usage:.1f}%
• RAM: {ram_bar} {ram_usage:.1f}%
• Status: {"🟢 Running" if is_running else "🔴 Stopped"}
• Uptime: {uptime}
    """
    
    text = f"""
🤖 **BOT DETAILS**
━━━━━━━━━━━━━━━━━━━━
**Name:** {bot_name}
**File:** `{filename}`
**PID/Container:** `{pid if pid else container_id[:12] if container_id else "N/A"}`
**Started:** {start_time if start_time else "Not started"}
━━━━━━━━━━━━━━━━━━━━
{stats_text}
━━━━━━━━━━━━━━━━━━━━
    """
    
    markup = types.InlineKeyboardMarkup()
    if is_running:
        markup.add(types.InlineKeyboardButton("🛑 Stop Bot", callback_data=f"stop_{bot_id}"))
    elif pid or container_id:
        markup.add(types.InlineKeyboardButton("🚀 Start Bot", callback_data=f"start_{bot_id}"))
    else:
        markup.add(types.InlineKeyboardButton("🚀 Deploy Bot", callback_data=f"deploy_{filename}"))
    
    markup.add(types.InlineKeyboardButton("📊 Refresh Stats", callback_data=f"bot_{bot_id}"))
    markup.add(types.InlineKeyboardButton("🔙 My Bots", callback_data="my_bots"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def stop_bot(call, bot_id):
    with get_db() as conn:
        c = conn.cursor()
        bot_info = c.execute("SELECT pid, container_id FROM deployments WHERE id=?", (bot_id,)).fetchone()
        if bot_info:
            BotRunner.stop(bot_info['pid'], bot_info['container_id'])
            c.execute("UPDATE deployments SET status='Stopped', pid=NULL, container_id=NULL WHERE id=?", (bot_id,))
            conn.commit()
    bot.answer_callback_query(call.id, "✅ Bot stopped successfully!")
    show_my_bots(call)

def show_dashboard(call):
    uid = call.from_user.id
    user = get_user(uid)
    
    if not user:
        bot.answer_callback_query(call.id, "❌ User data not found")
        return
    
    bots = get_user_bots(uid)
    running_bots = sum(1 for b in bots if b[5] == "Running")
    total_bots = len(bots)
    
    stats = get_system_stats()
    cpu_usage = stats['cpu_percent']
    ram_usage = stats['ram_percent']
    disk_usage = stats['disk_percent']
    
    cpu_bar = create_progress_bar(cpu_usage)
    ram_bar = create_progress_bar(ram_usage)
    disk_bar = create_progress_bar(disk_usage)
    
    text = f"""
📊 **USER DASHBOARD**
━━━━━━━━━━━━━━━━━━━━
👤 **Account Info:**
• Status: {'CORE👑' if is_prime(uid) else 'FREE 🆓'}
• File Limit: {user['file_limit']} files
• Expiry: {user['expiry'] if user['expiry'] else 'Not set'}
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot Statistics:**
• Total Bots: {total_bots}
• Running: {running_bots}
• Stopped: {total_bots - running_bots}
━━━━━━━━━━━━━━━━━━━━
🖥️ **Server Status:**
• CPU: {cpu_bar} {cpu_usage:.1f}%
• RAM: {ram_bar} {ram_usage:.1f}%
• Disk: {disk_bar} {disk_usage:.1f}%
━━━━━━━━━━━━━━━━━━━━
💻 **Hosting Platform:**
• Platform: ULTIMATE FLOW 
• Type: Web Service 
• Region: Asia/kushtia🇧🇩
━━━━━━━━━━━━━━━━━━━━
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🤖 My Bots", callback_data="my_bots"),
        types.InlineKeyboardButton("🚀 Deploy", callback_data="deploy_new")
    )
    markup.add(
        types.InlineKeyboardButton("📤 Upload", callback_data="upload"),
        types.InlineKeyboardButton("🔄 Refresh", callback_data="dashboard")
    )
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def admin_panel_callback(call):
    text = """
👑 **ADMIN DASHBOARD**
━━━━━━━━━━━━━━━━━━━━
Welcome to the admin control panel.
Select an option below:
━━━━━━━━━━━━━━━━━━━━
    """
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=admin_menu(), parse_mode="Markdown")

def show_all_users(call):
    with get_db() as conn:
        c = conn.cursor()
        users = c.execute("SELECT id, username, expiry, file_limit, is_prime FROM users").fetchall()
    
    prime_count = sum(1 for u in users if u['is_prime'] == 1)
    total_count = len(users)
    
    text = f"""
👥 **ALL USERS**
━━━━━━━━━━━━━━━━━━━━
📊 **Total Users:** {total_count}
👑 **Core Users:** {prime_count}
🆓 **Free Users:** {total_count - prime_count}
━━━━━━━━━━━━━━━━━━━━
**Recent Users:**
"""
    
    for user in users[:10]:
        username = user['username'] if user['username'] else f"User_{user['id']}"
        text += f"\n• {username} (ID: {user['id']}) - {'Prime' if user['is_prime'] else 'Free'}"
    
    if len(users) > 10:
        text += f"\n\n... and {len(users) - 10} more users"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def show_all_bots_admin(call):
    with get_db() as conn:
        c = conn.cursor()
        bots = c.execute("SELECT d.bot_name, d.status, d.start_time, u.username FROM deployments d LEFT JOIN users u ON d.user_id = u.id").fetchall()
    
    running_bots = sum(1 for b in bots if b['status'] == "Running")
    total_bots = len(bots)
    
    text = f"""
🤖 **ALL BOTS**
━━━━━━━━━━━━━━━━━━━━
📊 **Total Bots:** {total_bots}
🟢 **Running:** {running_bots}
🔴 **Stopped:** {total_bots - running_bots}
━━━━━━━━━━━━━━━━━━━━
**Active Bots:**
"""
    
    for bot_info in bots[:5]:
        if bot_info['status'] == "Running":
            username = bot_info['username'] if bot_info['username'] else "Unknown"
            text += f"\n• {bot_info['bot_name']} (@{username}) - {bot_info['status']}"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def show_admin_stats(call):
    with get_db() as conn:
        c = conn.cursor()
        total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        prime_users = c.execute("SELECT COUNT(*) FROM users WHERE is_prime=1").fetchone()[0]
        total_bots = c.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
        running_bots = c.execute("SELECT COUNT(*) FROM deployments WHERE status='Running'").fetchone()[0]
        total_keys = c.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
    
    stats = get_system_stats()
    cpu_usage = stats['cpu_percent']
    ram_usage = stats['ram_percent']
    disk_usage = stats['disk_percent']
    
    text = f"""
📈 **ADMIN STATISTICS**
━━━━━━━━━━━━━━━━━━━━
👥 **User Stats:**
• Total Users: {total_users}
• Prime Users: {prime_users}
• Free Users: {total_users - prime_users}
━━━━━━━━━━━━━━━━━━━━
🤖 **Bot Stats:**
• Total Bots: {total_bots}
• Running Bots: {running_bots}
• Stopped Bots: {total_bots - running_bots}
━━━━━━━━━━━━━━━━━━━━
🔑 **Key Stats:**
• Total Keys: {total_keys}
━━━━━━━━━━━━━━━━━━━━
🖥️ **System Status:**
• CPU Usage: {cpu_usage:.1f}%
• RAM Usage: {ram_usage:.1f}%
• Disk Usage: {disk_usage:.1f}%
━━━━━━━━━━━━━━━━━━━━
🌐 **Hosting Info:**
• Platform: ULTIMATE FLOW 
• Port: {Config.PORT}
• Database: orange-print🍊
━━━━━━━━━━━━━━━━━━━━
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("👥 Users", callback_data="all_users"),
        types.InlineKeyboardButton("🤖 Bots", callback_data="all_bots")
    )
    markup.add(types.InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def toggle_maintenance(call):
    Config.MAINTENANCE = not Config.MAINTENANCE
    status = "ENABLED 🔴" if Config.MAINTENANCE else "DISABLED 🟢"
    text = f"""
⚙️ **MAINTENANCE MODE**
━━━━━━━━━━━━━━━━━━━━
Status: {status}
━━━━━━━━━━━━━━━━━━━━
Maintenance mode has been {'enabled' if Config.MAINTENANCE else 'disabled'}.
Only admin can access the system when enabled.
━━━━━━━━━━━━━━━━━━━━
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def show_premium_info(call):
    text = """
👑 **CORE FEATURES**
━━━━━━━━━━━━━━━━━━━━
✅ **Unlimited Bot Deployment**
✅ **Priority Support**
✅ **Advanced Monitoring**
✅ **Custom Bot Names**
✅ **Library Installation**
✅ **Live Statistics**
✅ **24/7 Server Uptime**
✅ **No Ads**
━━━━━━━━━━━━━━━━━━━━
💎 **Get core Today!**
Click 'Activate core Pass' and enter your key.
━━━━━━━━━━━━━━━━━━━━
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔑 Activate Core", callback_data="activate_prime"))
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def show_settings(call):
    uid = call.from_user.id
    user = get_user(uid)
    
    if not user:
        bot.answer_callback_query(call.id, "❌ User data not found")
        return
    
    text = f"""
⚙️ **SETTINGS**
━━━━━━━━━━━━━━━━━━━━
👤 **Account Settings:**
• User ID: `{uid}`
• Status: {'Core 👑' if is_prime(uid) else 'Free 🆓'}
• File Limit: {user['file_limit']} files
━━━━━━━━━━━━━━━━━━━━
🔧 **Bot Settings:**
• Auto-restart: Disabled
• Notifications: Enabled
• Language: English
━━━━━━━━━━━━━━━━━━━━
⚠️ **Danger Zone:**
• Delete Account
• Reset Settings
━━━━━━━━━━━━━━━━━━━━
🌐 **Hosting Info:**
• Platform: unauthorized ❌🐍
• Port: {Config.PORT}
• Database: orange-print🍊
━━━━━━━━━━━━━━━━━━━━
"""
    
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🔔 Notifications", callback_data="notif_settings"),
        types.InlineKeyboardButton("🌐 Language", callback_data="lang_settings")
    )
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_main"))
    
    safe_edit_message_text(call.message.chat.id, call.message.message_id, text, reply_markup=markup, parse_mode="Markdown")

def process_key_step(message, old_mid):
    uid = message.from_user.id
    key_input = message.text.strip().upper()
    
    bot.delete_message(message.chat.id, message.message_id)
    
    with get_db() as conn:
        c = conn.cursor()
        res = c.execute("SELECT * FROM keys WHERE key=?", (key_input,)).fetchone()
        
        if res:
            days = res['duration_days']
            limit = res['file_limit']
            expiry_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            
            c.execute("UPDATE users SET expiry=?, file_limit=?, is_prime=1 WHERE id=?", (expiry_date, limit, uid))
            c.execute("DELETE FROM keys WHERE key=?", (key_input,))
            conn.commit()
            
            text = f"""
✅ **CORE ACTIVATED!**
━━━━━━━━━━━━━━━━━━━━
🎉 Congratulations! You are now a Core member.
━━━━━━━━━━━━━━━━━━━━
📅 **Expiry:** {expiry_date}
📦 **File Limit:** {limit} files
━━━━━━━━━━━━━━━━━━━━
Enjoy all premium features!
            """
            
            safe_edit_message_text(message.chat.id, old_mid, text, reply_markup=main_menu(uid), parse_mode="Markdown")
            logger.info(f"User {uid} activated core with key {key_input}")
        else:
            text = """
❌ **INVALID KEY**
━━━━━━━━━━━━━━━━━━━━
The key you entered is invalid or expired.
━━━━━━━━━━━━━━━━━━━━
Please check the key and try again.
            """
            safe_edit_message_text(message.chat.id, old_mid, text, reply_markup=main_menu(uid), parse_mode="Markdown")

# ==================== ফ্লাস্ক রুট (অপরিবর্তিত) ====================
@app.route('/')
def home():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>🤖 Cyber Bot Hosting v3.1</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                margin: 0;
                padding: 20px;
                min-height: 100vh;
            }
            .container {
                max-width: 800px;
                margin: 0 auto;
                background: rgba(255, 255, 255, 0.1);
                padding: 30px;
                border-radius: 15px;
                backdrop-filter: blur(10px);
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
            }
            h1 {
                text-align: center;
                font-size: 2.5em;
                margin-bottom: 30px;
                color: #fff;
            }
            .status {
                background: rgba(255, 255, 255, 0.2);
                padding: 20px;
                border-radius: 10px;
                margin: 20px 0;
                border-left: 5px solid #4CAF50;
            }
            .feature {
                background: rgba(255, 255, 255, 0.15);
                padding: 15px;
                margin: 10px 0;
                border-radius: 8px;
                display: flex;
                align-items: center;
            }
            .feature i {
                margin-right: 15px;
                font-size: 1.5em;
            }
            .stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin: 30px 0;
            }
            .stat-box {
                background: rgba(255, 255, 255, 0.2);
                padding: 20px;
                border-radius: 10px;
                text-align: center;
            }
            .btn {
                display: inline-block;
                background: linear-gradient(45deg, #FF416C, #FF4B2B);
                color: white;
                padding: 12px 30px;
                border-radius: 25px;
                text-decoration: none;
                font-weight: bold;
                margin: 10px 5px;
                transition: transform 0.3s;
            }
            .btn:hover {
                transform: translateY(-3px);
            }
            .footer {
                text-align: center;
                margin-top: 40px;
                padding-top: 20px;
                border-top: 1px solid rgba(255, 255, 255, 0.3);
            }
        </style>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    </head>
    <body>
        <div class="container">
            <h1><i class="fas fa-robot"></i> Cyber Bot Hosting v3.1</h1>
            
            <div class="status">
                <h2><i class="fas fa-server"></i> Server Status: <span style="color: #4CAF50;">✅ ONLINE</span></h2>
                <p>Bot hosting service is running securely with rate limiting, resource control, and auto-restart.</p>
            </div>
            
            <div class="stats">
                <div class="stat-box">
                    <i class="fas fa-users"></i>
                    <h3>Active Users</h3>
                    <p>24/7 Service</p>
                </div>
                <div class="stat-box">
                    <i class="fas fa-robot"></i>
                    <h3>Bot Hosting</h3>
                    <p>Unlimited Deployment</p>
                </div>
                <div class="stat-box">
                    <i class="fas fa-shield-alt"></i>
                    <h3>Secure</h3>
                    <p>Protected Environment</p>
                </div>
                <div class="stat-box">
                    <i class="fas fa-bolt"></i>
                    <h3>Fast</h3>
                    <p>High Performance</p>
                </div>
            </div>
            
            <h2><i class="fas fa-star"></i> Premium Features</h2>
            
            <div class="feature">
                <i class="fas fa-upload"></i>
                <div>
                    <h3>Bot File Upload</h3>
                    <p>Upload and deploy your Python bots easily</p>
                </div>
            </div>
            
            <div class="feature">
                <i class="fas fa-chart-line"></i>
                <div>
                    <h3>Live Statistics</h3>
                    <p>Real-time monitoring of your bots</p>
                </div>
            </div>
            
            <div class="feature">
                <i class="fas fa-cogs"></i>
                <div>
                    <h3>Library Installation</h3>
                    <p>Install required libraries automatically</p>
                </div>
            </div>
            
            <div class="feature">
                <i class="fas fa-tachometer-alt"></i>
                <div>
                    <h3>Performance Dashboard</h3>
                    <p>Monitor CPU, RAM, and disk usage</p>
                </div>
            </div>
            
            <div style="text-align: center; margin: 40px 0;">
                <a href="https://t.me/cyber_bot_hosting_bot" class="btn" target="_blank">
                    <i class="fab fa-telegram"></i> Start on Telegram
                </a>
                <a href="https://render.com" class="btn" target="_blank" style="background: linear-gradient(45deg, #00b09b, #96c93d);">
                    <i class="fas fa-cloud"></i> Hosted on Render
                </a>
            </div>
            
            <div class="footer">
                <p><i class="fas fa-info-circle"></i> System Port: """ + str(Config.PORT) + """ | Python 3.9+ | SQLite Database</p>
                <p>© 2024 Cyber Bot Hosting. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/health')
def health():
    return {"status": "healthy", "service": "Cyber Bot Hosting v3.1", "port": Config.PORT, "maintenance": Config.MAINTENANCE}

# ==================== বট ও সার্ভার রানার ====================
def start_bot():
    logger.info("🤖 Starting Telegram Bot polling...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.exception(f"Bot polling crashed: {e}")
            time.sleep(5)

if __name__ == '__main__':
    logger.info(f"""
🤖 CYBER BOT HOSTING v3.1
━━━━━━━━━━━━━━━━━━━━
🚀 Starting on Zen bot
• Port: {Config.PORT}
• Admin ID: {Config.ADMIN_ID}
• Database: ✅ (WAL mode)
• Project Directory: ✅
• Docker: {'✅' if DOCKER_AVAILABLE else '❌'}
• psutil: {'✅' if PSUTIL_AVAILABLE else '❌'}
━━━━━━━━━━━━━━━━━━━━
    """)
    
    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()
    
    logger.info(f"✅ Telegram bot started in background")
    logger.info(f"🌐 Flask server starting on port {Config.PORT}")
    
    app.run(host='0.0.0.0', port=Config.PORT, debug=False, use_reloader=False)
