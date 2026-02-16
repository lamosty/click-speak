"""py2app build configuration for ClickSpeak."""

from setuptools import setup

APP = ["src/clickspeak/__main__.py"]

OPTIONS = {
    "iconfile": "ClickSpeak.icns",
    "argv_emulation": False,
    "plist": {
        "CFBundleIdentifier": "com.lamosty.clickspeak",
        "CFBundleName": "ClickSpeak",
        "CFBundleDisplayName": "ClickSpeak",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSMicrophoneUsageDescription": "ClickSpeak needs microphone access for voice-to-text transcription.",
        "NSAppleEventsUsageDescription": "ClickSpeak needs accessibility access to paste transcribed text into other apps.",
        "NSInputMonitoringUsageDescription": "ClickSpeak needs input monitoring permission to use the global hotkey (Option+Space).",
        "NSHighResolutionCapable": True,
    },
    "packages": [
        "clickspeak",
        "rumps",
        "pynput",
        "sounddevice",
        "openwakeword",
        "numpy",
        "numba",
    ],
}

setup(
    name="ClickSpeak",
    app=APP,
    options={"py2app": OPTIONS},
)
