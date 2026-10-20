#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Repost Watcher Plugin
Replies "это уже было" to a photo in the chat when it repeats an earlier one
(repost_detector tells a re-post of the same picture from a derivative meme).
"""

import asyncio
import logging
import os
import numpy as np
from pyrogram import Client, filters
from pyrogram.types import Message, LinkPreviewOptions
from pyrogram.enums import ParseMode

from config import get_settings
from repost_detector import ROW, RepostIndex, features, load_model

logger = logging.getLogger(__name__)

CHAT_ID = -1001833941803
LINK = "https://t.me/c/1833941803/{}"
DATA_DIR = os.path.join(os.path.abspath(get_settings()["session_path"]), "repost")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
INDEX_PATH = os.path.join(DATA_DIR, "index.bin")

# serialises the startup and every photo, so rows are appended in message order
_lock = asyncio.Lock()
# references to the running tasks, so they are not garbage-collected mid-flight
_tasks = set()
_index = None


def _spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _open_index():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    index = RepostIndex(INDEX_PATH, PHOTOS_DIR)
    load_model()
    return index


def _check(path, msg_id, author, date):
    pdq, sscd = features(path)
    return pdq, sscd, _index.first_repost(path, msg_id, author, date, pdq, sscd)


async def _add_photo(client: Client, message: Message, check: bool):
    """Download one photo message and append it to the index; with check, return the original it re-posts."""
    file = f"{message.id}.jpg"
    path = os.path.join(PHOTOS_DIR, file)
    if message.from_user:
        author = message.from_user.id
    elif message.sender_chat:
        author = message.sender_chat.id
    else:
        author = 0
    date = int(message.date.timestamp())
    await client.download_media(message, file_name=path)
    if check:
        pdq, sscd, old_id = await asyncio.to_thread(_check, path, message.id, author, date)
    else:
        pdq, sscd = await asyncio.to_thread(features, path)
        old_id = None
    _index.append(np.array([(message.id, author, date, pdq, sscd, file)], ROW))
    return old_id


async def _startup(client: Client):
    """Load the index and the model, then index the photos posted while the bot was down."""
    global _index
    async with _lock:
        try:
            _index = await asyncio.to_thread(_open_index)
            logger.info(f"Repost index loaded: {len(_index.ids)} photos, max id {_index.max_id}")
            if not len(_index.ids):
                logger.info("Repost index is empty, backfill skipped")
                return
            # min_id is inclusive in Kurigram; reverse=True yields oldest first
            count = 0
            async for message in client.get_chat_history(CHAT_ID, min_id=_index.max_id + 1, reverse=True):
                if not message.photo:
                    continue
                try:
                    await _add_photo(client, message, check=False)
                    count += 1
                except Exception as e:
                    logger.error(f"Error in repost backfill for message {message.id}: {str(e)}", exc_info=True)
            logger.info(f"Repost backfill done: {count} photos, max id {_index.max_id}")
        except Exception as e:
            logger.error(f"Error in repost watcher startup: {str(e)}", exc_info=True)


async def _on_photo(client: Client, message: Message):
    async with _lock:
        try:
            if message.id <= _index.max_id:
                return
            # an album comes as one update per photo: the first one to get here takes the whole album and
            # answers once, the others find their photos already indexed and return above
            album = [message]
            if message.media_group_id:
                album = sorted((m for m in await client.get_media_group(CHAT_ID, message.id)
                                if m.photo and m.id > _index.max_id), key=lambda m: m.id)
            hits = []
            for m in album:
                try:
                    old_id = await _add_photo(client, m, check=True)
                except Exception as e:
                    logger.error(f"Error in repost watcher for message {m.id}: {str(e)}", exc_info=True)
                    continue
                if old_id is not None:
                    hits.append((m, old_id))
            if hits:
                originals = list(dict.fromkeys(old_id for _, old_id in hits))
                if len(originals) == 1:
                    text = f"[это уже было]({LINK.format(originals[0])})"
                else:
                    text = "это уже было: " + ", ".join(f"[{i}]({LINK.format(o)})" for i, o in enumerate(originals, 1))
                await hits[0][0].reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                            link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:
            logger.error(f"Error in repost watcher for message {message.id}: {str(e)}", exc_info=True)


def start(client: Client):
    """Schedule the startup; called once after all handlers are registered."""
    _spawn(_startup(client))


def register_handler(client: Client, group: int = 0):
    """Register the photo handler for the chat"""
    @client.on_message(filters.chat(CHAT_ID) & filters.photo, group=group)
    async def repost_wrapper(client: Client, message: Message):
        # stage 2 may take minutes, so it must not hold a dispatcher worker
        _spawn(_on_photo(client, message))
        await message.continue_propagation()

    logger.info("Repost watcher handler registered")
