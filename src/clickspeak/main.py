from __future__ import annotations

import logging
import atexit
import os
import threading
import subprocess
import time
import platform
import ctypes
import ctypes.util
import sys
from pathlib import Path

import numpy as np
import objc
from AppKit import NSImage, NSPasteboard, NSPasteboardTypeString, NSSound, NSEvent, NSScreen
from Foundation import NSObject
import rumps

from clickspeak.audio import AudioCapture, get_device_by_name, list_input_devices, refresh_devices
from clickspeak.config import Config, load_config, save_config
from clickspeak.inject import inject_text
from clickspeak.transcribe import Transcriber
from clickspeak.vad import VoiceActivityDetector
from clickspeak.wake_word import WakeWordDetector

_log_file = Path.home() / ".config" / "clickspeak" / "app.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_log_file), mode="w"),
    ],
)
logger = logging.getLogger(__name__)

APP_BUNDLE_ID = os.environ.get("CLICKSPEAK_BUNDLE_IDENTIFIER", "com.lamosty.clickspeak")
APP_DISPLAY_NAME = os.environ.get("CLICKSPEAK_APP_NAME", "ClickSpeak")

# SF Symbol names for menu bar states
_SF_SYMBOLS = {
    "idle": "mic",
    "listening": "mic.fill",
    "recording": "waveform",
    "transcribing": "ellipsis.circle",
}
_ICON_DIR = Path.home() / ".config" / "clickspeak" / "icons"


def _generate_sf_icon(symbol_name: str, out_path: Path, tint_color=None) -> bool:
    """Render an SF Symbol to a PNG for the menu bar, optionally tinted."""
    from AppKit import NSBitmapImageRep, NSGraphicsContext, NSCompositingOperationSourceAtop
    from Foundation import NSMakeRect, NSMakeSize

    base = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol_name, None)
    if base is None:
        return False

    size = NSMakeSize(18, 18)
    out_image = NSImage.alloc().initWithSize_(size)
    out_image.lockFocus()
    base.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, 18, 18),
        NSMakeRect(0, 0, base.size().width, base.size().height),
        2,  # NSCompositingOperationSourceOver
        1.0,
    )
    if tint_color is not None:
        tint_color.set()
        NSGraphicsContext.currentContext().setCompositingOperation_(NSCompositingOperationSourceAtop)
        from AppKit import NSBezierPath
        NSBezierPath.fillRect_(NSMakeRect(0, 0, 18, 18))
    rep = NSBitmapImageRep.alloc().initWithFocusedViewRect_(NSMakeRect(0, 0, 18, 18))
    out_image.unlockFocus()

    if rep is None:
        return False
    png = rep.representationUsingType_properties_(4, {})  # NSBitmapImageFileTypePNG
    if png is None:
        return False
    png.writeToFile_atomically_(str(out_path), True)
    return True


def _ensure_icons() -> dict[str, str]:
    """Generate and cache menu bar icon PNGs from SF Symbols."""
    from AppKit import NSColor
    _ICON_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for state, symbol in _SF_SYMBOLS.items():
        icon_path = _ICON_DIR / f"{state}.png"
        paths[state] = str(icon_path)
        if icon_path.exists():
            continue
        try:
            tint = NSColor.redColor() if state == "recording" else None
            if not _generate_sf_icon(symbol, icon_path, tint_color=tint):
                logger.warning("SF Symbol '%s' not available", symbol)
        except Exception:
            logger.exception("Failed to generate icon for %s", state)
    return paths


# ── Permission helpers ───────────────────────────────────────

def _is_accessibility_authorized() -> bool | None:
    if platform.system() != "Darwin":
        return None
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        pass
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        lib.AXIsProcessTrusted.argtypes = []
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def _request_accessibility_access() -> bool | None:
    if platform.system() != "Darwin":
        return None
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
    except Exception:
        return _is_accessibility_authorized()


def _input_monitoring_preflight() -> bool | None:
    if platform.system() != "Darwin":
        return None
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        lib.CGPreflightListenEventAccess.argtypes = []
        lib.CGPreflightListenEventAccess.restype = ctypes.c_bool
        return bool(lib.CGPreflightListenEventAccess())
    except Exception:
        return None


def _request_input_monitoring() -> bool | None:
    if platform.system() != "Darwin":
        return None
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        lib.CGRequestListenEventAccess.argtypes = []
        lib.CGRequestListenEventAccess.restype = ctypes.c_bool
        return bool(lib.CGRequestListenEventAccess())
    except Exception:
        return None


def permission_payload() -> dict[str, object]:
    from datetime import datetime
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "app": "clickspeak",
        "permissions": {
            "accessibility": _is_accessibility_authorized(),
            "input_monitoring": _input_monitoring_preflight(),
            "microphone": None,
            "platform": platform.system(),
        },
        "platform": platform.system(),
    }


# ── Main-thread dispatch helper ─────────────────────────────

class _MainThreadDispatcher(NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(_MainThreadDispatcher, self).init()
        self._callback = callback
        return self

    def dispatch_(self, _):
        self._callback()


# ── App ──────────────────────────────────────────────────────

class ClickSpeakApp(rumps.App):
    def __init__(self) -> None:
        # Generate SF Symbol icons for menu bar
        try:
            self._icon_paths = _ensure_icons()
        except Exception:
            logger.warning("Failed to generate menu bar icons")
            self._icon_paths = {}

        idle_icon = self._icon_paths.get("idle")
        super().__init__(APP_DISPLAY_NAME, title=None, icon=idle_icon, template=True)

        self.config: Config = load_config()
        self._recording = False
        self._transcribing = False
        self._listening = False
        self._option_held = False
        self._hotkey_space_active = False
        self._shutting_down = False
        self._cleanup_done = False
        # keyboard.Controller removed — using CGEvent directly to avoid
        # pynput's CGEventTap which interferes with other apps' shortcuts

        # Permission state
        self._accessibility_trusted: bool | None = None
        self._input_monitoring_authorized: bool | None = None

        # Components
        device = get_device_by_name(self.config.audio_device) if self.config.audio_device else None
        self.audio = AudioCapture(
            sample_rate=self.config.sample_rate,
            energy_threshold=self.config.energy_threshold,
            device=device,
        )
        self.wake_word = WakeWordDetector(
            model_name=self.config.wake_word,
            threshold=self.config.wake_word_threshold,
        )
        self.vad = VoiceActivityDetector(
            silence_threshold=self.config.silence_threshold,
            sample_rate=self.config.sample_rate,
        )
        self.transcriber = Transcriber(model_name=self.config.model_name)

        # Wire callbacks
        self.audio.on_chunk = self._on_audio_chunk
        self.wake_word.on_detected = self._on_wake_word
        self.vad.on_speech_end = self._on_speech_end

        # ── Menu ─────────────────────────────────────────────
        self.status_item = rumps.MenuItem("Status: Idle")

        self.listening_item = rumps.MenuItem("Start Listening", callback=self._toggle_listening)

        self.mode_menu = rumps.MenuItem("Trigger Mode")
        for mode in ("on_demand", "wake_word", "both"):
            item = rumps.MenuItem(mode, callback=self._set_mode)
            item.state = mode == self.config.trigger_mode
            self.mode_menu.add(item)

        self.device_menu = rumps.MenuItem("Audio Device")
        self._rebuild_device_menu()

        self.history_menu = rumps.MenuItem("Recent Transcriptions")
        self._transcription_history: list[str] = []
        self._history_max = 10

        # Shortcut info (read-only, reflects actual bindings)
        self.shortcut_info = rumps.MenuItem("Shortcuts")
        self.shortcut_info.add(rumps.MenuItem("Middle Mouse Click — start/stop recording"))
        self.shortcut_info.add(rumps.MenuItem("Option+Space — start/stop recording"))
        self.shortcut_info.add(rumps.MenuItem("Middle Mouse Drag — fast scroll"))

        self.pause_audio_item = rumps.MenuItem(
            "Pause Audio While Recording",
            callback=self._toggle_pause_audio,
        )
        self.pause_audio_item.state = self.config.pause_audio_while_recording
        self._did_pause_audio = False

        self.menu = [
            self.status_item,
            self.listening_item,
            None,  # separator
            self.history_menu,
            None,  # separator
            self.shortcut_info,
            self.pause_audio_item,
            self.mode_menu,
            self.device_menu,
            None,  # separator
            rumps.MenuItem("Permissions...", callback=self._open_permissions),
        ]

        # Middle mouse state
        self._middle_mouse_down = False
        self._middle_mouse_initial_pos: tuple[float, float] | None = None
        self._middle_mouse_scroll_mode = False
        self._middle_mouse_timer: threading.Timer | None = None

        # ── Startup ──────────────────────────────────────────
        self._hotkey_listener = None  # NSEvent global monitor handle
        self._mouse_listener = None  # NSEvent global monitor handle
        self._audio_device_listener_registered = False
        self._device_available = False  # configured device is actually producing audio
        self._scroll_tap = None
        self._scroll_tap_callback = None
        self._check_permissions()
        self._start_hotkey_listener()
        self._start_mouse_listener()
        self._device_poll_timer: threading.Timer | None = None
        self._start_scroll_wheel_modifier()
        self._start_audio_device_listener()

        # Only auto-start the audio stream for wake word mode.
        # In hotkey-only mode the stream opens on-demand when recording
        # starts, avoiding always-on mic usage and system conflicts.
        if self.config.trigger_mode in ("wake_word", "both"):
            if self.config.audio_device:
                refresh_devices()
                device = get_device_by_name(self.config.audio_device)
                if device is not None:
                    logger.info("Found configured device '%s' at index %d", self.config.audio_device, device)
                    self.audio.set_device(device)
                    if self._start_listening():
                        self._device_available = True
                        logger.info("Listening on '%s'", self.config.audio_device)
                    else:
                        logger.warning("Failed to start on '%s' — will retry", self.config.audio_device)
                else:
                    logger.info("Configured device '%s' not available — waiting", self.config.audio_device)
                    self._set_status(f"Waiting for {self.config.audio_device}...")
                    self._set_icon("idle")
            else:
                self._start_listening()
                self._device_available = True
            self._start_device_poll()
        else:
            # Hotkey-only: resolve device but don't open stream yet
            if self.config.audio_device:
                refresh_devices()
                device = get_device_by_name(self.config.audio_device)
                if device is not None:
                    self.audio.set_device(device)
                    self._device_available = True
            else:
                self._device_available = True
            self._set_status("Ready")
            self._set_icon("idle")
            logger.info("On-demand mode — audio stream opens when recording")
        atexit.register(self._cleanup)

    # ── Status ───────────────────────────────────────────────

    def _set_status(self, text: str) -> None:
        self._run_on_main(lambda: setattr(self.status_item, "title", f"Status: {text}"))

    def _set_icon(self, state: str) -> None:
        path = self._icon_paths.get(state)
        if path:
            is_template = state != "recording"  # recording icon is red, not template
            def _update():
                self.icon = path
                self.template = is_template
            self._run_on_main(_update)
        else:
            fallback = {"idle": "mic", "listening": "mic+", "recording": "REC", "transcribing": "..."}
            text = fallback.get(state, "?")
            self._run_on_main(lambda: setattr(self, "title", text))

    # ── Permissions ──────────────────────────────────────────

    def _check_permissions(self) -> None:
        """Request permissions one at a time via native macOS dialogs.

        Each permission is requested, then we wait for the user to grant
        it before moving to the next.  Microphone is requested automatically
        when the audio stream starts.
        """
        self._accessibility_trusted = _is_accessibility_authorized()
        self._input_monitoring_authorized = _input_monitoring_preflight()
        logger.info("Permissions: accessibility=%s, input_monitoring=%s",
                     self._accessibility_trusted, self._input_monitoring_authorized)

        # Step 1: Input Monitoring
        if not self._input_monitoring_authorized:
            logger.info("Requesting Input Monitoring via native prompt")
            _request_input_monitoring()
            if not self._permission_dialog(
                "ClickSpeak Setup (1/2)",
                "ClickSpeak needs Input Monitoring for keyboard shortcuts.\n\n"
                "If a system dialog appeared, click \"Open System Settings\" "
                "and toggle ClickSpeak ON.\n\n"
                "Click Continue when done.",
            ):
                return
            # Re-check
            self._input_monitoring_authorized = _input_monitoring_preflight()
            if not self._input_monitoring_authorized:
                # Open Settings directly as fallback
                subprocess.run(
                    ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"],
                    check=False,
                )
                self._permission_dialog(
                    "ClickSpeak Setup (1/2)",
                    "Toggle ClickSpeak ON in the Input Monitoring list, then click Continue.",
                )
                self._input_monitoring_authorized = _input_monitoring_preflight()

        # Step 2: Accessibility
        self._accessibility_trusted = _is_accessibility_authorized()
        if not self._accessibility_trusted:
            logger.info("Requesting Accessibility via native prompt")
            _request_accessibility_access()
            if not self._permission_dialog(
                "ClickSpeak Setup (2/2)",
                "ClickSpeak needs Accessibility to type transcribed text.\n\n"
                "If a system dialog appeared, click \"Open System Settings\" "
                "and toggle ClickSpeak ON.\n\n"
                "Click Continue when done.",
            ):
                return
            self._accessibility_trusted = _is_accessibility_authorized()
            if not self._accessibility_trusted:
                subprocess.run(
                    ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                    check=False,
                )
                self._permission_dialog(
                    "ClickSpeak Setup (2/2)",
                    "Toggle ClickSpeak ON in the Accessibility list, then click Continue.",
                )
                self._accessibility_trusted = _is_accessibility_authorized()

        if self._accessibility_trusted and self._input_monitoring_authorized:
            subprocess.run(["killall", "System Settings"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _permission_dialog(title: str, message: str) -> bool:
        """Show a blocking dialog. Returns True if user clicked Continue/OK."""
        result = subprocess.run(
            ["osascript", "-e",
             f'display dialog "{message}" with title "{title}" '
             f'buttons {{"Quit", "Continue"}} default button "Continue"'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        return "Continue" in result.stdout

    def _open_permissions(self, _sender=None) -> None:
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=False,
        )

    # ── Audio pause/resume ──────────────────────────────────

    def _toggle_pause_audio(self, sender) -> None:
        sender.state = not sender.state
        self.config.pause_audio_while_recording = bool(sender.state)
        save_config(self.config)

    @staticmethod
    def _send_media_play_pause() -> None:
        """Simulate the media play/pause key via NSEvent system-defined event."""
        from Quartz import CGEventPost
        from Foundation import NSPoint
        NX_KEYTYPE_PLAY = 16
        for down in (True, False):
            flags = 0x0a if down else 0x0b
            data1 = (NX_KEYTYPE_PLAY << 16) | (flags << 8)
            event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14,  # NSEventTypeSystemDefined
                NSPoint(0, 0),
                0,
                0,
                0,
                None,
                8,  # NX_SUBTYPE_AUX_CONTROL_BUTTONS
                data1,
                -1,
            )
            if event:
                CGEventPost(0, event.CGEvent())

    # ── Volume boost for BT HFP ─────────────────────────────

    _saved_volume: int | None = None
    _VOLUME_BOOST = 15  # percentage points to add (capped at 100)

    def _get_volume(self) -> int | None:
        try:
            r = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=2,
            )
            return int(r.stdout.strip()) if r.returncode == 0 else None
        except Exception:
            return None

    @staticmethod
    def _set_volume(level: int) -> None:
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {level}"],
            capture_output=True, timeout=2,
        )

    def _boost_volume(self) -> None:
        vol = self._get_volume()
        if vol is not None:
            self._saved_volume = vol
            boosted = min(vol + self._VOLUME_BOOST, 100)
            self._set_volume(boosted)
            logger.info("Volume boosted %d → %d (HFP compensation)", vol, boosted)

    def _restore_volume(self) -> None:
        if self._saved_volume is not None:
            self._set_volume(self._saved_volume)
            logger.info("Volume restored to %d", self._saved_volume)
            self._saved_volume = None

    # ── Listening ────────────────────────────────────────────

    def _toggle_listening(self, _sender=None) -> None:
        if self._listening:
            self._stop_listening()
        else:
            self._start_listening()

    def _start_listening(self) -> bool:
        try:
            self.audio.start()
        except Exception:
            logger.exception("Failed to start audio")
            self._set_status("Mic blocked — check permissions")
            return False

        self._listening = True
        self.listening_item.title = "Stop Listening"
        self.icon = self._icon_paths.get("listening")
        self.template = True
        self._set_status("Listening")
        self.wake_word.enabled = self.config.trigger_mode in ("wake_word", "both")
        return True

    def _stop_listening(self) -> None:
        try:
            self.audio.stop()
        except Exception:
            logger.exception("Failed to stop audio")
        self._listening = False
        self._recording = False
        self.listening_item.title = "Start Listening"
        self.icon = self._icon_paths.get("idle")
        self.template = True
        self.wake_word.enabled = False
        self.vad.enabled = False

    # ── Trigger mode ─────────────────────────────────────────

    def _set_mode(self, sender) -> None:
        for item in self.mode_menu.values():
            item.state = False
        sender.state = True
        self.config.trigger_mode = sender.title
        save_config(self.config)
        if sender.title in ("wake_word", "both"):
            # Wake word needs always-on audio stream
            if not self._listening:
                self._start_listening()
            self.wake_word.enabled = True
        else:
            # Hotkey-only: stop always-on stream
            if self._listening and not self._recording:
                self._stop_listening()
                self._set_status("Ready")
        if self._hotkey_listener is None:
            self._start_hotkey_listener()

    # ── Audio device ─────────────────────────────────────────

    def _rebuild_device_menu(self) -> None:
        try:
            self.device_menu.clear()
        except AttributeError:
            pass

        default = rumps.MenuItem("System Default", callback=self._select_device)
        default.state = self.config.audio_device is None
        self.device_menu.add(default)

        for dev in list_input_devices():
            item = rumps.MenuItem(dev["name"], callback=self._select_device)
            item.state = self.config.audio_device == dev["name"]
            self.device_menu.add(item)

    def _select_device(self, sender) -> None:
        for item in self.device_menu.values():
            item.state = False
        sender.state = True

        if sender.title == "System Default":
            self.config.audio_device = None
            self.audio.set_device(None)
        else:
            self.config.audio_device = sender.title
            self.audio.set_device(get_device_by_name(sender.title))
        save_config(self.config)

    # ── Audio device change detection ─────────────────────

    def _start_audio_device_listener(self) -> None:
        """Register a CoreAudio property listener to detect device connect/disconnect."""
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))

            # AudioObjectPropertyAddress struct: {selector, scope, element}
            class AudioObjectPropertyAddress(ctypes.Structure):
                _fields_ = [
                    ("mSelector", ctypes.c_uint32),
                    ("mScope", ctypes.c_uint32),
                    ("mElement", ctypes.c_uint32),
                ]

            # kAudioHardwarePropertyDevices = 'dev#' = 0x64657623
            # kAudioObjectPropertyScopeGlobal = 'glob' = 0x676C6F62
            # kAudioObjectPropertyElementMain = 0
            # kAudioObjectSystemObject = 1
            addr = AudioObjectPropertyAddress(0x64657623, 0x676C6F62, 0)

            LISTENER_FUNC = ctypes.CFUNCTYPE(
                ctypes.c_int,       # OSStatus return
                ctypes.c_uint32,    # AudioObjectID
                ctypes.c_uint32,    # number of addresses
                ctypes.POINTER(AudioObjectPropertyAddress),
                ctypes.c_void_p,    # client data
            )

            def _on_devices_changed(obj_id, num_addrs, addrs, client_data):
                logger.info("Audio devices changed — rebuilding menu")
                self._run_on_main(self._on_audio_devices_changed)
                return 0

            # Must keep reference to prevent GC
            self._audio_device_callback = LISTENER_FUNC(_on_devices_changed)

            lib.AudioObjectAddPropertyListener.argtypes = [
                ctypes.c_uint32,
                ctypes.POINTER(AudioObjectPropertyAddress),
                LISTENER_FUNC,
                ctypes.c_void_p,
            ]
            lib.AudioObjectAddPropertyListener.restype = ctypes.c_int

            status = lib.AudioObjectAddPropertyListener(
                1,  # kAudioObjectSystemObject
                ctypes.byref(addr),
                self._audio_device_callback,
                None,
            )
            if status == 0:
                self._audio_device_listener_registered = True
                logger.info("Audio device change listener registered")
            else:
                logger.warning("Failed to register audio device listener: %d", status)
        except Exception:
            logger.exception("Failed to set up audio device change listener")

    def _on_audio_devices_changed(self) -> None:
        """Rebuild device menu on CoreAudio notifications.
        Also trigger an immediate reconnect attempt if we're waiting."""
        logger.info("Audio devices changed callback fired")
        self._rebuild_device_menu()

        # Fast path: if waiting for device, try reconnect now instead of
        # waiting for the next poll cycle
        if self.config.audio_device and not self._device_available:
            self._try_fast_reconnect()

    # ── Device heartbeat polling ─────────────────────────

    def _start_device_poll(self) -> None:
        """Poll every 3s to detect BT device connect/disconnect.

        macOS keeps Bluetooth devices in the device list even after
        disconnect, so we can't rely on the list. Instead:
        - Detect disconnect: audio callback stops firing (stale heartbeat)
        - Detect reconnect: start a test stream, check if callbacks fire
        """
        if not self.config.audio_device:
            return
        self._poll_reconnect_phase: int = 0  # 0=idle, 1=stream started, 2=checking

        def _poll():
            if self._shutting_down:
                return

            now = time.monotonic()

            if self._device_available and self._listening:
                # ── Device supposedly active — check for dead stream ──
                # Dead BT devices keep firing callbacks but with all-zero data.
                # Real mics always have some background noise (peak > 0).
                dead = (self.audio.last_nonzero_time > 0
                        and now - self.audio.last_nonzero_time > 5.0)
                if dead:
                    logger.warning("No real audio for >5s — device '%s' lost",
                                   self.config.audio_device)
                    self._run_on_main(self._on_device_lost)

            elif not self._device_available:
                # ── Device not active — try to reconnect ──
                if self._poll_reconnect_phase == 0:
                    # Phase 0: refresh PortAudio (stale cache misses BT changes),
                    # then start a test stream
                    try:
                        self.audio.stop()
                    except Exception:
                        pass
                    refresh_devices()
                    device = get_device_by_name(self.config.audio_device)
                    if device is not None:
                        self.audio.last_nonzero_time = 0.0
                        self.audio.set_device(device)
                        try:
                            self.audio.start()
                            self._poll_reconnect_phase = 1
                            logger.info("Reconnect test: stream started for '%s'",
                                        self.config.audio_device)
                        except Exception:
                            logger.debug("Reconnect test: failed to start")
                    else:
                        logger.debug("Reconnect: '%s' not in device list",
                                     self.config.audio_device)

                elif self._poll_reconnect_phase == 1:
                    # Phase 1: check if real audio is flowing
                    if (self.audio.last_nonzero_time > 0
                            and now - self.audio.last_nonzero_time < 3.0):
                        # Device is alive — resume listening
                        logger.info("Device '%s' reconnected", self.config.audio_device)
                        self._poll_reconnect_phase = 0
                        self._run_on_main(self._on_device_reconnected)
                    else:
                        # Dead — stop and reset for next attempt
                        logger.debug("Reconnect test: no audio, stopping")
                        try:
                            self.audio.stop()
                        except Exception:
                            pass
                        self._poll_reconnect_phase = 0

            # Schedule next poll
            if not self._shutting_down:
                self._device_poll_timer = threading.Timer(3.0, _poll)
                self._device_poll_timer.daemon = True
                self._device_poll_timer.start()

        self._device_poll_timer = threading.Timer(3.0, _poll)
        self._device_poll_timer.daemon = True
        self._device_poll_timer.start()
        logger.info("Device heartbeat polling started (every 3s)")

    def _try_fast_reconnect(self) -> None:
        """Triggered by CoreAudio callback — try to reconnect immediately."""
        refresh_devices()
        device = get_device_by_name(self.config.audio_device)
        if device is None:
            return
        logger.info("Fast reconnect: '%s' found, starting stream", self.config.audio_device)
        try:
            self.audio.stop()
        except Exception:
            pass
        self.audio.last_nonzero_time = 0.0
        self.audio.set_device(device)
        try:
            self.audio.start()
        except Exception:
            logger.debug("Fast reconnect: failed to start stream")
            return
        # Check for real audio after a short delay
        def _check():
            if self._device_available or self._shutting_down:
                return
            if (self.audio.last_nonzero_time > 0
                    and time.monotonic() - self.audio.last_nonzero_time < 2.0):
                logger.info("Fast reconnect: device alive!")
                self._run_on_main(self._on_device_reconnected)
            else:
                logger.debug("Fast reconnect: no audio, poll will retry")
                try:
                    self.audio.stop()
                except Exception:
                    pass
        threading.Timer(1.5, _check).start()

    def _on_device_lost(self) -> None:
        """Audio stream is dead — device effectively disconnected."""
        logger.warning("Device '%s' lost — pausing, will auto-reconnect",
                       self.config.audio_device)
        self._device_available = False
        self._poll_reconnect_phase = 0
        try:
            self.audio.stop()
        except Exception:
            pass
        self._listening = False
        self._recording = False
        self._set_status(f"Waiting for {self.config.audio_device}...")
        self._set_icon("idle")

    def _on_device_reconnected(self) -> None:
        """Device started producing audio again — resume full listening."""
        self._device_available = True
        self._listening = True
        self.listening_item.title = "Stop Listening"
        self.wake_word.enabled = self.config.trigger_mode in ("wake_word", "both")
        self._set_status("Listening")
        self._set_icon("listening")
        self._play_sound("Glass")
        logger.info("Resumed listening on '%s'", self.config.audio_device)

    # ── Hotkey (Option+Space) ────────────────────────────────

    def _start_hotkey_listener(self) -> None:
        """Use NSEvent global monitor instead of pynput CGEventTap.

        NSEvent monitors observe events after they've been dispatched,
        so they don't interfere with other apps' keyboard shortcuts
        (ShiftIt, BetterTouchTool, etc.).
        """
        if self._hotkey_listener is not None:
            try:
                NSEvent.removeMonitor_(self._hotkey_listener)
            except Exception:
                pass
            self._hotkey_listener = None

        if self._accessibility_trusted is not True or self._input_monitoring_authorized is not True:
            logger.warning("Permissions not confirmed (accessibility=%s, input_monitoring=%s), attempting listener anyway",
                           self._accessibility_trusted, self._input_monitoring_authorized)

        # NSEventMaskKeyDown | NSEventMaskKeyUp | NSEventMaskFlagsChanged
        mask = (1 << 10) | (1 << 11) | (1 << 12)

        try:
            def _handle_ns_event(event):
                event_type = event.type()
                if event_type == 12:  # NSEventTypeFlagsChanged
                    # NSEventModifierFlagOption = 1 << 19
                    self._option_held = bool(event.modifierFlags() & (1 << 19))
                elif event_type == 10:  # NSEventTypeKeyDown
                    if self._option_held and event.keyCode() == 49:  # Space
                        if not self._hotkey_space_active:
                            self._hotkey_space_active = True
                            self._consume_hotkey_char()
                            self._handle_hotkey()
                elif event_type == 11:  # NSEventTypeKeyUp
                    if event.keyCode() == 49:  # Space
                        self._hotkey_space_active = False

            self._hotkey_listener = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, _handle_ns_event,
            )
            logger.info("Hotkey listener started (NSEvent global monitor)")
        except Exception:
            logger.exception("Failed to start hotkey listener")
            self._hotkey_listener = None

    def _handle_hotkey(self) -> None:
        logger.info("HOTKEY: mode=%s listening=%s recording=%s transcribing=%s",
                     self.config.trigger_mode, self._listening, self._recording, self._transcribing)
        if self._transcribing:
            logger.info("HOTKEY: blocked — still transcribing")
            return
        # Hotkey can always STOP recording regardless of trigger mode
        if self._recording:
            logger.info("HOTKEY: stopping recording")
            self._on_speech_end()
            return
        # Starting requires trigger mode to include hotkey
        if self.config.trigger_mode not in ("on_demand", "both"):
            return
        # Don't start if configured device isn't available
        if self.config.audio_device and not self._device_available:
            logger.info("HOTKEY: blocked — waiting for %s", self.config.audio_device)
            return
        if not self._listening and not self._start_listening():
            return
        logger.info("HOTKEY: starting recording")
        self._start_recording()

    def _consume_hotkey_char(self) -> None:
        """Send a backspace to remove the character Option+Space inserts."""
        from Quartz import CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags, kCGHIDEventTap
        def _clear():
            time.sleep(0.01)
            try:
                # kVK_Delete (backspace) = 0x33
                down = CGEventCreateKeyboardEvent(None, 0x33, True)
                CGEventSetFlags(down, 0)
                up = CGEventCreateKeyboardEvent(None, 0x33, False)
                CGEventSetFlags(up, 0)
                CGEventPost(kCGHIDEventTap, down)
                CGEventPost(kCGHIDEventTap, up)
            except Exception:
                pass
        threading.Thread(target=_clear, daemon=True).start()

    # ── Scroll wheel speed modifier ────────────────────────

    def _start_scroll_wheel_modifier(self) -> None:
        """CGEventTap that scales discrete (mouse wheel) scroll events.
        Trackpad gestures (precise deltas) are passed through unchanged,
        matching drtic-macos-app FastScrollView behaviour."""
        multiplier = self.config.mouse_wheel_scroll_multiplier
        if multiplier == 1.0:
            logger.info("Scroll wheel multiplier is 1.0 — no tap needed")
            return

        try:
            cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
            cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

            CALLBACK = ctypes.CFUNCTYPE(
                ctypes.c_void_p,   # CGEventRef return
                ctypes.c_void_p,   # CGEventTapProxy
                ctypes.c_uint32,   # CGEventType
                ctypes.c_void_p,   # CGEventRef
                ctypes.c_void_p,   # userInfo
            )

            config_ref = self.config  # capture for closure

            def _scroll_callback(proxy, event_type, event, user_info):
                # kCGScrollWheelEventIsContinuous (field 88): 0 = discrete wheel, 1 = trackpad
                cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
                is_continuous = cg.CGEventGetIntegerValueField(event, 88)
                if is_continuous:
                    return event  # don't touch trackpad

                mult = config_ref.mouse_wheel_scroll_multiplier
                if mult == 1.0:
                    return event

                # Scale scrollDeltaAxis1 (field 11) — vertical scroll
                cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
                cg.CGEventSetIntegerValueField.restype = None

                delta_y = cg.CGEventGetIntegerValueField(event, 11)
                cg.CGEventSetIntegerValueField(event, 11, int(delta_y * mult))

                # Scale scrollDeltaAxis2 (field 12) — horizontal scroll
                delta_x = cg.CGEventGetIntegerValueField(event, 12)
                if delta_x != 0:
                    cg.CGEventSetIntegerValueField(event, 12, int(delta_x * mult))

                return event

            self._scroll_tap_callback = CALLBACK(_scroll_callback)

            # CGEventTapCreate
            cg.CGEventTapCreate.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_uint64, CALLBACK, ctypes.c_void_p,
            ]
            cg.CGEventTapCreate.restype = ctypes.c_void_p

            # kCGHIDEventTap=0, kCGHeadInsertEventTap=0, kCGEventTapOptionDefault=0
            # scrollWheel mask = 1 << 22
            scroll_mask = 1 << 22
            tap = cg.CGEventTapCreate(0, 0, 0, scroll_mask, self._scroll_tap_callback, None)
            if not tap:
                logger.warning("Could not create scroll wheel event tap (check Accessibility)")
                return

            self._scroll_tap = tap

            # Add to run loop on a background thread
            cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
            cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
            source = cf.CFMachPortCreateRunLoopSource(None, tap, 0)

            def _run_tap():
                cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
                cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
                # kCFRunLoopCommonModes
                common_modes = ctypes.c_void_p.in_dll(cf, "kCFRunLoopCommonModes")
                loop = cf.CFRunLoopGetCurrent()
                cf.CFRunLoopAddSource(loop, source, common_modes)
                cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
                cg.CGEventTapEnable(tap, True)
                cf.CFRunLoopRun.restype = None
                cf.CFRunLoopRun()

            t = threading.Thread(target=_run_tap, daemon=True)
            t.start()
            logger.info("Scroll wheel multiplier active: %.2f", multiplier)

        except Exception:
            logger.exception("Failed to set up scroll wheel modifier")

    # ── Middle mouse (scroll + dictation toggle) ──────────

    def _start_mouse_listener(self) -> None:
        """Use NSEvent global monitor for middle mouse instead of pynput.

        pynput's mouse.Listener creates a CGEventTap that can interfere
        with other apps' keyboard shortcuts (ShiftIt, BetterTouchTool, etc.).
        """
        if self._mouse_listener is not None:
            try:
                NSEvent.removeMonitor_(self._mouse_listener)
            except Exception:
                pass

        # NSEventMaskOtherMouseDown | NSEventMaskOtherMouseUp
        mask = (1 << 25) | (1 << 26)

        try:
            def _handle_mouse_event(event):
                # buttonNumber 2 = middle mouse
                if event.buttonNumber() != 2:
                    return
                event_type = event.type()
                loc = event.locationInWindow()
                screen_h = NSScreen.mainScreen().frame().size.height
                x, y = loc.x, screen_h - loc.y

                if event_type == 25:  # NSEventTypeOtherMouseDown
                    self._on_middle_mouse(x, y, pressed=True)
                elif event_type == 26:  # NSEventTypeOtherMouseUp
                    self._on_middle_mouse(x, y, pressed=False)

            self._mouse_listener = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, _handle_mouse_event,
            )
            logger.info("Middle mouse listener started (NSEvent global monitor)")
        except Exception:
            logger.exception("Failed to start mouse listener")
            self._mouse_listener = None

    def _on_middle_mouse(self, x: float, y: float, pressed: bool) -> None:
        if not self.config.middle_mouse_fast_scroll_enabled:
            return
        if pressed:
            self._middle_mouse_down = True
            self._middle_mouse_scroll_mode = False
            self._middle_mouse_initial_pos = (x, y)
            self._middle_mouse_start_scroll_monitor()
        else:
            self._middle_mouse_down = False
            self._middle_mouse_stop_scroll_monitor()
            if not self._middle_mouse_scroll_mode:
                self._handle_hotkey()

    def _middle_mouse_start_scroll_monitor(self) -> None:
        """Direct-mapped fast scroll matching drtic-macos-app FastScrollView.
        Scroll delta = mouse movement delta * multiplier (1:1 feel)."""
        self._middle_mouse_last_y: float | None = None

        def _poll():
            if not self._middle_mouse_down:
                return
            pos = self._middle_mouse_initial_pos
            if pos is None:
                return

            current = NSEvent.mouseLocation()
            screen_h = NSScreen.mainScreen().frame().size.height
            current_y = screen_h - current.y
            initial_y = pos[1]

            threshold = self.config.middle_mouse_drag_threshold
            if not self._middle_mouse_scroll_mode and abs(current_y - initial_y) > threshold:
                self._middle_mouse_scroll_mode = True
                self._middle_mouse_last_y = current_y

            if self._middle_mouse_scroll_mode and self._middle_mouse_last_y is not None:
                delta = current_y - self._middle_mouse_last_y
                self._middle_mouse_last_y = current_y
                multiplier = max(self.config.middle_mouse_drag_scroll_multiplier, 1.0)
                scroll_amount = int(-delta * multiplier)
                if scroll_amount != 0:
                    self._send_scroll_event(scroll_amount)

            if self._middle_mouse_down:
                self._middle_mouse_timer = threading.Timer(0.016, _poll)
                self._middle_mouse_timer.daemon = True
                self._middle_mouse_timer.start()

        self._middle_mouse_timer = threading.Timer(0.016, _poll)
        self._middle_mouse_timer.daemon = True
        self._middle_mouse_timer.start()

    def _middle_mouse_stop_scroll_monitor(self) -> None:
        if self._middle_mouse_timer is not None:
            self._middle_mouse_timer.cancel()
            self._middle_mouse_timer = None

    @staticmethod
    def _send_scroll_event(amount: int) -> None:
        try:
            cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
            # CGEventCreateScrollWheelEvent(source, units, wheelCount, wheel1)
            cg.CGEventCreateScrollWheelEvent.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
            ]
            cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
            cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            cg.CGEventPost.restype = None
            cg.CFRelease.argtypes = [ctypes.c_void_p]
            cg.CFRelease.restype = None

            # kCGScrollEventUnitPixel = 0, kCGHIDEventTap = 0
            event = cg.CGEventCreateScrollWheelEvent(None, 0, 1, amount)
            if event:
                cg.CGEventPost(0, event)
                cg.CFRelease(event)
        except Exception:
            logger.exception("Failed to send scroll event")

    # ── Audio pipeline ───────────────────────────────────────

    def _on_audio_chunk(self, chunk: np.ndarray) -> None:
        if self._transcribing:
            return
        try:
            # Wake word runs during recording (for stop) OR when trigger mode includes it (for start)
            if self._recording or self.config.trigger_mode in ("wake_word", "both"):
                self.wake_word.process(chunk)
            if self._recording:
                self.vad.process(chunk)
        except Exception:
            logger.exception("Audio processing error")

    def _on_wake_word(self) -> None:
        if self._transcribing:
            return
        # Wake word can always STOP recording regardless of trigger mode
        if self._recording:
            logger.info("Wake word detected — stopping recording")
            self._on_speech_end()
            return
        # Starting requires trigger mode to include wake_word
        if self.config.trigger_mode not in ("wake_word", "both"):
            return
        logger.info("Wake word detected — starting recording")
        self._start_recording()

    def _start_recording(self) -> None:
        if self.config.pause_audio_while_recording:
            self._did_pause_audio = True
            self._send_media_play_pause()
        else:
            # Boost volume to compensate for BT HFP quality/volume drop
            self._boost_volume()
        self._recording = True
        self.vad.enabled = False
        self.wake_word.enabled = True  # always enable for stop detection
        self.wake_word.reset()
        self.wake_word.set_cooldown(1.5)  # let initial utterance pass
        self.wake_word.set_threshold(0.3)  # lower threshold during recording for easier stop
        self.audio.start_recording()
        self._play_sound("Glass")
        self._set_status("Recording...")
        self._set_icon("recording")

    def _on_speech_end(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.vad.enabled = False
        self.wake_word.set_threshold(self.config.wake_word_threshold)  # restore normal threshold
        audio = self.audio.stop_recording()
        self._play_sound("Funk")
        logger.info("SPEECH_END: captured %d samples (%.1fs)", len(audio), len(audio) / self.config.sample_rate if len(audio) > 0 else 0)

        # Restore volume if it was boosted for HFP compensation
        self._restore_volume()

        # On-demand mode: close the audio stream immediately so macOS
        # can switch BT headphones back to A2DP (high quality) faster.
        # The audio data is already in memory — mic is no longer needed.
        if self.config.trigger_mode not in ("wake_word", "both"):
            self._stop_listening()

        if len(audio) == 0:
            self._set_status("No speech captured")
            self._finish_transcription()
            return

        self._transcribing = True
        self._set_status("Transcribing...")
        self._set_icon("transcribing")
        threading.Thread(target=self._transcribe_and_inject, args=(audio,), daemon=True).start()

    def _transcribe_and_inject(self, audio: np.ndarray) -> None:
        try:
            start = time.perf_counter()
            text = self.transcriber.transcribe(audio, self.config.sample_rate).strip()
            ms = (time.perf_counter() - start) * 1000
            # Strip trailing wake word (user said it to stop recording)
            text = self._strip_wake_word(text)
            logger.info("Transcribed %d chars in %.0f ms", len(text), ms)

            if text:
                preview = text[:80].replace("\n", " ")
                logger.info("TRANSCRIBE: got text: %s", preview)
                self._set_status(f"Transcribed: {preview}")
                self._run_on_main(lambda t=text: self._add_to_history(t))
                success = inject_text(text)
                logger.info("INJECT: result=%s", success)
                if success:
                    self._set_status("Sent")
                else:
                    self._set_status("Paste failed — check accessibility")
            else:
                self._set_status("No text transcribed")
        except Exception:
            logger.exception("Transcription failed")
            self._set_status("Transcription failed")
        finally:
            self._run_on_main(self._finish_transcription)

    def _finish_transcription(self) -> None:
        self._transcribing = False
        if self.config.trigger_mode in ("wake_word", "both") and self._listening:
            # Keep stream open for wake word detection
            self.wake_word.enabled = True
            self.wake_word.reset()
            self.wake_word.set_cooldown(2.0)
            self._set_status("Listening")
            self.icon = self._icon_paths.get("listening")
            self.template = True
        else:
            # On-demand: stream was already closed in _on_speech_end
            self._set_status("Ready")
            self.icon = self._icon_paths.get("idle")
            self.template = True
        # Resume audio after a short delay to let BT switch back to A2DP
        if self._did_pause_audio:
            self._did_pause_audio = False
            def _resume():
                time.sleep(1.5)
                self._run_on_main(lambda: self._send_media_play_pause())
            threading.Thread(target=_resume, daemon=True).start()

    @staticmethod
    def _play_sound(name: str) -> None:
        sound = NSSound.soundNamed_(name)
        if sound:
            sound.play()

    def _strip_wake_word(self, text: str) -> str:
        """Remove trailing wake word(s) from transcription (user said it to stop)."""
        import re
        wake = self.config.wake_word.replace("_", " ")
        # Strip all trailing wake word instances with optional punctuation
        pattern = r'([\s,]*\b' + re.escape(wake) + r'[?.!,]*)+\s*$'
        return re.sub(pattern, '', text, flags=re.IGNORECASE).strip()

    # ── Transcription history ────────────────────────────────

    def _add_to_history(self, text: str) -> None:
        self._transcription_history.insert(0, text)
        if len(self._transcription_history) > self._history_max:
            self._transcription_history = self._transcription_history[:self._history_max]
        self._rebuild_history_menu()

    def _rebuild_history_menu(self) -> None:
        try:
            self.history_menu.clear()
        except AttributeError:
            pass

        if not self._transcription_history:
            empty = rumps.MenuItem("(empty)")
            self.history_menu.add(empty)
            return

        for i, text in enumerate(self._transcription_history):
            preview = text[:60].replace("\n", " ")
            if len(text) > 60:
                preview += "..."
            item = rumps.MenuItem(preview, callback=self._copy_history_item)
            item._clickspeak_full_text = text
            self.history_menu.add(item)

    def _copy_history_item(self, sender) -> None:
        text = getattr(sender, "_clickspeak_full_text", sender.title)
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        self._set_status("Copied to clipboard")
        logger.info("Copied history item to clipboard (%d chars)", len(text))

    # ── Helpers ──────────────────────────────────────────────

    def _run_on_main(self, func) -> None:
        if self._shutting_down:
            return
        dispatcher = _MainThreadDispatcher.alloc().initWithCallback_(func)
        dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
            "dispatch:", None, False
        )

    def _cleanup(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self._shutting_down = True
        try:
            if self._hotkey_listener is not None:
                NSEvent.removeMonitor_(self._hotkey_listener)
        except Exception:
            pass
        try:
            if self._mouse_listener is not None:
                NSEvent.removeMonitor_(self._mouse_listener)
        except Exception:
            pass
        self._middle_mouse_stop_scroll_monitor()
        if self._device_poll_timer is not None:
            self._device_poll_timer.cancel()
        try:
            self.audio.stop()
        except Exception:
            pass


def main() -> None:
    app = ClickSpeakApp()
    app.run()


if __name__ == "__main__":
    main()
