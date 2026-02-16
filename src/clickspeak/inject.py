"""Text injection via clipboard paste for macOS."""

from __future__ import annotations

import logging
import time

from AppKit import NSPasteboard, NSPasteboardTypeString
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGHIDEventTap,
    CGEventGetLocation,
    CGEventCreate,
)

logger = logging.getLogger(__name__)

# Virtual keycodes for macOS
_kVK_ANSI_V = 0x09
_kVK_Return = 0x24


def _click_at_cursor() -> None:
    """Simulate a left mouse click at the current cursor position to focus the window."""
    pos = CGEventGetLocation(CGEventCreate(None))
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pos, 0)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pos, 0)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def _paste_clipboard() -> None:
    """Simulate Cmd+V using CGEvent (works when the app has accessibility)."""
    down = CGEventCreateKeyboardEvent(None, _kVK_ANSI_V, True)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    up = CGEventCreateKeyboardEvent(None, _kVK_ANSI_V, False)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def _press_return() -> None:
    """Simulate pressing the Return key with no modifier flags."""
    down = CGEventCreateKeyboardEvent(None, _kVK_Return, True)
    CGEventSetFlags(down, 0)  # clear all modifiers
    up = CGEventCreateKeyboardEvent(None, _kVK_Return, False)
    CGEventSetFlags(up, 0)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def inject_text(text: str, press_enter: bool = True) -> bool:
    """Inject *text* into the active application by simulating Cmd+V.

    The current clipboard contents are saved beforehand and restored
    after a short delay so the user's clipboard is not clobbered.
    """
    pb = NSPasteboard.generalPasteboard()

    # Save old clipboard
    old = pb.stringForType_(NSPasteboardTypeString) or ""

    try:
        _click_at_cursor()
        time.sleep(0.05)
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        _paste_clipboard()
        time.sleep(0.5)
        if press_enter:
            _press_return()
            time.sleep(0.05)
        return True
    except Exception:
        logger.exception("Text injection failed")
        return False
    finally:
        try:
            pb.clearContents()
            pb.setString_forType_(old, NSPasteboardTypeString)
        except Exception:
            logger.warning("Unable to restore clipboard after injection")
