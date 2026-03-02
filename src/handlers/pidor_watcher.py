#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Pidor Watcher Plugin
Выбор "пидора дня" по команде /пидор на основе XOR-метрики хешей.

Алгоритм (детерминирован по дате, не рандомный):
1. day_hash  = sha256(str(unix_timestamp полуночи UTC сегодня))
2. user_hash = sha256(str(user.id)) для каждого участника
3. distance  = int(day_hash[:16], 16) XOR int(user_hash[:16], 16)
4. Победитель = участник с минимальным XOR-расстоянием от хеша дня
"""

import hashlib
import logging
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import Message

logger = logging.getLogger(__name__)


def get_day_hash() -> str:
    """
    Вернуть sha256-хеш текущего дня.

    Берём полночь текущего дня в UTC, конвертируем в unix timestamp,
    превращаем в строку и хешируем.

    Returns:
        hex-строка sha256 (64 символа)
    """
    now_utc = datetime.now(timezone.utc)
    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    unix_ts = int(midnight_utc.timestamp())
    return hashlib.sha256(str(unix_ts).encode()).hexdigest()


def get_user_hash(user_id: int) -> str:
    """
    Вернуть sha256-хеш пользователя по его Telegram user.id.

    Используем user.id (а не username), т.к. он никогда не меняется.

    Args:
        user_id: Telegram user ID (целое число)

    Returns:
        hex-строка sha256 (64 символа)
    """
    return hashlib.sha256(str(user_id).encode()).hexdigest()


def xor_distance(hash_a: str, hash_b: str) -> int:
    """
    Вычислить XOR-расстояние между двумя sha256 hex-строками.

    Берём первые 16 hex-символов (= 8 байт = uint64) каждого хеша
    и возвращаем их XOR. Меньше = ближе.

    XOR-метрика (аналог Kademlia DHT) обеспечивает равномерное
    распределение без систематического смещения в пользу
    пользователей с определёнными диапазонами ID.

    Args:
        hash_a: hex-строка первого хеша
        hash_b: hex-строка второго хеша

    Returns:
        целое число — XOR расстояние (0 = одинаковые, больше = дальше)
    """
    a = int(hash_a[:16], 16)
    b = int(hash_b[:16], 16)
    return a ^ b


def select_pidor(members: list, day_hash: str) -> object | None:
    """
    Выбрать "пидора дня" из списка участников.

    Итерируем по участникам, вычисляем XOR-расстояние между хешем дня
    и хешем user.id каждого участника, возвращаем ближайшего.

    Args:
        members: список объектов pyrogram ChatMember
        day_hash: хеш текущего дня из get_day_hash()

    Returns:
        ChatMember с минимальным XOR-расстоянием, или None если список пуст
    """
    if not members:
        return None

    def distance_for_member(member):
        user_hash = get_user_hash(member.user.id)
        return xor_distance(day_hash, user_hash)

    return min(members, key=distance_for_member)


async def handle_pidor(client: Client, message: Message):
    """
    Обработка команды /пидор или /pidor.

    Процесс:
    1. Получить список участников чата через get_chat_members
    2. Отфильтровать ботов и удалённые аккаунты
    3. Вычислить хеш дня
    4. Найти участника с минимальным XOR-расстоянием
    5. Отправить сообщение с результатом
    """
    try:
        chat_id = message.chat.id

        # Собираем всех участников чата
        members = []
        async for member in client.get_chat_members(chat_id):
            user = member.user
            # Пропускаем ботов и удалённые аккаунты
            if user.is_bot:
                continue
            if user.is_deleted:
                continue
            members.append(member)

        if not members:
            await message.reply_text("😔 Не удалось найти участников чата")
            return

        # Вычисляем хеш дня и выбираем победителя
        day_hash = get_day_hash()
        winner_member = select_pidor(members, day_hash)

        if not winner_member:
            await message.reply_text("😔 Не удалось определить пидора дня")
            return

        winner = winner_member.user

        # Формируем упоминание победителя
        if winner.username:
            mention = f"@{winner.username}"
        else:
            first = winner.first_name or ""
            last = winner.last_name or ""
            full_name = f"{first} {last}".strip() or f"id:{winner.id}"
            mention = f"[{full_name}](tg://user?id={winner.id})"

        await message.reply_text(
            f"🌈 Пидор дня — {mention}!",
            parse_mode="markdown"
        )

        logger.info(
            f"Pidor of the day in chat {chat_id}: user_id={winner.id}, "
            f"username={winner.username}, from {len(members)} members"
        )

    except Exception as e:
        logger.error(f"Error in pidor handler: {str(e)}", exc_info=True)
        await message.reply_text("❌ Произошла ошибка при определении пидора дня")


def register_handler(client: Client, group: int = 0):
    """Регистрация обработчика команды /пидор и /pidor"""

    @client.on_message(
        filters.command(["пидор", "pidor"]) & filters.group,
        group=group
    )
    async def pidor_wrapper(client: Client, message: Message):
        await handle_pidor(client, message)
        await message.continue_propagation()

    logger.info("Pidor watcher handler registered")
