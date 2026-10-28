#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Mention Watcher Plugin
Tags chat members on @<group> (@all included): the members who signed up with /join <group>
and did not /leave it. A group appears with its first /join and disappears with its last /leave.
The names go out in replies of five.
"""

import html
import json
import logging
import os
import re
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode

from config import get_settings

logger = logging.getLogger(__name__)

CHAT_ID = -1001833941803
GROUPS_PATH = os.path.join(os.path.abspath(get_settings()["session_path"]), "mention_groups.json")
# only the first five mentions in a message get a push notification
BATCH = 5
# the lookbehind skips e-mail addresses
TAG = re.compile(r"(?<!\w)@(\w+)")

# group name -> ids of the users who joined it, loaded from GROUPS_PATH on first use
_groups = None


def _load():
    global _groups
    if _groups is None:
        _groups = {}
        if os.path.exists(GROUPS_PATH):
            with open(GROUPS_PATH, encoding="utf-8") as f:
                _groups = json.load(f)
    return _groups


def _save():
    # written whole: a write cut short by a restart must not leave a broken file
    tmp = GROUPS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_groups, f)
    os.replace(tmp, GROUPS_PATH)


def _group_name(message: Message):
    """The group the command names, without a leading @ and lowercased, or None when it is missing or invalid."""
    if len(message.command) < 2:
        return None
    name = message.command[1].removeprefix("@").lower()
    return name if re.fullmatch(r"\w+", name) else None


async def handle_join(client: Client, message: Message):
    try:
        name = _group_name(message)
        groups = _load()
        if name is None:
            text = "напиши /join moscow"
            if groups:
                text += " — есть: " + ", ".join(f"@{group}" for group in sorted(groups))
            await message.reply_text(text)
            return
        members = groups.setdefault(name, [])
        if message.from_user.id in members:
            await message.reply_text(f"ты уже в @{name}")
            return
        members.append(message.from_user.id)
        _save()
        await message.reply_text(f"ты в @{name}")
    except Exception as e:
        logger.error(f"Error in join handler: {str(e)}", exc_info=True)


async def handle_leave(client: Client, message: Message):
    try:
        name = _group_name(message)
        if name is None:
            await message.reply_text("напиши /leave moscow")
            return
        groups = _load()
        members = groups.get(name, [])
        if message.from_user.id not in members:
            await message.reply_text(f"тебя и не было в @{name}")
            return
        members.remove(message.from_user.id)
        if not members:
            del groups[name]
        _save()
        await message.reply_text(f"ты больше не в @{name}")
    except Exception as e:
        logger.error(f"Error in leave handler: {str(e)}", exc_info=True)


async def handle_mention(client: Client, message: Message):
    try:
        text = message.text or message.caption
        # a command such as "/join @moscow" names a group, it does not call it
        if text.startswith("/"):
            return
        tags = {tag.lower() for tag in TAG.findall(text)}
        groups = _load()
        # a tag that is not a group is an ordinary username
        wanted = {user_id for tag in tags for user_id in groups.get(tag, [])}
        if not wanted:
            return

        me = await client.get_me()
        sender = message.from_user.id if message.from_user else None
        mentions = []
        # this also caches the users' access hashes, which the mention links need
        async for member in client.get_chat_members(CHAT_ID):
            user = member.user
            if user.id not in wanted:
                continue
            if user.is_bot or user.is_deleted or user.id in (me.id, sender):
                continue
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username or f"id:{user.id}"
            mentions.append(f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>')
        if not mentions:
            return

        for i in range(0, len(mentions), BATCH):
            await message.reply_text(", ".join(mentions[i:i + BATCH]), parse_mode=ParseMode.HTML)
        logger.info(f"Mention {sorted(tags)} in chat {CHAT_ID}: tagged {len(mentions)} users")
    except Exception as e:
        logger.error(f"Error in mention handler: {str(e)}", exc_info=True)


def register_handler(client: Client, group: int = 0):
    """Register the /join, /leave and @mention handlers for the chat"""

    # text only: filters.command also matches a media caption
    @client.on_message(filters.command("join") & filters.text & filters.chat(CHAT_ID), group=group)
    async def join_wrapper(client: Client, message: Message):
        await handle_join(client, message)
        await message.continue_propagation()

    @client.on_message(filters.command("leave") & filters.text & filters.chat(CHAT_ID), group=group)
    async def leave_wrapper(client: Client, message: Message):
        await handle_leave(client, message)
        await message.continue_propagation()

    # not from the bot itself: its own "ты в @moscow" must not call the group
    @client.on_message(filters.chat(CHAT_ID) & (filters.text | filters.caption) & ~filters.me, group=group)
    async def mention_wrapper(client: Client, message: Message):
        await handle_mention(client, message)
        await message.continue_propagation()

    logger.info("Mention watcher handler registered")
