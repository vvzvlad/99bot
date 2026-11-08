#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Mention Watcher Plugin
Tags chat members on #<group> (#all included): the members who signed up with /join <group>
and did not /leave it. A group appears with its first /join and disappears with its last /leave.
The names go out in replies of five.
"""

import difflib
import html
import json
import logging
import os
import re
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError

from config import get_settings

logger = logging.getLogger(__name__)

CHAT_ID = -1001833941803
GROUPS_PATH = os.path.join(os.path.abspath(get_settings()["session_path"]), "mention_groups.json")
# only the first five mentions in a message get a push notification
BATCH = 5
# groups are called with #, not @: a group named after someone's username took over every
# @mention of that person; the lookbehind skips a # glued to a word, such as page#anchor
# (a link's /#anchor still counts)
TAG = re.compile(r"(?<!\w)#(\w+)")
# a tag at least this similar (difflib ratio) to a group's name is taken for a misspelling of it:
# #наркоманы_ебучие against #наркоманы_ебаные scores 0.81
CLOSE = 0.8
# a group name: Latin a-z, Russian а-я and ё, digits and underscores, with at least one letter
# or digit; "\w" let in a lone underscore, invisible Hangul fillers and one-letter names such
# as #ʘ and #𐳓
NAME = re.compile(r"(?=.*[0-9a-zа-яё])[0-9a-zа-яё_]+")
# Latin letters that look like Russian ones: "#бимбo" with a Latin o means #бимбо, not a
# misspelling of it, and the hint "нет #бимбo, есть #бимбо" reads as nonsense
LOOKALIKE = str.maketrans("aceopxy", "асеорху")

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
    """The group the command names, without a leading # and lowercased, or None when it is missing or invalid."""
    if len(message.command) < 2:
        return None
    name = message.command[1].removeprefix("#").lower()
    return name if NAME.fullmatch(name) else None


def _find(name, groups):
    """The existing group the name means, lookalike Latin and Russian letters taken as one, or None."""
    if name in groups:
        return name
    folded = name.translate(LOOKALIKE)
    return next((group for group in groups if group.translate(LOOKALIKE) == folded), None)


async def handle_join(client: Client, message: Message):
    try:
        name = _group_name(message)
        groups = _load()
        if name is None:
            text = "напиши /join moscow"
            if groups:
                text += " — есть: " + ", ".join(f"#{group}" for group in sorted(groups))
            await message.reply_text(text)
            return
        # a group must not be named after a member's username: #vvzvlad reads as that member;
        # collectible usernames count too, and the name is given without @ so as not to ping him
        async for member in client.get_chat_members(CHAT_ID):
            handles = [member.user.username, *(u.username for u in member.user.usernames or [])]
            if name.translate(LOOKALIKE) in {h.lower().translate(LOOKALIKE) for h in handles if h}:
                await message.reply_text(f"{name} — это ник, а не группа")
                return
        # "/join бимбo" with a Latin o joins #бимбо rather than starting a lookalike next to it
        name = _find(name, groups) or name
        reply = message.reply_to_message
        # "/join moscow @vasya" signs up the named user; /join in reply to someone's message
        # signs up that message's author, whoever sends it; a reply to a bot (the
        # "напиши /join moscow" hint included) signs up the sender
        if len(message.command) > 2:
            handle = message.command[2].removeprefix("@")
            try:
                user = await client.get_users(handle)
            except RPCError:
                await message.reply_text(f"не знаю @{handle}")
                return
            who = user.first_name or handle
        elif reply and reply.from_user and not (reply.from_user.is_self or reply.from_user.is_bot):
            user, who = reply.from_user, reply.from_user.first_name or "он"
        else:
            user, who = message.from_user, "ты"
        members = groups.setdefault(name, [])
        if user.id in members:
            await message.reply_text(f"{who} уже в #{name}")
            return
        members.append(user.id)
        _save()
        await message.reply_text(f"{who} в #{name}")
    except Exception as e:
        logger.error(f"Error in join handler: {str(e)}", exc_info=True)


async def handle_leave(client: Client, message: Message):
    try:
        name = _group_name(message)
        if name is None:
            await message.reply_text("напиши /leave moscow")
            return
        groups = _load()
        name = _find(name, groups) or name
        members = groups.get(name, [])
        if message.from_user.id not in members:
            await message.reply_text(f"тебя и не было в #{name}")
            return
        members.remove(message.from_user.id)
        if not members:
            del groups[name]
        _save()
        await message.reply_text(f"ты больше не в #{name}")
    except Exception as e:
        logger.error(f"Error in leave handler: {str(e)}", exc_info=True)


async def handle_mention(client: Client, message: Message):
    try:
        text = message.text or message.caption
        # a command such as "/join #moscow" names a group, it does not call it
        if text.startswith("/"):
            return
        tags = {tag.lower() for tag in TAG.findall(text)}
        groups = _load()
        found = {tag: _find(tag, groups) for tag in tags}
        # a tag that is not a group is an ordinary hashtag...
        wanted = {user_id for group in found.values() if group for user_id in groups[group]}
        # ...unless it is a group's name misspelled: then the bot names the group instead of
        # guessing and calling it, so a hashtag that merely resembles a group calls nobody
        typos = [(tag, close[0]) for tag in sorted(tag for tag, group in found.items() if group is None)
                 if (close := difflib.get_close_matches(tag, groups, n=1, cutoff=CLOSE))]
        if typos:
            # no markup: a double underscore in a misspelled name would otherwise turn into italics
            await message.reply_text("\n".join(f"нет #{tag}, есть #{group}" for tag, group in typos),
                                     parse_mode=ParseMode.DISABLED)
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
    """Register the /join, /leave and #group handlers for the chat"""

    # text only: filters.command also matches a media caption
    @client.on_message(filters.command("join") & filters.text & filters.chat(CHAT_ID), group=group)
    async def join_wrapper(client: Client, message: Message):
        await handle_join(client, message)
        await message.continue_propagation()

    @client.on_message(filters.command("leave") & filters.text & filters.chat(CHAT_ID), group=group)
    async def leave_wrapper(client: Client, message: Message):
        await handle_leave(client, message)
        await message.continue_propagation()

    # not from the bot itself: its own "ты в #moscow" must not call the group; and not a
    # forward: a channel post's own hashtags (#moscow) must not call it either
    @client.on_message(filters.chat(CHAT_ID) & (filters.text | filters.caption) & ~filters.me
                       & ~filters.forwarded, group=group)
    async def mention_wrapper(client: Client, message: Message):
        await handle_mention(client, message)
        await message.continue_propagation()

    logger.info("Mention watcher handler registered")
