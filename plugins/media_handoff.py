"""Fast, shared-channel delivery of main-catalog media to clone bots."""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict

from database.users_chats_db import db
from info import BIN_CHANNEL
from utils import get_database_channel

logger = logging.getLogger(__name__)

_CACHE_LIMIT = 4096
_ADMIN_TTL = 300
_STAGED_MESSAGES = OrderedDict()
_ADMIN_STATUS = OrderedDict()
_STAGE_LOCKS = {}
_ADMIN_LOCKS = {}


class CloneMediaUnavailable(Exception):
    """A clone cannot access the shared media cache channel."""


def _remember(cache, key, value):
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > _CACHE_LIMIT:
        cache.popitem(last=False)


def _get_lock(cache, key):
    lock = cache.get(key)
    if lock is not None:
        return lock
    if len(cache) >= _CACHE_LIMIT:
        for old_key, old_lock in tuple(cache.items()):
            if not old_lock.locked():
                cache.pop(old_key, None)
                break
    lock = asyncio.Lock()
    cache[key] = lock
    return lock


async def _is_admin(bot, channel_id):
    bot_id = int(bot.me.id)
    key = (bot_id, int(channel_id))
    now = time.monotonic()
    cached = _ADMIN_STATUS.get(key)
    if cached and cached[0] > now:
        _ADMIN_STATUS.move_to_end(key)
        return cached[1]

    lock = _get_lock(_ADMIN_LOCKS, key)
    async with lock:
        cached = _ADMIN_STATUS.get(key)
        now = time.monotonic()
        if cached and cached[0] > now:
            _ADMIN_STATUS.move_to_end(key)
            return cached[1]
        member = await bot.get_chat_member(int(channel_id), bot_id)
        status = str(getattr(member.status, "value", member.status)).lower()
        is_admin = status in {"administrator", "owner", "creator"}
        _remember(_ADMIN_STATUS, key, (now + (_ADMIN_TTL if is_admin else 5), is_admin))
        return is_admin


async def _staged_message_id(main_bot, channel_id, file_id):
    cache_key = hashlib.sha256(f"{int(channel_id)}:{file_id}".encode("utf-8")).hexdigest()
    cached_id = _STAGED_MESSAGES.get(cache_key)
    if cached_id:
        _STAGED_MESSAGES.move_to_end(cache_key)
        return cached_id

    lock = _get_lock(_STAGE_LOCKS, cache_key)
    async with lock:
        cached_id = _STAGED_MESSAGES.get(cache_key)
        if cached_id:
            _STAGED_MESSAGES.move_to_end(cache_key)
            return cached_id

        # Persisted entries are trusted: cache-channel messages are intentionally
        # retained, so a Telegram get_messages round trip on every send is wasteful.
        cached = await db.get_clone_media_cache(cache_key)
        if cached:
            message_id = int(cached["message_id"])
            _remember(_STAGED_MESSAGES, cache_key, message_id)
            return message_id

        try:
            staged = await main_bot.send_cached_media(chat_id=int(channel_id), file_id=file_id)
        except Exception as exc:
            logger.warning("Main bot could not stage catalog media (%s)", type(exc).__name__)
            raise CloneMediaUnavailable(
                "The main bot could not read this catalog file. Its file reference may have expired; refresh or re-index that file."
            ) from None

        message_id = int(staged.id)
        _remember(_STAGED_MESSAGES, cache_key, message_id)
        await db.set_clone_media_cache(cache_key, channel_id, message_id)
        return message_id


async def _relay_to_clone(
    client, *, chat_id, file_id, caption, protect_content, reply_markup
):
    from dreamxbotz.Bot import dreamxbotz as main_bot

    channel_id = int(get_database_channel(client) or BIN_CHANNEL)
    main_is_admin, clone_is_admin = await asyncio.gather(
        _is_admin(main_bot, channel_id),
        _is_admin(client, channel_id),
    )
    if not main_is_admin:
        raise CloneMediaUnavailable(
            "The main bot must be an admin in the selected database/cache channel."
        )
    if not clone_is_admin:
        raise CloneMediaUnavailable(
            "Add this clone bot as an admin in the database/cache channel, then try again."
        )

    message_id = await _staged_message_id(main_bot, channel_id, file_id)
    try:
        return await client.copy_message(
            chat_id=chat_id,
            from_chat_id=channel_id,
            message_id=message_id,
            caption=caption,
            protect_content=protect_content,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        # Recover once if an operator removed a cached channel message manually.
        error_name = type(exc).__name__.lower()
        error_text = str(exc).lower()
        missing_message = (
            error_name in {"messageidinvalid", "messagetocopynotfound", "messageempty"}
            or "message_id_invalid" in error_text
            or "message to copy not found" in error_text
        )
        if not missing_message:
            raise

        cache_key = hashlib.sha256(f"{channel_id}:{file_id}".encode("utf-8")).hexdigest()
        _STAGED_MESSAGES.pop(cache_key, None)
        await db.delete_clone_media_cache(cache_key)
        message_id = await _staged_message_id(main_bot, channel_id, file_id)
        return await client.copy_message(
            chat_id=chat_id,
            from_chat_id=channel_id,
            message_id=message_id,
            caption=caption,
            protect_content=protect_content,
            reply_markup=reply_markup,
        )


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
    """Use one channel copy for clone catalog IDs; main bot sends stay direct."""
    if getattr(client, "is_clone", False):
        try:
            return await _relay_to_clone(
                client,
                chat_id=chat_id,
                file_id=file_id,
                caption=caption,
                protect_content=protect_content,
                reply_markup=reply_markup,
            )
        except CloneMediaUnavailable:
            raise
        except Exception as exc:
            logger.warning("Clone media relay failed (%s)", type(exc).__name__)
            raise CloneMediaUnavailable(
                "The clone cannot access the shared database channel. Make both bots admins there, then retry."
            ) from None

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
