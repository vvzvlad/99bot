#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
from pyrogram import Client, filters
from pyrogram.types import Message
from services.chat_manager import ChatManager
from pyrogram.errors import ChatAdminRequired, PhotoInvalidDimensions, PhotoExtInvalid

logger = logging.getLogger(__name__)

async def handle_repic(client: Client, message: Message):
    """
    Handle /repic command
    
    Usage:
    - /repic (with attached photo) - set chat photo from attachment
    - /repic (reply to message with photo) - set chat photo from replied message
    """
    chat_manager = ChatManager(client)
    temp_photo_path = None
    
    try:
        # Get photo from message or reply
        photo = None
        
        # Check if it's a reply to another message
        if message.reply_to_message:
            if message.reply_to_message.photo:
                photo = message.reply_to_message.photo
            else:
                await message.reply_text("❌ Сообщение, на которое вы ответили, не содержит фото")
                return
        # Check if current message has photo
        elif message.photo:
            photo = message.photo
        else:
            await message.reply_text(
                "❌ Использование:\n"
                "• `/repic` (с прикрепленным фото) - установить фото чата\n"
                "• `/repic` (ответ на сообщение с фото) - использовать фото из сообщения"
            )
            return
        
        if not photo:
            await message.reply_text("❌ Не найдено фото для установки")
            return
        
        # Download photo to temp directory
        temp_dir = "temp"
        os.makedirs(temp_dir, exist_ok=True)
        
        # Generate unique filename
        temp_photo_path = os.path.join(temp_dir, f"chat_photo_{message.chat.id}_{message.id}.jpg")
        
        # Download the photo
        await message.reply_text("⏳ Загружаю фото...")
        await client.download_media(photo.file_id, file_name=temp_photo_path)
        
        if not os.path.exists(temp_photo_path):
            await message.reply_text("❌ Не удалось загрузить фото")
            return
        
        # Set chat photo
        await message.reply_text("⏳ Устанавливаю фото чата...")
        await chat_manager.set_chat_photo(message, temp_photo_path)
        await message.reply_text("✅ Фото чата успешно установлено")
        
    except ChatAdminRequired:
        await message.reply_text("❌ У бота нет прав администратора в этом чате")
        logger.error(f"Bot lacks admin rights in chat {message.chat.id}")
    except PhotoInvalidDimensions:
        await message.reply_text("❌ Неправильные размеры фото. Используйте квадратное фото")
        logger.error(f"Invalid photo dimensions for chat {message.chat.id}")
    except PhotoExtInvalid:
        await message.reply_text("❌ Недопустимый формат фото")
        logger.error(f"Invalid photo format for chat {message.chat.id}")
    except Exception as e:
        await message.reply_text(f"❌ Ошибка при установке фото чата: {str(e)}")
        logger.error(f"Error in repic handler: {str(e)}", exc_info=True)
    finally:
        # Cleanup temp file if it still exists (normally handled by chat_manager)
        if temp_photo_path and os.path.exists(temp_photo_path):
            try:
                os.remove(temp_photo_path)
                logger.debug(f"Cleaned up temp photo: {temp_photo_path}")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp photo {temp_photo_path}: {str(e)}")
