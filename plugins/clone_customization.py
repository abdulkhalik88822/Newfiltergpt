"""Owner controls for a running clone bot, opened with /customize."""

import asyncio
import datetime
import html
import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.users_chats_db import db
from info import CUSTOM_FILE_CAPTION
from plugins.clones import clone_manager, _clone_config, _valid_http_url, parse_custom_buttons
from utils import is_clone_operator

logger = logging.getLogger(__name__)
CONTROL_FLOWS = {}
MAX_CLONE_ADMINS = 20


def _button(text, action):
    return InlineKeyboardButton(text, callback_data=f"cc:{action}")


def _home_markup(settings):
    mode = "PRIVATE" if settings.get("clone_mode") == "private" else "PUBLIC"
    return InlineKeyboardMarkup([
        [_button("💵 MONETIZATION", "monetization")],
        [_button("📝 START MESSAGE", "welcome")],
        [_button("📢 LOG CHANNEL", "log")],
        [_button("☁️ DATABASE CHANNEL", "database")],
        [_button("👥 ADMINS", "admins")],
        [_button("📊 BOT STATUS", "status")],
        [_button(f"🛍 BOT MODE · {mode}", "mode")],
        [_button("⏱ RESTART BOT", "restart")],
        [_button("🚫 DELETE BOT", "delete_check")],
        [_button("🔎 MORE FEATURES", "more")],
    ])


async def _record_for_client(client):
    if not getattr(client, "is_clone", False):
        return None
    record = await db.get_clone_bot(client.clone_bot_id)
    if not record or int(record.get("owner_id", 0)) != int(getattr(client, "clone_owner_id", 0)):
        return None
    return record


async def _render_home(target, client, record):
    settings = getattr(client, "clone_settings", {}) or {}
    username = getattr(client, "username", "") or getattr(getattr(client, "me", None), "username", "")
    identity = f"@{html.escape(username.lstrip('@'))}" if username else html.escape(getattr(client, "first_name", "Clone bot"))
    text = (
        f"🤖 <b>YOUR CLONE BOT - {identity}</b>\n\n"
        "🖊 <b>Choose a control below to customize this bot.</b>\n\n"
        f"Status: <b>{'RUNNING' if getattr(client, 'is_connected', False) else 'OFFLINE'}</b> · "
        f"Mode: <b>{str(settings.get('clone_mode', 'public')).upper()}</b>"
    )
    markup = _home_markup(settings)
    if hasattr(target, "edit_text"):
        try:
            return await target.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return await target.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def _save(client, record, settings):
    await clone_manager.set_settings(record, settings)


def _owner(client, user_id):
    try:
        return int(user_id) == int(getattr(client, "clone_owner_id", 0))
    except (TypeError, ValueError):
        return False


async def _check_channel(client, raw, require_main_bot=False):
    channel_id = int(raw)
    chat = await client.get_chat(channel_id)
    chat_type = str(getattr(chat.type, "value", chat.type)).lower()
    if chat_type not in {"channel", "supergroup"}:
        raise ValueError("Use a channel or supergroup ID.")
    member = await client.get_chat_member(channel_id, client.me.id)
    status = str(getattr(member.status, "value", member.status)).lower()
    if status not in {"administrator", "owner", "creator"}:
        raise ValueError("Make this bot an admin in that channel first.")
    if require_main_bot:
        from dreamxbotz.Bot import dreamxbotz as main_bot
        if not await _bot_is_channel_admin(main_bot, channel_id):
            raise ValueError("Make the main bot an admin in this database channel too.")
    return channel_id


async def _bot_is_channel_admin(bot, channel_id):
    member = await bot.get_chat_member(channel_id, bot.me.id)
    status = str(getattr(member.status, "value", member.status)).lower()
    return status in {"administrator", "owner", "creator"}


async def _show_admins(target, client):
    settings = getattr(client, "clone_settings", {}) or {}
    admins = settings.get("clone_admins", []) or []
    shown = "\n".join(f"• <code>{int(user_id)}</code>" for user_id in admins) or "No extra admins added."
    markup = InlineKeyboardMarkup([
        [_button("➕ ADD ADMIN", "admins_add")],
        [_button("➖ REMOVE ADMIN", "admins_remove")],
        [_button("🗑 CLEAR ADMINS", "admins_clear")],
        [_button("⬅ BACK", "menu")],
    ])
    text = f"<b>👥 CLONE ADMINS</b>\nOwner: <code>{client.clone_owner_id}</code>\n\n{shown}\n\nOnly the owner can add or remove admins."
    if hasattr(target, "edit_text"):
        return await target.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    return await target.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def _show_monetization(target, client):
    settings = getattr(client, "clone_settings", {}) or {}
    plans = settings.get("premium_plans", {}) or {}
    plan_text = ", ".join(f"{amount}⭐={duration}" for amount, duration in plans.items()) or "No Star plans"
    markup = InlineKeyboardMarkup([
        [_button("🔄 TURN PREMIUM " + ("OFF" if settings.get("premium_enabled", True) else "ON"), "toggle_premium")],
        [_button("✏️ EDIT STAR PLANS", "edit_premium_plans")],
        [_button("🧵 EDIT PLAN TEXT", "edit_premium_text")],
        [_button("⬅ BACK", "menu")],
    ])
    text = (
        "<b>💵 MONETIZATION</b>\n"
        f"Premium: <b>{'ON' if settings.get('premium_enabled', True) else 'OFF'}</b>\n"
        f"Star plans: <code>{html.escape(plan_text)}</code>\n"
        f"Plan message: <code>{html.escape(settings.get('premium_text') or 'default')[:350]}</code>"
    )
    if hasattr(target, "edit_text"):
        return await target.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    return await target.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def _show_more(target, client):
    settings = getattr(client, "clone_settings", {}) or {}
    markup = InlineKeyboardMarkup([
        [_button(f"🔐 VERIFICATION · {'ON' if settings.get('is_verify') else 'OFF'}", "toggle:is_verify")],
        [_button(f"⏱ SECOND VERIFICATION · {'ON' if settings.get('second_verification', True) else 'OFF'}", "toggle:second_verification")],
        [_button(f"🔗 SHORTENER · {'ON' if settings.get('shortener_enabled') else 'OFF'}", "toggle:shortener_enabled")],
        [_button(f"📢 FORCE SUBSCRIBE · {'ON' if settings.get('fsub_enabled') else 'OFF'}", "toggle:fsub_enabled")],
        [_button(f"🗑 AUTO DELETE · {'ON' if settings.get('auto_delete') else 'OFF'}", "toggle:auto_delete")],
        [_button(f"🔒 PROTECT CONTENT · {'ON' if settings.get('file_secure') else 'OFF'}", "toggle:file_secure")],
        [_button("✏️ SET SHORTENERS", "edit:shortener")],
        [_button("🗑 CLEAR SHORTENERS", "reset:shortener")],
        [_button("✏️ SET TUTORIAL LINKS", "edit:tutorials")],
        [_button("📢 SET FORCE-SUB CHANNELS", "edit:fsub")],
        [_button("🍿 SET FILE CAPTION", "edit:caption")],
        [_button("🔄 DEFAULT CAPTION", "reset:caption")],
        [_button("🔘 SET RESULT BUTTONS", "edit:custom_buttons")],
        [_button("🗑 CLEAR RESULT BUTTONS", "reset:custom_buttons")],
        [_button("⏲ SET AUTO-DELETE DELAY", "edit:delete_time")],
        [_button("⬅ BACK", "menu")],
    ])
    text = "<b>🔎 MORE FEATURES</b>\nManage verification, force-subscribe, result delivery, and search settings for this clone only."
    if hasattr(target, "edit_text"):
        return await target.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    return await target.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@Client.on_message(filters.command("customize") & filters.private)
async def customize_clone(client, message):
    if not getattr(client, "is_clone", False):
        return await message.reply_text("Open /customize in the clone bot you want to manage.")
    if not is_clone_operator(client, message.from_user.id):
        return await message.reply_text("This private control panel is only for this clone's owner and approved admins.")
    record = await _record_for_client(client)
    if not record:
        return await message.reply_text("This clone is not registered in the management database.")
    await message.reply_text(
        "<b>YOU CAN CUSTOMISE YOUR BOT SETTINGS FROM BELOW BUTTON</b>",
        reply_markup=InlineKeyboardMarkup([[_button("🤖 CUSTOMIZE YOUR BOT SETTINGS 🤖", "menu")]]),
        parse_mode=ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^cc:"))
async def customize_clone_callback(client, query):
    if not getattr(client, "is_clone", False) or not is_clone_operator(client, query.from_user.id):
        return await query.answer("Open this menu from your clone bot.", show_alert=True)
    record = await _record_for_client(client)
    if not record:
        return await query.answer("Clone record not found.", show_alert=True)

    action = query.data.split(":", 1)[1]
    settings = dict(getattr(client, "clone_settings", {}) or {})
    await query.answer()

    if action == "menu":
        return await _render_home(query.message, client, record)
    if action == "welcome":
        CONTROL_FLOWS[(client.clone_bot_id, query.from_user.id)] = "welcome"
        return await query.message.reply_text(
            "Send your start message (up to 500 characters). Placeholders: <code>{first_name}</code>, <code>{bot_name}</code>. Send <code>default</code> to reset, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
    if action in {"log", "database"}:
        field = "clone_log_channel" if action == "log" else "clone_database_channel"
        CONTROL_FLOWS[(client.clone_bot_id, query.from_user.id)] = field
        current = settings.get(field)
        label = "log" if action == "log" else "file-storage/database"
        return await query.message.reply_text(
            f"Send the {label} channel ID. This bot must be an admin there. For the database channel, both the main bot and this clone must be admins; clones share the main file catalog. Current: <code>{current or 'using host default'}</code>. Send <code>clear</code> to use the host default, or /cancel.",
            parse_mode=ParseMode.HTML,
        )
    if action == "admins":
        return await _show_admins(query.message, client)
    if action in {"admins_add", "admins_remove", "admins_clear"}:
        if not _owner(client, query.from_user.id):
            return await query.message.reply_text("Only the clone owner can manage admins.")
        if action == "admins_clear":
            settings["clone_admins"] = []
            await _save(client, record, settings)
            return await _show_admins(query.message, client)
        CONTROL_FLOWS[(client.clone_bot_id, query.from_user.id)] = action
        prompt = "Send numeric Telegram user IDs separated by spaces." if action == "admins_add" else "Send the numeric user ID to remove."
        return await query.message.reply_text(prompt + " Send /cancel to stop.")
    if action == "status":
        stored = await db.get_clone_bot(client.clone_bot_id) or record
        settings = getattr(client, "clone_settings", {}) or {}
        admins = settings.get("clone_admins", []) or []
        status = "RUNNING" if getattr(client, "is_connected", False) else "OFFLINE"
        return await query.message.edit_text(
            f"<b>📊 BOT STATUS</b>\nBot: @{html.escape((client.me.username or '').lstrip('@'))}\nID: <code>{client.clone_bot_id}</code>\n"
            f"Runtime: <b>{status}</b>\nSaved state: <b>{html.escape(str(stored.get('status', 'unknown')))}</b>\n"
            f"Mode: <b>{html.escape(str(settings.get('clone_mode', 'public')).upper())}</b>\n"
            f"Approved admins: <b>{len(admins)}</b>\nCreated: <code>{stored.get('created_at', 'unknown')}</code>",
            reply_markup=InlineKeyboardMarkup([[_button("↻ REFRESH", "status")], [_button("⬅ BACK", "menu")]]),
            parse_mode=ParseMode.HTML,
        )
    if action == "mode":
        settings["clone_mode"] = "private" if settings.get("clone_mode", "public") == "public" else "public"
        await _save(client, record, settings)
        return await _render_home(query.message, client, record)
    if action == "monetization":
        return await _show_monetization(query.message, client)
    if action == "toggle_premium":
        settings["premium_enabled"] = not settings.get("premium_enabled", True)
        await _save(client, record, settings)
        return await _show_monetization(query.message, client)
    if action in {"edit_premium_plans", "edit_premium_text"}:
        CONTROL_FLOWS[(client.clone_bot_id, query.from_user.id)] = "premium_plans" if action.endswith("plans") else "premium_text"
        prompt = "Send plans as <code>10=7day,20=1month</code>, or <code>off</code>." if action.endswith("plans") else "Send the premium plan message, or <code>default</code>."
        return await query.message.reply_text(prompt, parse_mode=ParseMode.HTML)
    if action == "more":
        return await _show_more(query.message, client)
    if action.startswith("toggle:"):
        key = action.split(":", 1)[1]
        allowed = {"is_verify", "second_verification", "shortener_enabled", "fsub_enabled", "auto_delete", "file_secure"}
        if key not in allowed:
            return await query.message.reply_text("Unknown setting.")
        next_value = not settings.get(key, key in {"second_verification", "auto_delete"})
        if key == "is_verify" and next_value and not (settings.get("shortener_enabled") and settings.get("shortner") and settings.get("api")):
            return await query.message.reply_text("Configure the first shortener before enabling verification.")
        if key == "shortener_enabled" and next_value and not (settings.get("shortner") and settings.get("api")):
            return await query.message.reply_text("Set the shortener website and API first.")
        if key == "fsub_enabled" and next_value and not settings.get("fsub"):
            return await query.message.reply_text("Add a force-sub channel first.")
        settings[key] = next_value
        await _save(client, record, settings)
        return await _show_more(query.message, client)
    if action.startswith("edit:"):
        field = action.split(":", 1)[1]
        allowed = {"shortener", "tutorials", "fsub", "caption", "custom_buttons", "delete_time"}
        if field not in allowed:
            return await query.message.reply_text("Unknown setting.")
        CONTROL_FLOWS[(client.clone_bot_id, query.from_user.id)] = field
        prompts = {
            "shortener": "Send six lines: website/API for each of three shorteners. Use <code>-</code> for an unused pair.",
            "tutorials": "Send up to three HTTP(S) tutorial links, one per line. Use <code>-</code> to leave a stage unchanged.",
            "fsub": "Send up to five channel IDs separated by spaces, or <code>clear</code>.",
            "caption": "Send the caption template. Supported: <code>{file_name}</code>, <code>{file_size}</code>, <code>{file_caption}</code>.",
            "custom_buttons": "Send button pairs like <code>[Open][buttonurl:https://example.com]</code>. Use :same for two buttons on one row.",
            "delete_time": "Send auto-delete delay in seconds (10–86400).",
        }
        return await query.message.reply_text(prompts[field], parse_mode=ParseMode.HTML)
    if action.startswith("reset:"):
        field = action.split(":", 1)[1]
        if field == "caption":
            settings["caption"] = CUSTOM_FILE_CAPTION
        elif field == "custom_buttons":
            settings["custom_buttons"] = []
            settings["custom_button_text"] = ""
            settings["custom_button_url"] = ""
        elif field == "shortener":
            for name in ("shortner", "api", "shortner_two", "api_two", "shortner_three", "api_three"):
                settings[name] = ""
            settings["shortener_enabled"] = False
        else:
            return await query.message.reply_text("Unknown reset option.")
        await _save(client, record, settings)
        return await _show_more(query.message, client)
    if action == "restart":
        await query.message.reply_text("♻️ Restart queued. This clone will reconnect shortly.")
        asyncio.create_task(_restart_clone(record))
        return
    if action == "delete_check":
        if not _owner(client, query.from_user.id):
            return await query.message.reply_text("Only the clone owner can delete this bot.")
        return await query.message.edit_text(
            "<b>Delete this clone?</b> Its clone settings and premium-user list will be removed. Revoke the BotFather token separately if you want to invalidate it.",
            reply_markup=InlineKeyboardMarkup([[_button("🗑 CONFIRM DELETE", "delete_confirm")], [_button("CANCEL", "menu")]]),
            parse_mode=ParseMode.HTML,
        )
    if action == "delete_confirm":
        if not _owner(client, query.from_user.id):
            return await query.message.reply_text("Only the clone owner can delete this bot.")
        await query.message.edit_text("Deleting this clone and its clone-specific settings…")
        asyncio.create_task(_delete_clone(record))
        return
    return await query.message.reply_text("Unknown control.")


async def _restart_clone(record):
    bot_id = str(record["_id"])
    try:
        await asyncio.sleep(1)
        await clone_manager.deactivate(record)
        await clone_manager.activate(record)
    except Exception as exc:
        logger.warning("Clone restart failed for %s (%s)", bot_id, type(exc).__name__)


async def _delete_clone(record):
    try:
        await asyncio.sleep(1)
        await clone_manager.delete(record)
    except Exception as exc:
        logger.warning("Clone deletion failed for %s (%s)", record.get("_id"), type(exc).__name__)


@Client.on_message(filters.private & filters.incoming, group=-99)
async def clone_customization_flow(client, message):
    if not getattr(client, "is_clone", False) or not message.from_user:
        return
    key = (int(client.clone_bot_id), int(message.from_user.id))
    field = CONTROL_FLOWS.get(key)
    if not field:
        return
    if not is_clone_operator(client, message.from_user.id):
        CONTROL_FLOWS.pop(key, None)
        return
    command = (message.command or [""])[0].split("@", 1)[0].lower()
    if command == "cancel":
        CONTROL_FLOWS.pop(key, None)
        await message.reply_text("Customization cancelled.")
        return
    record = await _record_for_client(client)
    if not record:
        CONTROL_FLOWS.pop(key, None)
        return await message.reply_text("Clone record not found; the change was not saved.")
    raw = (message.text or "").strip()
    settings = dict(getattr(client, "clone_settings", {}) or {})
    try:
        if field == "welcome":
            if raw.lower() == "default":
                await clone_manager.set_welcome(record, "")
            elif not raw or len(raw) > 500:
                raise ValueError("Keep the welcome message between 1 and 500 characters.")
            else:
                await clone_manager.set_welcome(record, raw)
        elif field in {"clone_log_channel", "clone_database_channel"}:
            if raw.lower() == "clear":
                settings[field] = None
            else:
                settings[field] = await _check_channel(
                    client, raw, require_main_bot=(field == "clone_database_channel")
                )
            await _save(client, record, settings)
        elif field in {"admins_add", "admins_remove"}:
            if not _owner(client, message.from_user.id):
                raise ValueError("Only the owner can manage clone admins.")
            admins = {int(user_id) for user_id in settings.get("clone_admins", [])}
            if field == "admins_add":
                new_ids = {int(part) for part in raw.split()}
                new_ids.discard(int(client.clone_owner_id))
                if not new_ids or any(user_id <= 0 for user_id in new_ids):
                    raise ValueError("Send valid positive numeric user IDs.")
                admins.update(new_ids)
                if len(admins) > MAX_CLONE_ADMINS:
                    raise ValueError(f"A clone can have up to {MAX_CLONE_ADMINS} extra admins.")
            else:
                admin_id = int(raw)
                admins.discard(admin_id)
            settings["clone_admins"] = sorted(admins)
            await _save(client, record, settings)
        elif field == "premium_plans":
            if raw.lower() == "off":
                settings["premium_plans"] = {}
            else:
                plans = {}
                for pair in raw.split(","):
                    amount_text, duration = pair.strip().split("=", 1)
                    amount = int(amount_text)
                    duration = duration.strip()
                    if not 1 <= amount <= 100000 or _duration_seconds(duration) <= 0:
                        raise ValueError("Use amount=duration pairs such as 10=7day.")
                    plans[amount] = duration
                if not 1 <= len(plans) <= 8:
                    raise ValueError("Add between 1 and 8 Star plans.")
                settings["premium_plans"] = plans
            await _save(client, record, settings)
        elif field == "premium_text":
            settings["premium_text"] = "" if raw.lower() == "default" else raw
            if len(settings["premium_text"]) > 900:
                raise ValueError("Plan text is limited to 900 characters.")
            await _save(client, record, settings)
        elif field == "shortener":
            parts = [part.strip() for part in (message.text or "").splitlines()]
            if len(parts) != 6:
                raise ValueError("Send six lines: three website/API pairs.")
            website_keys = ("shortner", "shortner_two", "shortner_three")
            api_keys = ("api", "api_two", "api_three")
            for index, (website_key, api_key) in enumerate(zip(website_keys, api_keys)):
                website, api = parts[index * 2:index * 2 + 2]
                if website == "-" and api == "-":
                    settings[website_key], settings[api_key] = "", ""
                elif not _valid_http_url(website) or not api:
                    raise ValueError("Each shortener needs a valid HTTP(S) URL and API key.")
                else:
                    settings[website_key], settings[api_key] = website, api
            if not settings.get("shortner") or not settings.get("api"):
                raise ValueError("The first shortener is required.")
            settings["shortener_enabled"] = True
            await _save(client, record, settings)
        elif field == "caption":
            if not raw or len(raw) > 900:
                raise ValueError("Caption must be 1–900 characters.")
            settings["caption"] = raw
            await _save(client, record, settings)
        elif field == "fsub":
            if raw.lower() == "clear":
                settings["fsub"], settings["fsub_enabled"] = [], False
            else:
                channels = list(dict.fromkeys(int(part) for part in raw.split()))
                if not channels or len(channels) > 5:
                    raise ValueError("Send 1–5 channel IDs, or clear.")
                for channel_id in channels:
                    await _check_channel(client, str(channel_id))
                settings["fsub"], settings["fsub_enabled"] = channels, True
            await _save(client, record, settings)
        elif field == "tutorials":
            parts = [part.strip() for part in (message.text or "").splitlines()]
            if len(parts) > 3:
                raise ValueError("Send up to three tutorial links.")
            for index, name in enumerate(("tutorial", "tutorial_2", "tutorial_3")):
                value = parts[index] if index < len(parts) else ""
                if value and value != "-":
                    if not _valid_http_url(value):
                        raise ValueError("Tutorials must use HTTP or HTTPS links.")
                    settings[name] = value
            await _save(client, record, settings)
        elif field == "custom_buttons":
            buttons = parse_custom_buttons(message.text or "")
            if not buttons:
                raise ValueError("No valid buttons found; follow the shown format.")
            settings["custom_buttons"] = buttons
            await _save(client, record, settings)
        elif field == "delete_time":
            seconds = int(raw)
            if not 10 <= seconds <= 86400:
                raise ValueError("Delay must be from 10 to 86400 seconds.")
            settings["delete_time"] = seconds
            await _save(client, record, settings)
        else:
            return
    except Exception as exc:
        await message.reply_text(str(exc)[:300] + " Try again or /cancel.")
        return

    CONTROL_FLOWS.pop(key, None)
    await message.reply_text(
        "✅ Saved for this clone.",
        reply_markup=InlineKeyboardMarkup([[_button("⚙️ BACK TO CUSTOMIZE", "menu")]]),
    )
