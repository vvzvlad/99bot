#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import asyncio
import uvloop
import signal
import sys
from dotenv import load_dotenv

# Load environment variables FIRST before any imports that use them
load_dotenv()

from pyrogram import filters
from telegram_client import TelegramClient
from config import get_settings, setup_logging
from handlers import handle_rename, handle_repic

logger = logging.getLogger(__name__)

# Get settings
settings = get_settings()

# Initialize Telegram client
tg_client = TelegramClient()

# Shutdown flag
shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()

async def main():
    """Main bot function"""
    setup_logging(settings["log_level"])
    
    logger.info("Starting Telegram Chat Manager Bot")
    logger.info(f"Using uvloop for async operations")
    
    try:
        # Start Telegram client
        await tg_client.start()
        
        # Register command handlers using decorators
        @tg_client.client.on_message(filters.command("rename") & (filters.group | filters.supergroup))
        async def rename_wrapper(client, message):
            await handle_rename(client, message)
        
        @tg_client.client.on_message(filters.command("repic") & (filters.group | filters.supergroup))
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
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run the bot with uvloop
    try:
        asyncio.run(main(), loop_factory=uvloop.new_event_loop)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Critical error: {str(e)}", exc_info=True)
        sys.exit(1)
