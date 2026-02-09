#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Repic Watcher Plugin
Обработка команды /repic для смены фото чата
"""

import logging
import os
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import ChatAdminRequired, PhotoInvalidDimensions, PhotoExtInvalid

logger = logging.getLogger(__name__)

async def handle_repic(client: Client, message: Message):
    """
    Обработка команды /repic

    Объединяет логику:
    - Извлечение фото из сообщения/ответа
    - Создание временной директории
    - Скачивание фото
    - Удаление командного сообщения
    - Проверка существования файла
    - Вызов client.set_chat_photo()
    - Логирование
    - Очистка временных файлов в finally
    - Обработка ошибок
    """
    temp_photo_path = None

    try:
        # Get photo from message or reply
        photo = None

        # Check if it's a reply to another message
        if message.reply_to_message:
            if message.reply_to_message.photo:
                photo = message.reply_to_message.photo
            else:
                await message.delete()
                return
        # Check if current message has photo
        elif message.photo:
            photo = message.photo
        else:
            await message.delete()
            return

        if not photo:
            await message.delete()
            return

        # Delete the command message
        await message.delete()

        # Download photo to temp directory
        temp_dir = "temp"
        os.makedirs(temp_dir, exist_ok=True)

        # Generate unique filename
        temp_photo_path = os.path.join(temp_dir, f"chat_photo_{message.chat.id}_{message.id}.jpg")

        # Download the photo
        await client.download_media(photo.file_id, file_name=temp_photo_path)

        if not os.path.exists(temp_photo_path):
            logger.error(f"Photo file not found: {temp_photo_path}")
            return

        chat_id = message.chat.id
        await client.set_chat_photo(chat_id, photo=temp_photo_path)
        logger.info(f"Chat {chat_id} photo updated with: {temp_photo_path}")

    except ChatAdminRequired:
        logger.error(f"Bot lacks admin rights in chat {message.chat.id}")
    except PhotoInvalidDimensions:
        logger.error(f"Invalid photo dimensions for chat {message.chat.id}")
    except PhotoExtInvalid:
        logger.error(f"Invalid photo format for chat {message.chat.id}")
    except Exception as e:
        logger.error(f"Failed to set chat photo for {message.chat.id}: {str(e)}")
        logger.error(f"Error in repic handler: {str(e)}", exc_info=True)
    finally:
        # Cleanup temp file
        if temp_photo_path and os.path.exists(temp_photo_path):
            try:
                os.remove(temp_photo_path)
                logger.debug(f"Temp photo file removed: {temp_photo_path}")
            except Exception as e:
                logger.warning(f"Failed to remove temp file {temp_photo_path}: {str(e)}")


def register_handler(client: Client, group: int = 0):
    """Регистрация обработчика команды /repic"""
    @client.on_message(filters.command("repic") & filters.group, group=group)
    async def repic_wrapper(client: Client, message: Message):
        await handle_repic(client, message)
        await message.continue_propagation()

    logger.info("Repic watcher handler registered")
