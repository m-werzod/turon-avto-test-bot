"""Inline keyboards and the callback vocabulary.

Callback payloads are built through :class:`CB` rather than by writing string
literals at each call site, so a renamed action cannot leave a dead button that
silently does nothing.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.locales.i18n import LANGUAGE_LABELS, SUPPORTED_LANGUAGES, t


class CB:
    """Callback data constants.

    Values are kept short because Telegram caps callback data at 64 bytes.
    """

    # Navigation
    MENU = "menu"
    CLOSE = "close"
    NOOP = "noop"

    # Language
    SET_UI_LANG = "lang:ui"  # + ":<code>"
    SET_CONTENT_LANG = "lang:content"  # + ":<code>"

    # Channels
    CHANNELS = "ch"
    CHANNEL_ADD = "ch:add"
    CHANNEL_REMOVE = "ch:rm"  # + ":<id>"

    # Scheduler
    SCHED = "sc"
    SCHED_COUNT = "sc:count"  # + ":<1|2|3>"
    SCHED_EDIT = "sc:edit"
    SCHED_PAUSE = "sc:pause"
    SCHED_RESUME = "sc:resume"
    SCHED_WEEKENDS = "sc:wk"

    # Content
    STATS = "st"
    STATS_REFRESH = "st:rf"
    SEND_NOW = "sn"
    SEND_NOW_CONFIRM = "sn:ok"

    # Import
    IMPORT = "im"
    IMPORT_FILE = "im:file"  # + ":<index>"
    IMPORT_UPLOAD = "im:up"

    # Backup / logs / settings
    BACKUP = "bk"
    BACKUP_CREATE = "bk:new"
    LOGS = "lg"
    LOGS_ALL = "lg:all"
    LOGS_ERRORS = "lg:err"
    LOGS_DOWNLOAD = "lg:dl"
    SETTINGS = "se"
    SETTINGS_UI_LANG = "se:ui"
    SETTINGS_CONTENT_LANG = "se:cl"


def _back_button(language: str, target: str = CB.MENU) -> InlineKeyboardButton:
    """A single "back" button pointing at ``target``."""
    return InlineKeyboardButton(text=t("common.back", language), callback_data=target)


def language_keyboard(prefix: str = CB.SET_UI_LANG) -> InlineKeyboardMarkup:
    """Language picker.

    Labels are intentionally not translated — someone choosing a language needs
    to recognise their own, whatever the interface currently speaks.
    """
    builder = InlineKeyboardBuilder()
    for code in SUPPORTED_LANGUAGES:
        builder.button(text=LANGUAGE_LABELS[code], callback_data=f"{prefix}:{code}")
    builder.adjust(1)
    return builder.as_markup()


def main_menu_keyboard(language: str, *, paused: bool) -> InlineKeyboardMarkup:
    """The admin panel.

    Pause and resume share a slot: only the action that currently applies is
    shown, so the admin never has to work out which of two buttons is live.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.connect_channel", language), callback_data=CB.CHANNELS)
    builder.button(text=t("menu.scheduler", language), callback_data=CB.SCHED)
    builder.button(text=t("menu.statistics", language), callback_data=CB.STATS)
    builder.button(text=t("menu.update_tests", language), callback_data=CB.IMPORT)
    builder.button(text=t("menu.send_now", language), callback_data=CB.SEND_NOW)

    if paused:
        builder.button(text=t("menu.resume", language), callback_data=CB.SCHED_RESUME)
    else:
        builder.button(text=t("menu.pause", language), callback_data=CB.SCHED_PAUSE)

    builder.button(text=t("menu.backup", language), callback_data=CB.BACKUP)
    builder.button(text=t("menu.logs", language), callback_data=CB.LOGS)
    builder.button(text=t("menu.settings", language), callback_data=CB.SETTINGS)
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def back_keyboard(language: str, target: str = CB.MENU) -> InlineKeyboardMarkup:
    """A keyboard with only a back button."""
    return InlineKeyboardMarkup(inline_keyboard=[[_back_button(language, target)]])


def channels_keyboard(language: str, channels: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Channel list with per-channel remove buttons.

    Args:
        language: Interface language.
        channels: ``(id, display name)`` pairs.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("channel.add_new", language), callback_data=CB.CHANNEL_ADD)
    for channel_id, name in channels:
        builder.button(
            text=f"{t('channel.remove', language)} {name}",
            callback_data=f"{CB.CHANNEL_REMOVE}:{channel_id}",
        )
    builder.button(text=t("common.back", language), callback_data=CB.MENU)
    builder.adjust(1)
    return builder.as_markup()


def scheduler_keyboard(language: str, *, paused: bool) -> InlineKeyboardMarkup:
    """Scheduler settings."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("scheduler.edit_times", language), callback_data=CB.SCHED_EDIT)
    builder.button(text=t("scheduler.toggle_weekends", language), callback_data=CB.SCHED_WEEKENDS)
    if paused:
        builder.button(text=t("menu.resume", language), callback_data=CB.SCHED_RESUME)
    else:
        builder.button(text=t("menu.pause", language), callback_data=CB.SCHED_PAUSE)
    builder.button(text=t("common.back", language), callback_data=CB.MENU)
    builder.adjust(1)
    return builder.as_markup()


def posts_per_day_keyboard(language: str) -> InlineKeyboardMarkup:
    """One, two or three posts per day."""
    builder = InlineKeyboardBuilder()
    for count in (1, 2, 3):
        builder.button(
            text=t("scheduler.count_option", language, count=count),
            callback_data=f"{CB.SCHED_COUNT}:{count}",
        )
    builder.button(text=t("common.cancel", language), callback_data=CB.SCHED)
    builder.adjust(3, 1)
    return builder.as_markup()


def confirm_keyboard(
    language: str, confirm_callback: str, cancel_callback: str = CB.MENU
) -> InlineKeyboardMarkup:
    """A yes/no confirmation."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("common.confirm", language), callback_data=confirm_callback)
    builder.button(text=t("common.cancel", language), callback_data=cancel_callback)
    builder.adjust(2)
    return builder.as_markup()


def stats_keyboard(language: str) -> InlineKeyboardMarkup:
    """Statistics panel."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("stats.refresh", language), callback_data=CB.STATS_REFRESH)
    builder.button(text=t("common.back", language), callback_data=CB.MENU)
    builder.adjust(1)
    return builder.as_markup()


def import_keyboard(language: str, files: list[str]) -> InlineKeyboardMarkup:
    """Import sources: discovered files plus an upload option.

    Buttons carry a list index rather than a filename — a path would blow past
    Telegram's 64-byte callback limit and break on non-ASCII names.
    """
    builder = InlineKeyboardBuilder()
    for index, name in enumerate(files[:10]):
        builder.button(text=f"📄 {name}"[:60], callback_data=f"{CB.IMPORT_FILE}:{index}")
    builder.button(text=t("import.source_upload", language), callback_data=CB.IMPORT_UPLOAD)
    builder.button(text=t("common.back", language), callback_data=CB.MENU)
    builder.adjust(1)
    return builder.as_markup()


def backup_keyboard(language: str) -> InlineKeyboardMarkup:
    """Backup panel."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("backup.create", language), callback_data=CB.BACKUP_CREATE)
    builder.button(text=t("common.back", language), callback_data=CB.MENU)
    builder.adjust(1)
    return builder.as_markup()


def logs_keyboard(language: str) -> InlineKeyboardMarkup:
    """Log viewer."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("logs.filter_all", language), callback_data=CB.LOGS_ALL)
    builder.button(text=t("logs.filter_errors", language), callback_data=CB.LOGS_ERRORS)
    builder.button(text=t("logs.download", language), callback_data=CB.LOGS_DOWNLOAD)
    builder.button(text=t("common.back", language), callback_data=CB.MENU)
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def settings_keyboard(language: str) -> InlineKeyboardMarkup:
    """Settings panel."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("settings.change_ui_language", language), callback_data=CB.SETTINGS_UI_LANG
    )
    builder.button(
        text=t("settings.change_content_language", language),
        callback_data=CB.SETTINGS_CONTENT_LANG,
    )
    builder.button(text=t("scheduler.toggle_weekends", language), callback_data=CB.SCHED_WEEKENDS)
    builder.button(text=t("common.back", language), callback_data=CB.MENU)
    builder.adjust(1)
    return builder.as_markup()
