"""Ashby (jobs.ashbyhq.com) adapter.

Ashby is React-heavy with custom select widgets. URL pattern::

    https://jobs.ashbyhq.com/<company>/<job-id>
    https://jobs.ashbyhq.com/<company>/<job-id>/application

Some companies embed Ashby via an iframe (``#ashby_embed_iframe``); the shared
loop is scoped to it automatically. The Apply CTA, the custom dropdowns, and the
server-rendered spam/duplicate banners are all handled by the shared perception
+ reasoning loop (the reasoner detects the banners via the condition detector).
Ashby's selector/widget quirks are supplied to the reasoner as hints.
"""
from __future__ import annotations

from .autonomous_base import AutonomousAdapter


class AshbyAdapter(AutonomousAdapter):
    platform_name = "ashby"
    hints_key = "ashby"
    # Ashby form is full page; when embedded it lives in this iframe.
    iframe_selector = "#ashby_embed_iframe, iframe[src*='ashbyhq']"
