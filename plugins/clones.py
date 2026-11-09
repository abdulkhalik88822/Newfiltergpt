"""Owner-scoped creation and management of user-owned bot clones."""

import asyncio
import datetime
import html
import logging
import re
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pyrogram import Client, filters, StopPropagation
from pyrogram.enums import ParseMode
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from database.users_chats_db import db
from utils import is_clone_access_allowed
from info import (
    API_HASH,
    API_ID,
    AUTO_DELETE,
    AUTH_CHANNELS,
    CLONE_ENCRYPTION_KEY,
    CUSTOM_FILE_CAPTION,
    DELETE_TIME,
    IS_VERIFY,
    MAX_CLONES_PER_USER,
    MAX_TOTAL_CLONES,
    PROTECT_CONTENT,
    SHORTENER_API,
    SHORTENER_API2,
    SHORTENER_API3,
    SHORTENER_WEBSITE,
    SHORTENER_WEBSITE2,
    SHORTENER_WEBSITE3,
    STAR_PREMIUM_PLANS,
    THREE_VERIFY_GAP,
    TUTORIAL,
    TUTORIAL_2,
    TUTORIAL_3,
    TWO_VERIFY_GAP,
)

logger = logging.getLogger(__name__)

TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
TOKEN_SEARCH_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])\d{5,}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
MAX_WELCOME_LENGTH = 500
CLONE_COMMANDS = (
    BotCommand("start", "Start the bot"),
    BotCommand("alive", "Check bot status"),
    BotCommand("customize", "Manage this clone bot"),
    BotCommand("settings", "Configure group settings"),
    BotCommand("id", "Show Telegram IDs"),
    BotCommand("info", "Show user information"),
    BotCommand("myplan", "Check your premium plan"),
)
CLONE_FLOWS = {}


def _extract_single_bot_token(value):
    """Accept a pasted token or a BotFather message containing exactly one token."""
    text = str(value or "").strip()
    if TOKEN_PATTERN.fullmatch(text):
        return text
    matches = set(TOKEN_SEARCH_PATTERN.findall(text))
    return next(iter(matches)) if len(matches) == 1 else None
CLONE_SETTINGS_FIELDS = {
    "is_verify", "shortner", "api", "api_two", "api_three", "api_encrypted", "api_two_encrypted", "api_three_encrypted",
    "shortner_two", "shortner_three", "tutorial", "tutorial_2", "tutorial_3",
    "verify_time", "third_verify_time", "caption", "fsub", "file_secure",
    "auto_delete", "delete_time", "custom_button_text", "custom_button_url",
    "custom_buttons", "shortener_enabled", "second_verification", "fsub_enabled",
    "premium_enabled", "premium_plans", "premium_text",
    "clone_log_channel", "clone_database_channel", "clone_admins", "clone_mode",
}
SETTING_PROMPTS = {
    "shortener": "Send six lines: website 1, API key 1, website 2, API key 2, website 3, API key 3. For an unused pair send <code>-</code> on both lines. The first pair is required.",
    "caption": "Send the file caption (up to 900 characters). Placeholders: {file_name}, {file_size}, {file_caption}.",
    "fsub": "Send up to five channel IDs separated by spaces. The clone bot must already be an admin in each channel. Send <code>clear</code> to remove clone-specific force-sub channels.",
    "custom_button": "Send the button label on line 1 and its HTTPS URL on line 2.",
    "custom_buttons": "Send one or more button pairs in this format: <code>[Button text][buttonurl:https://example.com]</code>. Put each row on a new line. Add <code>:same</code> after a URL to place that button beside the previous button, e.g. <code>[Two][buttonurl:https://example.com:same]</code>.",
    "premium_plans": "Send Star plans as comma-separated amount=duration pairs, for example <code>10=7day,20=15day,40=1month</code>. Send <code>off</code> to disable plan buttons.",
    "premium_text": "Send the premium plan text (up to 900 characters), or send <code>default</code> to restore the default message.",
    "verify_time": "Send the second and optional third verification gaps in seconds separated by a space (each 60–604800).",
    "tutorials": "Send up to three tutorial URLs on separate lines (first, second, third shortener). Use <code>-</code> to keep a stage unchanged.",
    "delete_time": "Send the result auto-delete delay in seconds (10–86400).",
}
DEFAULT_CLONE_SETTINGS = {
    "is_verify": IS_VERIFY,
    "shortner": SHORTENER_WEBSITE,
    "api": SHORTENER_API,
    "shortner_two": SHORTENER_WEBSITE2,
    "api_two": SHORTENER_API2,
    "shortner_three": SHORTENER_WEBSITE3,
    "api_three": SHORTENER_API3,
    "shortener_enabled": bool(SHORTENER_WEBSITE and SHORTENER_API),
    "second_verification": True,
    "tutorial": TUTORIAL,
    "tutorial_2": TUTORIAL_2,
    "tutorial_3": TUTORIAL_3,
    "verify_time": TWO_VERIFY_GAP,
    "third_verify_time": THREE_VERIFY_GAP,
    "caption": CUSTOM_FILE_CAPTION,
    "fsub": list(AUTH_CHANNELS),
    "fsub_enabled": bool(AUTH_CHANNELS),
    "file_secure": PROTECT_CONTENT,
    "auto_delete": AUTO_DELETE,
    "delete_time": DELETE_TIME,
    "custom_button_text": "",
    "custom_button_url": "",
    "custom_buttons": [],
    "premium_enabled": True,
    "premium_plans": dict(STAR_PREMIUM_PLANS),
    "premium_text": "",
    "clone_log_channel": None,
    "clone_database_channel": None,
    "clone_admins": [],
    "clone_mode": "public",
}


class CloneError(Exception):
    """A safe, user-facing clone operation error with no secret data."""


class CloneManager:
    def __init__(self):
        self.clients = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _cipher():
        if not CLONE_ENCRYPTION_KEY:
            return None
        try:
            return Fernet(CLONE_ENCRYPTION_KEY.encode("utf-8"))
        except (TypeError, ValueError):
            logger.error("Clone encryption key is configured incorrectly.")
            return None

    def _encrypt(self, token):
        cipher = self._cipher()
        if cipher is None:
            raise CloneError(
                "Clone storage is not configured. The bot owner must set a valid "
                "CLONE_ENCRYPTION_KEY before clones can be created."
            )
        return cipher.encrypt(token.encode("utf-8")).decode("ascii")

    def _decrypt(self, encrypted_token):
        cipher = self._cipher()
        if cipher is None:
            raise CloneError("Clone storage key is missing or invalid.")
        try:
            return cipher.decrypt(encrypted_token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise CloneError(
                "This clone token cannot be decrypted. Restore the original "
                "CLONE_ENCRYPTION_KEY to use saved clones."
            ) from None

    @staticmethod
    def _make_client(bot_id, owner_id, token, welcome=""):
        client = Client(
            name=f"clone_{bot_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=token,
            # Match the main bot's update concurrency for clone responsiveness.
            workers=60,
            sleep_threshold=5,
            in_memory=True,
            plugins={"root": "plugins"},
        )
        client.is_clone = True
        client.clone_bot_id = int(bot_id) if str(bot_id).isdigit() else 0
        client.clone_owner_id = int(owner_id)
        client.clone_welcome = welcome or ""
        client.clone_settings = dict(DEFAULT_CLONE_SETTINGS)
        return client

    async def _start_record(self, record):
        bot_id = str(record["_id"])
        if bot_id in self.clients:
            return self.clients[bot_id]

        token = self._decrypt(record["token_encrypted"])
        client = self._make_client(
            bot_id,
            record["owner_id"],
            token,
            record.get("welcome", ""),
        )
        token = ""
        try:
            await client.start()
            me = await client.get_me()
            if not me or not getattr(me, "is_bot", False):
                raise CloneError("Telegram did not validate this as a bot token.")
            # Each clone receives only its own user-facing command menu.
            # Main-bot clone management and admin commands are not published.
            try:
                await client.set_bot_commands(list(CLONE_COMMANDS))
            except Exception as exc:
                logger.warning(
                    "Could not publish command menu for clone %s (%s)",
                    bot_id,
                    type(exc).__name__,
                )
            client.username = f"@{me.username}" if me.username else ""
            client.first_name = me.first_name or "Bot"
            client.clone_settings.update(record.get("settings", {}))
            for api_key, encrypted_key in (("api", "api_encrypted"), ("api_two", "api_two_encrypted"), ("api_three", "api_three_encrypted")):
                encrypted_api = client.clone_settings.pop(encrypted_key, None)
                if encrypted_api is not None:
                    client.clone_settings[api_key] = self._decrypt(encrypted_api) if encrypted_api else ""
            self.clients[bot_id] = client
            return client
        except Exception as exc:
            try:
                if getattr(client, "is_connected", False):
                    await client.stop()
            except Exception:
                pass
            if isinstance(exc, CloneError):
                raise
            raise CloneError(
                "Could not start this bot. Check its BotFather token and try again."
            ) from None

    async def create(self, owner_id, token):
        if not TOKEN_PATTERN.fullmatch(token):
            raise CloneError("That does not look like a BotFather token. Please retry or /cancel.")
        encrypted = self._encrypt(token)

        async with self._lock:
            if await db.count_clone_bots(owner_id) >= MAX_CLONES_PER_USER:
                raise CloneError(f"You can create up to {MAX_CLONES_PER_USER} clones.")
            if await db.count_clone_bots() >= MAX_TOTAL_CLONES:
                raise CloneError(
                    f"The server clone limit ({MAX_TOTAL_CLONES}) is full. "
                    "Remove an unused clone and try again."
                )

            # Start first so an invalid/revoked token is never saved as a clone.
            probe_name = f"pending_{uuid.uuid4().hex}"
            probe = self._make_client(probe_name, owner_id, token)
            try:
                await probe.start()
                me = await probe.get_me()
                if not me or not getattr(me, "is_bot", False):
                    raise CloneError("Telegram did not validate this as a bot token.")
                bot_id = str(me.id)
                existing = await db.get_clone_bot(bot_id)
                if existing:
                    raise CloneError("This bot is already registered as a clone.")

                try:
                    await probe.stop()
                except Exception:
                    pass

                record = {
                    "_id": bot_id,
                    "owner_id": int(owner_id),
                    "username": me.username or "",
                    "first_name": me.first_name or "Bot",
                    "token_encrypted": encrypted,
                    "enabled": True,
                    "status": "starting",
                    "welcome": "",
                    "created_at": datetime.datetime.utcnow(),
                    "updated_at": datetime.datetime.utcnow(),
                }
                await db.add_clone_bot(record)
                try:
                    client = await self._start_record(record)
                except Exception:
                    await db.delete_clone_bot(bot_id, owner_id)
                    raise
                await db.update_clone_bot(
                    bot_id,
                    owner_id,
                    {"status": "active", "updated_at": datetime.datetime.utcnow()},
                )
                record["status"] = "active"
                return record
            except CloneError:
                if getattr(probe, "is_connected", False):
                    try:
                        await probe.stop()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if getattr(probe, "is_connected", False):
                    try:
                        await probe.stop()
                    except Exception:
                        pass
                logger.warning(
                    "Clone token validation failed for owner %s (%s)",
                    owner_id,
                    type(exc).__name__,
                )
                raise CloneError(
                    "Telegram could not start that bot. Confirm the token in BotFather and retry."
                ) from None

    async def activate(self, record):
        async with self._lock:
            await self._start_record(record)
            await db.update_clone_bot(
                record["_id"],
                record["owner_id"],
                {"enabled": True, "status": "active", "updated_at": datetime.datetime.utcnow()},
            )

    async def deactivate(self, record):
        async with self._lock:
            client = self.clients.pop(str(record["_id"]), None)
            if client:
                try:
                    await client.stop()
                except Exception as exc:
                    logger.warning(
                        "Clone stop failed for bot %s (%s)",
                        record["_id"],
                        type(exc).__name__,
                    )
            await db.update_clone_bot(
                record["_id"],
                record["owner_id"],
                {"enabled": False, "status": "paused", "updated_at": datetime.datetime.utcnow()},
            )

    async def delete(self, record):
        async with self._lock:
            client = self.clients.pop(str(record["_id"]), None)
            if client:
                try:
                    await client.stop()
                except Exception as exc:
                    logger.warning(
                        "Clone stop failed during deletion for bot %s (%s)",
                        record["_id"],
                        type(exc).__name__,
                    )
            await db.delete_clone_premium_users(record["_id"])
            await db.delete_clone_group_settings(record["_id"])
            await db.delete_clone_bot(record["_id"], record["owner_id"])

    async def set_welcome(self, record, welcome):
        await db.update_clone_bot(
            record["_id"],
            record["owner_id"],
            {"welcome": welcome, "updated_at": datetime.datetime.utcnow()},
        )
        client = self.clients.get(str(record["_id"]))
        if client:
            client.clone_welcome = welcome

    async def set_settings(self, record, settings):
        safe_settings = {key: value for key, value in settings.items() if key in CLONE_SETTINGS_FIELDS}
        stored_settings = dict(safe_settings)
        for api_key, encrypted_key in (("api", "api_encrypted"), ("api_two", "api_two_encrypted"), ("api_three", "api_three_encrypted")):
            api_value = stored_settings.pop(api_key, None)
            if api_value is not None:
                stored_settings[encrypted_key] = self._encrypt(api_value) if api_value else ""
        if isinstance(stored_settings.get("premium_plans"), dict):
            stored_settings["premium_plans"] = {
                str(amount): duration
                for amount, duration in stored_settings["premium_plans"].items()
            }
        await db.update_clone_settings(record["_id"], record["owner_id"], stored_settings)
        client = self.clients.get(str(record["_id"]))
        if client:
            client.clone_settings = dict(DEFAULT_CLONE_SETTINGS)
            client.clone_settings.update(safe_settings)

    async def restore(self):
        if self._cipher() is None:
            logger.info("Clone restore skipped: set a valid CLONE_ENCRYPTION_KEY to enable clones.")
            return
        records = await db.get_enabled_clone_bots()
        if len(records) > MAX_TOTAL_CLONES:
            logger.warning(
                "Found %s enabled clones, but MAX_TOTAL_CLONES is %s; only the configured limit will start.",
                len(records),
                MAX_TOTAL_CLONES,
            )
        for record in records[:MAX_TOTAL_CLONES]:
            try:
                async with self._lock:
                    await self._start_record(record)
                    await db.update_clone_bot(
                        record["_id"],
                        record["owner_id"],
                        {"status": "active", "updated_at": datetime.datetime.utcnow()},
                    )
            except Exception as exc:
                logger.warning(
                    "Could not restore clone %s (%s)",
                    record.get("_id", "unknown"),
                    type(exc).__name__,
                )
                await db.update_clone_bot(
                    record["_id"],
                    record["owner_id"],
                    {"status": "error", "updated_at": datetime.datetime.utcnow()},
                )
            await asyncio.sleep(0.25)

    async def stop_all(self):
        clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            try:
                if getattr(client, "is_connected", False):
                    await client.stop()
            except Exception as exc:
                logger.warning("Clone shutdown failed (%s)", type(exc).__name__)


clone_manager = CloneManager()


def _is_main_bot(client):
    return not getattr(client, "is_clone", False)


async def _show_home(client, target, user_id):
    records = await db.get_user_clone_bots(user_id)
    running = sum(1 for record in records if str(record["_id"]) in clone_manager.clients)
    key_ready = clone_manager._cipher() is not None
    text = (
        "🤖 <b>MY BOT CLONES</b>\n\n"
        f"Bots: <b>{len(records)}/{MAX_CLONES_PER_USER}</b> · Running: <b>{running}</b>\n\n"
        "Create a bot with @BotFather, paste its token here, then activate or pause it from this menu.\n"
        "Clone tokens are encrypted in MongoDB. Only manage bots you own."
    )
    if not key_ready:
        text += "\n\n⚠️ Clone creation is disabled until the bot owner configures CLONE_ENCRYPTION_KEY."
    buttons = [
        [InlineKeyboardButton("➕ CREATE OWN CLONE", callback_data="clone_create")],
        [InlineKeyboardButton("🧰 MANAGE MY CLONES", callback_data="clone_list")],
        [InlineKeyboardButton("✖ CLOSE", callback_data="clone_close")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    if hasattr(target, "edit_text"):
        try:
            return await target.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return await target.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def _owned_record(query, bot_id):
    record = await db.get_clone_bot(bot_id)
    if not record or int(record.get("owner_id", 0)) != query.from_user.id:
        return None
    return record


@Client.on_message(filters.command(["clone", "clones", "mybots"]) & filters.private)
async def clone_command(client, message):
    if not _is_main_bot(client):
        return
    await _show_home(client, message, message.from_user.id)


@Client.on_callback_query(filters.regex(r"^clone_home$"))
async def clone_home_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    await query.answer()
    await _show_home(client, query.message, query.from_user.id)


@Client.on_callback_query(filters.regex(r"^clone_create$"))
async def clone_create_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    if clone_manager._cipher() is None:
        return await query.answer("Clone encryption is not configured yet.", show_alert=True)
    owner_id = query.from_user.id
    if await db.count_clone_bots(owner_id) >= MAX_CLONES_PER_USER:
        return await query.answer(f"You can create up to {MAX_CLONES_PER_USER} clones.", show_alert=True)
    if await db.count_clone_bots() >= MAX_TOTAL_CLONES:
        return await query.answer("The server clone limit is full.", show_alert=True)
    CLONE_FLOWS[owner_id] = {"stage": "token", "created_at": datetime.datetime.utcnow()}
    await query.answer()
    await query.message.reply_text(
        "1) Open <a href='https://t.me/BotFather'>@BotFather</a> and send <code>/newbot</code>.\n"
        "2) Finish BotFather's setup and copy the new bot token.\n"
        "3) Paste only the token text here, or forward a BotFather message containing exactly one token. I will validate it, encrypt it, and delete your message if Telegram allows.\n\n"
        "Send /cancel to stop. Only submit a token for a bot you own; anyone with a token can control that bot.",
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^clone_list$"))
async def clone_list_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    records = await db.get_user_clone_bots(query.from_user.id)
    if not records:
        await query.answer("You have no clones yet.", show_alert=True)
        return
    rows = []
    for record in records[:MAX_CLONES_PER_USER]:
        bot_id = str(record["_id"])
        is_running = bot_id in clone_manager.clients
        label = "🟢" if is_running else "⚪"
        name = record.get("username") or record.get("first_name") or bot_id
        name = name.lstrip("@")[:32]
        rows.append([InlineKeyboardButton(f"{label} @{name}", callback_data=f"clone_view:{bot_id}")])
    rows.append([InlineKeyboardButton("⬅ BACK", callback_data="clone_home")])
    await query.answer()
    await query.message.edit_text(
        "<b>YOUR CLONES</b>\nChoose a bot to activate, pause, customize, or delete it.",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^clone_view:(\d+)$"))
async def clone_view_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    bot_id = query.data.split(":", 1)[1]
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    running = bot_id in clone_manager.clients
    label = html.escape(("@" + record.get("username", "")) if record.get("username") else record.get("first_name", "Bot"))
    status = "🟢 Running" if running else "⚪ Paused" if not record.get("enabled") else "🟠 Needs retry"
    action = "⏸ PAUSE" if running else "▶️ ACTIVATE / RETRY"
    rows = [
        [InlineKeyboardButton(action, callback_data=f"clone_toggle:{bot_id}")],
        [InlineKeyboardButton("⚙️ SETTINGS", callback_data=f"clone_settings:{bot_id}")],
        [InlineKeyboardButton("✏️ SET WELCOME MESSAGE", callback_data=f"clone_welcome:{bot_id}")],
        [InlineKeyboardButton("🗑 DELETE CLONE", callback_data=f"clone_delete_check:{bot_id}")],
        [InlineKeyboardButton("⬅ BACK TO MY CLONES", callback_data="clone_list")],
    ]
    await query.answer()
    await query.message.edit_text(
        f"<b>{label}</b>\nID: <code>{bot_id}</code>\nStatus: {status}\n\n"
        f"Welcome message: {'custom' if record.get('welcome') else 'default'}",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^clone_toggle:(\d+)$"))
async def clone_toggle_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    bot_id = query.data.split(":", 1)[1]
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    try:
        if bot_id in clone_manager.clients:
            await clone_manager.deactivate(record)
            message = "Clone paused."
        else:
            await clone_manager.activate(record)
            message = "Clone is running."
    except CloneError as exc:
        return await query.answer(str(exc), show_alert=True)
    except Exception as exc:
        logger.warning("Clone toggle failed for bot %s (%s)", bot_id, type(exc).__name__)
        return await query.answer("Could not change this clone's status. Check the token and logs.", show_alert=True)
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer(message, show_alert=True)
    running = bot_id in clone_manager.clients
    label = html.escape(("@" + record.get("username", "")) if record.get("username") else record.get("first_name", "Bot"))
    status = "🟢 Running" if running else "⚪ Paused" if not record.get("enabled") else "🟠 Needs retry"
    action = "⏸ PAUSE" if running else "▶️ ACTIVATE / RETRY"
    rows = [
        [InlineKeyboardButton(action, callback_data=f"clone_toggle:{bot_id}")],
        [InlineKeyboardButton("⚙️ SETTINGS", callback_data=f"clone_settings:{bot_id}")],
        [InlineKeyboardButton("✏️ SET WELCOME MESSAGE", callback_data=f"clone_welcome:{bot_id}")],
        [InlineKeyboardButton("🗑 DELETE CLONE", callback_data=f"clone_delete_check:{bot_id}")],
        [InlineKeyboardButton("⬅ BACK TO MY CLONES", callback_data="clone_list")],
    ]
    await query.answer(message, show_alert=True)
    await query.message.edit_text(
        f"<b>{label}</b>\nID: <code>{bot_id}</code>\nStatus: {status}\n\n"
        f"Welcome message: {'custom' if record.get('welcome') else 'default'}",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


def _clone_config(record):
    settings = dict(DEFAULT_CLONE_SETTINGS)
    stored_settings = dict(record.get("settings", {}))
    settings.update(stored_settings)
    for api_key, encrypted_key in (("api", "api_encrypted"), ("api_two", "api_two_encrypted"), ("api_three", "api_three_encrypted")):
        encrypted_api = settings.pop(encrypted_key, None)
        if encrypted_api is not None:
            try:
                settings[api_key] = clone_manager._decrypt(encrypted_api) if encrypted_api else ""
            except CloneError:
                settings[api_key] = ""
    return settings


async def _render_clone_settings(query, record, feature=None):
    bot_id = str(record["_id"])
    settings = _clone_config(record)
    if feature is None:
        rows = [
            [InlineKeyboardButton("🤖 MY CLONE BOT", callback_data=f"clone_view:{bot_id}")],
            [InlineKeyboardButton("💸 PREMIUM PLAN", callback_data=f"clone_feature:{bot_id}:premium")],
            [InlineKeyboardButton("🔗 LINK SHORTENER", callback_data=f"clone_feature:{bot_id}:shortener")],
            [InlineKeyboardButton("⏱ TOKEN VERIFICATION", callback_data=f"clone_feature:{bot_id}:verify")],
            [InlineKeyboardButton("🍿 CUSTOM CAPTION", callback_data=f"clone_feature:{bot_id}:caption")],
            [InlineKeyboardButton("📢 CUSTOM FORCE SUBSCRIBE", callback_data=f"clone_feature:{bot_id}:fsub")],
            [InlineKeyboardButton("🔘 CUSTOM BUTTON", callback_data=f"clone_feature:{bot_id}:custom_button")],
            [InlineKeyboardButton("♻️ AUTO DELETE", callback_data=f"clone_feature:{bot_id}:auto_delete")],
            [InlineKeyboardButton("🔒 PROTECT CONTENT", callback_data=f"clone_feature:{bot_id}:protect")],
            [InlineKeyboardButton("⬅ BACK", callback_data=f"clone_view:{bot_id}")],
        ]
        body = "Choose a setting for this clone. Settings are saved per clone and apply to its search bot."
    else:
        config = settings
        if feature == "premium":
            plans = config.get("premium_plans", "server defaults")
            if isinstance(plans, dict):
                plans = ", ".join(f"{amount}⭐={duration}" for amount, duration in plans.items()) or "off"
            plan_text = config.get("premium_text") or "default text"
            body = f"<b>💸 PREMIUM PLAN</b>\nPayments: {'ON' if config.get('premium_enabled', True) else 'OFF'}\nPlans: <code>{html.escape(str(plans))}</code>\nPlan text: <code>{html.escape(str(plan_text))[:300]}</code>\nPremium users are isolated to this clone."
            rows = [
                [InlineKeyboardButton("🔄 ON / OFF", callback_data=f"clone_setting_toggle:{bot_id}:premium_enabled")],
                [InlineKeyboardButton("✏️ SET STAR PLANS", callback_data=f"clone_setting_edit:{bot_id}:premium_plans")],
                [InlineKeyboardButton("🧵 PREMIUM PLAN TEXT", callback_data=f"clone_setting_edit:{bot_id}:premium_text")],
                [InlineKeyboardButton("➕ ADD PREMIUM USER", callback_data=f"clone_premium_user:{bot_id}:add")],
                [InlineKeyboardButton("➖ REMOVE PREMIUM USER", callback_data=f"clone_premium_user:{bot_id}:remove")],
                [InlineKeyboardButton("🚦 PREMIUM USERS LIST", callback_data=f"clone_premium_list:{bot_id}")],
            ]
        elif feature == "shortener":
            body = (
                "<b>🔗 LINK SHORTENER</b>\n"
                f"Shortlink: {'ON' if config.get('shortener_enabled', False) else 'OFF'}\n"
                f"1) <code>{html.escape(str(config.get('shortner', 'not set')))}</code> · API {'set' if config.get('api') else 'not set'}\n"
                f"2) <code>{html.escape(str(config.get('shortner_two', 'not set')))}</code> · API {'set' if config.get('api_two') else 'not set'}\n"
                f"3) <code>{html.escape(str(config.get('shortner_three', 'not set')))}</code> · API {'set' if config.get('api_three') else 'not set'}"
            )
            rows = [
                [InlineKeyboardButton("🔄 ON / OFF", callback_data=f"clone_setting_toggle:{bot_id}:shortener_enabled")],
                [InlineKeyboardButton("✏️ SET UP TO 3 SHORTENERS", callback_data=f"clone_setting_edit:{bot_id}:shortener")],
                [InlineKeyboardButton("🗑 DELETE SHORTLINKS", callback_data=f"clone_setting_reset:{bot_id}:shortener")],
            ]
        elif feature == "verify":
            body = f"<b>⏱ TOKEN VERIFICATION</b>\nStatus: {'ON' if config.get('is_verify', False) else 'OFF'}\nSecond gap: {config.get('verify_time', 1200)} sec\nThird gap: {config.get('third_verify_time', 54000)} sec\nTutorials: configurable for all three stages."
            rows = [
                [InlineKeyboardButton("🔄 ON / OFF", callback_data=f"clone_setting_toggle:{bot_id}:is_verify")],
                [InlineKeyboardButton("⏲ SET GAP", callback_data=f"clone_setting_edit:{bot_id}:verify_time")],
                [InlineKeyboardButton("📚 SET TUTORIAL URLS", callback_data=f"clone_setting_edit:{bot_id}:tutorials")],
                [InlineKeyboardButton(f"⏱ SECOND VERIFICATION {'ON' if config.get('second_verification', True) else 'OFF'}", callback_data=f"clone_setting_toggle:{bot_id}:second_verification")],
            ]
        elif feature == "caption":
            body = f"<b>🍿 CUSTOM CAPTION</b>\nCurrent: <code>{html.escape(str(config.get('caption', 'server default')))[:700]}</code>"
            rows = [
                [InlineKeyboardButton("✏️ SET CAPTION", callback_data=f"clone_setting_edit:{bot_id}:caption")],
                [InlineKeyboardButton("🔄 DEFAULT CAPTION", callback_data=f"clone_setting_reset:{bot_id}:caption")],
            ]
        elif feature == "fsub":
            channel_ids = config.get("fsub", [])
            shown = ", ".join(map(str, channel_ids)) if channel_ids else "not set"
            body = f"<b>📢 CUSTOM FORCE SUBSCRIBE</b>\nStatus: {'ON' if config.get('fsub_enabled', False) else 'OFF'}\nChannels: <code>{html.escape(shown)}</code>\n\nThe clone bot must be an admin in every channel."
            rows = [
                [InlineKeyboardButton("➕ ADD / UPDATE CHANNELS", callback_data=f"clone_setting_edit:{bot_id}:fsub")],
                [InlineKeyboardButton("🔄 ON / OFF", callback_data=f"clone_setting_toggle:{bot_id}:fsub_enabled")],
                [InlineKeyboardButton("🗑 CLEAR CHANNELS", callback_data=f"clone_setting_reset:{bot_id}:fsub")],
            ]
        elif feature == "custom_button":
            body = f"<b>🔘 CUSTOM BUTTON</b>\nButtons: {sum(len(row) for row in config.get('custom_buttons', [])) or ('1' if config.get('custom_button_text') else 'none')}\nSupports single and double buttons per row."
            rows = [
                [InlineKeyboardButton("➕ ADD BUTTONS", callback_data=f"clone_setting_edit:{bot_id}:custom_buttons")],
                [InlineKeyboardButton("🗑 CLEAR BUTTONS", callback_data=f"clone_setting_reset:{bot_id}:custom_buttons")],
            ]
        elif feature == "auto_delete":
            body = f"<b>♻️ AUTO DELETE</b>\nStatus: {'ON' if config.get('auto_delete', True) else 'OFF'}\nDelay: {config.get('delete_time', 300)} sec"
            rows = [
                [InlineKeyboardButton("🔄 ON / OFF", callback_data=f"clone_setting_toggle:{bot_id}:auto_delete")],
                [InlineKeyboardButton("⏲ SET DELAY", callback_data=f"clone_setting_edit:{bot_id}:delete_time")],
            ]
        else:
            body = f"<b>🔒 PROTECT CONTENT</b>\nStatus: {'ON' if config.get('file_secure', False) else 'OFF'}\n\nWhen enabled, Telegram blocks forwarding and saving delivered files where supported."
            rows = [[InlineKeyboardButton("🔄 ON / OFF", callback_data=f"clone_setting_toggle:{bot_id}:file_secure")]]
        rows.append([InlineKeyboardButton("⬅ BACK TO SETTINGS", callback_data=f"clone_settings:{bot_id}")])
    await query.message.edit_text(body, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^clone_settings:(\d+)$"))
async def clone_settings_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone settings from the main bot.", show_alert=True)
    record = await _owned_record(query, query.data.split(":", 1)[1])
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    await query.answer()
    await _render_clone_settings(query, record)


@Client.on_callback_query(filters.regex(r"^clone_feature:(\d+):([a-z_]+)$"))
async def clone_feature_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone settings from the main bot.", show_alert=True)
    _, bot_id, feature = query.data.split(":", 2)
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    await query.answer()
    await _render_clone_settings(query, record, feature)


@Client.on_callback_query(filters.regex(r"^clone_setting_toggle:(\d+):([a-z_]+)$"))
async def clone_setting_toggle_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone settings from the main bot.", show_alert=True)
    _, bot_id, key = query.data.split(":", 2)
    record = await _owned_record(query, bot_id)
    if not record or key not in {"premium_enabled", "is_verify", "auto_delete", "file_secure", "shortener_enabled", "second_verification", "fsub_enabled"}:
        return await query.answer("Setting not found.", show_alert=True)
    settings = _clone_config(record)
    next_value = not settings.get(key, key in {"premium_enabled", "auto_delete"})
    if key == "is_verify" and next_value and (not settings.get("shortener_enabled") or not settings.get("shortner") or not settings.get("api")):
        return await query.answer("Set a working first shortener before enabling token verification.", show_alert=True)
    if key == "shortener_enabled" and next_value and (not settings.get("shortner") or not settings.get("api")):
        return await query.answer("Set a shortener website and API key first.", show_alert=True)
    if key == "fsub_enabled" and next_value and not settings.get("fsub"):
        return await query.answer("Add at least one force-subscribe channel first.", show_alert=True)
    settings[key] = next_value
    await clone_manager.set_settings(record, settings)
    await query.answer("Setting updated.")
    feature = {"premium_enabled": "premium", "is_verify": "verify", "second_verification": "verify", "auto_delete": "auto_delete", "file_secure": "protect", "shortener_enabled": "shortener", "fsub_enabled": "fsub"}[key]
    record["settings"] = settings
    await _render_clone_settings(query, record, feature)


@Client.on_callback_query(filters.regex(r"^clone_setting_reset:(\d+):([a-z_]+)$"))
async def clone_setting_reset_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone settings from the main bot.", show_alert=True)
    _, bot_id, key = query.data.split(":", 2)
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    settings = _clone_config(record)
    if key == "caption":
        settings["caption"] = CUSTOM_FILE_CAPTION
        feature = "caption"
    elif key == "fsub":
        settings["fsub"] = []
        settings["fsub_enabled"] = False
        feature = "fsub"
    elif key == "custom_buttons":
        settings["custom_buttons"] = []
        settings["custom_button_text"] = ""
        settings["custom_button_url"] = ""
        feature = "custom_button"
    elif key == "shortener":
        for name in ("shortner", "api", "shortner_two", "api_two", "shortner_three", "api_three"):
            settings[name] = ""
        settings["shortener_enabled"] = False
        feature = "shortener"
    else:
        return await query.answer("Setting not found.", show_alert=True)
    await clone_manager.set_settings(record, settings)
    record["settings"] = settings
    await query.answer("Reset complete.")
    await _render_clone_settings(query, record, feature)


@Client.on_callback_query(filters.regex(r"^clone_setting_edit:(\d+):([a-z_]+)$"))
async def clone_setting_edit_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone settings from the main bot.", show_alert=True)
    _, bot_id, field = query.data.split(":", 2)
    record = await _owned_record(query, bot_id)
    if not record or field not in SETTING_PROMPTS:
        return await query.answer("Setting not found.", show_alert=True)
    CLONE_FLOWS[query.from_user.id] = {"stage": "setting", "bot_id": bot_id, "field": field}
    await query.answer()
    await query.message.reply_text(SETTING_PROMPTS[field], parse_mode=ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^clone_premium_user:(\d+):(add|remove)$"))
async def clone_premium_user_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone settings from the main bot.", show_alert=True)
    _, bot_id, action = query.data.split(":", 2)
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    CLONE_FLOWS[query.from_user.id] = {
        "stage": f"premium_{action}",
        "bot_id": bot_id,
        "created_at": datetime.datetime.utcnow(),
    }
    prompt = "Send: <code>user_id duration</code>, for example <code>123456789 30day</code>." if action == "add" else "Send the Telegram user ID to remove from this clone's premium list."
    await query.answer()
    await query.message.reply_text(prompt, parse_mode=ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^clone_premium_list:(\d+)$"))
async def clone_premium_list_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone settings from the main bot.", show_alert=True)
    bot_id = query.data.split(":", 1)[1]
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    premium_users = await db.get_clone_premium_users(bot_id)
    lines = ["<b>PREMIUM USERS FOR THIS CLONE</b>"]
    now = datetime.datetime.utcnow()
    active = [item for item in premium_users if item.get("expiry_time") and item["expiry_time"] > now]
    for item in active[:35]:
        expiry = item["expiry_time"].strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"• <code>{item['user_id']}</code> — {expiry}")
    if not active:
        lines.append("No active manual premium users.")
    if len(active) > 35:
        lines.append(f"…plus {len(active) - 35} more")
    await query.answer()
    await query.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ BACK", callback_data=f"clone_feature:{bot_id}:premium")]]),
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^clone_welcome:(\d+)$"))
async def clone_welcome_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    bot_id = query.data.split(":", 1)[1]
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    CLONE_FLOWS[query.from_user.id] = {"stage": "welcome", "bot_id": bot_id}
    await query.answer()
    await query.message.reply_text(
        f"Send a new welcome message for <b>@{html.escape(record.get('username', 'bot'))}</b> "
        f"(up to {MAX_WELCOME_LENGTH} characters). Send /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^clone_delete_check:(\d+)$"))
async def clone_delete_check_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    bot_id = query.data.split(":", 1)[1]
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 YES, DELETE", callback_data=f"clone_delete:{bot_id}")],
        [InlineKeyboardButton("CANCEL", callback_data=f"clone_view:{bot_id}")],
    ])
    await query.answer()
    await query.message.edit_text(
        "Delete this clone and erase its stored encrypted token?\n\n"
        "This does not revoke the token at BotFather. Use <code>/revoke</code> there if you also want to invalidate it.",
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^clone_delete:(\d+)$"))
async def clone_delete_callback(client, query):
    if not _is_main_bot(client):
        return await query.answer("Open clone management from the main bot.", show_alert=True)
    bot_id = query.data.split(":", 1)[1]
    record = await _owned_record(query, bot_id)
    if not record:
        return await query.answer("Clone not found in your account.", show_alert=True)
    await clone_manager.delete(record)
    await query.answer("Clone deleted; revoke its token in BotFather if needed.", show_alert=True)
    await _show_home(client, query.message, query.from_user.id)


@Client.on_callback_query(filters.regex(r"^clone_close$"))
async def clone_close_callback(client, query):
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        await query.message.edit_text("Clone menu closed.")


@Client.on_message(filters.private & filters.incoming, group=-100)
async def clone_private_flow(client, message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    # Clones get a per-bot welcome page while their other search commands use
    # the same plugin set as the primary file bot.
    if getattr(client, "is_clone", False):
        if not is_clone_access_allowed(client, user_id):
            await message.reply_text("This bot is in private mode. Contact its owner for access.")
            raise StopPropagation
        command = message.command or []
        if command and command[0].split("@", 1)[0].lower() == "start" and len(command) == 1:
            welcome = getattr(client, "clone_welcome", "")
            if welcome:
                rendered = html.escape(welcome).replace("{first_name}", html.escape(message.from_user.first_name or ""))
                rendered = rendered.replace("{bot_name}", html.escape(getattr(client, "first_name", "Bot")))
            else:
                rendered = (
                    f"👋 Hello {html.escape(message.from_user.first_name or 'there')}! "
                    f"I am <b>{html.escape(getattr(client, 'first_name', 'File Bot'))}</b>.\n\n"
                    "Send a movie or file name to search."
                )
            await message.reply_text(rendered, parse_mode=ParseMode.HTML)
            raise StopPropagation
        return

    flow = CLONE_FLOWS.get(user_id)
    if not flow:
        return
    if flow.get("processing"):
        raise StopPropagation
    command = message.command or []
    if command and command[0].split("@", 1)[0].lower() == "cancel":
        CLONE_FLOWS.pop(user_id, None)
        await message.reply_text("Clone setup cancelled.")
        raise StopPropagation

    if flow.get("stage") == "token":
        if flow.get("processing"):
            raise StopPropagation
        raw_token_message = message.text or message.caption or ""
        token = _extract_single_bot_token(raw_token_message)
        raw_token_message = ""
        try:
            await message.delete()
        except Exception:
            pass
        if not token:
            now = asyncio.get_running_loop().time()
            if now - flow.get("last_token_hint_at", 0) >= 5:
                await message.reply_text(
                    "Send one active BotFather token as text, or forward a BotFather message that contains exactly one token. "
                    "For safety, a message with multiple tokens is rejected. Send /cancel to stop."
                )
                flow["last_token_hint_at"] = now
            raise StopPropagation
        flow["processing"] = True
        try:
            record = await clone_manager.create(user_id, token)
        except CloneError as exc:
            logger.info("Clone creation rejected for owner %s (%s)", user_id, type(exc).__name__)
            await message.reply_text(str(exc))
            if "does not look like" in str(exc):
                flow.pop("processing", None)
                CLONE_FLOWS[user_id] = flow
            else:
                CLONE_FLOWS.pop(user_id, None)
        except Exception as exc:
            logger.warning("Clone creation failed for owner %s (%s)", user_id, type(exc).__name__)
            await message.reply_text("Clone setup failed. Please try again later.")
            CLONE_FLOWS.pop(user_id, None)
        else:
            CLONE_FLOWS.pop(user_id, None)
            await message.reply_text(
                f"✅ Clone <b>@{html.escape(record.get('username', ''))}</b> created and activated.\n"
                "Open /mybots to pause it or change its welcome message.",
                parse_mode=ParseMode.HTML,
            )
        token = ""
        raise StopPropagation

    if flow.get("stage") == "welcome":
        welcome = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        if not welcome:
            await message.reply_text("Send a text welcome message, or /cancel.")
            raise StopPropagation
        if len(welcome) > MAX_WELCOME_LENGTH:
            await message.reply_text(f"Keep the welcome message under {MAX_WELCOME_LENGTH} characters.")
            raise StopPropagation
        record = await _owned_record_for_user(flow["bot_id"], user_id)
        if not record:
            CLONE_FLOWS.pop(user_id, None)
            await message.reply_text("That clone is no longer in your account.")
            raise StopPropagation
        await clone_manager.set_welcome(record, welcome)
        CLONE_FLOWS.pop(user_id, None)
        await message.reply_text("✅ Welcome message updated. Use {first_name} and {bot_name} as placeholders.")
        raise StopPropagation

    if flow.get("stage") == "setting":
        field = flow.get("field")
        raw = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        record = await _owned_record_for_user(flow["bot_id"], user_id)
        if not record:
            CLONE_FLOWS.pop(user_id, None)
            await message.reply_text("That clone is no longer in your account.")
            raise StopPropagation
        settings = _clone_config(record)
        try:
            if field == "shortener":
                parts = [line.strip() for line in (message.text or "").splitlines()]
                if len(parts) != 6:
                    raise CloneError("Send six lines (three website/API pairs); use - for each unused pair.")
                website_keys = ("shortner", "shortner_two", "shortner_three")
                api_keys = ("api", "api_two", "api_three")
                for index, (website_key, api_key) in enumerate(zip(website_keys, api_keys)):
                    website, api = parts[index * 2], parts[index * 2 + 1]
                    if website == "-" and api == "-":
                        continue
                    if not _valid_http_url(website) or not api:
                        raise CloneError(f"Shortener {index + 1} needs both a valid HTTP(S) website and an API key.")
                    settings[website_key], settings[api_key] = website, api
                if not settings.get("shortner") or not settings.get("api"):
                    raise CloneError("The first shortener website and API key are required.")
                settings["shortener_enabled"] = True
            elif field == "caption":
                if not raw or len(raw) > 900:
                    raise CloneError("Caption must contain 1–900 characters.")
                settings["caption"] = raw
            elif field == "fsub":
                if raw.lower() == "clear":
                    settings["fsub"] = []
                    settings["fsub_enabled"] = False
                else:
                    channel_ids = list(dict.fromkeys(int(value) for value in raw.split()))
                    if not channel_ids or len(channel_ids) > 5:
                        raise CloneError("Enter 1–5 channel IDs, or send clear.")
                    clone = clone_manager.clients.get(str(flow["bot_id"]))
                    if not clone:
                        raise CloneError("Activate this clone before checking channel permissions.")
                    for channel_id in channel_ids:
                        chat = await clone.get_chat(channel_id)
                        if str(getattr(chat.type, "value", chat.type)).lower() != "channel":
                            raise CloneError(f"{channel_id} is not a channel.")
                        member = await clone.get_chat_member(channel_id, clone.me.id)
                        status = str(getattr(member.status, "value", member.status)).lower()
                        if status not in {"administrator", "owner", "creator"}:
                            raise CloneError(f"Make @{clone.me.username or 'the clone bot'} an admin in {channel_id} first.")
                    settings["fsub"] = channel_ids
                    settings["fsub_enabled"] = True
            elif field == "custom_button":
                parts = raw.splitlines()
                if len(parts) != 2 or not parts[0].strip() or len(parts[0].strip()) > 40 or not _valid_http_url(parts[1].strip()):
                    raise CloneError("Send two lines: a button label (max 40 chars), then a valid HTTP(S) URL.")
                settings["custom_button_text"], settings["custom_button_url"] = parts[0].strip(), parts[1].strip()
            elif field == "custom_buttons":
                parsed = parse_custom_buttons(message.text or "")
                if not parsed:
                    raise CloneError("Add at least one valid button pair using the shown format.")
                settings["custom_buttons"] = parsed
            elif field == "premium_plans":
                if raw.lower() == "off":
                    settings["premium_plans"] = {}
                else:
                    plans = {}
                    for pair in raw.split(","):
                        amount_text, duration = pair.strip().split("=", 1)
                        amount = int(amount_text)
                        if not 1 <= amount <= 100000 or not re.fullmatch(r"\d+(?:s|min|hour|day|month|year)", duration.strip()):
                            raise ValueError
                        plans[amount] = duration.strip()
                    if not 1 <= len(plans) <= 8:
                        raise ValueError
                    settings["premium_plans"] = plans
            elif field == "premium_text":
                if raw.lower() == "default":
                    settings["premium_text"] = ""
                elif not raw or len(raw) > 900:
                    raise CloneError("Plan text must contain 1–900 characters, or send default.")
                else:
                    settings["premium_text"] = raw
            elif field == "verify_time":
                values = [int(value) for value in raw.split()]
                if not 1 <= len(values) <= 2 or any(not 60 <= seconds <= 604800 for seconds in values):
                    raise ValueError
                settings["verify_time"] = values[0]
                if len(values) == 2:
                    settings["third_verify_time"] = values[1]
            elif field == "tutorials":
                parts = [line.strip() for line in (message.text or "").splitlines()]
                if len(parts) > 3:
                    raise CloneError("Send at most three URL lines.")
                for index, key in enumerate(("tutorial", "tutorial_2", "tutorial_3")):
                    value = parts[index].strip() if index < len(parts) else ""
                    if value and value != "-":
                        if not _valid_http_url(value):
                            raise CloneError("Each tutorial must be a valid HTTP(S) URL.")
                        settings[key] = value
            elif field == "delete_time":
                seconds = int(raw)
                if not 10 <= seconds <= 86400:
                    raise ValueError
                settings["delete_time"] = seconds
            else:
                raise CloneError("That setting is not supported.")
        except CloneError as exc:
            await message.reply_text(str(exc))
            raise StopPropagation
        except (ValueError, TypeError):
            await message.reply_text("Input format is invalid. Please check the example and try again, or /cancel.")
            raise StopPropagation
        except Exception as exc:
            logger.warning("Clone setting update failed for %s (%s)", field, type(exc).__name__)
            await message.reply_text("Could not validate that setting. Check the bot permissions and try again.")
            raise StopPropagation
        await clone_manager.set_settings(record, settings)
        CLONE_FLOWS.pop(user_id, None)
        await message.reply_text(
            "✅ Setting saved for this clone.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ OPEN SETTINGS", callback_data=f"clone_settings:{flow['bot_id']}")]]),
        )
        raise StopPropagation

    if flow.get("stage") in {"premium_add", "premium_remove"}:
        raw = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        record = await _owned_record_for_user(flow["bot_id"], user_id)
        if not record:
            CLONE_FLOWS.pop(user_id, None)
            await message.reply_text("That clone is no longer in your account.")
            raise StopPropagation
        try:
            if flow["stage"] == "premium_add":
                user_text, duration_text = raw.split(maxsplit=1)
                target_user_id = int(user_text)
                seconds = _duration_seconds(duration_text)
                if target_user_id <= 0 or seconds <= 0:
                    raise ValueError
                expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
                await db.add_clone_premium_user(flow["bot_id"], target_user_id, expiry)
                result = f"✅ Added <code>{target_user_id}</code> to this clone's premium list until {expiry:%Y-%m-%d %H:%M UTC}."
            else:
                target_user_id = int(raw)
                removal = await db.remove_clone_premium_user(flow["bot_id"], target_user_id)
                if not removal.deleted_count:
                    raise CloneError("That user is not in this clone's premium list.")
                result = f"✅ Removed <code>{target_user_id}</code> from this clone's premium list."
        except CloneError as exc:
            await message.reply_text(str(exc))
            raise StopPropagation
        except (ValueError, TypeError):
            await message.reply_text("Invalid format. Use a positive user ID and a duration such as 30day, 12hour, or 1month. Try again or /cancel.")
            raise StopPropagation
        except Exception as exc:
            logger.warning("Clone premium user update failed (%s)", type(exc).__name__)
            await message.reply_text("Could not update this clone's premium list. Try again or /cancel.")
            raise StopPropagation
        CLONE_FLOWS.pop(user_id, None)
        await message.reply_text(
            result,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 PREMIUM SETTINGS", callback_data=f"clone_feature:{flow['bot_id']}:premium")]]),
        )
        raise StopPropagation


async def _owned_record_for_user(bot_id, owner_id):
    record = await db.get_clone_bot(bot_id)
    if record and int(record.get("owner_id", 0)) == int(owner_id):
        return record
    return None


def _valid_http_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _duration_seconds(value):
    match = re.fullmatch(r"\s*(\d+)\s*(s|min|hour|day|month|year)\s*", value.lower())
    if not match:
        return 0
    amount = int(match.group(1))
    factors = {"s": 1, "min": 60, "hour": 3600, "day": 86400, "month": 2592000, "year": 31536000}
    return amount * factors[match.group(2)]


def parse_custom_buttons(text):
    rows = []
    total = 0
    for line in text.splitlines():
        parts = re.findall(r"\[([^\]]+)\]", line)
        if not parts or len(parts) % 2:
            return None
        for index in range(0, len(parts), 2):
            label = parts[index].strip()
            target = parts[index + 1].strip()
            if not target.lower().startswith("buttonurl:"):
                return None
            url = target[len("buttonurl:"):]
            same_row = url.lower().endswith(":same")
            if same_row:
                url = url[:-5]
            if not label or len(label) > 50 or not _valid_http_url(url):
                return None
            button = {"text": label, "url": url}
            if same_row and rows and len(rows[-1]) < 2:
                rows[-1].append(button)
            else:
                rows.append([button])
            total += 1
            if total > 20:
                return None
    return rows
