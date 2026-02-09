#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from services.chat_manager import ChatManager
from pyrogram.errors import ChatAdminRequired, ChatNotModified

logger = logging.getLogger(__name__)

async def handle_rename(client: Client, message: Message):
    """
    Handle /rename command
    
    Usage:
    - /rename <text> - rename chat with provided text
    - /rename (reply to message) - rename chat with text from replied message
    """
    chat_manager = ChatManager(client)
    
    try:
        # Get new title from message or reply
        new_title = None
        
        # Check if it's a reply to another message
        if message.reply_to_message:
            if message.reply_to_message.text:
                new_title = message.reply_to_message.text
            else:
                await message.reply_text("❌ Сообщение, на которое вы ответили, не содержит текста")
                return
        # Check if there's text after command
        elif message.text and len(message.text.split(maxsplit=1)) > 1:
            new_title = message.text.split(maxsplit=1)[1]
        else:
            await message.reply_text(
                "❌ Использование:\n"
                "• `/rename <новое название>` - переименовать чат\n"
                "• `/rename` (ответ на сообщение) - использовать текст из сообщения"
            )
            return
        
        if not new_title or not new_title.strip():
            await message.reply_text("❌ Название чата не может быть пустым")
            return
        
        # Validate title length (Telegram limit is 255 characters)
        if len(new_title) > 255:
            await message.reply_text("❌ Название чата слишком длинное (максимум 255 символов)")
            return
        
        # Try to rename chat
        await chat_manager.rename_chat(message, new_title.strip())
        await message.reply_text(f"✅ Чат переименован в: {new_title.strip()}")
        
    except ChatAdminRequired:
        await message.reply_text("❌ У бота нет прав администратора в этом чате")
        logger.error(f"Bot lacks admin rights in chat {message.chat.id}")
    except ChatNotModified:
        await message.reply_text("❌ Название чата уже установлено в это значение")
        logger.info(f"Chat {message.chat.id} title not modified (already set to same value)")
    except Exception as e:
        await message.reply_text(f"❌ Ошибка при переименовании чата: {str(e)}")
        logger.error(f"Error in rename handler: {str(e)}", exc_info=True)
