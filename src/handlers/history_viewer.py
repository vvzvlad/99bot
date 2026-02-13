#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
History Viewer Plugin
Обработка команды /history для просмотра истории изменений названия чата
"""

import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from handlers.title_monitor import get_title_monitor

logger = logging.getLogger(__name__)

# Константы
DEFAULT_HISTORY_LIMIT = 50
MAX_MESSAGE_LENGTH = 4096


def format_history_message(history: list) -> str:
    """
    Форматировать список изменений в текстовое сообщение
    
    Args:
        history: Список записей истории из TitleMonitor.get_history()
    
    Returns:
        Отформатированная строка для отправки пользователю
        
    Raises:
        ValueError: если сообщение превышает MAX_MESSAGE_LENGTH
    """
    lines = []
    
    for entry in history:
        new_title = entry['new_title']
        username = entry['changed_by_username']
        
        # Формат: "Новое название (изменил @username)"
        if username:
            user_display = f"@{username}" if not username.startswith('@') else username
            line = f"{new_title} (изменил {user_display})"
        else:
            line = f"{new_title} (изменил неизвестный)"
        
        lines.append(line)
    
    message = "\n".join(lines)
    
    # Проверка длины
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError(
            f"Message length {len(message)} exceeds Telegram limit {MAX_MESSAGE_LENGTH}"
        )
    
    return message


async def handle_history(client: Client, message: Message):
    """
    Обработка команды /history или /история
    
    Процесс:
    1. Получить TitleMonitor instance
    2. Получить историю через get_history()
    3. Форматировать данные
    4. Отправить ответ пользователю
    5. Обработать edge cases
    """
    try:
        # Проверка: команда доступна только для @vvzvlad
        if not message.from_user or message.from_user.username != "vvzvlad":
            logger.info(
                f"History command ignored: user {message.from_user.username if message.from_user else 'unknown'} "
                f"is not authorized"
            )
            return
        
        # Получить TitleMonitor instance
        title_monitor = get_title_monitor()
        
        if not title_monitor:
            logger.error("TitleMonitor instance not found")
            await message.reply_text(
                "❌ Ошибка: система мониторинга не инициализирована"
            )
            return
        
        # Получить историю
        try:
            history = title_monitor.get_history(limit=DEFAULT_HISTORY_LIMIT)
        except Exception as e:
            logger.error(f"Failed to get history: {str(e)}", exc_info=True)
            await message.reply_text(
                "❌ Ошибка при чтении истории изменений"
            )
            return
        
        # Проверка: пустая история
        if not history:
            await message.reply_text(
                "📋 История изменений названия чата пуста"
            )
            return
        
        # Форматировать и отправить
        try:
            formatted_message = format_history_message(history)
            await message.reply_text(formatted_message)
            
            logger.info(
                f"History displayed in chat {message.chat.id}, "
                f"showing {len(history)} entries"
            )
            
        except ValueError as e:
            # Сообщение слишком длинное
            logger.warning(f"History message too long: {str(e)}")
            await message.reply_text(
                "⚠️ История слишком большая для отображения.\n"
                "Показаны последние несколько записей..."
            )
            # Попытка с меньшим количеством записей
            reduced_history = history[:5]
            formatted_message = format_history_message(reduced_history)
            await message.reply_text(formatted_message)
            
    except Exception as e:
        logger.error(f"Error in history handler: {str(e)}", exc_info=True)
        await message.reply_text(
            "❌ Произошла ошибка при обработке команды"
        )


def register_handler(client: Client, group: int = 0):
    """Регистрация обработчика команды /history"""
    
    @client.on_message(
        filters.command(["history", "история"]) & filters.group, 
        group=group
    )
    async def history_wrapper(client: Client, message: Message):
        await handle_history(client, message)
        await message.continue_propagation()
    
    logger.info("History viewer handler registered")
