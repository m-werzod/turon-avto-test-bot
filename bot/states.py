"""Finite-state machine states for multi-step admin flows."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ChannelStates(StatesGroup):
    """Connecting a channel."""

    waiting_for_identifier = State()


class ScheduleStates(StatesGroup):
    """Choosing daily posting times.

    ``waiting_for_times`` carries the expected count in its FSM data, so the
    handler can validate that the admin sent exactly as many times as they asked
    for.
    """

    waiting_for_times = State()


class ImportStates(StatesGroup):
    """Uploading a question file."""

    waiting_for_file = State()
