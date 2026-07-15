"""Shared perception layer for Module 4 browser automation.

One reusable :class:`BrowserStateCollector` turns a live Playwright page/frame
into a single structured :class:`BrowserState` — the object every adapter and
the agent loop will consume instead of re-scraping the DOM their own way.

This package is intentionally logic-free: it observes and reports. Deciding what
to do with the state (fill, click, submit, abort) stays with the callers.
"""
from .collector import (
    BrowserStateCollector,
    ExtractCaps,
    ScreenshotConfig,
    StabilityConfig,
    save_screenshot,
)
from .models import (
    BrowserState,
    ButtonElement,
    FormElement,
    InputField,
    ModalElement,
    NavigationState,
    PageMessage,
)

__all__ = [
    # collector
    "BrowserStateCollector",
    "ScreenshotConfig",
    "StabilityConfig",
    "ExtractCaps",
    "save_screenshot",
    # models
    "BrowserState",
    "NavigationState",
    "FormElement",
    "InputField",
    "ButtonElement",
    "PageMessage",
    "ModalElement",
]
