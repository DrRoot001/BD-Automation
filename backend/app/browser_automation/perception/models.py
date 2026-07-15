"""Structured browser-state model — the single perception object every adapter
and the agent loop will consume.

This is a *pure data* layer: it describes what was on the page at one moment in
time. It carries NO business logic and makes NO decisions — that belongs to the
callers (adapters, the agent loop, answer-resolution). The collector
(:mod:`.collector`) is what populates these structures from a live Playwright
page/frame.

Dataclasses (not Pydantic) are used deliberately:
  * :class:`BrowserState` carries raw screenshot *bytes*, which serialize
    awkwardly through Pydantic and are never meant to be JSON round-tripped;
  * perception is collected on a hot path (potentially every agent turn), so we
    avoid per-field validation overhead;
  * the primary consumer — the agent loop — already speaks dataclasses
    (``AgentAction`` / ``LoopResult``).

``to_dict()`` gives a JSON-safe view (screenshot elided) for logging/telemetry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Element-level structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InputField:
    """A single fillable control (input / select / textarea / combobox)."""
    selector: str
    field_type: str                     # text|email|tel|number|select|checkbox|radio|file|textarea|date|url|combobox
    label: str
    name: Optional[str] = None
    element_id: Optional[str] = None
    value: str = ""
    placeholder: Optional[str] = None
    required: bool = False
    disabled: bool = False
    readonly: bool = False
    checked: Optional[bool] = None      # checkbox / radio only
    options: Optional[List[str]] = None  # select / datalist / combobox choices
    visible: bool = True

    @property
    def is_filled(self) -> bool:
        """Best-effort "has a value" check. NOT authoritative for file inputs
        (whose DOM value is unreliable after upload) — callers should treat
        ``field_type == 'file'`` specially."""
        if self.field_type in ("checkbox", "radio"):
            return bool(self.checked)
        if self.field_type == "file":
            return bool(self.value)
        return bool((self.value or "").strip())


@dataclass
class ButtonElement:
    """A clickable button / [role=button] / submit input."""
    selector: str
    text: str
    button_type: Optional[str] = None   # submit|button|reset|None
    disabled: bool = False
    visible: bool = True
    is_submit: bool = False             # type=submit OR submit-like text


@dataclass
class FormElement:
    """A <form> and a summary of the controls it contains."""
    selector: str
    action: Optional[str] = None
    method: Optional[str] = None
    field_count: int = 0
    field_selectors: List[str] = field(default_factory=list)
    visible: bool = True


@dataclass
class PageMessage:
    """A surfaced message: validation error, generic error/alert, success
    confirmation, or a transient toast/snackbar.

    ``kind`` is the classifier the collector assigns:
        "validation" — an inline field error (has ``associated_field`` when known)
        "error"      — a non-field error / alert-danger banner
        "success"    — a confirmation ("application received", etc.)
        "toast"      — a transient toast / snackbar / [role=status] notice
        "alert"      — an [role=alert] / .alert with no stronger signal
    """
    kind: str
    text: str
    selector: Optional[str] = None
    associated_field: Optional[str] = None


@dataclass
class ModalElement:
    """A dialog / modal / overlay currently on top of the page."""
    selector: str
    text_preview: str = ""
    role: Optional[str] = None          # dialog|alertdialog|None
    has_close_button: bool = False
    close_selector: Optional[str] = None
    looks_like_application: bool = False  # heuristic: contains form-ish text


@dataclass
class NavigationState:
    """Where the browser is and whether it has settled."""
    url: str = ""
    title: str = ""
    ready_state: str = ""               # loading|interactive|complete
    is_loading: bool = False            # ready_state != complete
    frame_count: int = 0
    referrer: str = ""
    visibility_state: str = ""          # visible|hidden|prerender


# ─────────────────────────────────────────────────────────────────────────────
# Top-level perception object
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BrowserState:
    """One structured snapshot of the page at :attr:`captured_at`.

    Populated by :class:`.collector.BrowserStateCollector`. Every field is
    optional/defaulted so a partial capture (e.g. a detached frame) still yields
    a usable object rather than raising.
    """
    # ── meta ──
    captured_at: float = field(default_factory=time.time)
    url: str = ""
    title: str = ""
    # True when this snapshot was taken from an iframe rather than the top page.
    from_frame: bool = False
    frame_url: Optional[str] = None
    # Populated when collection partially failed (e.g. frame detached mid-capture).
    error: Optional[str] = None

    # ── raw surfaces ──
    html: str = ""
    visible_text: str = ""
    screenshot: Optional[bytes] = None  # PNG/JPEG bytes; elided from to_dict()

    # ── structured surfaces ──
    navigation: NavigationState = field(default_factory=NavigationState)
    forms: List[FormElement] = field(default_factory=list)
    inputs: List[InputField] = field(default_factory=list)
    buttons: List[ButtonElement] = field(default_factory=list)
    messages: List[PageMessage] = field(default_factory=list)
    modals: List[ModalElement] = field(default_factory=list)

    # ── captcha (from a VISIBLE widget, not a string in markup) ──
    captcha_present: bool = False
    captcha_kind: str = ""              # turnstile | hcaptcha | recaptcha

    # ── stability ──
    is_stable: bool = False
    stability_reason: str = ""          # why we concluded (un)stable

    # ── convenience views over `messages` ──
    @property
    def validation_errors(self) -> List[PageMessage]:
        return [m for m in self.messages if m.kind == "validation"]

    @property
    def errors(self) -> List[PageMessage]:
        return [m for m in self.messages if m.kind in ("validation", "error")]

    @property
    def success_messages(self) -> List[PageMessage]:
        return [m for m in self.messages if m.kind == "success"]

    @property
    def toasts(self) -> List[PageMessage]:
        return [m for m in self.messages if m.kind == "toast"]

    @property
    def alerts(self) -> List[PageMessage]:
        return [m for m in self.messages if m.kind in ("alert", "error", "toast")]

    # ── convenience views over elements ──
    @property
    def submit_buttons(self) -> List[ButtonElement]:
        return [b for b in self.buttons if b.is_submit and b.visible and not b.disabled]

    @property
    def visible_inputs(self) -> List[InputField]:
        return [i for i in self.inputs if i.visible]

    @property
    def required_inputs(self) -> List[InputField]:
        return [i for i in self.inputs if i.required]

    @property
    def has_form(self) -> bool:
        return bool(self.forms) or bool(self.inputs)

    @property
    def has_modal(self) -> bool:
        return bool(self.modals)

    @property
    def has_screenshot(self) -> bool:
        return bool(self.screenshot)

    def to_dict(self, include_html: bool = False, text_limit: int = 2_000) -> Dict[str, Any]:
        """JSON-safe view for logging / telemetry.

        Screenshot bytes are elided (only presence + size reported). ``html`` is
        omitted unless ``include_html`` (it is large and rarely wanted in logs);
        ``visible_text`` is truncated to ``text_limit`` chars.
        """
        def _msg(m: PageMessage) -> Dict[str, Any]:
            return {
                "kind": m.kind, "text": m.text, "selector": m.selector,
                "associated_field": m.associated_field,
            }

        d: Dict[str, Any] = {
            "captured_at": self.captured_at,
            "url": self.url,
            "title": self.title,
            "from_frame": self.from_frame,
            "frame_url": self.frame_url,
            "error": self.error,
            "is_stable": self.is_stable,
            "stability_reason": self.stability_reason,
            "screenshot_present": self.has_screenshot,
            "screenshot_size": len(self.screenshot) if self.screenshot else 0,
            "visible_text": (self.visible_text or "")[:text_limit],
            "navigation": {
                "url": self.navigation.url,
                "title": self.navigation.title,
                "ready_state": self.navigation.ready_state,
                "is_loading": self.navigation.is_loading,
                "frame_count": self.navigation.frame_count,
                "referrer": self.navigation.referrer,
                "visibility_state": self.navigation.visibility_state,
            },
            "counts": {
                "forms": len(self.forms),
                "inputs": len(self.inputs),
                "buttons": len(self.buttons),
                "messages": len(self.messages),
                "modals": len(self.modals),
                "validation_errors": len(self.validation_errors),
                "success_messages": len(self.success_messages),
            },
            "forms": [
                {"selector": f.selector, "action": f.action, "method": f.method,
                 "field_count": f.field_count, "visible": f.visible}
                for f in self.forms
            ],
            "inputs": [
                {"selector": i.selector, "field_type": i.field_type, "label": i.label,
                 "name": i.name, "value": i.value, "required": i.required,
                 "disabled": i.disabled, "checked": i.checked, "options": i.options,
                 "visible": i.visible}
                for i in self.inputs
            ],
            "buttons": [
                {"selector": b.selector, "text": b.text, "button_type": b.button_type,
                 "disabled": b.disabled, "visible": b.visible, "is_submit": b.is_submit}
                for b in self.buttons
            ],
            "messages": [_msg(m) for m in self.messages],
            "modals": [
                {"selector": m.selector, "role": m.role,
                 "text_preview": m.text_preview, "has_close_button": m.has_close_button,
                 "close_selector": m.close_selector,
                 "looks_like_application": m.looks_like_application}
                for m in self.modals
            ],
        }
        if include_html:
            d["html"] = self.html
        return d

    def summary(self) -> str:
        """One-line human summary for log lines."""
        parts = [
            f"url={self.url[:80]!r}",
            f"stable={self.is_stable}",
            f"forms={len(self.forms)}",
            f"inputs={len(self.inputs)}",
            f"buttons={len(self.buttons)}",
        ]
        if self.validation_errors:
            parts.append(f"val_errors={len(self.validation_errors)}")
        if self.success_messages:
            parts.append(f"success={len(self.success_messages)}")
        if self.modals:
            parts.append(f"modals={len(self.modals)}")
        if not self.has_screenshot:
            parts.append("no_screenshot")
        if self.error:
            parts.append(f"error={self.error!r}")
        return "BrowserState(" + " ".join(parts) + ")"
