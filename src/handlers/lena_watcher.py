#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Lena Pregnancy Progress Bar Handler
Responds to /lena command with a pregnancy progress bar showing weeks elapsed,
percentage complete, due date, and days remaining.
"""

import logging
from datetime import date, timedelta
from pyrogram import Client, filters
from pyrogram.types import Message

logger = logging.getLogger(__name__)

# Hardcoded last menstrual period date (LMP) — start of pregnancy
LMP_DATE = date(2026, 3, 12)
TOTAL_DAYS = 280  # 40 weeks

# Progress bar length in characters (each character represents 5%)
BAR_LENGTH = 20

# Russian month names in genitive case for date formatting
MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}


def pluralize_days(n: int) -> str:
    """Return correct Russian plural form of 'day' for a given number."""
    abs_n = abs(n)
    mod10, mod100 = abs_n % 10, abs_n % 100
    if 11 <= mod100 <= 14:
        return "дней"
    if mod10 == 1:
        return "день"
    if 2 <= mod10 <= 4:
        return "дня"
    return "дней"


async def handle_lena(client: Client, message: Message):
    try:
        today = date.today()
        elapsed = (today - LMP_DATE).days          # days since LMP
        percent = min(elapsed / TOTAL_DAYS * 100, 100.0)  # clamp to [0, 100]
        week = min(elapsed // 7 + 1, 40)           # clamp to [1, 40]
        due_date = LMP_DATE + timedelta(TOTAL_DAYS)
        days_left = (due_date - today).days

        # Build progress bar: filled blocks + empty blocks
        filled = round(percent / 100 * BAR_LENGTH)
        filled = max(0, min(BAR_LENGTH, filled))   # clamp to [0, BAR_LENGTH]
        bar = "▓" * filled + "░" * (BAR_LENGTH - filled)

        # Format due date in Russian
        due_date_ru = f"{due_date.day} {MONTHS_RU[due_date.month]} {due_date.year}"

        # Build remaining/overdue string with correct Russian plural form
        if days_left >= 0:
            remaining_str = f"⏳ Осталось: {days_left} {pluralize_days(days_left)}"
        else:
            overdue = -days_left
            remaining_str = f"🎉 ПДР прошёл {overdue} {pluralize_days(overdue)} назад"

        text = (
            f"{bar} {percent:.0f}%\n"
            f"\n"
            f"📅 Неделя {week} из 40\n"
            f"{remaining_str}"
        )

        await message.reply_text(text)
    except Exception as e:
        logger.error(f"Error in lena handler: {str(e)}", exc_info=True)


def register_handler(client: Client, group: int = 0):
    """Register /lena command handler"""

    @client.on_message(filters.command(["lena"]) & filters.group, group=group)
    async def lena_wrapper(client: Client, message: Message):
        await handle_lena(client, message)
        await message.continue_propagation()

    logger.info("Lena watcher handler registered")
