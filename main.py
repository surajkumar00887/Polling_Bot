import os
import time
import sqlite3
import threading
import logging
from datetime import datetime
import pytz
import random
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot_cleanup_handler import cleanup_group_data, get_group_storage_size, database_stats
from dotenv import load_dotenv
from flask import Flask, request

# 📚 दूसरी फाइल से प्रश्न इम्पोर्ट करें
from questions import QUIZ_LIST

# .env से सभी क्रेडेंशियल्स लोड करें
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SUPPORT_GROUP_ID = os.getenv("SUPPORT_GROUP_ID")
ALLOWED_GROUP_ID = os.getenv("ALLOWED_GROUP_ID")

if not API_TOKEN:
    raise ValueError("Error: BOT_TOKEN एनवायरनमेंट वेरिएबल्स में नहीं मिला!")

from telebot import apihelper
apihelper.ENABLE_MIDDLEWARE = True

bot = telebot.TeleBot(API_TOKEN)
telebot.logger.setLevel(logging.CRITICAL)

# Flask app बनाएं (Render के लिए)
app = Flask(__name__)

DB_FILE = "bot_data.db"

# ⏳ एक्टिव बैन काउंटडाउन ट्रैकर्स के लिए डिक्शनरी
active_ban_timers = {}

# 🚀 ग्लोबल बॉट यूज़रनेम वेरिएबल
BOT_USERNAME = "Bot"
try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    pass

if OWNER_ID:
    try: OWNER_ID = int(OWNER_ID)
    except ValueError: OWNER_ID = None

if SUPPORT_GROUP_ID:
    try: SUPPORT_GROUP_ID = int(SUPPORT_GROUP_ID)
    except ValueError: SUPPORT_GROUP_ID = None

if ALLOWED_GROUP_ID:
    try: ALLOWED_GROUP_ID = int(ALLOWED_GROUP_ID)
    except ValueError: ALLOWED_GROUP_ID = None

# =====================================================================
# 🌐 WEBHOOK ROUTES (Render के लिए)
# =====================================================================
@app.route('/', methods=['POST'])
def webhook():
    try:
        update = request.get_json()
        if update:
            from telebot.types import Update
            update = Update.de_json(update, bot)
            bot.process_new_updates([update])
    except Exception as e:
        print(f"Webhook error: {e}")
    return "ok", 200

@app.route('/health', methods=['GET'])
def health():
    return {"status": "ok"}, 200

def init_db():
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                current_index INTEGER DEFAULT 0,
                last_poll_id INTEGER DEFAULT NULL,
                last_sent_time REAL DEFAULT 0,
                language TEXT DEFAULT 'hindi',
                interval INTEGER DEFAULT 1800,
                auto_delete INTEGER DEFAULT 1
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                join_time REAL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS poll_mapping (
                poll_id TEXT PRIMARY KEY,
                chat_id INTEGER,
                correct_id INTEGER,
                creation_time REAL DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_scores (
                chat_id INTEGER,
                user_id INTEGER,
                user_name TEXT,
                correct_count INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('leaderboard_time', '22:00')")
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_limits (
                user_id INTEGER PRIMARY KEY,
                msg_count INTEGER DEFAULT 0,
                last_msg_time REAL DEFAULT 0
            )
        ''')
        
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN settings_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 

        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN start_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 

        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN help_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 
            
        try:
            cursor.execute("ALTER TABLE daily_scores ADD COLUMN last_score_msg_id INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass 
            
        try:
            cursor.execute("ALTER TABLE poll_mapping ADD COLUMN creation_time REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_bot_promoted INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN last_warning_time REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass 

        try:
            cursor.execute("ALTER TABLE users ADD COLUMN msg_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE groups ADD COLUMN msg_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        conn.commit()

init_db()

def is_user_admin(chat_id, user_id):
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False

def escape_html(text):
    if not text: return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
def truncate_explanation(explanation_text, max_length=100):
    if not explanation_text:
        return None
    
    explanation_text = str(explanation_text).strip()
    
    if len(explanation_text) <= max_length:
        return explanation_text
    
    truncated = explanation_text[:max_length].rsplit(' ', 1)[0] + "..."
    return truncated

def auto_reset_midnight_loop():
    tz = pytz.timezone('Asia/Kolkata')
    while True:
        try:
            now = datetime.now(tz)
            if now.hour == 0 and now.minute == 0:
                with sqlite3.connect(DB_FILE, timeout=20) as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE users SET msg_count = 0")
                    conn.commit()
                print("⏰ Success: Daily message limit automatic reset ho gayi!")
                time.sleep(60)
        except Exception as e:
            print(f"Error in automatic reset thread: {e}")
        time.sleep(30)

threading.Thread(target=auto_reset_midnight_loop, daemon=True).start()

def global_poll_manager():
    while True:
        try:
            with sqlite3.connect(DB_FILE, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT chat_id, current_index, last_poll_id, last_sent_time, language, interval, auto_delete, last_warning_time FROM groups")
                all_groups = cursor.fetchall()
                current_now = time.time()

                for chat_id, current_index, last_poll_id, last_sent_time, language, interval, auto_delete, last_warning_time in all_groups:
                    if current_now - last_sent_time >= interval:
                        
                        is_bot_admin = False
                        admin_check_error = None
                        try:
                            bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
                            if bot_member.status in ['administrator', 'creator']:
                                is_bot_admin = True
                            else:
                                admin_check_error = f"Bot status: {bot_member.status}"
                        except Exception as e:
                            admin_check_error = str(e)
                            is_bot_admin = False

                        if not is_bot_admin:
                            print(f"⚠️ [GROUP {chat_id}] Bot is NOT admin. Error: {admin_check_error}")
                            
                            warning_interval = 43200
                            if last_warning_time is None or current_now - last_warning_time >= warning_interval:
                                try:
                                    bot.send_message(
                                        chat_id=chat_id, 
                                        text="⚠️ **ALERT!**\n\nTo send polls, please re-promote me to Admin and grant permissions.",
                                        parse_mode="Markdown"
                                    )
                                    cursor.execute("UPDATE groups SET last_warning_time = ? WHERE chat_id = ?", (current_now, chat_id))
                                except Exception as warn_err:
                                    print(f"⚠️ [GROUP {chat_id}] Warning send failed: {warn_err}")
                            
                            cursor.execute("UPDATE groups SET last_sent_time = ? WHERE chat_id = ?", (current_now, chat_id))
                            conn.commit()
                            continue

                        if last_poll_id is not None and auto_delete == 1:
                            try:
                                bot.delete_message(chat_id=chat_id, message_id=last_poll_id)
                            except Exception as del_err:
                                print(f"❌ [GROUP {chat_id}] Old poll delete failed: {del_err}")

                        filtered_quiz = [q for q in QUIZ_LIST if q.get("lang", "hindi") == language]
                        if not filtered_quiz:
                            filtered_quiz = QUIZ_LIST

                        if current_index >= len(filtered_quiz):
                            current_index = 0

                        quiz = filtered_quiz[current_index]
                        explanation_text = truncate_explanation(quiz.get("explanation", None), max_length=100)
                        
                        try:
                            sent_message = bot.send_poll(
                                chat_id=chat_id,
                                question=quiz["question"],
                                options=quiz["options"],
                                type="quiz",
                                correct_option_id=quiz["correct_id"],
                                is_anonymous=False,  
                                explanation=explanation_text
                            )
                            new_poll_id = sent_message.message_id
                            poll_api_id = sent_message.poll.id
                            
                            cursor.execute("INSERT INTO poll_mapping (poll_id, chat_id, correct_id, creation_time) VALUES (?, ?, ?, ?)", 
                                           (poll_api_id, chat_id, quiz["correct_id"], time.time()))

                            new_index = (current_index + 1) % len(filtered_quiz)
                            cursor.execute('''
                                UPDATE groups 
                                SET current_index = ?, last_poll_id = ?, last_sent_time = ? 
                                WHERE chat_id = ?
                            ''', (new_index, new_poll_id, current_now, chat_id))
                            conn.commit()
                            print(f"✅ [GROUP {chat_id}] Poll sent successfully")

                        except Exception as e:
                            error_str = str(e).lower()
                            print(f"❌ [GROUP {chat_id}] Poll send failed: {e}")
                            
                            if "bot was kicked" in error_str or "chat not found" in error_str or "bot is not a member" in error_str:
                                cursor.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
                                conn.commit()
                                print(f"🗑️ [GROUP {chat_id}] Removed from database (bot kicked/left)")
                            else:
                                cursor.execute("UPDATE groups SET last_sent_time = ? WHERE chat_id = ?", (current_now, chat_id))
                                conn.commit()
                                
        except Exception as db_err:
            print(f"❌ Database loop error: {db_err}")
        time.sleep(5)

def get_settings_markup(chat_id):
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT language, interval, auto_delete FROM groups WHERE chat_id = ?", (chat_id,))
        res = cursor.fetchone()
    if not res: return None, None
    lang, interval, auto_delete = res[0], res[1], res[2]
    interval_mins = interval // 60
    del_status = "ON ✅" if auto_delete == 1 else "OFF 📴"
    
    text = (
        "⚙️ *Settings Panel (Quiz Settings)*\n\n"
        f"🌐 *Current Language:* {lang.upper()}\n"
        f"⏱️ *Quiz Interval:* {interval_mins} min\n"
        f"🗑️ *Auto Delete Poll:* {del_status}\n"
        f"🧾 Tap to auto delete polls botton to set Previous Quiz Polls deleting system.\n\n"
        "*Click on the buttons below to change configurations:*"
    )
    markup = InlineKeyboardMarkup()
    lang_text = "🌐 भाषा: HINDI 🇮🇳" if lang == 'hindi' else "🌐 Lang: ENGLISH 🇬🇧"
    
    btn_lang = InlineKeyboardButton(text=lang_text, callback_data=f"set_lang_{chat_id}", style="primary")
    btn_autodel = InlineKeyboardButton(text="🗑️ Auto-Delete Polls", callback_data=f"menu_autodel_{chat_id}", style="primary")
    
    btn_15m = InlineKeyboardButton(text="⏱️ 05 Min", callback_data=f"set_time_300_{chat_id}", style="success")
    btn_30m = InlineKeyboardButton(text="⏱️ 30 Min", callback_data=f"set_time_1800_{chat_id}", style="success")
    btn_45m = InlineKeyboardButton(text="⏱️ 45 Min", callback_data=f"set_time_2700_{chat_id}", style="success")
    btn_60m = InlineKeyboardButton(text="⏱️ 60 Min", callback_data=f"set_time_3600_{chat_id}", style="success")
    
    btn_close = InlineKeyboardButton(text="Close ❌", callback_data=f"panel_close_{chat_id}", style="danger")
    
    markup.row(btn_lang)
    markup.row(btn_autodel)
    markup.row(btn_15m, btn_30m)
    markup.row(btn_45m, btn_60m)
    markup.row(btn_close)
    return text, markup

def get_autodelete_markup(chat_id):
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT auto_delete FROM groups WHERE chat_id = ?", (chat_id,))
        res = cursor.fetchone()
    auto_delete = res[0] if res else 1
    status_text = "ON" if auto_delete == 1 else "OFF"
    text = (
        "🗑️ *Auto-Delete Quiz Polls Settings*\n\n"
        "⚠️ *Click on the control buttons*\n\n"
        f"📊 *Status:* \" {status_text} \"\n\n"
        "ℹ️ *What does this do?*\n"
        "• When ON: Previous quiz poll will be deleted automatically.\n"
        "• When OFF: Old quizzes will stay in chat history.\n\n"
        "👇 *Toggle auto-delete setting:*"
    )
    markup = InlineKeyboardMarkup()
    
    btn_on = InlineKeyboardButton(text="Turn On ✅", callback_data=f"autodel_on_{chat_id}", style="success")
    btn_off = InlineKeyboardButton(text="Turn Off 📴", callback_data=f"autodel_off_{chat_id}", style="danger")
    btn_back = InlineKeyboardButton(text="Back 🔙", callback_data=f"autodel_back_{chat_id}", style="danger")
    
    markup.row(btn_on, btn_off)
    markup.row(btn_back)
    return text, markup

@bot.message_handler(commands=['settings'])
def group_settings(message):
    chat_type = message.chat.type

    if chat_type == 'private':
        try: bot.reply_to(message, "❌ This command can only be used in groups.")
        except Exception: pass
        return  

    if not is_user_admin(message.chat.id, message.from_user.id):
        try: bot.reply_to(message, "❌ Only group admin's can change the settings.")
        except Exception: pass
        return
        
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT settings_msg_id FROM groups WHERE chat_id = ?", (message.chat.id,))
        row = cursor.fetchone()
        old_msg_id = row[0] if row and row[0] else 0

    if old_msg_id > 0:
        try:
            bot.delete_message(chat_id=message.chat.id, message_id=old_msg_id)
        except Exception:
            pass

    text, markup = get_settings_markup(message.chat.id)
    if text: 
        try: 
            new_msg = bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
            
            with sqlite3.connect(DB_FILE, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE groups SET settings_msg_id = ? WHERE chat_id = ?", (new_msg.message_id, message.chat.id))
                conn.commit()
                
            try:
                bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
            except Exception:
                pass
                
        except Exception: 
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith(('set_lang_', 'set_time_', 'menu_autodel_', 'autodel_', 'panel_close_')))
def handle_settings_callbacks(call):
    user_id = call.from_user.id
    data_parts = call.data.split('_')
    
    action = data_parts[0]       
    sub_action = data_parts[1]   
    chat_id = int(data_parts[-1]) 
    
    if not is_user_admin(chat_id, user_id):
        bot.answer_callback_query(call.id, "❌ You do not have admin permissions!", show_alert=True)
        return

    if action == "panel" and sub_action == "close":
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE groups SET settings_msg_id = 0 WHERE chat_id = ?", (chat_id,))
            conn.commit()
        try: 
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception: 
            pass
        return

    show_main_menu = True
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        
        if action == "set" and sub_action == "lang":
            cursor.execute("SELECT language FROM groups WHERE chat_id = ?", (chat_id,))
            res = cursor.fetchone()
            current_lang = res[0] if res else 'hindi'
            new_lang = 'english' if current_lang == 'hindi' else 'hindi'
            cursor.execute("UPDATE groups SET language = ? WHERE chat_id = ?", (new_lang, chat_id))
            bot.answer_callback_query(call.id, f"Language changed to {new_lang.upper()} / भाषा बदल दी गई है।")
            
        elif action == "set" and sub_action == "time":
            new_interval = int(data_parts[2]) 
            cursor.execute("UPDATE groups SET interval = ? WHERE chat_id = ?", (new_interval, chat_id))
            bot.answer_callback_query(call.id, f"समय अंतराल बदलकर {new_interval // 60} मिनट कर दिया गया है।")
            
        elif action == "menu" and sub_action == "autodel":
            show_main_menu = False
            bot.answer_callback_query(call.id) 
            
        elif action == "autodel":
            if sub_action == "on":
                cursor.execute("UPDATE groups SET auto_delete = 1 WHERE chat_id = ?", (chat_id,))
                bot.answer_callback_query(call.id, "Auto-Delete चालू (ON) कर दिया गया है।")
                show_main_menu = False
            elif sub_action == "off":
                cursor.execute("UPDATE groups SET auto_delete = 0 WHERE chat_id = ?", (chat_id,))
                bot.answer_callback_query(call.id, "Auto-Delete बंद (OFF) कर दिया गया है।")
                show_main_menu = False
            elif sub_action == "back":
                bot.answer_callback_query(call.id, "मुख्य मेनू पर वापस जा रहे हैं...")
                show_main_menu = True
                
        conn.commit()
        
    if show_main_menu: 
        text, markup = get_settings_markup(chat_id)
    else: 
        text, markup = get_autodelete_markup(chat_id)
        
    try: 
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    except Exception: 
        pass

@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    poll_id = str(poll_answer.poll_id)
    user_id = poll_answer.user.id
    
    first_name = poll_answer.user.first_name if poll_answer.user.first_name else ""
    last_name = poll_answer.user.last_name if poll_answer.user.last_name else ""
    user_name = f"{first_name} {last_name}".strip()
    if not user_name: 
        user_name = f"User_{user_id}"

    if not poll_answer.option_ids:
        return

    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT chat_id, correct_id, creation_time FROM poll_mapping WHERE poll_id = ?", (poll_id,))
        mapping = cursor.fetchone()
        
        if not mapping:
            print(f"⚠️ Warning: Poll ID {poll_id} not found in database mapping.")
            return  

        chat_id = mapping[0]
        correct_id = mapping[1]
        creation_time = mapping[2] if mapping[2] is not None else time.time()
        chosen_option = poll_answer.option_ids[0]
        
        if time.time() - creation_time > 86400:
            return  

        if chosen_option == correct_id:
            cursor.execute('''
                INSERT INTO daily_scores (chat_id, user_id, user_name, correct_count, wrong_count)
                VALUES (?, ?, ?, 1, 0)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name = excluded.user_name,
                correct_count = daily_scores.correct_count + 1
            ''', (chat_id, user_id, user_name))
        else:
            cursor.execute('''
                INSERT INTO daily_scores (chat_id, user_id, user_name, correct_count, wrong_count)
                VALUES (?, ?, ?, 0, 1)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                user_name = excluded.user_name,
                wrong_count = daily_scores.wrong_count + 1
            ''', (chat_id, user_id, user_name))
            
        conn.commit()

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    chat_type = message.chat.type
    message_text = message.text.strip() if message.text else ""
    
    if chat_type in ['group', 'supergroup']:
        expected_full_command = f"/start@{BOT_USERNAME}"
        if "@" in message_text and not message_text.startswith(expected_full_command):
            return  

    first_name = message.from_user.first_name if message.from_user.first_name else ""
    last_name = message.from_user.last_name if message.from_user.last_name else ""
    full_name = f"{first_name} {last_name}".strip()
    if not full_name: 
        full_name = f"User_{user_id}"

    image_folder = "images"  
    selected_image_path = None

    try:
        if os.path.exists(image_folder) and os.path.isdir(image_folder):
            all_images = [f for f in os.listdir(image_folder) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            if all_images:
                selected_image_path = os.path.join(image_folder, random.choice(all_images))
    except Exception as e:
        print(f"इमेज फोल्डर रीड करने में एरर: {e}")

    if chat_type in ['group', 'supergroup']:
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT start_msg_id FROM groups WHERE chat_id = ?", (message.chat.id,))
            row = cursor.fetchone()
            old_start_id = row[0] if row is not None else 0

        if old_start_id > 0:
            try: 
                bot.delete_message(chat_id=message.chat.id, message_id=old_start_id)
            except Exception: 
                pass

        group_text = (
            f"🎉 *Bot activated successfully!*\n"
            f"📢 Automated quizzes have been activated for this group.\n\n"
            f"🇮🇳 *Group Name:* [{message.chat.title}]\n"
            f"This bot is the easiest way to keep your groups active and engaged.\n\n"
            f"📌 *My Features:*\n"
            f"📊 *Daily Auto Poll:* Automatically sends a new poll every day at your set time interval.\n"
            f"🏆 *Auto Result:* Generates results daily at your set time showing the Top 20 users' scores with negative marking.\n\n"
            f"🚀 *How to Get Started:*\n"
            f"1. Make me a *Group Admin* (so I have permission to send polls).\n"
            f"2. Use the *`/settings`* command inside your group to configure everything.\n\n"
            f"For any help, simply type *`/help`*."
        )
        group_markup = InlineKeyboardMarkup()
        add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
        group_markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=add_to_group_url, style="success"))
        
        new_msg = None
        try: 
            if selected_image_path:
                with open(selected_image_path, "rb") as photo_file:
                    new_msg = bot.send_photo(
                        chat_id=message.chat.id, 
                        photo=photo_file, 
                        caption=group_text, 
                        reply_markup=group_markup, 
                        parse_mode="Markdown"
                    )
            else:
                raise ValueError("No image found")
        except Exception: 
            try:
                new_msg = bot.send_message(chat_id=message.chat.id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
            except Exception: 
                pass

        if new_msg:
            try:
                with sqlite3.connect(DB_FILE, timeout=20) as conn:
                    cursor = conn.cursor()
                    cursor.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (message.chat.id,))
                    cursor.execute("UPDATE groups SET start_msg_id = ? WHERE chat_id = ?", (new_msg.message_id, message.chat.id))
                    conn.commit()
            except Exception: 
                pass

        try:
            bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            pass

        return

    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, user_name, join_time) VALUES (?, ?, ?)", (user_id, full_name, time.time()))
        conn.commit()

    if OWNER_ID and user_id == OWNER_ID:
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = 'leaderboard_time'")
            res = cursor.fetchone()
            db_time = res[0] if res is not None else "22:00"
            
        welcome_text = (
            f"👑 *Greetings, Chief ({message.from_user.first_name})!*\n\n"
            f"⏳ Current leaderboard time: **{db_time}**\n"
            "⚙️ You can change the time directly here by typing *`/settime HH:MM`*\n"
            "🏆 To send the result immediately and reset the score, type *`/sendresult`*\n"
            "📢 Replying to any message with *`/broadcast`* will send it to all groups and users' personal inboxes.\n"
            "✨ you can make anyone an admin by using the *`/promote`* command.\n"
            "🍥 reply to any user's message with the *`/ban`* command.\n"
            "⚡ If you don't *`/unban`* them within 5 minutes, I will ban them permanently.\n"
            "📊 Use *`/status`* to see the bot's live stats."
        )
    else:
        welcome_text = (
            f"👋 *Hey {message.from_user.first_name}!*\n"
            f"*Welcome!* This bot is the easiest way to keep your groups active and engaged.\n\n"
            f"*📌 My Features:*\n\n"
            f"📊 *Daily Auto Poll:*\n"
            "Automatically sends a new poll every day at your set time interval.\n\n"
            "🏆 *Auto Result:*\n"
            "Generates results daily at 10 PM showing the Top 20 users' scores with negative marking.\n\n"
            "🚀 *How to Get Started:*\n\n"
            "1. *Add me* to your Telegram group.\n"
            "2. Make me a *Group Admin (so I have permission to send polls).*\n"
            "3. Use the *`/settings`* command inside your group to configure everything.\n\n"
            "For any help, simply type *`/help`* ."
        )
        
    markup = InlineKeyboardMarkup()
    add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=add_to_group_url, style="success"))
    
    try: 
        if selected_image_path:
            with open(selected_image_path, "rb") as photo_file:
                bot.send_photo(
                    chat_id=message.chat.id, 
                    photo=photo_file, 
                    caption=welcome_text, 
                    reply_markup=markup, 
                    parse_mode="Markdown"
                )
        else:
            bot.send_message(chat_id=message.chat.id, text=welcome_text, reply_markup=markup, parse_mode="Markdown")
    except Exception: 
        try: 
            bot.send_message(chat_id=message.chat.id, text=welcome_text, reply_markup=markup, parse_mode="Markdown")
        except Exception: 
            pass

@bot.message_handler(commands=['help'])
def send_help(message):
    chat_type = message.chat.type
    message_text = message.text.strip() if message.text else ""
    
    if chat_type in ['group', 'supergroup']:
        expected_full_command = f"/help@{BOT_USERNAME}"
        if "@" in message_text and not message_text.startswith(expected_full_command):
            return

    if chat_type in ['group', 'supergroup']:
        with sqlite3.connect(DB_FILE, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT help_msg_id FROM groups WHERE chat_id = ?", (message.chat.id,))
            row = cursor.fetchone()
            old_help_id = row[0] if row and row[0] else 0

        if old_help_id > 0:
            try: 
                bot.delete_message(chat_id=message.chat.id, message_id=old_help_id)
            except Exception: 
                pass

    help_text = (
        "⚡ *Help & Guide - Daily Poll Bot:*\n\n"
        "Here is a quick guide on how to configure and use the bot in your group:\n\n"
        "🛠 *Setup Instructions:*\n\n"
        "**Step 1:** Add this bot to your group.\n"
        "**Step 2:** Grant the bot Admin Permissions.\n"
        "**Step 3:** Type *`/settings`* inside the group to set up your poll timing and quiz language.\n\n"
        "🕒 *How the System Works:*\n\n"
        "**Polls:** Sent automatically during your configured daytime intervals.\n"
        "**Leaderboard:** Published automatically every single night at **10:00 PM.**\n"
        "Scoring: Accuracy matters! The leaderboard calculates the Top 20 users with a **negative marking system** applied for wrong answers.\n\n"
        "🔐 *`/settings`* - Open the configuration panel (Group Admins only)."
    )
    markup = InlineKeyboardMarkup()
    
    if OWNER_ID:
        owner_url = f"tg://user?id={int(OWNER_ID)}"
        markup.add(InlineKeyboardButton(text="💬 Contact Support", url=owner_url))
    
    try: 
        new_help_msg = bot.send_message(chat_id=message.chat.id, text=help_text, reply_markup=markup, parse_mode="Markdown")
        
        if chat_type in ['group', 'supergroup']:
            with sqlite3.connect(DB_FILE, timeout=20) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE groups SET help_msg_id = ? WHERE chat_id = ?", (new_help_msg.message_id, message.chat.id))
                conn.commit()
                
            try:
                bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
            except Exception:
                pass
                
    except Exception: 
        pass

@bot.my_chat_member_handler()
def handle_left_or_joined(my_chat_member):
    new_status = my_chat_member.new_chat_member.status
    old_status = my_chat_member.old_chat_member.status
    chat_id = my_chat_member.chat.id
    chat_title = my_chat_member.chat.title
    
    with sqlite3.connect(DB_FILE, timeout=20) as conn:
        cursor = conn.cursor()
        
        if new_status in ["administrator", "member"]:
            cursor.execute("SELECT chat_id FROM groups WHERE chat_id = ?", (chat_id,))
            group_exists = cursor.fetchone()
            
            if not group_exists or old_status in ["left", "kicked"]:
                if not group_exists:
                    cursor.execute("INSERT OR IGNORE INTO groups (chat_id, interval, last_sent_time) VALUES (?, 1800, 0)", (chat_id,))
                    conn.commit()
                
                group_text = (
                    f"🌟 *Hey everyone,* I'm poll bot, Thanks for the invite 💖\n\n"
                    f"🎉 *Join Group Successfully!*\n"
                    f"🇮🇳 *Group Name:* [{chat_title}]\n"
                    f"📢 Automated quizzes have been activated for this group.\n\n"
                    f"This bot is the easiest way to keep your groups active and engaged.\n\n"
                    f"📌 *My Features:*\n"
                    f"📊 *Daily Auto Poll:* Automatically sends a new poll every day at your set time interval.\n"
                    f"🏆 *Auto Result:* Generates results daily at 10 PM showing the Top 20 users' scores with negative marking.\n"
                    f"💡 *Results* ka wait nahi karna chahte to `/myscore` command send kare!\n\n"
                    f"🚀 *How to Get Started:*\n"
                    f"1. Make me a *Group Admin (so I have permission to send polls).*\n"
                    f"2. Use the *`/settings`* command inside your group to configure everything.\n\n"
                    f"For any help, simply type *`/help`*."
                )
                
                group_markup = InlineKeyboardMarkup()
                add_to_group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
                group_markup.add(InlineKeyboardButton(text="✨ ᴀᴅᴅ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ", url=add_to_group_url, style="primary"))
                
                try:
                    bot.send_message(chat_id=chat_id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
                except Exception:
                    try:
                        bot.send_message(chat_id=chat_id, text=group_text, reply_markup=group_markup, parse_mode="Markdown")
                    except Exception: pass
                
        elif new_status in ["left", "kicked"]:
            cleanup_group_data(chat_id, DB_FILE)

# ❤️‍🩹 थ्रेड्स स्टार्ट करें
threading.Thread(target=global_poll_manager, daemon=True).start()

print("✅ Successfully initialized bot! 🚀")

# =====================================================================
# 🌐 Render पर चलाने के लिए Flask server शुरू करें
# =====================================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    # Render के लिए WEBHOOK_URL को dynamically generate करें
    # Render पर RENDER_EXTERNAL_HOSTNAME environment variable automatic में set होता है
    render_hostname = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
    
    if render_hostname:
        # अगर Render पर है तो hostname use करें
        webhook_url = f"https://{render_hostname}/"
    else:
        # Local या अन्य platform के लिए fallback
        webhook_url = os.environ.get('WEBHOOK_URL', f"http://localhost:{port}/")
    
    # Webhook setup करें
    try:
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        print(f"✅ Webhook successfully set to: {webhook_url}")
    except Exception as e:
        print(f"❌ Webhook setup error: {e}")
        print(f"⚠️ Bot will try to work anyway, but webhooks may not function properly")
    
    print(f"🚀 Starting Flask server on http://0.0.0.0:{port}")
    print(f"📡 Listening for Telegram updates...")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
