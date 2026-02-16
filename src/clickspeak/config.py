from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "clickspeak"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class Config:
    audio_device: str | None = None
    wake_word: str = "hey_jarvis"
    wake_word_threshold: float = 0.5
    trigger_mode: str = "on_demand"  # on_demand | wake_word | both
    energy_threshold: float = 0.01
    silence_threshold: float = 1.5
    sample_rate: int = 16000
    model_name: str = "mlx-community/parakeet-tdt-0.6b-v3"
    middle_mouse_fast_scroll_enabled: bool = True
    middle_mouse_drag_scroll_multiplier: float = 12.0
    middle_mouse_drag_threshold: float = 8.0
    mouse_wheel_scroll_multiplier: float = 1.0  # < 1.0 to slow down, > 1.0 to speed up
    pause_audio_while_recording: bool = False


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        return Config()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = json.load(f)

    # Migrate legacy trigger_mode values
    if data.get("trigger_mode") == "hotkey":
        data["trigger_mode"] = "on_demand"
    return Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})


def save_config(config: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(config), f, indent=2)
        f.write("\n")
