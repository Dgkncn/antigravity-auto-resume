#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-click installer for the Antigravity Native Auto-Resume Hook Plugin.
Copies plugin/auto-resume to ~/.gemini/config/plugins/auto-resume, binds the current Python
executable path in hooks.json, and enables 'auto-resume' in ~/.gemini/config/config.json.
"""

import json
import pathlib
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(__file__).resolve().parent
SRC_PLUGIN = ROOT / "plugin" / "auto-resume"
CONFIG_DIR = pathlib.Path.home() / ".gemini" / "config"
DST_PLUGIN = CONFIG_DIR / "plugins" / "auto-resume"
GLOBAL_CFG = CONFIG_DIR / "config.json"


def main() -> None:
    py_exe = sys.executable
    DST_PLUGIN.mkdir(parents=True, exist_ok=True)

    for fname in ("plugin.json", "auto_resume_hook.py"):
        shutil.copy2(SRC_PLUGIN / fname, DST_PLUGIN / fname)

    hooks_obj = {
        "auto-resume-on-filter": {
            "enabled": True,
            "PostInvocation": [
                {
                    "type": "command",
                    "command": f'"{py_exe}" ./auto_resume_hook.py',
                    "timeout": 8,
                }
            ],
            "Stop": [
                {
                    "type": "command",
                    "command": f'"{py_exe}" ./auto_resume_hook.py',
                    "timeout": 8,
                }
            ],
        }
    }
    (DST_PLUGIN / "hooks.json").write_text(
        json.dumps(hooks_obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[✓] 已安装内置 Hook 插件至: {DST_PLUGIN}")

    cfg = {}
    if GLOBAL_CFG.exists():
        try:
            cfg = json.loads(GLOBAL_CFG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    plugins = cfg.setdefault("plugins", {})
    plugins["auto-resume"] = {"enabled": True}
    GLOBAL_CFG.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[✓] 已在全局配置启用 auto-resume 插件: {GLOBAL_CFG}")


if __name__ == "__main__":
    main()
