#!/usr/bin/env python3
"""Point an existing OpenCode install at the LPU hackathon gateway.

Works on macOS and Windows. Does nothing if OpenCode is not installed.
"""

from __future__ import annotations

import getpass
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://lpu-hack.ackodev.com"
MODELS = [
    "vultr/glm-5.3-flash",
    "vultr/glm-5.3",
    "vultr/deepseek-v4.1-flash-1017",
    "vultr/qwen3.8-27b",
    "vultr/qwen3.8-flash-next",
    "openai/gpt-6-luna",
    "openai/gpt-6.1-terra",
]
DEFAULT_MODEL = "vultr/glm-5.3-flash"


def home() -> Path:
    return Path.home()


def opencode_binary() -> Path | None:
    names = ["opencode.exe", "opencode"] if sys.platform == "win32" else ["opencode"]
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    candidates = [
        home() / ".opencode" / "bin" / "opencode",
        home() / ".opencode" / "bin" / "opencode.exe",
        home() / "AppData" / "Local" / "opencode" / "opencode.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def config_path() -> Path:
    return home() / ".config" / "opencode" / "opencode.jsonc"


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        backup(path)
        print(f"existing {path} was not valid JSON, saved a .bak and starting clean")
        return {}


def configure(key: str) -> None:
    path = config_path()
    existing = load_config(path)
    provider = existing.setdefault("provider", {})
    provider["bifrost"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Bifrost",
        "options": {"baseURL": f"{BASE}/openai", "apiKey": key},
        "models": {model: {} for model in MODELS},
    }
    existing["model"] = f"bifrost/{DEFAULT_MODEL}"
    enabled = existing.setdefault("enabled_providers", [])
    if "bifrost" not in enabled:
        enabled.append("bifrost")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"configured {path}")


def check(key: str) -> None:
    req = urllib.request.Request(
        f"{BASE}/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode(errors="replace")
        raise SystemExit(f"gateway check failed: HTTP {exc.code} {detail}") from exc
    ids = [item.get("id") for item in body.get("data") or [] if isinstance(item, dict)]
    print(f"gateway ok, {len(ids)} models visible")


def main() -> None:
    binary = opencode_binary()
    if binary is None:
        print("OpenCode is not installed. Install it first, then run this script again.")
        print("macOS/Linux: curl -fsSL https://opencode.ai/install | bash")
        print("Windows: irm https://opencode.ai/install.ps1 | iex")
        raise SystemExit(1)
    print(f"found OpenCode at {binary}")
    key = getpass.getpass("Bifrost virtual key: ")
    if not key.startswith("sk-bf-"):
        raise SystemExit("key must start with sk-bf-")
    configure(key)
    check(key)
    print("restart OpenCode, then use model vultr/glm-5.3-flash")


if __name__ == "__main__":
    main()

