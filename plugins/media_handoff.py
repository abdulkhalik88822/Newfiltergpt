"""Send main-bot catalog media through a shared channel for clone bots."""

import hashlib
import logging

from database.users_chats_db import db
from info import BIN_CHANNEL
from utils import get_database_channel

logger = logging.getLogger(__name__)


class CloneMediaUnavailable(Exception):
    """A clone cannot access either its Telegram file ID or the shared cache channel."""


def _needs_handoff(error):
    name = type(error).__name__.lower()
    message = str(error).lower()
    return (
        name in {"mediaempty", "filereferenceexpired", "fileidinvalid", "badrequest"}
        or any(code in message for code in (
            "media_empty", "file_reference_expired", "file_id_invalid",
            "wrong file identifier", "wrong remote file identifier",
        ))
    )


async def _is_admin(bot, channel_id):
    member = await bot.get_chat_member(channel_id, bot.me.id)
    status = str(getattr(member.status, "value", member.status)).lower()
    return status in {"administrator", "owner", "creator"}


async def _staged_message_id(main_bot, channel_id, file_id):
    cache_key = hashlib.sha256(f"{int(channel_id)}:{file_id}".encode("utf-8")).hexdigest()
    cached = await db.get_clone_media_cache(cache_key)
    if cached:
        try:
            staged = await main_bot.get_messages(channel_id, int(cached["message_id"]))
            if staged and not getattr(staged, "empty", False):
                return int(cached["message_id"])
        except Exception:
            pass
        await db.delete_clone_media_cache(cache_key)

    try:
        staged = await main_bot.send_cached_media(chat_id=channel_id, file_id=file_id)
    except Exception as exc:
        logger.warning("Main bot could not stage catalog media (%s)", type(exc).__name__)
        raise CloneMediaUnavailable(
            "The main bot could not read this catalog file. Its file reference may have expired; refresh or re-index that file."
        ) from None
    await db.set_clone_media_cache(cache_key, channel_id, staged.id)
    return int(staged.id)


async def send_catalog_media(
    client,
    *,
    chat_id,
    file_id,
    file_type,
    caption=None,
    video_cover=None,
    protect_content=False,
    reply_markup=None,
):
    """Try the clone's cached file ID, then relay through a shared cache channel."""
    try:
        if file_type == "video":
            return await client.send_video(
                chat_id=chat_id,
                video=file_id,
                video_cover=video_cover,
                caption=caption,
                protect_content=protect_content,
                reply_markup=reply_markup,
            )
        return await client.send_cached_media(
            chat_id=chat_id,
            file_id=file_id,
            caption=caption,
            protect_content=protect_content,
            reply_markup=reply_markup,
        )
    except Exception as direct_error:
        if not getattr(client, "is_clone", False) or not _needs_handoff(direct_error):
            raise

    try:
        from dreamxbotz.Bot import dreamxbotz as main_bot

        channel_id = int(get_database_channel(client) or BIN_CHANNEL)
        if not await _is_admin(main_bot, channel_id):
            raise CloneMediaUnavailable(
                "The main bot must be an admin in the selected database channel to relay files."
            )
        if not await _is_admin(client, channel_id):
            raise CloneMediaUnavailable(
                "Add this clone bot as an admin in the database/cache channel, then try the file again."
            )
        message_id = await _staged_message_id(main_bot, channel_id, file_id)
        return await client.copy_message(
            chat_id=chat_id,
            from_chat_id=channel_id,
            message_id=message_id,
            caption=caption,
            protect_content=protect_content,
            reply_markup=reply_markup,
        )
    except CloneMediaUnavailable:
        raise
    except Exception as exc:
        logger.warning("Clone media relay failed (%s)", type(exc).__name__)
        raise CloneMediaUnavailable(
            "The clone cannot access the shared database channel. Make both the main bot and clone bot admins there, then retry."
        ) from None
