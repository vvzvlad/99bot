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

from telegram_client import TelegramClient
from config import get_settings, setup_logging
from handlers import rename_watcher
from handlers import repic_watcher
from handlers import title_monitor
from handlers import service_cleaner

logger = logging.getLogger(__name__)

# Get settings
settings = get_settings()


async def main():
    """Main bot function"""
    
    setup_logging(settings["log_level"])
    
    logger.info("Starting Telegram Chat Manager Bot")
    logger.info(f"Using uvloop for async operations")
    
    # Create shutdown event and client AFTER event loop is running
    shutdown_event = asyncio.Event()
    tg_client = TelegramClient()
    
    try:
        # Start Telegram client
        await tg_client.start()
        
        # Register command handlers
        rename_watcher.register_handler(tg_client.client)
        repic_watcher.register_handler(tg_client.client)
        service_cleaner.register_handler(tg_client.client)
        title_monitor.register_handler(tg_client.client)
        
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
