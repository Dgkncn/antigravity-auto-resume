#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Native Lifecycle Hook: Auto-Resume on False-Positive Filter / Stream Interrupt
Coordinates with Desktop Watcher (agy_auto_resume_watcher.py):
- On the 1st filter block within ROTATE_WINDOW_SEC (180s), attempts fast internal resume.
- On the 2nd+ filter block within ROTATE_WINDOW_SEC, yields to agy_auto_resume_watcher.py
  so the desktop watcher can rotate the UI model (Gemini 4 Carbon -> Argon -> Hydrogen)
  before sending '接着执行'.
"""

import json
import pathlib
import sys
import time

TRIGGER_PHRASES = (
    "This request was blocked by Gemini's filters",
    "Error: The stream was interrupted",
    "The stream was interrupted. Please continue",
)

RESUME_MESSAGES = [
    "接着执行",
    "继续执行刚才未完成的任务",
    "请接着上一步继续完成剩余步骤",
]

ROTATE_WINDOW_SEC = 180.0
ROTATE_HITS_THRESHOLD = 2
MAX_CONSECUTIVE_RETRIES = 6
STATE_FILE = pathlib.Path(__file__).resolve().parent / "hook_state.json"


def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_tail_lines(path: pathlib.Path, max_bytes: int = 65536) -> list[dict]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                raw = f.read().decode("utf-8", errors="replace")
                lines = raw.splitlines()[1:]
            else:
                raw = f.read().decode("utf-8", errors="replace")
                lines = raw.splitlines()
        out = []
        for line in lines[-25:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


def emit_noop(event_name: str) -> None:
    if event_name == "Stop":
        print(json.dumps({"decision": "stop"}))
    else:
        print("{}")


def main() -> None:
    try:
        raw_in = sys.stdin.read()
        payload = json.loads(raw_in) if raw_in.strip() else {}
    except Exception:
        payload = {}

    event_name = payload.get("hookEventName", "")
    conv_id = payload.get("conversationId", "default")
    transcript_str = payload.get("transcriptPath", "")
    hook_error = str(payload.get("error") or "")
    term_reason = str(payload.get("terminationReason") or "")

    if term_reason == "USER_CANCELED":
        emit_noop(event_name)
        return

    steps = []
    if transcript_str:
        tpath = pathlib.Path(transcript_str)
        if not tpath.exists() and conv_id != "default":
            alt = pathlib.Path.home() / ".gemini" / "antigravity" / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
            if alt.exists():
                tpath = alt
        steps = read_tail_lines(tpath)

    matched = False
    latest_step_idx = -1

    if steps:
        last_step = steps[-1]
        latest_step_idx = last_step.get("step_index", -1)
        last_content = str(last_step.get("content") or "")
        if any(p in last_content for p in TRIGGER_PHRASES):
            matched = True

    if not matched and hook_error and any(p in hook_error for p in TRIGGER_PHRASES):
        matched = True

    if not matched:
        emit_noop(event_name)
        return

    last_user_step_idx = -1
    for s in reversed(steps):
        if s.get("source") == "USER_EXPLICIT" and s.get("type") == "USER_INPUT":
            last_user_step_idx = s.get("step_index", -1)
            break

    now = time.time()
    state = load_state()
    conv_state = state.get(conv_id, {})

    if conv_state.get("user_step_idx") != last_user_step_idx:
        conv_state = {
            "user_step_idx": last_user_step_idx,
            "retries": 0,
            "handled_steps": [],
            "recent_block_steps": {},
        }

    recent_block_steps: dict[str, float] = {
        k: float(v)
        for k, v in (conv_state.get("recent_block_steps") or {}).items()
        if now - float(v) <= ROTATE_WINDOW_SEC
    }
    if latest_step_idx != -1:
        recent_block_steps.setdefault(str(latest_step_idx), now)
    conv_state["recent_block_steps"] = recent_block_steps

    if len(recent_block_steps) >= ROTATE_HITS_THRESHOLD:
        state[conv_id] = conv_state
        save_state(state)
        emit_noop(event_name)
        return

    handled_steps = conv_state.get("handled_steps", [])
    step_tag = f"{event_name}:{latest_step_idx}"
    if latest_step_idx != -1 and step_tag in handled_steps:
        emit_noop(event_name)
        return

    retries = int(conv_state.get("retries", 0))
    if retries >= MAX_CONSECUTIVE_RETRIES:
        emit_noop(event_name)
        return

    msg = RESUME_MESSAGES[retries % len(RESUME_MESSAGES)]
    conv_state["retries"] = retries + 1
    handled_steps.append(step_tag)
    conv_state["handled_steps"] = handled_steps[-20:]
    conv_state["updated_at"] = now
    state[conv_id] = conv_state
    save_state(state)

    if event_name == "PostInvocation":
        print(json.dumps({
            "injectSteps": [{"userMessage": msg}],
            "terminationBehavior": "force_continue",
        }, ensure_ascii=False))
    elif event_name == "Stop":
        print(json.dumps({
            "decision": "continue",
            "reason": msg,
        }, ensure_ascii=False))
    else:
        print("{}")


if __name__ == "__main__":
    main()
