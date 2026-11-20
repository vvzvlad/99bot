#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
import asyncio

from pyrogram import Client
from pyrogram.handlers import DisconnectHandler
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class TelegramClient:
    def __init__(self):
        self._ensure_session_directory()
        self.client = Client(
            name="chat_manager_bot",
            api_id=settings["tg_api_id"],
            api_hash=settings["tg_api_hash"],
            workdir=settings["session_path"],
        )
        self.disconnect_count = 0
        self.max_disconnects = 3
        self._setup_connection_handlers()

    def _ensure_session_directory(self):
        try:
            os.makedirs(settings["session_path"], exist_ok=True)
            logger.debug(f'Session directory created/verified: {settings["session_path"]}')
        except Exception as e:
            logger.error(f'Failed to create session directory {settings["session_path"]}: {str(e)}')
            raise

    def _setup_connection_handlers(self):
        """Sets up connection/disconnection handlers"""
        self.client.add_handler(DisconnectHandler(self._on_disconnect))
        logger.info("connection_handlers: connection handlers set up")

    async def _on_disconnect(self, _client, _session):
        """Handles disconnection from Telegram servers"""
        self.disconnect_count += 1
        logger.warning(f"connection_handler: connection lost (#{self.disconnect_count})")
        
        if self.disconnect_count >= self.max_disconnects:
            logger.critical(f"connection_handler: reached disconnect limit ({self.max_disconnects})")
            self._restart_app()

    async def start(self):
        try:
            if not self.client.is_connected:
                await self.client.start()
                logger.info("Telegram client connected successfully")
                # Reset disconnect counter on successful connection
                self.disconnect_count = 0
                logger.info("connection_handler: connection established")
        except Exception as e:
            logger.error(f"Failed to start Telegram client: {str(e)}")
            raise

    def _restart_app(self):
        """Exits the process so that Docker's restart policy starts a fresh one.

        The bot runs as PID 1 of its container with no init in front of it, and the kernel
        drops any signal sent to PID 1 from inside the container unless the process has a
        handler for it. A SIGTERM sent to itself was therefore silently ignored: the bot kept
        running on a session that no longer received updates. os._exit cannot be ignored.
        """
        logger.critical("connection_handler: exiting so that Docker restarts the bot")
        os._exit(1)

    async def stop(self):
        if self.client.is_connected:
            await self.client.stop()
            logger.info("Telegram client disconnected")
