#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import csv
import os
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import MessageServiceType

logger = logging.getLogger(__name__)

# Global instance
_instance = None

def get_title_monitor():
    """Get the global TitleMonitor instance"""
    return _instance

def set_title_monitor(monitor):
    """Set the global TitleMonitor instance"""
    global _instance
    _instance = monitor


class TitleMonitor:
    """Monitor for tracking chat title changes and saving them to CSV"""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self.csv_file = os.path.join(data_dir, "chat_title_changes.csv")
        self._ensure_data_directory()
        self._initialize_csv()
    
    def _ensure_data_directory(self):
        """Ensure the data directory exists"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            logger.debug(f'Data directory created/verified: {self.data_dir}')
        except Exception as e:
            logger.error(f'Failed to create data directory {self.data_dir}: {str(e)}')
            raise
    
    def _initialize_csv(self):
        """Initialize CSV file with headers if it doesn't exist"""
        try:
            if not os.path.exists(self.csv_file):
                with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(['timestamp', 'new_title', 'changed_by_username'])
                logger.info(f'Created new CSV file: {self.csv_file}')
        except Exception as e:
            logger.error(f'Failed to initialize CSV file: {str(e)}')
            raise
    
    async def handle_title_change(self, client: Client, message: Message):
        """
        Handle chat title change events
        
        Args:
            client: Pyrogram client instance
            message: Message object containing service message about title change
        """
        try:
            # Check if this is a service message about new chat title
            if not message.service:
                return
            
            if message.service != MessageServiceType.NEW_CHAT_TITLE:
                return
            
            # Extract information
            new_title = message.new_chat_title
            
            # Get username - handle both user and bot changes
            changed_by_username = ""
            if message.from_user:
                changed_by_username = message.from_user.username or ""
                if not changed_by_username and message.from_user.is_bot:
                    # For bots without username, use first_name or "bot"
                    changed_by_username = message.from_user.first_name or "bot"
            
            # Get current timestamp
            timestamp = datetime.utcnow().isoformat()
            
            # Write to CSV
            self._write_to_csv(timestamp, new_title, changed_by_username)
            
            logger.info(
                f"Title change monitored: new_title='{new_title}', "
                f"changed_by=@{changed_by_username if changed_by_username else 'unknown'}"
            )
            
        except Exception as e:
            logger.error(f"Error handling title change: {str(e)}", exc_info=True)
    
    async def log_title_change(self, new_title: str, changed_by_username: str):
        """
        Directly log a title change (used by chat_manager when service message is deleted)
        
        Args:
            new_title: New chat title
            changed_by_username: Username of who changed the title
        """
        try:
            timestamp = datetime.utcnow().isoformat()
            self._write_to_csv(timestamp, new_title, changed_by_username)
            logger.info(
                f"Title change logged directly: new_title='{new_title}', "
                f"changed_by=@{changed_by_username if changed_by_username else 'unknown'}"
            )
        except Exception as e:
            logger.error(f"Error logging title change: {str(e)}", exc_info=True)
    
    def _write_to_csv(self, timestamp: str, new_title: str, changed_by_username: str):
        """Write a title change record to the CSV file"""
        try:
            with open(self.csv_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([timestamp, new_title, changed_by_username])
        except Exception as e:
            logger.error(f"Failed to write to CSV: {str(e)}", exc_info=True)


def register_handler(client: Client):
    """Регистрация обработчика мониторинга изменений названия чата"""
    from config import load_config

    if get_title_monitor() is None:
        settings = load_config()
        monitor = TitleMonitor(data_dir=settings.get("session_path", "data"))
        set_title_monitor(monitor)
        logger.info("Title monitor initialized")

    @client.on_message(filters.service & filters.group)
    async def title_monitor_wrapper(client: Client, message: Message):
        if message.service == MessageServiceType.NEW_CHAT_TITLE:
            title_monitor = get_title_monitor()
            if title_monitor:
                await title_monitor.handle_title_change(client, message)

    logger.info("Title monitor handler registered")
