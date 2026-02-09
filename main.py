#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import asyncio
import uvloop
import signal
import sys
from dotenv import load_dotenv

load_dotenv()

# Set uvloop as event loop policy
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from pyrogram import filters
from telegram_client import TelegramClient
from config import get_settings, setup_logging
from handlers import handle_rename, handle_repic

logger = logging.getLogger(__name__)

# Get settings
settings = get_settings()

# Shutdown flag - будет создан в main()
shutdown_event = None

# Global client reference - будет создан в main()
tg_client = None

async def main():
    """Main bot function"""
    global tg_client, shutdown_event
    
    setup_logging(settings["log_level"])
    
    logger.info("Starting Telegram Chat Manager Bot")
    logger.info(f"Using uvloop for async operations")
    
    # Create shutdown event and client AFTER event loop is running
    shutdown_event = asyncio.Event()
    tg_client = TelegramClient()
    
    try:
        # Start Telegram client
        await tg_client.start()
        
        # Register command handlers using decorators
        @tg_client.client.on_message(filters.command("rename") & filters.group)
        async def rename_wrapper(client, message):
            await handle_rename(client, message)
        
        @tg_client.client.on_message(filters.command("repic") & filters.group)
        async def repic_wrapper(client, message):
            await handle_repic(client, message)
        
        logger.info("Bot started successfully. Listening for commands...")
        logger.info("Available commands:")
        logger.info("  /rename - Rename chat")
        logger.info("  /repic - Set chat photo")
        
        # Wait for shutdown signal
        await shutdown_event.wait()
        
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {str(e)}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Shutting down...")
        await tg_client.stop()
        logger.info("Bot stopped")

if __name__ == "__main__":
    # Run the bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Critical error: {str(e)}", exc_info=True)
        sys.exit(1)
