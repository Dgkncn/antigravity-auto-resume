#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity 自动检测拦截 + 短时间二次拦截自动轮换三模型 + 自动“接着执行”守护脚本
==================================================================================
支持功能：
1. 实时监听 ~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl
   （零截图、零 OCR、0% CPU 占用、100% 精准识别）。
2. 当检测到会话出现：
   - "This request was blocked by Gemini's filters"
   - "Error: The stream was interrupted"
   - "The stream was interrupted. Please continue"
3. 【新增：短时间二次拦截自动轮换模型】
   - 默认在 180 秒窗口期内（--rotate-window 180），若同一会话累计命中 >= 2 次拦截（--rotate-hits 2），
     立即在以下三个模型之间自动循环切换：
       Gemini 4 Carbon (Medium) -> Gemini 4 Argon (Medium) -> Gemini 4 Hydrogen (Medium) -> Gemini 4 Carbon
   - 切换模型完成后，自动聚焦输入框发送“接着执行”，让新模型无缝接管当前进度！
4. 单次偶发拦截时，等待 2.5s 配合内置 Hook 自愈；若未自愈则直接发送“接着执行”。
"""

import argparse
import ctypes
from ctypes import wintypes
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

TRIGGER_PHRASES = (
    "This request was blocked by Gemini's filters",
    "Error: The stream was interrupted",
    "The stream was interrupted. Please continue",
)

RESUME_PROMPTS = [
    "接着执行",
    "请继续完成刚才未完成的步骤",
    "接着上一步继续执行并输出结果",
]

# 三个轮换模型顺序（对应截图菜单）
MODEL_ROTATION = [
    ("carbon", "Gemini 4 Carbon"),
    ("argon", "Gemini 4 Argon"),
    ("hydrogen", "Gemini 4 Hydrogen"),
]

import sqlite3

BRAIN_DIR = pathlib.Path.home() / ".gemini" / "antigravity" / "brain"
SUMMARY_DB = pathlib.Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"

# ---------------------------------------------------------------------------
# CRITICAL: Enable Per-Monitor DPI Awareness V2 BEFORE any Win32 coord calls!
# On 150%/200% Windows scaling, UIA returns physical pixels (e.g. 810, 1638).
# Without DPI awareness, SetCursorPos doubles coordinates off-screen!
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_V = 0x56

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE


def now_str() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def get_clipboard_text() -> str | None:
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False

    try:
        user32.EmptyClipboard()
        encoded = (text + "\0").encode("utf-16-le")
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h_mem:
            return False
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return False
        ctypes.memmove(ptr, encoded, len(encoded))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        return True
    finally:
        user32.CloseClipboard()


def send_key(vk: int, up: bool = False) -> None:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=(KEYEVENTF_KEYUP if up else 0), time=0, dwExtraInfo=0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def tap_key(vk: int, hold_sec: float = 0.03) -> None:
    send_key(vk, up=False)
    time.sleep(hold_sec)
    send_key(vk, up=True)


def send_ctrl_v_and_enter() -> None:
    send_key(VK_CONTROL, up=False)
    time.sleep(0.03)
    tap_key(VK_V, 0.03)
    time.sleep(0.03)
    send_key(VK_CONTROL, up=True)
    time.sleep(0.18)
    tap_key(VK_RETURN, 0.04)


def click_screen_point(x: int, y: int, restore_cursor: bool = True) -> None:
    orig_pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(orig_pt))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    down = INPUT(type=INPUT_MOUSE)
    down.u.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN, time=0, dwExtraInfo=0)
    up = INPUT(type=INPUT_MOUSE)
    up.u.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP, time=0, dwExtraInfo=0)
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(0.04)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    time.sleep(0.06)
    if restore_cursor:
        user32.SetCursorPos(orig_pt.x, orig_pt.y)


def get_process_exe_name(pid: int) -> str:
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h_proc)


def get_conversation_window_title(conv_id: str) -> str:
    if not conv_id or not SUMMARY_DB.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{SUMMARY_DB.as_posix()}?mode=ro", uri=True, timeout=2.0)
        try:
            row = con.execute(
                "SELECT title, preview FROM conversation_summaries WHERE conversation_id = ? LIMIT 1",
                (conv_id,),
            ).fetchone()
            if row:
                return str(row[0] or row[1] or "").strip()
        finally:
            con.close()
    except Exception:
        pass
    return ""


def find_antigravity_windows(conv_id: str = "") -> list[tuple[int, int, str, RECT]]:
    results = []
    title_buf = ctypes.create_unicode_buffer(512)
    cls_buf = ctypes.create_unicode_buffer(256)
    expected_title = get_conversation_window_title(conv_id).lower() if conv_id else ""

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = get_process_exe_name(pid.value).lower()
        if exe != "antigravity.exe":
            return True
        user32.GetClassNameW(hwnd, cls_buf, 256)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        if cls_buf.value == "Chrome_WidgetWin_1" and (w > 300 or user32.IsIconic(hwnd)):
            results.append((int(hwnd), int(pid.value), title_buf.value, rect))
        return True

    user32.EnumWindows(enum_cb, 0)

    def sort_key(item):
        _, _, t, r = item
        tl = t.lower()
        title_match = 1 if (expected_title and expected_title[:10] in tl) else 0
        area = (r.right - r.left) * (r.bottom - r.top)
        return (title_match, area)

    results.sort(key=sort_key, reverse=True)
    return results


def bring_window_to_front(hwnd: int) -> RECT:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.25)

    fg_hwnd = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    cur_tid = kernel32.GetCurrentThreadId()

    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        if not user32.SetForegroundWindow(hwnd):
            tap_key(VK_MENU, 0.01)
            user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)

    time.sleep(0.2)
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def run_ps_uia(ps_body: str, timeout_sec: float = 8.0) -> str:
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_body],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        return lines[-1] if lines else "NONE"
    except Exception as e:
        return f"ERR:{e}"


# ---------------------------------------------------------------------------
# UIA Model Rotation: Gemini 4 Carbon -> Gemini 4 Argon -> Gemini 4 Hydrogen
# ---------------------------------------------------------------------------
_last_rotated_idx: int | None = None


def detect_model_from_transcript(steps: list[dict]) -> str | None:
    """Fallback detection from recent USER_SETTINGS_CHANGE in transcript.jsonl."""
    for s in reversed(steps):
        txt = str(s.get("content") or "")
        m = re.search(r"Model Selection`\s+from\s+.+?\s+to\s+Gemini\s+4\s+(Carbon|Argon|Hydrogen)", txt, re.I)
        if m:
            return m.group(1).lower()
    return None


def uia_find_model_trigger(hwnd: int) -> tuple[str | None, tuple[int, int, int, int] | None]:
    """
    Find the Model Selector button/pill inside Antigravity.exe composer bar.
    Returns (detected_model_key, (cx, cy, left, top)).
    """
    ps = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
if (-not $root) {{ Write-Output 'NONE'; exit }}

$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
if ($all.Count -lt 12) {{
    Start-Sleep -Milliseconds 500
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
}}

$best = $null
$bestScore = -999999
$bestKey = ''

foreach ($el in $all) {{
    try {{
        if (-not $el.Current.IsEnabled -or $el.Current.IsOffscreen) {{ continue }}
        $name = [string]$el.Current.Name
        if ([string]::IsNullOrWhiteSpace($name)) {{ continue }}
        $nl = $name.ToLowerInvariant()
        $key = ''
        if ($nl.Contains('gemini 4 carbon') -or ($nl.Contains('carbon') -and $nl.Contains('gemini'))) {{ $key = 'carbon' }}
        elseif ($nl.Contains('gemini 4 argon') -or ($nl.Contains('argon') -and $nl.Contains('gemini'))) {{ $key = 'argon' }}
        elseif ($nl.Contains('gemini 4 hydrogen') -or ($nl.Contains('hydrogen') -and $nl.Contains('gemini'))) {{ $key = 'hydrogen' }}
        if (-not $key) {{ continue }}

        $r = $el.Current.BoundingRectangle
        if ($r.Width -lt 40 -or $r.Width -gt 420 -or $r.Height -lt 14 -or $r.Height -gt 65) {{ continue }}
        if ($name.Length -gt 80) {{ continue }}

        $ct = $el.Current.ControlType.ProgrammaticName
        $bonus = 0
        if ($ct -match 'Button|ComboBox|MenuItem') {{ $bonus = 2000 }}
        $score = [int]$r.Bottom + $bonus
        if ($score -gt $bestScore) {{
            $bestScore = $score
            $best = $el
            $bestKey = $key
        }}
    }} catch {{}}
}}

if ($best) {{
    $br = $best.Current.BoundingRectangle
    $cx = [int]($br.Left + $br.Width * 0.5)
    $cy = [int]($br.Top + $br.Height * 0.5)
    $l  = [int]$br.Left
    $t  = [int]$br.Top
    Write-Output "OK,$bestKey,$cx,$cy,$l,$t"
}} else {{
    Write-Output "NONE"
}}
"""
    out = run_ps_uia(ps, timeout_sec=8.0)
    if out.startswith("OK,"):
        parts = out.split(",")
        _, mkey, cx, cy, l, t = parts[:6]
        return mkey, (int(cx), int(cy), int(l), int(t))
    return None, None


def uia_find_menu_option(hwnd: int, target_label: str, exclude_lt: tuple[int, int]) -> tuple[int, int] | None:
    """
    Find the popup menu row for `target_label` (e.g. 'Gemini 4 Argon'),
    excluding the trigger pill itself at `exclude_lt`.
    Returns (click_x, click_y) on the left/label side of the menu item.
    """
    ex_l, ex_t = exclude_lt
    target_lower = target_label.lower()
    ps = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$desk = [System.Windows.Automation.AutomationElement]::RootElement
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
if (-not $root) {{ Write-Output 'NONE'; exit }}
$pidVal = $root.Current.ProcessId

# Search all Antigravity windows/popups matching ProcessId
$pidCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $pidVal)
$wins = $desk.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCond)

$best = $null
$bestArea = 99999999

foreach ($w in $wins) {{
    $els = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($el in $els) {{
        try {{
            if (-not $el.Current.IsEnabled -or $el.Current.IsOffscreen) {{ continue }}
            $name = [string]$el.Current.Name
            if ([string]::IsNullOrWhiteSpace($name)) {{ continue }}
            $nl = $name.ToLowerInvariant()
            if (-not $nl.Contains('{target_lower}')) {{ continue }}
            if ($name.Length -gt 90) {{ continue }}

            $r = $el.Current.BoundingRectangle
            if ($r.Width -lt 30 -or $r.Width -gt 650 -or $r.Height -lt 14 -or $r.Height -gt 90) {{ continue }}

            $dl = [Math]::Abs($r.Left - {ex_l})
            $dt = [Math]::Abs($r.Top - {ex_t})
            if ($dl -lt 18 -and $dt -lt 18) {{ continue }}

            $ct = $el.Current.ControlType.ProgrammaticName
            $area = [int]($r.Width * $r.Height)
            if ($ct -match 'MenuItem|ListItem|Button') {{ $area = [int]($area * 0.5) }}
            if ($area -lt $bestArea) {{
                $bestArea = $area
                $best = $el
            }}
        }} catch {{}}
    }}
}}

if ($best) {{
    $br = $best.Current.BoundingRectangle
    $offsetX = [Math]::Min(80, [int]($br.Width * 0.35))
    $cx = [int]($br.Left + $offsetX)
    $cy = [int]($br.Top + $br.Height * 0.5)
    Write-Output "OK,$cx,$cy"
}} else {{
    Write-Output "NONE"
}}
"""
    out = run_ps_uia(ps, timeout_sec=7.0)
    if out.startswith("OK,"):
        _, sx, sy = out.split(",")[:3]
        return int(sx), int(sy)
    return None


def uia_find_effort_medium_if_open(hwnd: int) -> tuple[int, int] | None:
    """
    If clicking a model row with '>' opened the Effort Level submenu (Low / Medium / High),
    find the standalone 'Medium' item and click it.
    """
    ps = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$desk = [System.Windows.Automation.AutomationElement]::RootElement
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
if (-not $root) {{ Write-Output 'NONE'; exit }}
$pidVal = $root.Current.ProcessId
$pidCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $pidVal)
$wins = $desk.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCond)

foreach ($w in $wins) {{
    $els = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($el in $els) {{
        try {{
            if (-not $el.Current.IsEnabled -or $el.Current.IsOffscreen) {{ continue }}
            $name = ([string]$el.Current.Name).Trim()
            if ($name -match '^Medium(\\b|\\s)' -and (-not ($name -match 'Gemini'))) {{
                $ct = $el.Current.ControlType.ProgrammaticName
                $r = $el.Current.BoundingRectangle
                if ($ct -match 'MenuItem|ListItem|Button|Text' -and $r.Width -ge 35 -and $r.Width -le 300 -and $r.Height -ge 16 -and $r.Height -le 65) {{
                    $cx = [int]($r.Left + $r.Width * 0.45)
                    $cy = [int]($r.Top + $r.Height * 0.5)
                    Write-Output "OK,$cx,$cy"
                    exit
                }}
            }}
        }} catch {{}}
    }}
}}
Write-Output 'NONE'
"""
    out = run_ps_uia(ps, timeout_sec=6.0)
    if out.startswith("OK,"):
        _, sx, sy = out.split(",")[:3]
        return int(sx), int(sy)
    return None


def rotate_antigravity_model(
    steps_hint: list[dict] | None = None,
    restore_prev_window: bool = False,
    conv_id: str = "",
) -> tuple[bool, str]:
    """
    Rotate model in Antigravity UI across:
      Gemini 4 Carbon -> Gemini 4 Argon -> Gemini 4 Hydrogen -> Gemini 4 Carbon
    Returns (switched_ok, target_model_label).
    """
    global _last_rotated_idx

    wins = find_antigravity_windows(conv_id)
    if not wins:
        print(f"[{now_str()}] [!] 未找到 Antigravity.exe 窗口，无法切换模型")
        return False, ""

    hwnd, _, title, _ = wins[0]
    prev_fg = user32.GetForegroundWindow()

    try:
        bring_window_to_front(hwnd)
        cur_key, trig = uia_find_model_trigger(hwnd)
        if not cur_key and steps_hint:
            cur_key = detect_model_from_transcript(steps_hint)

        # Determine next model index in MODEL_ROTATION
        keys = [k for k, _ in MODEL_ROTATION]
        if cur_key in keys:
            cur_idx = keys.index(cur_key)
        elif _last_rotated_idx is not None:
            cur_idx = _last_rotated_idx
        else:
            cur_idx = 0  # default assume Carbon -> switch to Argon

        next_idx = (cur_idx + 1) % len(MODEL_ROTATION)
        target_key, target_label = MODEL_ROTATION[next_idx]
        cur_label = MODEL_ROTATION[cur_idx][1]

        if not trig:
            print(f"[{now_str()}] [!] UIA 未定位到当前模型按钮 (目标切换: {cur_label} -> {target_label})")
            return False, target_label

        tcx, tcy, tl, tt = trig
        print(f"[{now_str()}] [模型轮换] 当前模型: {cur_label} -> 准备切换为: {target_label} (按钮坐标 {tcx},{tcy})")

        # 1. Click model dropdown button
        click_screen_point(tcx, tcy, restore_cursor=False)
        time.sleep(0.42)

        # 2. Find and click target model item in popup menu
        opt_pt = uia_find_menu_option(hwnd, target_label, (tl, tt))
        if not opt_pt:
            time.sleep(0.35)
            opt_pt = uia_find_menu_option(hwnd, target_label, (tl, tt))

        if not opt_pt:
            print(f"[{now_str()}] [!] 弹出菜单中未找到选项 {target_label!r}，按 Esc 关闭菜单")
            tap_key(VK_ESCAPE, 0.03)
            return False, target_label

        click_screen_point(opt_pt[0], opt_pt[1], restore_cursor=False)
        time.sleep(0.35)

        # 3. Check if trigger already updated; if submenu ('Medium') popped open, click 'Medium'
        verify_key, _ = uia_find_model_trigger(hwnd)
        if verify_key != target_key:
            med_pt = uia_find_effort_medium_if_open(hwnd)
            if med_pt:
                print(f"[{now_str()}] [*] 检测到二级推理档位菜单，自动点击 Medium {med_pt}")
                click_screen_point(med_pt[0], med_pt[1], restore_cursor=True)
                time.sleep(0.30)
                verify_key, _ = uia_find_model_trigger(hwnd)

        _last_rotated_idx = next_idx
        if verify_key == target_key:
            print(f"[{now_str()}] [✓] 模型轮换成功！当前已切换至: {target_label} (Medium)")
            return True, target_label

        # Ensure any lingering popup menu is dismissed cleanly
        tap_key(VK_ESCAPE, 0.03)
        time.sleep(0.1)
        print(f"[{now_str()}] [*] 已执行模型点击切换 -> {target_label}")
        return True, target_label
    finally:
        if restore_prev_window and prev_fg and prev_fg != hwnd and user32.IsWindowVisible(prev_fg):
            time.sleep(0.1)
            user32.SetForegroundWindow(prev_fg)


# ---------------------------------------------------------------------------
# UIA Chat Input Focus & Send (Anchored to Model Pill to avoid right-pane IDE editor!)
# ---------------------------------------------------------------------------
def uia_focus_chat_input(hwnd: int) -> tuple[bool, tuple[int, int] | None, bool]:
    """
    Locate the real Chat Input Box ('Ask anything, @ to mention, / for actions')
    directly above the Model Selector pill, NEVER touching any open code editor on the right!
    Returns (found_ok, (click_x, click_y), already_has_queued_msg).
    """
    ps_script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
if (-not $root) {{ Write-Output 'NONE'; exit }}

$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
if ($all.Count -lt 12) {{
    Start-Sleep -Milliseconds 450
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
}}

$hasQueued = 0
$modelRect = $null
$askBox = $null

foreach ($el in $all) {{
    try {{
        if (-not $el.Current.IsEnabled -or $el.Current.IsOffscreen) {{ continue }}
        $name = [string]$el.Current.Name
        if ([string]::IsNullOrWhiteSpace($name)) {{ continue }}
        $nl = $name.ToLowerInvariant()

        if ($nl.Contains('queued messages')) {{
            $hasQueued = 1
        }}
        if ($nl.Contains('ask anything')) {{
            $r = $el.Current.BoundingRectangle
            if ($r.Width -ge 80 -and $r.Height -ge 14) {{
                $askBox = $r
            }}
        }}
        if ($nl.Contains('gemini 4 carbon') -or $nl.Contains('gemini 4 argon') -or $nl.Contains('gemini 4 hydrogen')) {{
            $r2 = $el.Current.BoundingRectangle
            if ($r2.Width -ge 40 -and $r2.Width -le 420 -and $r2.Height -le 65) {{
                if (-not $modelRect -or $r2.Bottom -gt $modelRect.Bottom) {{
                    $modelRect = $r2
                }}
            }}
        }}
    }} catch {{}}
}}

if ($askBox) {{
    $cx = [int]($askBox.Left + [Math]::Min(120, $askBox.Width * 0.35))
    $cy = [int]($askBox.Top + $askBox.Height * 0.5)
    Write-Output "OK,$cx,$cy,$hasQueued"
    exit
}}

if ($modelRect) {{
    # Chat input box sits directly ~55 physical pixels above the Gemini 4 model pill!
    $cx = [int]($modelRect.Left + 80)
    $cy = [int]($modelRect.Top - 55)
    Write-Output "OK,$cx,$cy,$hasQueued"
    exit
}}

Write-Output "NONE,0,0,$hasQueued"
"""
    out = run_ps_uia(ps_script, timeout_sec=7.0)
    if out.startswith("OK,"):
        _, sx, sy, sq = out.split(",")[:4]
        return True, (int(sx), int(sy)), (sq == "1")
    if out.startswith("NONE,"):
        parts = out.split(",")
        return False, None, (len(parts) >= 4 and parts[3] == "1")
    return False, None, False


def send_resume_to_antigravity(
    message: str,
    bottom_offset_px: int = 110,
    restore_prev_window: bool = True,
    conv_id: str = "",
) -> bool:
    wins = find_antigravity_windows(conv_id)
    if not wins:
        print(f"[{now_str()}] [!] 未找到正在运行的 Antigravity.exe 窗口！")
        return False

    hwnd, _, title, _ = wins[0]
    prev_fg = user32.GetForegroundWindow()
    prev_clip = get_clipboard_text()

    try:
        rect = bring_window_to_front(hwnd)
        w = rect.right - rect.left

        uia_ok, pt, has_queued = uia_focus_chat_input(hwnd)
        if has_queued:
            print(f"[{now_str()}] [*] 检测到聊天框上方已有 Queued Messages 排队消息，跳过重复发送。")
            return True

        if uia_ok and pt:
            click_screen_point(pt[0], pt[1])
            print(f"[{now_str()}] [*] 精准定位聊天输入框成功 {pt} (窗口: {title})")
        else:
            cx = rect.left + int(w * 0.35)
            cy = rect.bottom - bottom_offset_px
            click_screen_point(cx, cy)
            print(f"[{now_str()}] [*] 使用聊天区兜底坐标点击 ({cx}, {cy}) (窗口: {title})")

        time.sleep(0.14)
        if not set_clipboard_text(message):
            print(f"[{now_str()}] [!] 写入剪贴板失败")
            return False

        send_ctrl_v_and_enter()
        time.sleep(0.25)
        print(f"[{now_str()}] [✓] 已自动向 Antigravity 发送: {message!r}")
        return True
    finally:
        if prev_clip is not None:
            set_clipboard_text(prev_clip)
        if restore_prev_window and prev_fg and prev_fg != hwnd and user32.IsWindowVisible(prev_fg):
            time.sleep(0.1)
            user32.SetForegroundWindow(prev_fg)


# ---------------------------------------------------------------------------
# Transcript Tail Scanner
# ---------------------------------------------------------------------------
def read_last_steps(transcript_path: pathlib.Path, max_bytes: int = 49152) -> list[dict]:
    try:
        size = transcript_path.stat().st_size
        if size == 0:
            return []
        with transcript_path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                lines = f.read().decode("utf-8", errors="replace").splitlines()[1:]
            else:
                lines = f.read().decode("utf-8", errors="replace").splitlines()
        steps = []
        for line in lines[-25:]:
            line = line.strip()
            if line:
                try:
                    steps.append(json.loads(line))
                except Exception:
                    pass
        return steps
    except Exception:
        return []


def iter_recent_transcripts(max_age_hours: float = 48.0) -> list[pathlib.Path]:
    if not BRAIN_DIR.exists():
        return []
    cutoff = time.time() - max_age_hours * 3600
    found = []
    for conv_dir in BRAIN_DIR.iterdir():
        if not conv_dir.is_dir():
            continue
        tfile = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            if tfile.exists() and tfile.stat().st_mtime >= cutoff:
                found.append(tfile)
        except Exception:
            continue
    return found


def is_blocked_step(step: dict) -> str | None:
    content = str(step.get("content") or "")
    for phrase in TRIGGER_PHRASES:
        if phrase in content:
            return phrase
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity 拦截自动恢复 + 三模型自动轮换守护脚本")
    parser.add_argument("--interval", type=float, default=0.8, help="日志轮询间隔秒数 (默认 0.8s)")
    parser.add_argument("--wait-hook", type=float, default=2.5, help="首次偶发拦截等待内置 Hook 自愈的秒数 (默认 2.5s)")
    parser.add_argument("--rotate-window", type=float, default=180.0, help="短时间窗口秒数：在此时间内命中 N 次拦截则切换模型 (默认 180s)")
    parser.add_argument("--rotate-hits", type=int, default=2, help="窗口期内触发模型轮换的拦截次数阈值 (默认 2 次)")
    parser.add_argument("--max-retries", type=int, default=9, help="单轮用户提问最大连续自动恢复次数 (默认 9，够三模型各试一轮)")
    parser.add_argument("--message", type=str, default="", help="自定义固定发送文本（留空则自动发送“接着执行”轮换话术）")
    parser.add_argument("--no-restore-focus", action="store_true", help="发送后保持停留在 Antigravity 窗口")
    parser.add_argument("--retroactive", action="store_true", help="启动时若当前会话正处于被拦截状态，立即恢复一次")
    parser.add_argument("--test-send", action="store_true", help="立即向 Antigravity 测试发送一次“接着执行”并退出")
    parser.add_argument("--test-rotate", action="store_true", help="立即测试在三个模型之间切换一次并退出")
    args = parser.parse_args()

    if args.test_rotate:
        ok, label = rotate_antigravity_model(restore_prev_window=not args.no_restore_focus)
        sys.exit(0 if ok else 1)

    if args.test_send:
        msg = args.message or "接着执行"
        ok = send_resume_to_antigravity(msg, restore_prev_window=not args.no_restore_focus)
        sys.exit(0 if ok else 1)

    print("==========================================================================")
    print("  Antigravity 误拦截自动“接着执行” + 三模型智能轮换守护程序已启动")
    print(f"  监控目录 : {BRAIN_DIR}")
    print(f"  模型轮换 : Carbon <-> Argon <-> Hydrogen ({int(args.rotate_window)}s 内命中 {args.rotate_hits} 次拦截即切换)")
    print(f"  轮询间隔 : {args.interval}s | 单轮最大连续重试 : {args.max_retries} 次")
    print("==========================================================================")

    file_cursors: dict[str, tuple[float, int]] = {}
    handled_steps: dict[str, set[int]] = {}
    retry_counts: dict[str, tuple[int, int]] = {}          # conv_id -> (last_user_step_idx, retries)
    block_history: dict[str, list[tuple[int, float]]] = {}  # conv_id -> [(blocked_step_idx, timestamp)]

    for tfile in iter_recent_transcripts():
        conv_id = tfile.parents[2].name
        st = tfile.stat()
        steps = read_last_steps(tfile)
        handled_steps[conv_id] = set()
        if steps and not args.retroactive:
            for s in steps:
                if is_blocked_step(s):
                    handled_steps[conv_id].add(int(s.get("step_index", -1)))
            file_cursors[conv_id] = (st.st_mtime, st.st_size)
        print(f"[{now_str()}] [监控就绪] 会话 {conv_id[:8]}... (最新 step={steps[-1].get('step_index') if steps else 'N/A'})")

    while True:
        try:
            for tfile in iter_recent_transcripts():
                conv_id = tfile.parents[2].name
                st = tfile.stat()
                sig = (st.st_mtime, st.st_size)
                if file_cursors.get(conv_id) == sig:
                    continue
                file_cursors[conv_id] = sig

                steps = read_last_steps(tfile)
                if not steps:
                    continue

                seen = handled_steps.setdefault(conv_id, set())
                now_ts = time.time()

                # Record any newly appeared blocked steps in sliding window (even if hook quickly added a step right after #1)
                hist = [(idx, ts) for (idx, ts) in block_history.get(conv_id, []) if now_ts - ts <= args.rotate_window]
                recorded_idxs = {idx for idx, _ in hist}
                for s in steps[-8:]:
                    s_idx = int(s.get("step_index", -1))
                    if s_idx not in seen and s_idx not in recorded_idxs and is_blocked_step(s):
                        hist.append((s_idx, now_ts))
                        recorded_idxs.add(s_idx)
                block_history[conv_id] = hist

                last_step = steps[-1]
                last_idx = int(last_step.get("step_index", -1))
                matched_phrase = is_blocked_step(last_step)
                if not matched_phrase or last_idx in seen:
                    continue

                # Find most recent explicit user message step_index (excluding our own auto-sent prompts? No, keep per-session safety cap across pure blocks)
                user_step_idx = -1
                for s in reversed(steps):
                    if s.get("source") == "USER_EXPLICIT" and s.get("type") == "USER_INPUT":
                        txt = str(s.get("content") or "")
                        if not any(rp in txt for rp in RESUME_PROMPTS):
                            user_step_idx = int(s.get("step_index", -1))
                            break

                prev_user_idx, used_retries = retry_counts.get(conv_id, (-999, 0))
                if prev_user_idx != user_step_idx:
                    used_retries = 0

                if used_retries >= args.max_retries:
                    seen.add(last_idx)
                    print(f"[{now_str()}] [!] 会话 {conv_id[:8]} 已连续自动恢复 {used_retries} 次达上限，暂停自动发送。")
                    continue

                window_hits = len(block_history.get(conv_id, []))
                need_rotate = window_hits >= args.rotate_hits

                print(
                    f"\n[{now_str()}] [检测到拦截!] 会话 {conv_id[:8]} | step={last_idx} | "
                    f"{int(args.rotate_window)}s 内累计拦截: {window_hits}/{args.rotate_hits} 次"
                )

                if need_rotate:
                    print(f"[{now_str()}] [⚡ 达到二次拦截阈值] 立即触发模型轮换 (Carbon -> Argon -> Hydrogen)...")
                    seen.add(last_idx)
                    prev_fg_saved = user32.GetForegroundWindow()
                    rotated_ok, new_model = rotate_antigravity_model(
                        steps_hint=steps,
                        restore_prev_window=False,
                        conv_id=conv_id,
                    )
                    # Clear window counter after rotating so the new model gets a fresh 2-strike budget
                    block_history[conv_id] = []
                    time.sleep(0.25)
                else:
                    print(f"[{now_str()}] [*] 第 1 次拦截，等待 {args.wait_hook}s 观察内置 Hook 是否已自动续跑...")
                    time.sleep(args.wait_hook)
                    steps_after = read_last_steps(tfile)
                    if steps_after and int(steps_after[-1].get("step_index", -1)) > last_idx:
                        seen.add(last_idx)
                        print(f"[{now_str()}] [✓] 内置 Hook 已自动恢复会话 (新 step={steps_after[-1].get('step_index')})。")
                        continue
                    seen.add(last_idx)
                    prev_fg_saved = None

                msg = args.message if args.message else RESUME_PROMPTS[used_retries % len(RESUME_PROMPTS)]
                retry_counts[conv_id] = (user_step_idx, used_retries + 1)

                sent = send_resume_to_antigravity(
                    msg,
                    bottom_offset_px=110,
                    restore_prev_window=(not args.no_restore_focus and prev_fg_saved is None),
                    conv_id=conv_id,
                )
                if prev_fg_saved and not args.no_restore_focus and user32.IsWindowVisible(prev_fg_saved):
                    wins = find_antigravity_windows(conv_id)
                    agy_hwnd = wins[0][0] if wins else 0
                    if prev_fg_saved != agy_hwnd:
                        time.sleep(0.1)
                        user32.SetForegroundWindow(prev_fg_saved)

                if sent:
                    time.sleep(3.0)
                    steps_verify = read_last_steps(tfile)
                    if steps_verify and int(steps_verify[-1].get("step_index", -1)) > last_idx:
                        print(f"[{now_str()}] [✓] 确认成功！会话 {conv_id[:8]} 已继续执行 (累计续跑 {used_retries + 1}/{args.max_retries})。")
                    else:
                        print(f"[{now_str()}] [*] 消息已投递（若当前仍在运行则已进入排队队列）。")

            time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n[{now_str()}] [*] 已手动停止监控。")
            break
        except Exception as e:
            print(f"[{now_str()}] [!] 监控循环异常: {e}")
            time.sleep(2.0)


if __name__ == "__main__":
    main()
