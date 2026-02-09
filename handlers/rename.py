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
                await message.delete()
                return
        # Check if there's text after command
        elif message.text and len(message.text.split(maxsplit=1)) > 1:
            new_title = message.text.split(maxsplit=1)[1]
        else:
            await message.delete()
            return
        
        if not new_title or not new_title.strip():
            await message.delete()
            return
        
        # Truncate title if it's longer than 255 characters (Telegram limit)
        if len(new_title) > 255:
            new_title = new_title[:255]
        
        # Delete the command message
        await message.delete()
        
        # Try to rename chat
        await chat_manager.rename_chat(message, new_title.strip())
        
    except ChatAdminRequired:
        logger.error(f"Bot lacks admin rights in chat {message.chat.id}")
    except ChatNotModified:
        logger.info(f"Chat {message.chat.id} title not modified (already set to same value)")
        await message.delete()
    except Exception as e:
        logger.error(f"Error in rename handler: {str(e)}", exc_info=True)
