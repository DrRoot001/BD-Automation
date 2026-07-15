"""Generic adapter — the shared perception-driven agent with no platform quirks.

It is now just :class:`AutonomousAdapter` with the default hooks: navigate to the
URL, then run the shared observe → reason → act loop. All decision logic lives in
the base + the autonomous agent; nothing platform-specific here.
"""
from __future__ import annotations

from typing import Optional

from .autonomous_base import AutonomousAdapter
from ..autonomous import AutonomousAgent


class GenericFormAdapter(AutonomousAdapter):
    platform_name = "generic"

    def __init__(self, agent: Optional[AutonomousAgent] = None):
        super().__init__(agent=agent)
