import os
import asyncio
import time
import re
import random
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import SessionPasswordNeeded, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired, FloodWait, UserNotParticipant, ChannelPrivate
from telethon import TelegramClient as TClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError
import aiosqlite
import json
import hashlib

import io
import sqlite3

# Simple SQLite database helper for accounts
class Database:
    def __init__(self, path: str = "accounts.db"):
        self.path = path

    def _connect(self):
        return sqlite3.connect(self.path, check_same_thread=False)

    def init_db(self):
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE,
                    chat_id INTEGER,
                    reserved INTEGER DEFAULT 0,
                    session_string TEXT,
                    active INTEGER DEFAULT 0
                )
                """
            )

    def add_account(self, owner_id: int, phone: str, chat_id: int, active: int, session_string: str):
        # Upsert by phone to avoid duplicates
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO accounts (id, phone, chat_id, reserved, session_string, active)
                VALUES (
                    COALESCE((SELECT id FROM accounts WHERE phone = ?), NULL),
                    ?, ?, 0, ?, ?
                )
                """,
                (phone, phone, chat_id, session_string, active),
            )
            con.commit()

    def get_accounts(self, owner_id: int):
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("SELECT id, phone, chat_id, reserved, session_string, active FROM accounts")
            return cur.fetchall()

    def remove_account(self, account_id: int):
        with self._connect() as con:
            con.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            con.commit()

    def set_account_active(self, account_id: int, active: int):
        with self._connect() as con:
            con.execute("UPDATE accounts SET active = ? WHERE id = ?", (active, account_id))
            con.commit()


# Initialize DB
db = Database("accounts.db")
db.init_db()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===== CONFIGURATION =====
# Bot credentials
API_ID = 21453458
API_HASH = '565cac9ed11ff64ca7e2626f7b1b18b2'
BOT_TOKEN = '8065402926:AAFQSLlQM0D3FT6GJKZx8onxTggmvL_MwL0'
ADMIN_USER_ID = 5621201759
LOG_CHANNEL_ID = -1002874694180

# Bot settings
SESSION_NAME = "pokemon_guesser_bot"
WORKERS = 4

from pyrogram import enums as _enums

# Authorization decorator (also restricts to private chats)
def authorized_only(func):
    async def wrapper(client, message):
        # Only allow in private chats
        if getattr(message, "chat", None) and message.chat.type != _enums.ChatType.PRIVATE:
            # Silently ignore in groups/channels
            return
        # Authorization check
        if message.from_user.id not in AUTHORIZED_USERS:
            await message.reply("❌ You are not authorized to use this command.")
            return
        return await func(client, message)
    return wrapper

# Load authorized users from file
def load_authorized_users():
    if os.path.exists('authorized_users.json'):
        with open('authorized_users.json', 'r') as f:
            return set(json.load(f))
    return {ADMIN_USER_ID}

# Save authorized users to file
def save_authorized_users():
    with open('authorized_users.json', 'w') as f:
        json.dump(list(AUTHORIZED_USERS), f)

# Load authorized users
AUTHORIZED_USERS = load_authorized_users()
if ADMIN_USER_ID not in AUTHORIZED_USERS:
    AUTHORIZED_USERS.add(ADMIN_USER_ID)
    save_authorized_users()

# Initialize the Pyrogram client
app = Client(
    name=SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=WORKERS,
    plugins=dict(root="handlers")
)

# Print startup info
print("="*50)
print(f"Starting {SESSION_NAME}...")
print(f"API_ID: {API_ID}")
print(f"API_HASH: {API_HASH}")
print(f"BOT_TOKEN: {BOT_TOKEN[:10]}...")
print(f"ADMIN_USER_ID: {ADMIN_USER_ID}")
print(f"LOG_CHANNEL_ID: {LOG_CHANNEL_ID}")
print("="*50)

# Database setup
DB_NAME = 'bot_data.db'

# Global variables
login_states = {}
account_clients = {}
account_tasks = {}

# Helper functions
async def is_admin(user_id: int) -> bool:
    """Check if a user is an admin."""
    return user_id == ADMIN_USER_ID

# Admin commands
@app.on_message(filters.command("auth") & filters.user(ADMIN_USER_ID))
@authorized_only
async def auth_commands(client, message):
    if len(message.command) < 3:
        await message.reply(
            "Usage:\n"
            "/auth add <user_id> - Authorize a user\n"
            "/auth remove <user_id> - Remove user authorization\n"
            "/auth list - List all authorized users"
        )
        return

    cmd = message.command[1].lower()
    
    if cmd == "add":
        try:
            user_id = int(message.command[2])
            if user_id in AUTHORIZED_USERS:
                await message.reply(f"User {user_id} is already authorized.")
            else:
                AUTHORIZED_USERS.add(user_id)
                save_authorized_users()
                await message.reply(f"✅ User {user_id} has been authorized.")
        except (ValueError, IndexError):
            await message.reply("❌ Invalid user ID format.")
            
    elif cmd == "remove":
        try:
            user_id = int(message.command[2])
            if user_id == ADMIN_USER_ID:
                await message.reply("❌ Cannot remove owner's authorization.")
            elif user_id in AUTHORIZED_USERS:
                AUTHORIZED_USERS.remove(user_id)
                save_authorized_users()
                await message.reply(f"✅ User {user_id} has been removed from authorized users.")
            else:
                await message.reply(f"❌ User {user_id} is not in the authorized list.")
        except (ValueError, IndexError):
            await message.reply("❌ Invalid user ID format.")
            
    elif cmd == "list":
        users_list = "\n".join([f"- `{uid}`" for uid in sorted(AUTHORIZED_USERS)])
        await message.reply(f"🔐 Authorized Users:\n{users_list}")
    else:
        await message.reply("❌ Unknown command. Use /auth for help.")

@app.on_message(filters.command('start'))
@authorized_only
async def start_cmd(client, message: Message):
    user = message.from_user
    try:
        # Try to log to the channel, but don't fail if we can't
        try:
            await app.send_message(
                LOG_CHANNEL_ID,
                f"USER {user.username or user.id} WANTS TO USE THE BOT."
            )
        except Exception as e:
            print(f"Warning: Could not log to channel: {e}")
            # Continue execution even if logging fails
            
        if not await is_admin(user.id):
            await message.reply("❌ You are not authorized to use this bot.")
            return
            
        await message.reply("👋 Welcome, Admin! Use /help to see commands.")
        
    except Exception as e:
        print(f"Error in start_cmd: {e}")
        # Still respond to the user even if there was an error
        if not await is_admin(user.id):
            await message.reply("❌ You are not authorized to use this bot.")
        else:
            await message.reply("👋 Welcome, Admin! Use /help to see commands.")

@app.on_message(filters.command('help'))
@authorized_only
async def help_cmd(client, message: Message):
    if not await is_admin(message.from_user.id):
        await message.reply("❌ You are not authorized to use this bot.")
        return
    await message.reply(
        "/login - Add a new account\n"
        "/accounts - List your accounts\n"
        "/logout - Log out an account\n"
        "/remove - Remove an account\n"
        "/startall - Start guessing for all accounts\n"
        "/stopall - Stop all guessing\n"
        "/status - Show status\n"
        "/help - Show this help message"
    )

async def cleanup_login_state(user_id):
    """Clean up login state and disconnect any active clients."""
    state = login_states.pop(user_id, None)
    if state and 'telethon_client' in state:
        try:
            await state['telethon_client'].disconnect()
        except:
            pass
    return state

@app.on_message(filters.command('cancel') & filters.private)
@authorized_only
async def cancel_cmd(client, message: Message):
    """Cancel the current login process."""
    user_id = message.from_user.id
    if user_id in login_states:
        state = await cleanup_login_state(user_id)
        if state and 'phone' in state:
            await message.reply(f"❌ Login process for {state['phone']} has been cancelled.")
        else:
            await message.reply("❌ Login process cancelled.")
    else:
        await message.reply("No active login process to cancel.")

@app.on_message(filters.command('login') & filters.private)
@authorized_only
async def login_cmd(client, message: Message):
    user_id = message.from_user.id
    if not await is_admin(user_id):
        await message.reply("❌ You are not authorized to use this bot.")
        return
    
    # Clean up any existing state
    await cleanup_login_state(user_id)
    
    login_states[user_id] = {'step': 'phone', 'retry_count': 0}
    await message.reply(
        "🔑 Please enter your phone number (with country code, e.g., +1234567890):\n"
        "You can type /cancel at any time to abort the login process."
    )

@app.on_message(filters.text & ~filters.command(['start', 'help', 'login', 'accounts', 'logout', 'remove', 'startall', 'stopall', 'status', 'cancel']))
@authorized_only
async def login_flow_handler(client, message: Message):
    user_id = message.from_user.id
    if not await is_admin(user_id):
        return
        
    state = login_states.get(user_id)
    if not state:
        return
        
    step = state.get('step')
    if not step:
        login_states.pop(user_id, None)
        return
        
    try:
        if step == 'phone':
            phone = message.text.strip()
            if not phone:
                await message.reply("❌ Please enter a valid phone number (e.g., +1234567890):")
                return
                
            state['phone'] = phone
            try:
                telethon_client = TClient(StringSession(), API_ID, API_HASH)
                await telethon_client.connect()
                sent = await telethon_client.send_code_request(phone)
                
                state['telethon_client'] = telethon_client
                state['sent'] = sent
                state['step'] = 'otp'
                state['retry_count'] = 0
                
                await message.reply("🔑 Please enter the 5-digit OTP you received (format: 1 2 3 4 5):")
                
            except Exception as e:
                error_msg = str(e).lower()
                if 'flood' in error_msg:
                    await message.reply("❌ Too many attempts. Please try again later.")
                elif 'phone' in error_msg:
                    await message.reply("❌ Invalid phone number. Please try again with a valid number.")
                else:
                    await message.reply(f"❌ Error sending code: {e}")
                
                login_states.pop(user_id, None)
                try:
                    if 'telethon_client' in locals():
                        await telethon_client.disconnect()
                except:
                    pass
                
        elif step == 'otp':
            otp_raw = message.text.strip()
            
            # More flexible OTP format handling
            if not re.fullmatch(r'[0-9\s]{5,10}', otp_raw):
                state['retry_count'] = state.get('retry_count', 0) + 1
                if state['retry_count'] >= 3:
                    await message.reply("❌ Too many invalid attempts. Please restart with /login.")
                    login_states.pop(user_id, None)
                    return
                await message.reply("❌ Invalid OTP format. Please enter exactly 5 digits (e.g., '12345' or '1 2 3 4 5'):")
                return
                
            otp = re.sub(r'\s+', '', otp_raw)
            telethon_client = state.get('telethon_client')
            
            if not telethon_client:
                await message.reply("❌ Session expired. Please restart with /login.")
                login_states.pop(user_id, None)
                return
                
            try:
                phone = state['phone']
                sent = state.get('sent')
                if not sent:
                    raise Exception("Session data missing")
                    
                await telethon_client.sign_in(phone=phone, code=otp, phone_code_hash=sent.phone_code_hash)
                state['step'] = 'group_id'
                state['retry_count'] = 0  # Reset retry counter
                await message.reply(
                    f"✅ Verification successful!\n"
                    f"Now, please provide the group ID where you want to use this account.\n"
                    "You can get the group ID by adding @username_to_id_bot to the group and sending /id"
                )
                
            except SessionPasswordNeededError:
                state['step'] = 'password'
                await message.reply("🔒 Please enter your 2FA password:")
                
            except Exception as e:
                error_msg = str(e).lower()
                if 'code' in error_msg or 'invalid' in error_msg:
                    state['retry_count'] = state.get('retry_count', 0) + 1
                    if state['retry_count'] >= 3:
                        await message.reply("❌ Too many failed attempts. Please restart with /login.")
                        login_states.pop(user_id, None)
                        try:
                            await telethon_client.disconnect()
                        except:
                            pass
                        return
                    await message.reply(f"❌ Invalid code. Please try again ({state['retry_count']}/3):")
                else:
                    await message.reply(f"❌ Error: {e}\nPlease restart with /login.")
                    login_states.pop(user_id, None)
                    try:
                        await telethon_client.disconnect()
                    except:
                        pass
                        
        elif step == 'password':
            password = message.text.strip()
            telethon_client = state.get('telethon_client')
            
            if not telethon_client:
                await message.reply("❌ Session expired. Please restart with /login.")
                login_states.pop(user_id, None)
                return
                
            try:
                if not password or password == '.':
                    await message.reply("❌ 2FA password is required. Please enter your password:")
                    return
                    
                await telethon_client.sign_in(password=password)
                state['step'] = 'group_id'
                state['retry_count'] = 0  # Reset retry counter
                await message.reply(
                    f"✅ 2FA verified!\n"
                    f"Now, please provide the group ID where you want to use {state['phone']}.\n"
                    "You can get the group ID by adding @username_to_id_bot to the group and sending /id"
                )
                
            except Exception as e:
                error_msg = str(e).lower()
                if 'password' in error_msg or 'invalid' in error_msg:
                    state['retry_count'] = state.get('retry_count', 0) + 1
                    if state['retry_count'] >= 3:
                        await message.reply("❌ Too many failed attempts. Please restart with /login.")
                        login_states.pop(user_id, None)
                        try:
                            await telethon_client.disconnect()
                        except:
                            pass
                        return
                    await message.reply(f"❌ Incorrect password. Please try again ({state['retry_count']}/3):")
                else:
                    await message.reply(f"❌ Error: {e}\nPlease restart with /login.")
                    login_states.pop(user_id, None)
                    try:
                        await telethon_client.disconnect()
                    except:
                        pass
                        
        elif step == 'group_id':
            try:
                group_id = int(message.text.strip())
                telethon_client = state.get('telethon_client')
                
                if not telethon_client:
                    await message.reply("❌ Session expired. Please restart with /login.")
                    login_states.pop(user_id, None)
                    return
                
                # Test if we can access the group
                try:
                    chat = await telethon_client.get_entity(group_id)
                    if not chat:
                        raise Exception("Group not found")
                    
                    # Check if bot is admin in the group
                    try:
                        me = await telethon_client.get_me()
                        participant = await telethon_client.get_permissions(group_id, me)
                        if not (participant.is_admin or participant.is_creator):
                            raise Exception("Bot must be an admin in the group")
                    except Exception as e:
                        await message.reply(
                            "❌ I need to be an admin in the group to function properly. "
                            "Please make me an admin and try again with the group ID:"
                        )
                        return
                except Exception as e:
                    await message.reply(f"❌ Cannot access group {group_id}. Please make sure the bot is added to the group and the group ID is correct.")
                    return
                
                # Save the session
                session_string = telethon_client.session.save()
                
                # Check if account already exists
                existing_accounts = db.get_accounts(ADMIN_USER_ID)
                for acc in existing_accounts:
                    if acc[1] == state['phone']:
                        db.remove_account(acc[0])
                
                # Add new account
                db.add_account(ADMIN_USER_ID, state['phone'], group_id, 0, session_string)
                
                await message.reply(
                    f"✅ Successfully logged in!\n"
                    f"📱 Account: {state['phone']}\n"
                    f"👥 Group: {chat.title if hasattr(chat, 'title') else group_id}\n\n"
                    "You can now use /startall to begin guessing!"
                )
                
            except ValueError:
                await message.reply("❌ Invalid group ID. Please enter a valid numeric group ID:")
                return
            except Exception as e:
                await message.reply(f"❌ Error: {e}\nPlease try again with a valid group ID:")
                return
            finally:
                try:
                    if 'telethon_client' in state:
                        await state['telethon_client'].disconnect()
                except:
                    pass
                login_states.pop(user_id, None)
                
    except Exception as e:
        await message.reply(f"❌ An unexpected error occurred: {e}\nPlease try again with /login.")
        login_states.pop(user_id, None)
        try:
            if 'telethon_client' in state:
                await state['telethon_client'].disconnect()
        except:
            pass

@app.on_message(filters.command('accounts'))
@authorized_only
async def accounts_cmd(client, message: Message):
    if not await is_admin(message.from_user.id):
        await message.reply("❌ You are not authorized to use this bot.")
        return
    accounts = db.get_accounts(ADMIN_USER_ID)
    if not accounts:
        await message.reply("No accounts found.")
        return
    msg = "<b>Accounts:</b>\n"
    for acc in accounts:
        msg += f"• <b>Phone:</b> {acc[1]} | <b>Chat ID:</b> {acc[2]} | <b>Active:</b> {'✔' if acc[5] else '❌'}\n"
    await message.reply(msg, parse_mode=enums.ParseMode.HTML)

@app.on_message(filters.command('logout'))
@authorized_only
async def logout_cmd(client, message: Message):
    """Log out and remove a specific account."""
    if not await is_admin(message.from_user.id):
        await message.reply("❌ You are not authorized to use this bot.")
        return
        
    args = message.text.split()
    if len(args) < 2:
        await message.reply("❌ Usage: /logout <phone>")
        return
        
    phone = args[1].strip()
    accounts = db.get_accounts(ADMIN_USER_ID)
    
    # Check if the account is currently active in guessing tasks
    global account_tasks, account_clients
    if phone in account_tasks:
        try:
            # Cancel the task
            account_tasks[phone].cancel()
            # Remove from active tasks
            del account_tasks[phone]
            # Disconnect the client if it exists
            if phone in account_clients:
                try:
                    if account_clients[phone].is_connected():
                        await account_clients[phone].disconnect()
                    del account_clients[phone]
                except Exception as e:
                    print(f"Error disconnecting client {phone}: {e}")
        except Exception as e:
            print(f"Error stopping task for {phone}: {e}")
    
    # Remove from database
    removed = False
    for acc in accounts:
        if acc[1] == phone:
            db.remove_account(acc[0])
            removed = True
            break
    
    if removed:
        await message.reply(f"✅ Successfully logged out and removed account: {phone}")
    else:
        await message.reply(f"❌ No account found with phone: {phone}")

@app.on_message(filters.command('remove'))
@authorized_only
async def remove_cmd(client, message: Message):
    if not await is_admin(message.from_user.id):
        await message.reply("❌ You are not authorized to use this bot.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Usage: /remove <phone>")
        return
    phone = args[1]
    accounts = db.get_accounts(ADMIN_USER_ID)
    for acc in accounts:
        if acc[1] == phone:
            db.remove_account(acc[0])
            await message.reply(f"✅ Removed account {phone}.")
            return
    await message.reply(f"❌ No account found for {phone}.")

# Guessing logic management
account_clients = {}
account_tasks = {}

async def get_account_clients():
    global account_clients
    # Clear any disconnected clients
    account_clients = {k: v for k, v in account_clients.items() if v.is_connected if hasattr(v, 'is_connected') and callable(v.is_connected) and await v.is_connected()}
    
    # Initialize any new accounts
    accounts = db.get_accounts(ADMIN_USER_ID)
    for acc in accounts:
        phone, session_string = acc[1], acc[4]
        if phone not in account_clients:
            try:
                client = TClient(StringSession(session_string), API_ID, API_HASH)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.start()
                account_clients[phone] = client
            except Exception as e:
                print(f"Failed to start client for {phone}: {str(e)}")
    
    return account_clients

# Global dictionary to store guessing state per chat
chat_states = {}

async def guessing_logic(client, chat_id):
    """Main guessing logic for the Pokemon guessing game."""
    # Initialize chat state if it doesn't exist
    if chat_id not in chat_states:
        chat_states[chat_id] = {
            'last_guess_time': 0,
            'pending_guess': False,
            'guess_timeout': 15,  # Time to wait for a response after /guess
            'client': client,     # Store the client in the state
            'chat_id': chat_id,   # Store the chat_id in the state
            'retry_lock': asyncio.Lock(),  # Prevent concurrent retries
            'is_running': True    # Control the guessing loop
        }
    
    state = chat_states[chat_id]
    
    async def send_guess_command():
        """Send the /guess command with error handling."""
        try:
            async with state['retry_lock']:
                await log_message(chat_id, f"Sending /guess to chat {chat_id}")
                await client.send_message(chat_id, '/guess')
                state['last_guess_time'] = time.time()
                state['pending_guess'] = True
                return True
        except Exception as e:
            error_msg = f"Error sending /guess: {str(e)}"
            await log_message(chat_id, error_msg)
            return False
    
    # Register message handler for this chat
    @client.on_message(filters.chat(chat_id))
    async def guess_pokemon(_, message):
        """Handle the Pokemon guessing game messages."""
        try:
            message_text = (message.text or '').lower()
            
            # Check if it's a "Who's that pokemon?" message
            if "who's that pokemon" in message_text and '?' in message_text:
                if not state['pending_guess']:
                    await log_message(chat_id, "Pokemon question detected. Sending guess command.")
                    asyncio.create_task(send_guess_command())
                return
                
            # Check if it's the answer to the previous guess
            if "the pokemon was" in message_text and state['pending_guess']:
                try:
                    pokemon_name = message_text.split("the pokemon was **")[1].split("**")[0].strip()
                    await log_message(chat_id, f"Correct answer was: {pokemon_name}")
                    
                    # Save the answer to cache if needed
                    if hasattr(message, 'photo') and message.photo:
                        # Get the smallest photo size
                        photo = message.photo.file_id
                        # Save photo info to cache
                        cache_dir = os.path.join("cache", f"{pokemon_name}.txt")
                        os.makedirs("cache", exist_ok=True)
                        with open(cache_dir, 'w') as f:
                            f.write(photo)
                except Exception as e:
                    await log_message(chat_id, f"Error processing answer: {e}")
                
                # Reset state and schedule next guess
                state['pending_guess'] = False
                await asyncio.sleep(2)
                asyncio.create_task(send_guess_command())
                
            # Check if it's a list of possible answers
            elif any(phrase in message_text for phrase in ["is it", "could it be", "maybe it's"]) and '?' in message_text:
                if not state['pending_guess']:
                    return
                    
                try:
                    # Extract possible answers from the message
                    possible_answers = []
                    lines = [line.strip() for line in message_text.split('\n') if line.strip()]
                    for line in lines[1:]:  # Skip the first line (question)
                        if line and len(line) < 30 and not any(char.isdigit() for char in line):
                            possible_answers.append(line.strip())
                    
                    # If we have possible answers, make a random guess
                    if possible_answers:
                        guess = random.choice(possible_answers)
                        await log_message(chat_id, f"Making a guess: {guess}")
                        await client.send_message(chat_id, f"/guess_{guess}")
                        state['pending_guess'] = False
                        # Schedule next guess
                        await asyncio.sleep(2)
                        asyncio.create_task(send_guess_command())
                except Exception as e:
                    await log_message(chat_id, f"Error making guess: {e}")
                    # Try to recover by restarting the guess cycle
                    await asyncio.sleep(2)
                    asyncio.create_task(send_guess_command())
        
        except Exception as e:
            await log_message(chat_id, f"Error in guess_pokemon: {e}")
    
    # Start the guessing process
    await send_guess_command()
    
    # Register the message handler with a unique name
    handler_name = f"guess_pokemon_{chat_id}"
    
    # Remove any existing handler for this chat to avoid duplicates
    client.remove_handler(handler_name)
    
    @client.on_message(filters.chat(int(chat_id)) & ~filters.command, group=hash(handler_name) % 1000)
    async def guess_pokemon_handler(_, message):
        """Handle the Pokemon guessing game messages."""
        try:
            # Get the current state for this chat
            if chat_id not in chat_states:
                return
                
            state = chat_states[chat_id]
            if not state.get('pending_guess', False):
                return
                
            try:
                pokemon_name = message.text.split("The pokemon was **")[1].split("**")[0].strip()
                await log_message(chat_id, f"Correct answer was: {pokemon_name}")
                
                # Save the answer for future reference
                cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
                os.makedirs(cache_dir, exist_ok=True)
                
                # Check if there's a cached image
                cache_file = os.path.join(cache_dir, f"{pokemon_name}.txt")
                if os.path.exists('cache.txt'):
                    os.rename('cache.txt', cache_file)
                
                state['pending_guess'] = False
                await asyncio.sleep(2)
                await send_guess_command()
                
            except Exception as e:
                await log_message(chat_id, f"Error processing answer: {e}")
                state['pending_guess'] = False
                await asyncio.sleep(2)
                await send_guess_command()
                
        except Exception as e:
            await log_message(chat_id, f"Error in handle_pokemon_answer: {e}")
    
    # Register the message handler for the image question
    @client.on_message(filters.chat(chat_id) & filters.photo)
    async def handle_pokemon_image(_, message):
        """Handle the Pokemon image question."""
        try:
            if chat_id not in chat_states:
                return
                
            state = chat_states[chat_id]
            if not state.get('pending_guess', False):
                return
                
            try:
                # Get the photo file ID
                photo = message.photo
                file_id = photo.file_id
                
                # Save the file ID for future reference
                with open('cache.txt', 'w') as f:
                    f.write(file_id)
                
                # Check if we have a cached answer for this image
                cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
                os.makedirs(cache_dir, exist_ok=True)
                
                for filename in os.listdir(cache_dir):
                    if filename.endswith('.txt'):
                        with open(os.path.join(cache_dir, filename), 'r') as f:
                            cached_id = f.read().strip()
                            if cached_id == file_id:
                                # Found a match, send the answer
                                pokemon_name = filename.split('.')[0]
                                await client.send_message(chat_id, f"/guess_{pokemon_name}")
                                state['pending_guess'] = False
                                await asyncio.sleep(2)
                                await send_guess_command()
                                return
                
                # If no match found, just send a random guess
                await log_message(chat_id, "No cached answer found, sending random guess.")
                await client.send_message(chat_id, "/guess")  # Default guess
                state['pending_guess'] = False
                await asyncio.sleep(2)
                await send_guess_command()
                
            except Exception as e:
                await log_message(chat_id, f"Error processing image: {e}")
                state['pending_guess'] = False
                await asyncio.sleep(2)
                await send_guess_command()
                
        except Exception as e:
            await log_message(chat_id, f"Error in handle_pokemon_image: {e}")
    
    # Start the main guessing loop
    try:
        while state.get('is_running', True):
            if not state.get('pending_guess', False):
                await send_guess_command()
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        await log_message(chat_id, "Guessing task was cancelled")
    except Exception as e:
        await log_message(chat_id, f"Error in guessing loop: {e}")
    finally:
        # Clean up
        state['is_running'] = False
        state['pending_guess'] = False
        try:
            await asyncio.sleep(3600)  # Sleep for a long time but keep the function running
        except asyncio.CancelledError:
            await log_message(chat_id, "Guessing task was cancelled.")
            return
        except Exception as e:
            await log_message(chat_id, f"Error in guessing loop: {e}")
            await asyncio.sleep(10)  # Wait before continuing the loop

async def log_message(chat_id, msg):
    """Helper function to log messages to console and admin."""
    if chat_id not in chat_states:
        return
        
    state = chat_states[chat_id]
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"[{timestamp}] [{state.get('phone', 'unknown')}] {msg}"
    print(log_msg)
    try:
        await app.send_message(ADMIN_USER_ID, log_msg)
    except:
        pass

async def send_guess_command(client, chat_id):
    """Send the /guess command with error handling and retries."""
    if chat_id not in chat_states:
        return False
        
    state = chat_states[chat_id]
    
    # Check if we should wait before sending next guess
    time_since_last = time.time() - state['last_guess_time']
    if time_since_last < state['guess_timeout']:
        await asyncio.sleep(state['guess_timeout'] - time_since_last)
    
    try:
        await log_message(chat_id, f"Sending /guess to chat {chat_id}")
        await client.send_message(chat_id, '/guess')
        state['last_guess_time'] = time.time()
        state['pending_guess'] = True
        state['retry_count'] = 0  # Reset retry count on success
        return True
    except Exception as e:
        state['retry_count'] += 1
        error_msg = f"Error sending /guess (attempt {state['retry_count']}/{state['max_retries']}): {str(e)}"
        await log_message(chat_id, error_msg)
        
        if state['retry_count'] >= state['max_retries']:
            await log_message(chat_id, "Max retries reached. Stopping guessing for this account.")
            return False
            
        # Wait before retrying
        await asyncio.sleep(state['retry_delay'] * state['retry_count'])
        return await send_guess_command(client, chat_id)

async def start_message_handler(client, chat_id):
    """Register message handler for the guessing game."""
    @client.on_message(filters.chat(chat_id))
    async def guess_pokemon(_, message):
        """Handle the Pokemon guessing game messages."""
        if chat_id not in chat_states:
            return
            
        state = chat_states[chat_id]
        message_text = message.text or '' if message.text else ''
        
        # Check if it's a Pokémon question message (more flexible matching)
        pokemon_question_phrases = [
            "who's that pokemon",
            "who is that pokemon",
            "guess the pokemon",
            "who's that pokémon",
            "who is that pokémon"
        ]
        
        # Check for photo messages that might be Pokémon questions
        if message.photo and any(phrase in message.caption.lower() for phrase in pokemon_question_phrases if message.caption):
            await log_message(chat_id, "Pokemon question with photo detected. Sending /guess command.")
            await send_guess_command(client, chat_id)
        # Check for text messages that are Pokémon questions
        elif any(phrase in message_text.lower() for phrase in pokemon_question_phrases):
            await log_message(chat_id, "Pokemon question detected. Sending /guess command.")
            await send_guess_command(client, chat_id)
        # Check if this is a response to our /guess command (options to choose from)
        elif "choose the correct answer" in message_text.lower() or "select the correct pokémon" in message_text.lower():
            await log_message(chat_id, f"Options detected: {message_text}")
            # Here you can add logic to select and click on an option
            # For now, we'll just log it
            pass
            
        # Check if it's a result message (after a guess)
        result_phrases = [
            "the pokémon was",
            "the pokemon was",
            "it was",
            "correct answer was"
        ]
        
        if any(phrase in message_text.lower() for phrase in result_phrases):
            state['pending_guess'] = False
            # Extract and log the Pokémon name if possible
            try:
                # Try different patterns to extract the Pokémon name
                patterns = [
                    r'\*\*(.*?)\*\*',  # **PokemonName**
                    r'`(.*?)`',          # `PokemonName`
                    r'"(.*?)"',         # "PokemonName"
                    r'\bwas\s+(?:a\s+)?([^.!?]+?)[.!?]',  # was a PokemonName.
                    r'\bis\s+(?:a\s+)?([^.!?]+?)[.!?]'    # is a PokemonName.
                ]
                
                pokemon_name = None
                for pattern in patterns:
                    match = re.search(pattern, message_text, re.IGNORECASE)
                    if match:
                        pokemon_name = match.group(1).strip()
                        if pokemon_name:
                            await log_message(chat_id, f"The Pokémon was: {pokemon_name}")
                            break
            except Exception as e:
                await log_message(chat_id, f"Error extracting Pokémon name: {e}")
            
            # Schedule next guess
            await asyncio.sleep(2)
            await send_guess_command(client, chat_id)
            return
            
        # Check if we need to make a guess (more flexible matching)
        if state.get('pending_guess', False) and any(phrase in message_text.lower() for phrase in ["choose", "select", "pick", "guess", "option"]):
            await log_message(chat_id, "Processing guessing options...")
            # Extract possible answers from the message using multiple patterns
            possible_answers = []
            
            # Pattern 1: Look for /guess_XXX patterns
            guess_matches = re.findall(r'/guess_([a-zA-Z0-9_]+)', message_text, re.IGNORECASE)
            if guess_matches:
                possible_answers.extend(guess_matches)
            
            # Pattern 2: Look for numbered options (1. Option1, 2. Option2, etc.)
            if not possible_answers:
                option_matches = re.findall(r'\d+[.)]\s*([^\n]+)', message_text)
                if option_matches:
                    possible_answers.extend([opt.strip().lower() for opt in option_matches])
            
            # Pattern 3: Look for lines that might contain Pokémon names
            if not possible_answers:
                lines = [line.strip() for line in message_text.split('\n') if line.strip()]
                if len(lines) > 1:  # If there are multiple lines, skip the first one (usually the question)
                    for line in lines[1:]:
                        # Skip empty lines or lines that are too long to be Pokémon names
                        if line and len(line) < 30 and not any(char.isdigit() for char in line):
                            possible_answers.append(line.strip())
            
            # If we found possible answers, make a random guess
            if possible_answers:
                # Choose a random answer
                guess = random.choice(possible_answers)
                await log_message(chat_id, f"Making a guess: {guess}")
                
                # Send the guess command
                try:
                    await client.send_message(chat_id, f"/guess_{guess}")
                    state['pending_guess'] = False
                    # Wait a bit before the next guess
                    await asyncio.sleep(2)
                    return
                except Exception as e:
                    await log_message(chat_id, f"Error making guess: {e}")
            else:
                # If we get here, we couldn't find any valid options
                await log_message(chat_id, "Could not find valid guessing options in the message")
                # Try to recover by sending another /guess command
                await asyncio.sleep(2)
                await send_guess_command(client, chat_id)
        
        # Check if this is a 'Who's that pokemon?' message with a photo
        if "who's that pokemon" in message_text.lower() and message.photo:
            await log_message(chat_id, "Detected 'Who's that pokemon?' with photo. Checking cache...")
            state['pending_guess'] = False
            
            try:
                # Download the photo
                photo_path = await message.download()
                
                # Generate a signature for the photo
                with Image.open(photo_path) as img:
                    # Resize to a small size to make comparison faster
                    img = img.resize((100, 100), Image.LANCZOS)
                    # Convert to grayscale
                    img = img.convert('L')
                    # Get the image data as bytes
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    photo_signature = hashlib.md5(img_byte_arr.getvalue()).hexdigest()
                
                # Clean up the downloaded file
                os.remove(photo_path)
                
                # Check if we've seen this photo before
                cache_dir = os.path.join('cache', chat_id)
                os.makedirs(cache_dir, exist_ok=True)
                
                cache_file = os.path.join(cache_dir, f"{photo_signature}.txt")
                if os.path.exists(cache_file):
                    with open(cache_file, 'r') as f:
                        pokemon_name = f.read().strip()
                    await log_message(chat_id, f"Found cached Pokémon: {pokemon_name}")
                    await client.send_message(chat_id, f"/guess_{pokemon_name}")
                    return
                else:
                    await log_message(chat_id, "New Pokémon photo detected. No cache found.")
                    # Save the photo for future reference
                    os.makedirs('saitama', exist_ok=True)
                    photo_path = os.path.join('saitama', f"{photo_signature}.jpg")
                    await message.download(photo_path)
            except Exception as e:
                await log_message(chat_id, f"Error processing photo: {e}")
        
        # Handle other message types or unknown commands
        # This is a catch-all for any messages we don't specifically handle
        # You can add more specific handlers above this if needed
        
        # Reset pending_guess if it's been too long since the last guess
        if state.get('pending_guess', False) and (time.time() - state.get('last_guess_time', 0) > 30):
            await log_message(chat_id, "Resetting pending_guess due to timeout")
            state['pending_guess'] = False
            
    # Add the handler to the client's message handlers
    client.add_handler(guess_pokemon)
    
    # Handler for when the answer is revealed
    @client.on_message(filters.regex(r"(The pokemon was|It was|Correct answer was)") & filters.chat(chat_id))
    async def save_pokemon(client, message):
        if chat_id not in chat_states:
            return
            
        state = chat_states[chat_id]
        state['pending_guess'] = False
        message_text = message.text or ''
        
        # Extract Pokémon name using multiple possible patterns
        pokemon_name = None
        patterns = [
            r'[Tt]he [Pp]ok[ée]mon was \*\*(.*?)\*\*',  # The pokemon was **name**
            r'[Ii]t was \*\*(.*?)\*\*',                  # It was **name**
            r'[Cc]orrect answer was \*\*(.*?)\*\*',      # Correct answer was **name**
            r'[Tt]he [Pp]ok[ée]mon was \"(.*?)\"',      # The pokemon was "name"
            r'[Ii]t was \"(.*?)\"',                    # It was "name"
            r'[Cc]orrect answer was \"(.*?)\"',        # Correct answer was "name"
            r'[Tt]he [Pp]ok[ée]mon was (?:a )?([A-Z][a-z]+)(?:\W|$)'  # The pokemon was Name
        ]
        
        for pattern in patterns:
            match = re.search(pattern, message_text)
            if match:
                pokemon_name = match.group(1).strip()
                break
        
        if not pokemon_name:
            await log_message(chat_id, f"Could not extract Pokémon name from: {message_text}")
            await asyncio.sleep(2)
            await send_guess_command(client, chat_id)
            return
        
        await log_message(chat_id, f"The Pokémon was: {pokemon_name}")
        
        # If we have a cache file with an unknown Pokémon, save it to the cache
        cache_file = "saitama/cache.txt"
        try:
            if os.path.exists(cache_file):
                try:
                    # Read the photo signature from the cache file
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        photo_signature = f.read().strip()
                    
                    if photo_signature:
                        # Save to cache with Pokémon name
                        cache_dir = "cache"
                        os.makedirs(cache_dir, exist_ok=True)
                        cache_path = os.path.join(cache_dir, f"{pokemon_name.lower()}.txt")
                        
                        # Save the photo signature to the cache
                        with open(cache_path, 'w', encoding='utf-8') as f:
                            f.write(photo_signature)
                        
                        await log_message(chat_id, f"Saved {pokemon_name} to cache as {cache_path}")
                        
                        # Clean up the temporary cache file
                        try:
                            os.remove(cache_file)
                        except Exception as e:
                            await log_message(chat_id, f"Error removing temporary cache file: {e}")
                    
                except Exception as e:
                    await log_message(chat_id, f"Error processing cache file: {e}")
            
        except Exception as e:
            await log_message(chat_id, f"Error in save_pokemon: {e}")
        finally:
            # Always prepare for the next guess
            await asyncio.sleep(2)
            await send_guess_command(client, chat_id)

    # Handler for when a game is already active
    @client.on_message(filters.regex("There is already a guessing game being played") & filters.chat(chat_id))
    async def handle_active_game(client, message):
        if chat_id not in chat_states:
            return
            
        state = chat_states[chat_id]
        await log_message(chat_id, "Game already active. Will retry shortly...")
        await asyncio.sleep(10)
        await send_guess_command(client, chat_id)
    
    # Add the save_pokemon handler to the client
    @client.on_message(filters.regex(r"(The pokemon was|It was|Correct answer was)") & filters.chat(chat_id))
    async def save_pokemon_handler(client, message):
        if chat_id not in chat_states:
            return
            
        state = chat_states[chat_id]
        state['pending_guess'] = False
        message_text = message.text or ''
        
        # Extract Pokémon name using multiple possible patterns
        pokemon_name = None
        patterns = [
            r'[Tt]he [Pp]ok[ée]mon was \*\*(.*?)\*\*',  # The pokemon was **name**
            r'[Ii]t was \*\*(.*?)\*\*',                  # It was **name**
            r'[Cc]orrect answer was \*\*(.*?)\*\*',      # Correct answer was **name**
            r'[Tt]he [Pp]ok[ée]mon was \"(.*?)\"',      # The pokemon was "name"
            r'[Ii]t was \"(.*?)\"',                    # It was "name"
            r'[Cc]orrect answer was \"(.*?)\"',        # Correct answer was "name"
            r'[Tt]he [Pp]ok[ée]mon was (?:a )?([A-Z][a-z]+)(?:\W|$)'  # The pokemon was Name
        ]
        
        for pattern in patterns:
            match = re.search(pattern, message_text)
            if match:
                pokemon_name = match.group(1).strip()
                break
        
        if not pokemon_name:
            await log_message(chat_id, f"Could not extract Pokémon name from: {message_text}")
            await asyncio.sleep(2)
            await send_guess_command(client, chat_id)
            return
        
        await log_message(chat_id, f"The Pokémon was: {pokemon_name}")
        
        # If we have a cache file with an unknown Pokémon, save it to the cache
        cache_file = "saitama/cache.txt"
        try:
            if os.path.exists(cache_file):
                try:
                    # Read the photo signature from the cache file
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        photo_signature = f.read().strip()
                    
                    if photo_signature:
                        # Save to cache with Pokémon name
                        cache_dir = "cache"
                        os.makedirs(cache_dir, exist_ok=True)
                        cache_path = os.path.join(cache_dir, f"{pokemon_name.lower()}.txt")
                        
                        # Save the photo signature to the cache
                        with open(cache_path, 'w', encoding='utf-8') as f:
                            f.write(photo_signature)
                        
                        await log_message(chat_id, f"Saved {pokemon_name} to cache as {cache_path}")
                        
                        # Clean up the temporary cache file
                        try:
                            os.remove(cache_file)
                        except Exception as e:
                            await log_message(chat_id, f"Error removing temporary cache file: {e}")
                    
                except Exception as e:
                    await log_message(chat_id, f"Error processing cache file: {e}")
            
        except Exception as e:
            await log_message(chat_id, f"Error in save_pokemon: {e}")
        finally:
            # Always prepare for the next guess
            await asyncio.sleep(2)
            await send_guess_command(client, chat_id)

    # Add the message handlers to the client
    client.add_handler(handle_active_game)
    
    try:
        # Check if client is connected
        is_connected = False
        if hasattr(client, 'is_connected'):
            if asyncio.iscoroutinefunction(client.is_connected):
                is_connected = await client.is_connected()
            else:
                is_connected = client.is_connected()
        
        # Connect if not connected
        if not is_connected:
            try:
                if asyncio.iscoroutinefunction(client.connect):
                    await client.connect()
                else:
                    client.connect()
                await asyncio.sleep(1)  # Small delay after connection
            except Exception as e:
                await log_message(chat_id, f"Failed to connect: {e}")
                return
        
        # Start the guessing process
        await send_guess_command(client, chat_id)

        # Keep the client running
        while True:
            try:
                await asyncio.sleep(3600)  # Keep the task alive
            except asyncio.CancelledError:
                await log_message(chat_id, "Guessing task was cancelled")
                break
            except Exception as e:
                await log_message(chat_id, f"Error in main loop: {e}")
                await asyncio.sleep(5)  # Prevent tight loop on errors
                
    except Exception as e:
        await log_message(chat_id, f"Fatal error in guessing logic: {e}")
    finally:
        # Clean up
        try:
            if hasattr(client, 'disconnect'):
                if asyncio.iscoroutinefunction(client.disconnect):
                    await client.disconnect()
                else:
                    client.disconnect()
        except Exception as e:
            await log_message(chat_id, f"Error during client disconnection: {e}")

async def is_task_running(task):
    """Check if an asyncio task is still running."""
    if task is None:
        return False
    return not (task.done() or task.cancelled())

@app.on_message(filters.command('startall'))
@authorized_only
async def startall_cmd(client, message: Message):
    """Start the guessing process for all accounts."""
    try:
        if not await is_admin(message.from_user.id):
            await message.reply("❌ You are not authorized to use this bot.")
            return
            
        accounts = db.get_accounts(ADMIN_USER_ID)
        if not accounts:
            await message.reply("No accounts found. Use /login to add an account first.")
            return
        
        global account_tasks, chat_states
        account_clients = await get_account_clients()
        started_count = 0
        errors = []
        
        for acc in accounts:
            phone, chat_id, acc_id = acc[1], acc[2], acc[0]
            try:
                # Skip if already running
                if phone in account_tasks and await is_task_running(account_tasks[phone].get('task')):
                    continue
                    
                if phone in account_clients:
                    client = account_clients[phone]
                    
                    # Connect client if not connected
                    try:
                        # Check if client is already connected
                        is_connected = False
                        if hasattr(client, 'is_connected'):
                            if asyncio.iscoroutinefunction(client.is_connected):
                                is_connected = await client.is_connected()
                            else:
                                is_connected = client.is_connected()
                        
                        if not is_connected:
                            # Connect the client
                            if asyncio.iscoroutinefunction(client.connect):
                                await client.connect()
                            else:
                                client.connect()
                            
                            # Check authorization if needed
                            if hasattr(client, 'is_user_authorized'):
                                if asyncio.iscoroutinefunction(client.is_user_authorized):
                                    if not await client.is_user_authorized():
                                        errors.append(f"❌ Account {phone} not authorized. Please log in again.")
                                        continue
                                elif not client.is_user_authorized():
                                    errors.append(f"❌ Account {phone} not authorized. Please log in again.")
                                    continue
                    except Exception as e:
                        errors.append(f"❌ Failed to connect {phone}: {str(e)}")
                        continue
                    
                    # Initialize chat state if it doesn't exist
                    if chat_id not in chat_states:
                        chat_states[chat_id] = {
                            'last_guess_time': 0,
                            'pending_guess': False,
                            'retry_count': 0,
                            'max_retries': 5,
                            'retry_delay': 2,
                            'guess_timeout': 10  # seconds between guesses
                        }
                    
                    # Create and store the task
                    try:
                        # Cancel existing task if it exists
                        if phone in account_tasks:
                            task_data = account_tasks[phone]
                            if 'task' in task_data and task_data['task']:
                                task_data['task'].cancel()
                                try:
                                    await task_data['task']
                                except asyncio.CancelledError:
                                    pass
                                
                        task = asyncio.create_task(guessing_logic(client, chat_id))
                        account_tasks[phone] = {'task': task, 'client': client}
                        db.set_account_active(acc_id, 1)
                        started_count += 1
                        await log_message(chat_id, f"Started guessing for {phone}")
                        
                        # Add a small delay between starting accounts
                        await asyncio.sleep(1)
                        
                    except Exception as e:
                        errors.append(f"❌ Error starting {phone}: {str(e)}")
                        if phone in account_tasks:
                            del account_tasks[phone]
                
            except Exception as e:
                error_msg = f"❌ Error processing {phone}: {str(e)}"
                errors.append(error_msg)
                await log_message(chat_id, error_msg)
        
        # Send final status
        status_msg = []
        if started_count > 0:
            status_msg.append(f"✅ Started guessing for {started_count} account{'s' if started_count != 1 else ''}.")
        if errors:
            status_msg.append("\nErrors:" + "\n• ".join([""] + errors))
        
        if not status_msg:
            status_msg.append("❌ No accounts were started. All accounts might already be running.")
            
        await message.reply("\n".join(status_msg))
        
    except Exception as e:
        error_msg = f"❌ Error in startall_cmd: {str(e)}"
        print(error_msg)
        await message.reply(error_msg)
        await message.reply(error_msg)

@app.on_message(filters.command('stopall'))
@authorized_only
async def stopall_cmd(client, message: Message):
    """Stop all running guessing tasks."""
    try:
        if not await is_admin(message.from_user.id):
            await message.reply("❌ You are not authorized to use this command.")
            return
            
        global account_tasks
        stopped = 0
        errors = []
        
        for phone, task_info in list(account_tasks.items()):
            try:
                task = task_info.get('task')
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    
                # Update database
                accs = db.get_accounts(ADMIN_USER_ID)
                for acc in accs:
                    if acc[1] == phone:
                        db.set_account_active(acc[0], 0)
                        stopped += 1
                        break
                        
            except Exception as e:
                errors.append(f"❌ Error stopping {phone}: {str(e)}")
            finally:
                # Clean up client connection
                if 'client' in task_info and await task_info['client'].is_connected():
                    await task_info['client'].disconnect()
                del account_tasks[phone]
        
        # Send status
        status_msg = [f"⏹️ Stopped guessing for {stopped} accounts."]
        if errors:
            status_msg.append("\nErrors:" + "\n• ".join([""] + errors))
            
        await message.reply("\n".join(status_msg))
        
    except Exception as e:
        error_msg = f"❌ Error in stopall_cmd: {str(e)}"
        print(error_msg)
        await message.reply(error_msg)

from pyrogram import enums

@app.on_message(filters.command('status'))
@authorized_only
async def status_cmd(client, message: Message):
    if not await is_admin(message.from_user.id):
        await message.reply("❌ You are not authorized to use this bot.")
        return
    accounts = db.get_accounts(ADMIN_USER_ID)
    if not accounts:
        await message.reply("No accounts found.")
        return
    msg = "<b>Status:</b>\n"
    for acc in accounts:
        msg += f"• <b>Phone:</b> {acc[1]} | <b>Active:</b> {'✔' if acc[5] else '❌'}\n"
    await message.reply(msg, parse_mode=enums.ParseMode.HTML)

if __name__ == "__main__":
    # Create necessary directories
    os.makedirs("cache", exist_ok=True)
    os.makedirs("saitama", exist_ok=True)
    
    print("Starting bot...")
    app.run()
    print("Bot stopped.")
