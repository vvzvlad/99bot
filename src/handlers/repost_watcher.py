#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Repost Watcher Plugin
Replies "это уже было" (or another phrase at random) to a photo in the chat when it repeats an earlier one
(repost_detector tells a re-post of the same picture from a derivative meme),
or a jab at the poster's memory when every original is their own picture, either one saying how many days ago the originals were posted,
and takes a picture (a sticker sent as a photo) and its reposts out of the watch on /ex.
It also reads the text on every picture (image_text, RapidOCR) and on every static and video sticker sent to the chat,
and answers /find <text> with links to the pictures and stickers that carry it. A sticker is only read for /find: it is
no repost.
"""

import asyncio
import json
import logging
import os
import random
import tempfile
import time
from datetime import datetime
import numpy as np
from pyrogram import Client, filters
from pyrogram.types import Message, LinkPreviewOptions
from pyrogram.enums import ParseMode

from config import get_app_timezone, get_settings, pluralize_days
from image_text import TextIndex, read_text
from repost_detector import ROW, RepostIndex, features, load_model

logger = logging.getLogger(__name__)

CHAT_ID = -1001833941803
LINK = "https://t.me/c/1833941803/{}"
DATA_DIR = os.path.join(os.path.abspath(get_settings()["session_path"]), "repost")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
INDEX_PATH = os.path.join(DATA_DIR, "index.bin")
EXCLUDED_PATH = os.path.join(DATA_DIR, "excluded.json")
TEXTS_PATH = os.path.join(DATA_DIR, "texts.jsonl")
FIND_MAX = 20  # /find lists at most this many pictures, the newest
# the first line of the reply to a repost of someone else's picture, one at random
REPOST = (
    "это уже было",
    "этот мем уже видели все, кроме тебя",
    "у этого мема уже внуки",
    "этот мем уже выплатил ипотеку",
    "этот мем помнит доллар по 30",
    "мы над этим уже поржали, спасибо",
)
# ...and to a repost when every original is the poster's own picture
ALZHEIMER = (
    "альцгеймер подкрался незаметно",
    "дед, ты это уже постил",
    "и постед ит эгейн эворд",
    "таблетки от склероза принять не забыл?",
    "память как у золотой рыбки: ты сам это уже постил",
)

# serialises the startup, every photo and /ex, so rows are appended in message order
_lock = asyncio.Lock()
# one OCR at a time, for the engine is shared; kept apart from _lock, so that neither "это уже было" nor the next photo
# waits for the text on a picture or a sticker to be read
_ocr_lock = asyncio.Lock()
# references to the running tasks, so they are not garbage-collected mid-flight
_tasks = set()
_index = None
# message ids of the pictures /ex took out (stickers sent as photos): neither they nor their reposts are reposts
_excluded = None
# the text read off every picture; /find reads it from the event loop, so it is only ever changed there
_texts = None


def _spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _open():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    index = RepostIndex(INDEX_PATH, PHOTOS_DIR)
    excluded = set()
    if os.path.exists(EXCLUDED_PATH):
        with open(EXCLUDED_PATH, encoding="utf-8") as f:
            excluded = set(json.load(f))
    texts = TextIndex(TEXTS_PATH)
    load_model()
    return index, excluded, texts


def _save_excluded():
    # written whole: a write cut short by a restart must not leave a broken file
    tmp = EXCLUDED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(_excluded), f)
    os.replace(tmp, EXCLUDED_PATH)


def _author(message: Message):
    if message.from_user:
        return message.from_user.id
    if message.sender_chat:
        return message.sender_chat.id
    return 0


def _ago(then, now):
    """How long before now the original was posted (both unix seconds), in calendar days of the app's timezone."""
    tz = get_app_timezone()
    days = (datetime.fromtimestamp(now, tz).date() - datetime.fromtimestamp(then, tz).date()).days
    return "сегодня" if days == 0 else f"{days} {pluralize_days(days)} назад"


async def _index_text(msg_id, file):
    """OCR one picture into the text index. A failure is logged and leaves the picture unread, nothing else."""
    try:
        async with _ocr_lock:
            text = await asyncio.to_thread(read_text, os.path.join(PHOTOS_DIR, file))
        # added here, not in the thread: /find reads the index from the event loop
        _texts.add(msg_id, text)
    except Exception as e:
        logger.error(f"Error reading the text on photo {msg_id}: {str(e)}", exc_info=True)


async def _index_sticker_text(client: Client, message: Message):
    """OCR one static or video sticker into the text index. A failure is logged, nothing else."""
    try:
        if _texts is None:
            return
        # the file is kept nowhere: stickers are not in the repost index
        with tempfile.TemporaryDirectory() as tmp:
            suffix = ".webm" if message.sticker.is_video else ".webp"
            path = await client.download_media(message, file_name=os.path.join(tmp, f"{message.id}{suffix}"))
            async with _ocr_lock:
                text = await asyncio.to_thread(read_text, path)
        # added here, not in the thread: /find reads the index from the event loop
        _texts.add(message.id, text)
    except Exception as e:
        logger.error(f"Error reading the text on sticker {message.id}: {str(e)}", exc_info=True)


async def _read_missing_texts():
    """Read the text on every indexed photo that has none yet: the ones taken in while the bot was down, and those
    tools/build_image_texts.py did not see."""
    missing = [(int(i), f.decode()) for i, f in zip(_index.ids, _index.files) if int(i) not in _texts.ids]
    logger.info(f"Reading the text on {len(missing)} photos")
    for msg_id, file in missing:
        await _index_text(msg_id, file)
    logger.info(f"Reading the text on {len(missing)} photos done")


def _check(path, msg_id, author, date, reply_to):
    pdq, sscd = features(path)
    return pdq, sscd, _index.first_repost(path, msg_id, author, date, pdq, sscd, reply_to)


async def _add_photo(client: Client, message: Message, check: bool):
    """Download one photo message and append it to the index; return its file name and, with check, the original
    it re-posts, or None.

    A photo that re-posts a picture /ex took out is no repost.
    """
    file = f"{message.id}.jpg"
    path = os.path.join(PHOTOS_DIR, file)
    author = _author(message)
    date = int(message.date.timestamp())
    # a reply to a message of another chat carries that chat's message id
    reply_to = None if message.external_reply else message.reply_to_message_id
    await client.download_media(message, file_name=path)
    if check:
        pdq, sscd, old_id = await asyncio.to_thread(_check, path, message.id, author, date, reply_to)
    else:
        pdq, sscd = await asyncio.to_thread(features, path)
        old_id = None
    _index.append(np.array([(message.id, author, date, pdq, sscd, file)], ROW))
    return file, (None if old_id in _excluded else old_id)


async def _startup(client: Client):
    """Load the index and the model, then take in what was posted while the bot was down."""
    global _index, _excluded, _texts
    # the handlers are registered by now: what is posted from here on reaches them and gets its answer
    started = time.time()
    async with _lock:
        try:
            _index, _excluded, _texts = await asyncio.to_thread(_open)
            logger.info(f"Repost index loaded: {len(_index.ids)} photos, max id {_index.max_id}")
            if not len(_index.ids):
                logger.info("Repost index is empty, backfill skipped")
                return
            # min_id is inclusive in Kurigram; reverse=True yields oldest first
            count = 0
            async for message in client.get_chat_history(CHAT_ID, min_id=_index.max_id + 1, reverse=True):
                # history runs on into what arrives during the backfill; leave that to the handlers
                if message.date.timestamp() >= started:
                    break
                if not message.photo:
                    continue
                try:
                    # nobody is answered for what was posted while the bot was down, so no stage 2 here
                    await _add_photo(client, message, check=False)
                    count += 1
                except Exception as e:
                    logger.error(f"Error in repost backfill for message {message.id}: {str(e)}", exc_info=True)
            logger.info(f"Repost backfill done: {count} photos, max id {_index.max_id}")
            _spawn(_read_missing_texts())
        except Exception as e:
            logger.error(f"Error in repost watcher startup: {str(e)}", exc_info=True)


async def _on_photo(client: Client, message: Message):
    # (message id, file) of the photos indexed here, their text read after the answer
    added = []
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
                    file, old_id = await _add_photo(client, m, check=True)
                except Exception as e:
                    logger.error(f"Error in repost watcher for message {m.id}: {str(e)}", exc_info=True)
                    continue
                added.append((m.id, file))
                if old_id is not None:
                    hits.append((m, old_id))
            if hits:
                originals = list(dict.fromkeys(old_id for _, old_id in hits))
                author = _author(message)
                own = all(int(_index.authors[_index.ids == o][0]) == author for o in originals)
                head = random.choice(ALZHEIMER if own else REPOST)
                now = int(message.date.timestamp())
                ago = {o: _ago(int(_index.dates[_index.ids == o][0]), now) for o in originals}
                if len(originals) == 1:
                    # a dash, not a comma: the phrase may end in "?"
                    text = f"[{head}]({LINK.format(originals[0])}) — {ago[originals[0]]}"
                else:
                    # a dash after a phrase that may end in "?" or hold a colon itself
                    sep = " — " if own else ": "
                    # numbered links, the age once per group of originals posted the same day (an album shares one date)
                    groups = {}
                    for i, o in enumerate(originals, 1):
                        groups.setdefault(ago[o], []).append(f"[{i}]({LINK.format(o)})")
                    text = f"{head}{sep}" + "; ".join(f"{', '.join(links)} ({age})" for age, links in groups.items())
                await hits[0][0].reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                            link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:
            logger.error(f"Error in repost watcher for message {message.id}: {str(e)}", exc_info=True)
    # after the answer and outside _lock: neither "это уже было" nor the next photo waits for the OCR
    for msg_id, file in added:
        await _index_text(msg_id, file)


async def _on_ex(message: Message):
    """/ex in reply to a picture, or to the bot's answer to it: the picture and those tied to it are no reposts."""
    async with _lock:
        try:
            target = message.reply_to_message
            pic_id = None
            if target and target.photo:
                pic_id = target.id
            elif target and target.outgoing:
                # the bot's "это уже было" replies to the picture; only its own message tells this chat's id,
                # reply_to_message_id of anyone else's may point into another chat
                pic_id = target.reply_to_message_id
            rows = np.nonzero(_index.ids == pic_id)[0] if pic_id else ()
            if len(rows):
                tied = await asyncio.to_thread(_index.reposts_of, rows[0])
                ids = [pic_id, *(int(i) for i in _index.ids[tied])]
                _excluded.update(ids)
                _save_excluded()
                logger.info(f"/ex on photo {pic_id}: {len(ids)} pictures excluded")
                await message.reply_text("картинка исключена")
        except Exception as e:
            logger.error(f"Error in repost exclusion for message {message.id}: {str(e)}", exc_info=True)


async def _on_find(message: Message):
    """/find <text>: links to the newest pictures and stickers that carry the text."""
    try:
        if _texts is None:
            return
        query = " ".join(message.command[1:])
        if not query:
            await message.reply_text("напиши /find и текст с картинки")
            return
        found = _texts.search(query)
        if not found:
            await message.reply_text("не нашёл")
            return
        links = ", ".join(f"[{n}]({LINK.format(i)})" for n, i in enumerate(found[:FIND_MAX], 1))
        head = f"нашёл {len(found)}" if len(found) <= FIND_MAX else f"нашёл {len(found)}, последние {FIND_MAX}"
        await message.reply_text(f"{head}: {links}", parse_mode=ParseMode.MARKDOWN,
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as e:
        logger.error(f"Error in picture text search for message {message.id}: {str(e)}", exc_info=True)


def start(client: Client):
    """Schedule the startup; called once after all handlers are registered."""
    _spawn(_startup(client))


def register_handler(client: Client, group: int = 0):
    """Register the photo, sticker, /ex and /find handlers for the chat"""
    @client.on_message(filters.chat(CHAT_ID) & filters.photo, group=group)
    async def repost_wrapper(client: Client, message: Message):
        # stage 2 may take minutes, so it must not hold a dispatcher worker
        _spawn(_on_photo(client, message))
        await message.continue_propagation()

    @client.on_message(filters.chat(CHAT_ID) & filters.sticker, group=group)
    async def sticker_wrapper(client: Client, message: Message):
        # an animated (.tgs) sticker has no picture to read; the download and the OCR must not hold a dispatcher worker
        if not message.sticker.is_animated:
            _spawn(_index_sticker_text(client, message))
        await message.continue_propagation()

    # text only: filters.command also matches a media caption, and a captioned photo is not a command
    @client.on_message(filters.command("ex") & filters.text & filters.chat(CHAT_ID), group=group)
    async def ex_wrapper(client: Client, message: Message):
        # waits for the lock, which a photo's stage 2 may hold for minutes
        _spawn(_on_ex(message))
        await message.continue_propagation()

    @client.on_message(filters.command("find") & filters.text & filters.chat(CHAT_ID), group=group)
    async def find_wrapper(client: Client, message: Message):
        # no lock: stage 2 of a photo may hold it for minutes, and the search only reads
        await _on_find(message)
        await message.continue_propagation()

    logger.info("Repost watcher handler registered")
