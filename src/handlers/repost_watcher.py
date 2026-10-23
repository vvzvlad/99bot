#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Repost Watcher Plugin
Replies "это уже было" (or another phrase at random) to a photo in the chat when it repeats an earlier one
(repost_detector tells a re-post of the same picture from a derivative meme),
or a jab at the poster's memory when every original is their own picture,
keeps every author's rating (1000 / percent of reposts among their pictures),
shows the rating of the active members on /rating
and takes a picture (a sticker sent as a photo) and its reposts out of both on /st.
"""

import asyncio
import json
import logging
import math
import os
import random
import time
import numpy as np
from PIL import Image
from pyrogram import Client, filters
from pyrogram.types import Message, LinkPreviewOptions
from pyrogram.enums import ParseMode

from config import get_settings
from repost_detector import ROW, RATE, RepostIndex, RepostRating, features, is_photo_aspect, load_model

logger = logging.getLogger(__name__)

CHAT_ID = -1001833941803
LINK = "https://t.me/c/1833941803/{}"
DATA_DIR = os.path.join(os.path.abspath(get_settings()["session_path"]), "repost")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
INDEX_PATH = os.path.join(DATA_DIR, "index.bin")
RATING_PATH = os.path.join(DATA_DIR, "rating.bin")
LAST_SEEN_PATH = os.path.join(DATA_DIR, "last_seen.json")
EXCLUDED_PATH = os.path.join(DATA_DIR, "excluded.json")
RATING_MIN_PICTURES = 50      # /rating leaves out authors with fewer counted pictures
RATING_ACTIVE_DAYS = 30       # ...and those who wrote nothing in the chat for longer
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

# serialises the startup, every photo, every last_seen update and /rating, so rows are appended in message order
_lock = asyncio.Lock()
# references to the running tasks, so they are not garbage-collected mid-flight
_tasks = set()
_index = None
_rating = None
# {"max_id": last chat message id seen, "seen": {"<author>": unix time of their last message}}
_last_seen = None
# message ids of the pictures /st took out (stickers sent as photos): neither they nor their reposts are reposts
_excluded = None


def _spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _open():
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    index = RepostIndex(INDEX_PATH, PHOTOS_DIR)
    rating = RepostRating(RATING_PATH)
    last_seen = {"max_id": 0, "seen": {}}
    if os.path.exists(LAST_SEEN_PATH):
        with open(LAST_SEEN_PATH, encoding="utf-8") as f:
            last_seen = json.load(f)
    excluded = set()
    if os.path.exists(EXCLUDED_PATH):
        with open(EXCLUDED_PATH, encoding="utf-8") as f:
            excluded = set(json.load(f))
    load_model()
    return index, rating, last_seen, excluded


def _save_last_seen():
    # written whole on every message: a write cut short by a restart must not leave a broken file
    tmp = LAST_SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_last_seen, f)
    os.replace(tmp, LAST_SEEN_PATH)


def _save_excluded():
    # written whole, as last_seen is
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


def _see(message: Message):
    """Take the message into last_seen (in memory)."""
    key = str(_author(message))
    seen = _last_seen["seen"]
    seen[key] = max(seen.get(key, 0), int(message.date.timestamp()))
    _last_seen["max_id"] = max(_last_seen["max_id"], message.id)


def _counted(path):
    """True when the picture counts in the rating: camera photos (4:3, 16:9) do not."""
    with Image.open(path) as im:
        return not is_photo_aspect(*im.size)


def _rating_value(n, r):
    """1000 / (percent of reposts) = 10 n / r, infinite without reposts."""
    return 10 * n / r if r else math.inf


def _comma(x):
    """One decimal with a comma, "∞" for infinity."""
    return "∞" if math.isinf(x) else f"{x:.1f}".replace(".", ",")


def _unless_excluded(old_id, counted):
    """(old_id, counted) of a photo, or (None, False) when it re-posts an excluded picture: it does not count at all."""
    if old_id in _excluded:
        return None, False
    return old_id, counted


def _check(path, msg_id, author, date):
    pdq, sscd = features(path)
    return pdq, sscd, _index.first_repost(path, msg_id, author, date, pdq, sscd, _excluded)


def _catch_up():
    """Append the rating rows of the index rows newer than the rating, as first_repost judges them now."""
    count = 0
    for j in np.nonzero(_index.ids > _rating.max_id)[0]:
        path = os.path.join(PHOTOS_DIR, _index.files[j].decode())
        msg_id, author = int(_index.ids[j]), int(_index.authors[j])
        old_id = _index.first_repost(path, msg_id, author, int(_index.dates[j]), _index.pdq[j], _index.sscd[j],
                                     _excluded)
        old_id, counted = _unless_excluded(old_id, _counted(path))
        _rating.append(np.array([(msg_id, author, counted, old_id is not None)], RATE))
        count += 1
    return count


async def _add_photo(client: Client, message: Message):
    """Download one photo message, append it to the index and the rating.

    Returns the original it re-posts (or None) and whether the picture counts in the rating.
    """
    file = f"{message.id}.jpg"
    path = os.path.join(PHOTOS_DIR, file)
    author = _author(message)
    date = int(message.date.timestamp())
    await client.download_media(message, file_name=path)
    pdq, sscd, old_id = await asyncio.to_thread(_check, path, message.id, author, date)
    _index.append(np.array([(message.id, author, date, pdq, sscd, file)], ROW))
    old_id, counted = _unless_excluded(old_id, _counted(path))
    _rating.append(np.array([(message.id, author, counted, old_id is not None)], RATE))
    return old_id, counted


async def _startup(client: Client):
    """Load the index, the rating, last_seen and the model, then take in what was posted while the bot was down."""
    global _index, _rating, _last_seen, _excluded
    # the handlers are registered by now: what is posted from here on reaches them and gets its answer
    started = time.time()
    async with _lock:
        try:
            _index, _rating, _last_seen, _excluded = await asyncio.to_thread(_open)
            logger.info(f"Repost index loaded: {len(_index.ids)} photos, max id {_index.max_id}; "
                        f"rating {len(_rating.ids)} rows; last seen max id {_last_seen['max_id']}")
            count = await asyncio.to_thread(_catch_up)
            logger.info(f"Repost rating caught up: {count} rows")
            if not len(_index.ids):
                logger.info("Repost index is empty, backfill skipped")
                return
            # min_id is inclusive in Kurigram; reverse=True yields oldest first
            count = 0
            min_id = min(_index.max_id, _last_seen["max_id"]) + 1
            async for message in client.get_chat_history(CHAT_ID, min_id=min_id, reverse=True):
                # history runs on into what arrives during the backfill; leave that to the handlers
                if message.date.timestamp() >= started:
                    break
                if message.id > _last_seen["max_id"] and not message.service:
                    _see(message)
                if not message.photo or message.id <= _index.max_id:
                    continue
                try:
                    await _add_photo(client, message)
                    count += 1
                except Exception as e:
                    logger.error(f"Error in repost backfill for message {message.id}: {str(e)}", exc_info=True)
            _save_last_seen()
            logger.info(f"Repost backfill done: {count} photos, max id {_index.max_id}; "
                        f"last seen max id {_last_seen['max_id']}")
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
            counted_hits = 0
            for m in album:
                try:
                    old_id, counted = await _add_photo(client, m)
                except Exception as e:
                    logger.error(f"Error in repost watcher for message {m.id}: {str(e)}", exc_info=True)
                    continue
                if old_id is not None:
                    hits.append((m, old_id))
                    counted_hits += counted
            if hits:
                originals = list(dict.fromkeys(old_id for _, old_id in hits))
                author = _author(message)
                own = all(int(_index.authors[_index.ids == o][0]) == author for o in originals)
                head = random.choice(ALZHEIMER if own else REPOST)
                if len(originals) == 1:
                    text = f"[{head}]({LINK.format(originals[0])})"
                else:
                    # a dash after a phrase that may end in "?" or hold a colon itself
                    sep = " — " if own else ": "
                    text = f"{head}{sep}" + ", ".join(f"[{i}]({LINK.format(o)})" for i, o in enumerate(originals, 1))
                if counted_hits:
                    # "before" leaves out only the reposted pictures, so new memes in the same album
                    # do not hide what the reposts cost
                    n, r = _rating.stats(author)
                    before = _comma(_rating_value(n - counted_hits, r - counted_hits))
                    after = _comma(_rating_value(n, r))
                    lead = "\nтвой рейтинг" if own else "\nты запостил баян, твой рейтинг"
                    # when every counted picture is a repost the rating stays at 10
                    text += f"{lead}: {after}" if before == after else f"{lead} понижен: {before} → {after}"
                await hits[0][0].reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                            link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as e:
            logger.error(f"Error in repost watcher for message {message.id}: {str(e)}", exc_info=True)


async def _track(message: Message):
    # under the lock: last_seen is loaded by the startup, and its backfill takes only messages newer than max_id
    async with _lock:
        try:
            _see(message)
            _save_last_seen()
        except Exception as e:
            logger.error(f"Error in last_seen for message {message.id}: {str(e)}", exc_info=True)


async def _on_rating(client: Client, message: Message):
    async with _lock:
        try:
            members = {}
            async for member in client.get_chat_members(CHAT_ID):
                if not member.user.is_bot and not member.user.is_self:
                    members[member.user.id] = member.user
            active_since = time.time() - RATING_ACTIVE_DAYS * 86400
            rows = sorted(((_rating_value(n, r), n, r, members[a]) for a, (n, r) in _rating.table().items()
                           if a in members and n >= RATING_MIN_PICTURES
                           and _last_seen["seen"].get(str(a), 0) >= active_since),
                          key=lambda row: (row[0], row[1]), reverse=True)
            lines = ["Рейтинг = 1000 / % баянов"]
            for i, (value, n, r, user) in enumerate(rows, 1):
                name = " ".join(x for x in (user.first_name, user.last_name) if x)
                lines.append(f"{i}. {name} — {_comma(value)} (баянов {_comma(100 * r / n)} %, картинок {n})")
            # plain text: a member's name must not be read as markup
            await message.reply_text("\n".join(lines), parse_mode=ParseMode.DISABLED)
        except Exception as e:
            logger.error(f"Error in repost rating for message {message.id}: {str(e)}", exc_info=True)


async def _on_st(message: Message):
    """/st in reply to a picture, or to the bot's answer to it: the picture and those tied to it as reposts stop counting."""
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
                _rating.exclude(ids)
                logger.info(f"/st on photo {pic_id}: {len(ids)} pictures excluded")
                await message.reply_text("картинка исключена")
        except Exception as e:
            logger.error(f"Error in repost exclusion for message {message.id}: {str(e)}", exc_info=True)


def start(client: Client):
    """Schedule the startup; called once after all handlers are registered."""
    _spawn(_startup(client))


def register_handler(client: Client, group: int = 0):
    """Register the photo, /rating and /st handlers for the chat, and the last_seen tracker one group later"""
    @client.on_message(filters.chat(CHAT_ID) & filters.photo, group=group)
    async def repost_wrapper(client: Client, message: Message):
        # stage 2 may take minutes, so it must not hold a dispatcher worker
        _spawn(_on_photo(client, message))
        await message.continue_propagation()

    @client.on_message(filters.command(["rating", "рейтинг"]) & filters.chat(CHAT_ID), group=group)
    async def rating_wrapper(client: Client, message: Message):
        # waits for the lock, which a photo's stage 2 may hold for minutes
        _spawn(_on_rating(client, message))
        await message.continue_propagation()

    # text only: filters.command also matches a media caption, and a captioned photo is not a command
    @client.on_message(filters.command("st") & filters.text & filters.chat(CHAT_ID), group=group)
    async def st_wrapper(client: Client, message: Message):
        # waits for the lock, as /rating does
        _spawn(_on_st(message))
        await message.continue_propagation()

    # its own group, so it sees every message whatever the handlers of the photo group do with it;
    # service messages (joins, pins) are not activity, as in tools/build_repost_index.py
    @client.on_message(filters.chat(CHAT_ID) & ~filters.service, group=group + 1)
    async def last_seen_wrapper(client: Client, message: Message):
        _spawn(_track(message))
        await message.continue_propagation()

    logger.info("Repost watcher handler registered")
