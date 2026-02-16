"""ClickSpeak executable entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import traceback

# .app bundles have a minimal PATH that excludes Homebrew.
# Ensure common tool paths are available for ffmpeg, etc.
_EXTRA_PATHS = "/opt/homebrew/bin:/usr/local/bin"
os.environ["PATH"] = _EXTRA_PATHS + ":" + os.environ.get("PATH", "/usr/bin:/bin")


def _launch_diagnostic_dialog(message: str) -> None:
    safe = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display dialog "{safe}" with title "ClickSpeak Error" buttons "OK" default button "OK" ',
        ],
        check=False,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="clickspeak", add_help=False)
    parser.add_argument("--check-permissions", action="store_true")
    parser.add_argument("--open-setup", action="store_true")
    parser.add_argument("--version", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.version:
        from importlib.metadata import version
        try:
            print(f"clickspeak {version('clickspeak')}")
        except Exception:
            print("clickspeak (version unknown)")
        return

    if args.check_permissions:
        from clickspeak.main import permission_payload

        print(json.dumps(permission_payload(), indent=2))
        return

    if args.open_setup:
        _launch_diagnostic_dialog(
            "Run the app and choose Settings -> Setup & Permissions... to complete onboarding."
        )

    try:
        from clickspeak.main import main as run_app

        run_app()
    except Exception as exc:
        err_text = str(exc)
        message = f"ClickSpeak failed to start: {err_text}"
        if "_objc" in err_text and "partially initialized module 'objc'" in err_text:
            message += (
                "\n\nThis usually means the local Python environment has a broken PyObjC setup."
                "\nFix: rebuild the app runtime and launch from /Applications/ClickSpeak.app:\n"
                "  bash scripts/install_app.sh\n"
                "\nIf you are running from terminal, use a Python 3.12 runtime."
            )
        _launch_diagnostic_dialog(message)
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
