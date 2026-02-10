#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Repic Watcher Plugin
Обработка команды /repic для смены фото чата
"""

import logging
import os
import asyncio
import random
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import MessageServiceType, ChatMemberStatus
from pyrogram.errors import ChatAdminRequired, PhotoInvalidDimensions, PhotoExtInvalid, FloodWait
from pyrogram import raw

logger = logging.getLogger(__name__)


async def _set_chat_photo_with_history(client: Client, chat_id: int, photo_path: str) -> None:
    """
    Set chat photo while preserving avatar history.
    
    Uses Telegram's raw API to upload the photo properly so it's added to history
    instead of replacing the current one like set_chat_photo() does.
    
    Args:
        client: Pyrogram client instance
        chat_id: Chat ID where to set the photo
        photo_path: Path to the photo file to upload
    """
    logger.info(f"[AVATAR HISTORY] Setting chat photo with history preservation for chat {chat_id}")
    
    try:
        # Get the peer to determine chat type
        peer = await client.resolve_peer(chat_id)
        
        # Upload the file
        uploaded_file = await client.save_file(photo_path)
        
        # For supergroups/channels (starts with -100)
        if isinstance(peer, (raw.types.InputPeerChannel, raw.types.InputPeerChat)):
            if isinstance(peer, raw.types.InputPeerChannel):
                # For channels/supergroups, use messages.EditChatPhoto
                result = await client.invoke(
                    raw.functions.channels.EditPhoto(
                        channel=peer,
                        photo=raw.types.InputChatUploadedPhoto(
                            file=uploaded_file
                        )
                    )
                )
            else:
                # For basic groups, use messages.EditChatPhoto
                result = await client.invoke(
                    raw.functions.messages.EditChatPhoto(
                        chat_id=peer.chat_id,
                        photo=raw.types.InputChatUploadedPhoto(
                            file=uploaded_file
                        )
                    )
                )
            logger.info(f"[AVATAR HISTORY] Successfully set chat photo with history preservation")
        else:
            raise Exception(f"Unsupported peer type: {type(peer)}")
        
    except Exception as e:
        logger.error(f"[AVATAR HISTORY] Failed to use raw API, falling back to set_chat_photo: {str(e)}", exc_info=True)
        # Fallback to the original method (will replace instead of preserving history)
        await client.set_chat_photo(chat_id, photo=photo_path)
        logger.warning(f"[AVATAR HISTORY] Used fallback method - history may not be preserved")


def _should_process_repic(message: Message) -> bool:
    text = message.text or message.caption or ""
    if not text:
        return True
    first_token = text.split()[0].lower()
    if first_token.startswith("/"):
        command = first_token[1:]
    else:
        command = first_token
    command = command.split("@")[0]
    if command in {"репик"}:
        return random.random() <= 0.1
    return True


async def handle_repic(client: Client, message: Message):
    """
    Обработка команды /repic

    Объединяет логику:
    - Извлечение фото из сообщения/ответа
    - Создание временной директории
    - Скачивание фото
    - Удаление командного сообщения
    - Проверка существования файла
    - Вызов _set_chat_photo_with_history() для сохранения истории аватарок
    - Удаление служебного сообщения о смене фото
    - Логирование
    - Очистка временных файлов в finally
    - Обработка ошибок
    """
    temp_photo_path = None
    
    # DEBUG: Log incoming message details
    logger.info(f"[REPIC DEBUG] Command received in chat {message.chat.id}")
    logger.info(f"[REPIC DEBUG] Message has photo: {message.photo is not None}")
    logger.info(f"[REPIC DEBUG] Message has document: {message.document is not None}")
    logger.info(f"[REPIC DEBUG] Message is reply: {message.reply_to_message is not None}")
    if message.reply_to_message:
        logger.info(f"[REPIC DEBUG] Reply has photo: {message.reply_to_message.photo is not None}")
        logger.info(f"[REPIC DEBUG] Reply has document: {message.reply_to_message.document is not None}")
        if message.reply_to_message.document:
            logger.info(f"[REPIC DEBUG] Reply document mime_type: {message.reply_to_message.document.mime_type}")

    try:
        # Get photo from message or reply
        photo = None
        media_to_download = None

        # Check if it's a reply to another message
        if message.reply_to_message:
            if message.reply_to_message.photo:
                photo = message.reply_to_message.photo
                media_to_download = message.reply_to_message.photo
                logger.info(f"[REPIC DEBUG] Using photo from reply message")
            elif message.reply_to_message.document and message.reply_to_message.document.mime_type and message.reply_to_message.document.mime_type.startswith('image/'):
                # Handle image sent as document/file
                media_to_download = message.reply_to_message.document
                logger.info(f"[REPIC DEBUG] Using image document from reply message")
            else:
                logger.warning(f"[REPIC DEBUG] Reply message has no photo or image document")
                await message.delete()
                return
        # Check if current message has photo
        elif message.photo:
            photo = message.photo
            media_to_download = message.photo
            logger.info(f"[REPIC DEBUG] Using photo from current message")
        elif message.document and message.document.mime_type and message.document.mime_type.startswith('image/'):
            # Handle image sent as document/file
            media_to_download = message.document
            logger.info(f"[REPIC DEBUG] Using image document from current message")
        else:
            logger.warning(f"[REPIC DEBUG] No photo or image document found in message")
            await message.delete()
            return

        if not media_to_download:
            logger.warning(f"[REPIC DEBUG] No media to download")
            await message.delete()
            return

        # Delete the command message
        await message.delete()

        # Download photo to temp directory
        # Use absolute path to ensure files are saved correctly regardless of module location
        temp_dir = os.path.join(os.getcwd(), "temp")
        
        # DEBUG: Log current working directory and temp path resolution
        current_dir = os.getcwd()
        logger.info(f"[REPIC DEBUG] Current working directory: {current_dir}")
        logger.info(f"[REPIC DEBUG] Temp directory path (relative): temp")
        logger.info(f"[REPIC DEBUG] Temp directory path (absolute): {os.path.abspath(temp_dir)}")
        
        os.makedirs(temp_dir, exist_ok=True)
        
        # DEBUG: Verify temp directory was created
        logger.info(f"[REPIC DEBUG] Temp directory exists: {os.path.exists(temp_dir)}")
        logger.info(f"[REPIC DEBUG] Temp directory is dir: {os.path.isdir(temp_dir)}")

        # Generate unique filename
        temp_photo_path = os.path.join(temp_dir, f"chat_photo_{message.chat.id}_{message.id}.jpg")
        
        logger.info(f"[REPIC DEBUG] Downloading media to: {temp_photo_path}")
        logger.info(f"[REPIC DEBUG] Download path (absolute): {os.path.abspath(temp_photo_path)}")

        # Download the photo
        download_result = await client.download_media(media_to_download.file_id, file_name=temp_photo_path)
        
        # DEBUG: Log download result
        logger.info(f"[REPIC DEBUG] Media downloaded successfully")
        logger.info(f"[REPIC DEBUG] Download result path: {download_result}")
        logger.info(f"[REPIC DEBUG] Expected path: {temp_photo_path}")
        logger.info(f"[REPIC DEBUG] Expected path exists: {os.path.exists(temp_photo_path)}")
        if download_result:
            logger.info(f"[REPIC DEBUG] Download result exists: {os.path.exists(download_result)}")
            logger.info(f"[REPIC DEBUG] Download result absolute: {os.path.abspath(download_result)}")
        
        # DEBUG: List files in temp directory
        try:
            temp_files = os.listdir(temp_dir)
            logger.info(f"[REPIC DEBUG] Files in temp directory: {temp_files}")
        except Exception as e:
            logger.error(f"[REPIC DEBUG] Failed to list temp directory: {e}")

        if not os.path.exists(temp_photo_path):
            logger.error(f"Photo file not found: {temp_photo_path}")
            return

        chat_id = message.chat.id
        logger.info(f"[REPIC DEBUG] Setting chat photo for chat {chat_id}")
        
        # Set chat photo while preserving history (like Telegram client does)
        await _set_chat_photo_with_history(client, chat_id, temp_photo_path)
        logger.info(f"Chat {chat_id} photo updated with: {temp_photo_path}")
        # The service message is created after set_chat_photo, we need to fetch
        # recent messages and delete the service message
        try:
            # === DIAGNOSIS LOGGING START ===
            logger.warning(f"[AVATAR HISTORY DIAGNOSIS] Attempting to delete service message")
            logger.warning(f"[AVATAR HISTORY DIAGNOSIS] Service message deletion might prevent history preservation")
            # === DIAGNOSIS LOGGING END ===
            # Get the most recent message (should be the service message)
            async for msg in client.get_chat_history(chat_id, limit=1):
                if msg.service and msg.service == MessageServiceType.NEW_CHAT_PHOTO:
                    # === DIAGNOSIS LOGGING START ===
                    logger.warning(f"[AVATAR HISTORY DIAGNOSIS] Found service message (id: {msg.id})")
                    logger.warning(f"[AVATAR HISTORY DIAGNOSIS] About to delete service message - this might affect history")
                    # === DIAGNOSIS LOGGING END ===
                    await msg.delete()
                    logger.info(
                        f"Successfully deleted service message (id: {msg.id}) "
                        f"generated by bot's chat photo change action"
                    )
                break
        except Exception as e:
            logger.error(
                f"Failed to delete service message after bot photo change: {str(e)}", 
                exc_info=True
            )

    except FloodWait as e:
        logger.warning(f"FloodWait error: need to wait {e.value} seconds before retrying")
        await asyncio.sleep(e.value)
        logger.info(f"FloodWait timer expired, retrying set_chat_photo for chat {message.chat.id}")
        try:
            await _set_chat_photo_with_history(client, message.chat.id, temp_photo_path)
            logger.info(f"Chat {message.chat.id} photo updated with: {temp_photo_path} (after FloodWait retry)")
            
            # Delete service message after retry
            try:
                async for msg in client.get_chat_history(message.chat.id, limit=1):
                    if msg.service and msg.service == MessageServiceType.NEW_CHAT_PHOTO:
                        await msg.delete()
                        logger.info(
                            f"Successfully deleted service message (id: {msg.id}) "
                            f"after FloodWait retry"
                        )
                    break
            except Exception as e:
                logger.error(
                    f"Failed to delete service message after FloodWait retry: {str(e)}", 
                    exc_info=True
                )
        except Exception as e:
            logger.error(f"Failed to set chat photo after FloodWait retry: {str(e)}", exc_info=True)
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
        if not _should_process_repic(message):
            await message.delete()
            await message.continue_propagation()
            return
        await handle_repic(client, message)
        await message.continue_propagation()

    logger.info("Repic watcher handler registered")
