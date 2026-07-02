import argparse
import ctypes
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import struct
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, Canvas, DoubleVar, Frame, IntVar, Label, PanedWindow, StringVar, Tk, Toplevel, filedialog, messagebox, ttk
from tkinter import TclError
import tkinter.font as tkfont
from ctypes import wintypes

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk

from .core import Detector, capture_region, config_regions, list_monitors, parse_scales, save_rgb


APP_NAME = "ScreenWatchOCR"
APP_DIR = Path(__file__).resolve().parents[2]
LEGACY_DATA_DIR = APP_DIR / "app_data"


def user_data_dir():
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


DATA_DIR = user_data_dir()
STATE_PATH = DATA_DIR / "state.json"
PROFILES_DIR = DATA_DIR / "profiles"
THUMBS_DIR = DATA_DIR / "thumbs"
ALERTS_DIR = DATA_DIR / "screenshots"
PROFILE_COUNT = 5
TEMPLATE_NAME_RE = re.compile(r"^(\d+)-(\d+)-(.+)$")
STARTUP_LINK_NAME = "屏幕监控OCR.lnk"
MAX_WINDOW_ROWS = 30
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
PW_RENDERFULLCONTENT = 0x00000002
GW_OWNER = 4
GWL_EXSTYLE = -20
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
DWM_TNP_RECTDESTINATION = 0x1
DWM_TNP_OPACITY = 0x4
DWM_TNP_VISIBLE = 0x8
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
PREVIEW_W = 340
PREVIEW_H = 200
SCREEN_PREVIEW_SECONDS = 0.25
SOURCE_PREVIEW_SYNC_MS = 250
DWM_PREVIEW_SYNC_MS = 33
MIN_SCAN_INTERVAL_MS = 120
VK_LBUTTON = 0x01
INSTANCE_HOST = "127.0.0.1"
INSTANCE_PORT = 47627
INSTANCE_COMMAND = b"ScreenWatchOCR:show\n"
INSTANCE_ACK = b"ok\n"


def enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


enable_dpi_awareness()


def notify_existing_instance(timeout=0.5):
    try:
        with socket.create_connection((INSTANCE_HOST, INSTANCE_PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(INSTANCE_COMMAND)
            return sock.recv(len(INSTANCE_ACK)) == INSTANCE_ACK
    except OSError:
        return False


def claim_single_instance():
    if notify_existing_instance():
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((INSTANCE_HOST, INSTANCE_PORT))
        sock.listen(5)
        sock.settimeout(0.5)
        return sock
    except OSError:
        sock.close()
        return False


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


class WindowPlacement(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


class Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_int), ("cy", ctypes.c_int)]


class DwmThumbnailProperties(ctypes.Structure):
    _fields_ = [
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", wintypes.RECT),
        ("rcSource", wintypes.RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    ]


_WINAPI_READY = False


def configure_winapi():
    global _WINAPI_READY
    if _WINAPI_READY or os.name != "nt":
        return
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WindowPlacement)]
    user32.GetWindowPlacement.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(BitmapInfo), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    try:
        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.HRESULT
        dwmapi.DwmRegisterThumbnail.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.HANDLE)]
        dwmapi.DwmRegisterThumbnail.restype = ctypes.HRESULT
        dwmapi.DwmUnregisterThumbnail.argtypes = [wintypes.HANDLE]
        dwmapi.DwmUnregisterThumbnail.restype = ctypes.HRESULT
        dwmapi.DwmUpdateThumbnailProperties.argtypes = [wintypes.HANDLE, ctypes.POINTER(DwmThumbnailProperties)]
        dwmapi.DwmUpdateThumbnailProperties.restype = ctypes.HRESULT
        dwmapi.DwmQueryThumbnailSourceSize.argtypes = [wintypes.HANDLE, ctypes.POINTER(Size)]
        dwmapi.DwmQueryThumbnailSourceSize.restype = ctypes.HRESULT
    except Exception:
        pass
    _WINAPI_READY = True


def startup_dir():
    return Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_link_path():
    return startup_dir() / STARTUP_LINK_NAME


def app_target_path():
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    vbs = APP_DIR / "ScreenWatchOCR.vbs"
    return vbs if vbs.exists() else Path(sys.executable)


def subprocess_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def shortcut_target(path):
    if os.name != "nt" or not Path(path).exists():
        return ""
    script = f"$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut({ps_quote(path)}); Write-Output $l.TargetPath"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        creationflags=subprocess_flags(),
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def is_startup_enabled(target=None):
    link = startup_link_path()
    target = Path(target or app_target_path())
    try:
        return Path(shortcut_target(link)).resolve() == target.resolve()
    except Exception:
        return False


def set_startup_enabled(enabled, target=None):
    if os.name != "nt":
        raise RuntimeError("开机自启目前只支持 Windows。")
    link = startup_link_path()
    target = Path(target or app_target_path()).resolve()
    if enabled:
        link.parent.mkdir(parents=True, exist_ok=True)
        script = (
            "$s=New-Object -ComObject WScript.Shell; "
            f"$l=$s.CreateShortcut({ps_quote(link)}); "
            f"$l.TargetPath={ps_quote(target)}; "
            "$l.Arguments=''; "
            f"$l.WorkingDirectory={ps_quote(target.parent)}; "
            "$l.Save()"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            creationflags=subprocess_flags(),
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "创建开机自启快捷方式失败")
        if not is_startup_enabled(target):
            raise RuntimeError("开机自启快捷方式验证失败")
    elif link.exists() and is_startup_enabled(target):
        link.unlink()
    return is_startup_enabled(target)


def migrate_legacy_data():
    if not LEGACY_DATA_DIR.exists():
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("profiles", "templates", "thumbs"):
            src = LEGACY_DATA_DIR / name
            if src.exists():
                shutil.copytree(src, DATA_DIR / name, dirs_exist_ok=True)
        for name in ("state.json", "alerts.jsonl"):
            src = LEGACY_DATA_DIR / name
            if src.exists():
                shutil.copy2(src, DATA_DIR / name)
        for name in ("alerts", "screenshots"):
            src = LEGACY_DATA_DIR / name
            if src.exists():
                shutil.copytree(src, ALERTS_DIR, dirs_exist_ok=True)
    except Exception:
        pass


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:70] or "target"


def is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except Exception:
        return False


def parse_positive_int(value, name):
    number = int(value)
    if number < 1:
        raise ValueError(f"{name} must be >= 1")
    return number


def scan_interval_ms(value):
    return max(MIN_SCAN_INTERVAL_MS, parse_positive_int(value, "interval_ms"))


def parse_positive_float(value, name):
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} must be > 0")
    return number


def parse_volume(value):
    return max(0, min(100, int(value)))


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def template_stamp():
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


def template_name(profile, count, stamp=None):
    return f"{int(profile)}-{int(count)}-{stamp or template_stamp()}"


def template_suffix(name):
    match = TEMPLATE_NAME_RE.match(Path(str(name)).stem)
    return match.group(3) if match else template_stamp()


def available_template_name(profile, count, suffix, current_path=None, current_thumb=None):
    target_dir = DATA_DIR / "templates"
    current_path = Path(current_path).resolve() if current_path else None
    current_thumb = Path(current_thumb).resolve() if current_thumb else None
    while True:
        stem = template_name(profile, count, suffix)
        path = (target_dir / f"{stem}.png").resolve()
        thumb = (THUMBS_DIR / f"{stem}.png").resolve()
        path_ok = not path.exists() or (current_path and path == current_path)
        thumb_ok = not thumb.exists() or (current_thumb and thumb == current_thumb)
        if path_ok and thumb_ok:
            return stem
        suffix = template_stamp()


def save_template(image, profile, count, stamp=None):
    target_dir = DATA_DIR / "templates"
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = available_template_name(profile, count, stamp or template_stamp())
    path = target_dir / f"{stem}.png"
    image.convert("RGB").save(path)
    return path


def save_thumb(image, path):
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = THUMBS_DIR / Path(path).name
    thumb = image.convert("RGB")
    thumb.thumbnail((480, 320))
    thumb.save(thumb_path)
    return thumb_path


def ensure_thumb(target):
    thumb = target.get("thumb")
    if thumb and Path(thumb).exists():
        return target
    path = Path(target["path"])
    if path.exists():
        target = dict(target)
        with Image.open(path) as image:
            target["thumb"] = str(save_thumb(image, path))
    return target


def delete_target_files(target):
    removed = 0
    for key, parent in (("path", DATA_DIR / "templates"), ("thumb", THUMBS_DIR)):
        path = target.get(key)
        if path and is_under(path, parent):
            try:
                Path(path).unlink(missing_ok=True)
                removed += 1
            except TypeError:
                p = Path(path)
                if p.exists():
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def rename_target(target, profile, count):
    path = Path(target.get("path", ""))
    if not path.exists() or not is_under(path, DATA_DIR / "templates"):
        return target, False
    old_name = target.get("name")
    thumb = Path(target.get("thumb", ""))
    old_path = path.resolve()
    old_thumb = thumb.resolve() if thumb.exists() and is_under(thumb, THUMBS_DIR) else None
    suffix = template_suffix(path.name)
    stem = available_template_name(profile, count, suffix, old_path, old_thumb)
    new_path = DATA_DIR / "templates" / f"{stem}.png"
    new_thumb = THUMBS_DIR / f"{stem}.png"
    changed = path.resolve() != new_path.resolve()
    target = dict(target)
    if changed:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        path.rename(new_path)
        target["path"] = str(new_path)
    else:
        target["path"] = str(path)
    if old_thumb:
        if old_thumb != new_thumb.resolve():
            new_thumb.parent.mkdir(parents=True, exist_ok=True)
            old_thumb.rename(new_thumb)
            changed = True
        target["thumb"] = str(new_thumb)
    else:
        with Image.open(new_path) as image:
            target["thumb"] = str(save_thumb(image, new_path))
        changed = True
    target["name"] = stem
    return target, changed or old_name != stem


def normalize_target_names(targets, profile):
    changed = False
    renamed = []
    for index, target in enumerate(targets, 1):
        updated, did_change = rename_target(target, profile, index)
        renamed.append(updated)
        changed = changed or did_change
    return renamed, changed


def normalize_profile_file(number):
    path = PROFILES_DIR / f"profile_{number}.json"
    data = load_json(path, {})
    if not data:
        return False
    targets = [ensure_thumb(t) for t in data.get("targets", []) if Path(t.get("path", "")).exists()]
    targets, changed = normalize_target_names(targets, number)
    if changed or len(targets) != len(data.get("targets", [])):
        data["targets"] = targets
        write_json(path, data)
        return True
    return False


def normalize_saved_profiles():
    return sum(1 for number in range(1, PROFILE_COUNT + 1) if normalize_profile_file(number))


def prune_alerts(path, max_count):
    max_count = max(1, int(max_count))
    files = sorted(Path(path).glob("*.png"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in files[:-max_count]:
        old.unlink()
    return max(0, len(files) - max_count)


def read_clipboard_images():
    data = ImageGrab.grabclipboard()
    if isinstance(data, Image.Image):
        return [("clipboard", data)]
    images = []
    for item in data or []:
        path = Path(item)
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
            images.append((path.stem, Image.open(path)))
    return images


def window_key(title, ordinal=1):
    return f"{title}\0{int(ordinal)}"


def window_display(title, ordinal=1, duplicate=False):
    return f"{title} #{ordinal}" if duplicate else title


def app_from_legacy(value):
    if isinstance(value, dict):
        return {"title": value.get("title", ""), "ordinal": int(value.get("ordinal", 1) or 1)}
    return {"title": str(value), "ordinal": 1}


def window_is_cloaked(hwnd):
    try:
        cloaked = ctypes.c_int(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return result == 0 and bool(cloaked.value)
    except Exception:
        return False


def window_class(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, len(buf))
        return buf.value
    except Exception:
        return ""


def list_app_windows():
    if os.name != "nt":
        return []
    configure_winapi()
    user32 = ctypes.windll.user32
    windows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        ex_style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
        owner = user32.GetWindow(hwnd, GW_OWNER)
        if owner and not (ex_style & WS_EX_APPWINDOW):
            return True
        if ex_style & WS_EX_TOOLWINDOW:
            return True
        if window_is_cloaked(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        text = title.value.strip()
        if not text or text in {APP_NAME, "Screen Watch OCR", "Program Manager"}:
            return True
        if window_class(hwnd) in {"Windows.UI.Core.CoreWindow", "ApplicationFrameInputSinkWindow"}:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 40 or height < 40:
            return True
        windows.append({"hwnd": int(hwnd), "title": text, "width": width, "height": height})
        return True

    user32.EnumWindows(enum_proc, 0)
    seen = set()
    out = []
    counts = {}
    title_totals = {}
    for item in windows:
        title_totals[item["title"]] = title_totals.get(item["title"], 0) + 1
    for item in sorted(windows, key=lambda x: (x["title"].lower(), x["hwnd"])):
        counts[item["title"]] = counts.get(item["title"], 0) + 1
        item["ordinal"] = counts[item["title"]]
        item["key"] = window_key(item["title"], item["ordinal"])
        item["display"] = window_display(item["title"], item["ordinal"], title_totals[item["title"]] > 1)
        key = (item["hwnd"], item["title"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out[:MAX_WINDOW_ROWS]


def window_rect(hwnd):
    configure_winapi()
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if user32.IsIconic(hwnd):
        placement = WindowPlacement()
        placement.length = ctypes.sizeof(WindowPlacement)
        if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            rect = placement.rcNormalPosition
        elif not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    elif ctypes.windll.dwmapi.DwmGetWindowAttribute(int(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 2 or height < 2:
        return None
    return rect, width, height


def capture_window(hwnd):
    if os.name != "nt":
        return None
    configure_winapi()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    info = window_rect(int(hwnd))
    if not info:
        return None
    _rect, width, height = info
    hwnd_dc = user32.GetWindowDC(int(hwnd))
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old_obj = gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = user32.PrintWindow(int(hwnd), mem_dc, PW_RENDERFULLCONTENT)
        if not ok:
            gdi32.BitBlt(mem_dc, 0, 0, width, height, hwnd_dc, 0, 0, SRCCOPY)
        bmi = BitmapInfo()
        bmi.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        rows = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), DIB_RGB_COLORS)
        if rows != height:
            return None
        bgra = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4))
        return bgra[:, :, [2, 1, 0]].copy()
    except Exception:
        return None
    finally:
        if old_obj:
            gdi32.SelectObject(mem_dc, old_obj)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(int(hwnd), hwnd_dc)


def mostly_black(frame):
    return frame is None or float(np.mean(frame)) < 8


def black_fraction(frame, threshold=8):
    if frame is None:
        return 1.0
    return float(np.mean(np.max(frame, axis=2) < threshold))


def crop_black_padding(frame, threshold=8):
    if frame is None or black_fraction(frame, threshold) < 0.25:
        return frame
    mask = np.max(frame, axis=2) >= threshold
    if not np.any(mask):
        return frame
    rows, cols = np.where(mask)
    top, bottom = int(rows.min()), int(rows.max()) + 1
    left, right = int(cols.min()), int(cols.max()) + 1
    height, width = frame.shape[:2]
    if left <= width * 0.05 and top <= height * 0.05 and right >= width * 0.35 and bottom >= height * 0.35 and (right < width * 0.9 or bottom < height * 0.9):
        return frame[top:bottom, left:right].copy()
    return frame


def capture_window_visible(sct, hwnd):
    info = window_rect(hwnd)
    if not info:
        return None
    rect, width, height = info
    shot = sct.grab({"left": rect.left, "top": rect.top, "width": width, "height": height})
    return np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)


def capture_window_frame(sct, hwnd, mode_cache=None):
    mode_key = int(hwnd)
    if mode_cache and mode_cache.get(mode_key) == "visible":
        visible = capture_window_visible(sct, hwnd)
        if visible is not None and not mostly_black(visible):
            return visible
        mode_cache.pop(mode_key, None)

    frame = capture_window(hwnd)
    if not mostly_black(frame):
        frame = crop_black_padding(frame)
    if not mostly_black(frame) and black_fraction(frame) < 0.25:
        return frame
    visible = capture_window_visible(sct, hwnd)
    if visible is not None and not mostly_black(visible):
        if frame is None or mostly_black(frame) or black_fraction(visible) + 0.1 < black_fraction(frame):
            if mode_cache is not None and (frame is None or mostly_black(frame)):
                mode_cache[mode_key] = "visible"
            return visible
    return frame if frame is not None else visible


def capture_window_preview(sct, hwnd):
    visible = capture_window_visible(sct, hwnd)
    if visible is not None and not mostly_black(visible):
        return visible
    return capture_window_frame(sct, hwnd)


def dwm_register(dest_hwnd, source_hwnd):
    if os.name != "nt":
        return None
    configure_winapi()
    try:
        thumb = wintypes.HANDLE()
        if ctypes.windll.dwmapi.DwmRegisterThumbnail(int(dest_hwnd), int(source_hwnd), ctypes.byref(thumb)) != 0:
            return None
        return thumb
    except Exception:
        return None


def dwm_unregister(thumb):
    try:
        if thumb:
            ctypes.windll.dwmapi.DwmUnregisterThumbnail(thumb)
    except Exception:
        pass


def dwm_update(thumb, x, y, width, height, visible=True):
    try:
        props = DwmThumbnailProperties()
        props.dwFlags = DWM_TNP_VISIBLE | DWM_TNP_OPACITY
        props.opacity = 255
        props.fVisible = bool(visible)
        if not visible:
            return ctypes.windll.dwmapi.DwmUpdateThumbnailProperties(thumb, ctypes.byref(props)) == 0
        src = Size()
        ctypes.windll.dwmapi.DwmQueryThumbnailSourceSize(thumb, ctypes.byref(src))
        src_w, src_h = max(1, src.cx), max(1, src.cy)
        scale = min(width / src_w, height / src_h)
        dst_w, dst_h = max(1, int(src_w * scale)), max(1, int(src_h * scale))
        left = int(x + (width - dst_w) // 2)
        top = int(y + (height - dst_h) // 2)
        props.dwFlags |= DWM_TNP_RECTDESTINATION
        props.rcDestination = wintypes.RECT(left, top, left + dst_w, top + dst_h)
        return ctypes.windll.dwmapi.DwmUpdateThumbnailProperties(thumb, ctypes.byref(props)) == 0
    except Exception:
        return False


def hwnd_for_tk(window):
    try:
        return int(window.frame(), 16)
    except Exception:
        try:
            return int(window.winfo_id())
        except Exception:
            return 0


def beep_wave(volume, milliseconds=180, frequency=1200, sample_rate=22050):
    volume = parse_volume(volume)
    frames = int(sample_rate * milliseconds / 1000)
    amplitude = int(32767 * (volume / 100))
    raw = bytearray()
    for i in range(frames):
        sample = int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
        raw.extend(struct.pack("<h", sample))
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(raw)
    return out.getvalue()


def beep_for(seconds, volume=100):
    try:
        import winsound

        deadline = time.time() + float(seconds)
        while time.time() < deadline:
            level = parse_volume(volume() if callable(volume) else volume)
            if level:
                winsound.PlaySound(beep_wave(level), winsound.SND_MEMORY)
            else:
                time.sleep(0.18)
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Watch OCR")
        migrate_legacy_data()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        normalize_saved_profiles()
        self.state = load_json(STATE_PATH, {"last_profile": 1, "layout": {}})
        self.layout = self.state.get("layout", {})
        self.main_ratio = float(self.layout.get("main_ratio", 0.72))
        self.right_ratio = min(0.4, max(0.12, float(self.layout.get("right_ratio", 0.22))))
        self.left_ratio = float(self.layout.get("left_ratio", 0.58))
        self.instance_socket = None
        self.root.geometry(self.layout.get("geometry", "980x680"))
        self.root.minsize(820, 600)
        self.current_profile = int(self.state.get("last_profile", 1))
        self.current_profile = min(PROFILE_COUNT, max(1, self.current_profile))
        self.loading_profile = False
        self.targets = []
        self.thumb_refs = []
        self.target_vars = []
        self.target_cards = {}
        self.target_last_click = (None, 0)
        self.thumb_cache = {}
        self.selected_target = None
        self.thumb_w = 104
        self.thumb_h = 72
        self.last_scale = 1.0
        self.resize_job = None
        self.last_root_size = None
        self.resize_active_until = 0
        self.move_active_until = 0
        self.layout_active_until = 0
        self.layout_restore_job = None
        self.monitor_vars = {}
        self.monitor_info = {}
        self.window_info = {}
        self.window_choices = []
        self.selected_apps = []
        self.window_choice = StringVar(value="选择应用...")
        self.window_refresh_job = None
        self.selected_app_widgets = {}
        self.selected_empty_app_label = None
        self.source_widgets = {}
        self.dwm_thumbs = {}
        self.preview_sources = []
        self.preview_frames = {}
        self.preview_signatures = {}
        self.preview_lock = threading.Lock()
        self.preview_job = None
        self.source_previews_enabled = False
        self.dwm_sync_job = None
        self.worker = None
        self.tray_icon = None
        self.stop_event = threading.Event()
        self.close_event = threading.Event()
        self.beep_lock = threading.Lock()
        self.beep_until = 0
        self.events = queue.Queue()
        self.profile = IntVar(value=self.current_profile)
        self.startup_enabled = BooleanVar(value=is_startup_enabled())
        self.threshold = DoubleVar(value=0.90)
        self.scales = StringVar(value="1.0")
        self.interval_ms = IntVar(value=250)
        self.cooldown = DoubleVar(value=1.0)
        self.beep_seconds = DoubleVar(value=3.0)
        self.beep_volume = IntVar(value=100)
        self.beep_volume_level = 100
        self.beep_volume.trace_add("write", lambda *_args: self.update_beep_volume())
        self.max_templates = IntVar(value=100)
        self.max_alerts = IntVar(value=int(self.state.get("max_alerts", 50)))
        self.beep = BooleanVar(value=True)
        self.left = StringVar(value="0")
        self.top = StringVar(value="0")
        self.width = StringVar(value="")
        self.height = StringVar(value="")
        self.status = StringVar(value="添加图片或 Ctrl+V 粘贴截图，然后开始监控。")
        self.fonts = {name: tkfont.nametofont(name) for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont")}
        self.base_font_sizes = {name: font.cget("size") for name, font in self.fonts.items()}
        self.target_name_font = self.fonts["TkDefaultFont"].copy()
        self.target_name_font.configure(size=max(7, int(self.base_font_sizes["TkDefaultFont"] * 0.82)))
        self.style = ttk.Style()
        self.check_widgets = []
        self._build()
        self.refresh_monitors()
        self.load_profile(self.current_profile)
        self.root.bind_all("<Control-v>", self.handle_paste_hotkey)
        self.root.bind_all("<Control-V>", self.handle_paste_hotkey)
        self.root.bind("<Configure>", self.on_resize)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self.run_preview_worker, daemon=True).start()
        self.root.after(250, self.restore_layout)
        self.root.after(1000, self.refresh_windows_loop)
        self.root.after(100, self.poll_events)
        if self.root.state() != "withdrawn":
            self.ensure_tray_icon(show_errors=False)

    def _build(self):
        self.main_pane = PanedWindow(
            self.root,
            orient="horizontal",
            sashwidth=8,
            sashrelief="raised",
            bd=0,
            opaqueresize=False,
            proxybackground="#1573d1",
            proxyborderwidth=1,
            proxyrelief="flat",
        )
        self.main_pane.pack(fill="both", expand=True, padx=12, pady=12)
        left = ttk.Frame(self.main_pane)
        right_outer = ttk.Frame(self.main_pane, width=300)
        preview_outer = ttk.Frame(self.main_pane, width=380)
        self.right_canvas = Canvas(right_outer, highlightthickness=0)
        right_scroll = ttk.Scrollbar(right_outer, orient="vertical", command=self.right_canvas.yview)
        self.right_canvas.configure(yscrollcommand=right_scroll.set)
        right_scroll.pack(side="right", fill="y")
        self.right_canvas.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(self.right_canvas)
        right_window = self.right_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda _event: self.right_canvas.configure(scrollregion=self.right_canvas.bbox("all")))
        self.right_canvas.bind("<Configure>", lambda event: self.right_canvas.itemconfigure(right_window, width=event.width))
        self.right_canvas.bind("<MouseWheel>", self.scroll_right)
        right.bind("<MouseWheel>", self.scroll_right)
        self.main_pane.add(left, minsize=360, stretch="always")
        self.main_pane.add(right_outer, minsize=260, stretch="never")
        self.main_pane.add(preview_outer, minsize=320, stretch="never")
        self.main_pane.bind("<ButtonPress-1>", self.begin_layout_drag)
        self.main_pane.bind("<B1-Motion>", self.mark_layout_drag)
        self.main_pane.bind("<ButtonRelease-1>", self.end_layout_drag)

        preview_box = ttk.LabelFrame(preview_outer, text="来源预览")
        preview_box.pack(fill="both", expand=True)
        self.source_canvas = Canvas(preview_box, highlightthickness=0)
        source_scroll = ttk.Scrollbar(preview_box, orient="vertical", command=self.source_canvas.yview)
        self.source_canvas.configure(yscrollcommand=source_scroll.set)
        self.source_canvas.pack(side="left", fill="both", expand=True)
        source_scroll.pack(side="right", fill="y")
        self.source_frame = ttk.Frame(self.source_canvas)
        self.source_window = self.source_canvas.create_window((0, 0), window=self.source_frame, anchor="nw")
        self.source_frame.bind("<Configure>", lambda _event: self.source_canvas.configure(scrollregion=self.source_canvas.bbox("all")))
        self.source_canvas.bind("<Configure>", lambda event: self.source_canvas.itemconfigure(self.source_window, width=event.width))
        self.source_canvas.bind("<MouseWheel>", lambda event: self.source_canvas.yview_scroll(int(-event.delta / 120), "units"))

        profile_bar = ttk.Frame(left)
        profile_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(profile_bar, text="配置位").pack(side="left")
        profile_box = ttk.Combobox(profile_bar, textvariable=self.profile, values=list(range(1, PROFILE_COUNT + 1)), width=6, state="readonly")
        profile_box.pack(side="left", padx=8)
        profile_box.bind("<<ComboboxSelected>>", self.switch_profile)
        self.make_check(profile_bar, self.startup_enabled, "开机自启", self.toggle_startup).pack(side="left", padx=6)

        bar = ttk.Frame(left)
        bar.pack(fill="x")
        ttk.Button(bar, text="上传图片", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="粘贴图片", command=self.paste_images).pack(side="left", padx=6)
        ttk.Button(bar, text="截图作模板", command=self.capture_as_target).pack(side="left")
        ttk.Button(bar, text="删除选中", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=self.clear_targets).pack(side="left")

        self.left_pane = PanedWindow(left, orient="vertical", sashwidth=8, sashrelief="raised", bd=0)
        self.left_pane.pack(fill="both", expand=True, pady=(10, 0))
        self.left_pane.bind("<ButtonPress-1>", self.begin_layout_drag)
        self.left_pane.bind("<B1-Motion>", self.mark_layout_drag)
        self.left_pane.bind("<ButtonRelease-1>", self.end_layout_drag)

        gallery_box = ttk.LabelFrame(self.left_pane)
        gallery_label = ttk.Frame(gallery_box)
        ttk.Label(gallery_label, text="匹配图片").pack(side="left")
        self.target_select_btn = ttk.Button(gallery_label, text="全选", width=8, command=self.toggle_all_targets)
        self.target_select_btn.pack(side="left", padx=(8, 0))
        gallery_box.configure(labelwidget=gallery_label)
        self.left_pane.add(gallery_box, minsize=170)
        self.target_canvas = Canvas(gallery_box, highlightthickness=0, height=260)
        self.target_canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(gallery_box, orient="vertical", command=self.target_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.target_canvas.configure(yscrollcommand=scroll.set)
        self.target_canvas.bind("<Button-1>", lambda _event: self.target_canvas.focus_set())
        self.target_canvas.bind("<MouseWheel>", self.scroll_targets)
        self.gallery_inner = ttk.Frame(self.target_canvas)
        self.gallery_window = self.target_canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")
        self.gallery_inner.bind("<Configure>", lambda _event: self.target_canvas.configure(scrollregion=self.target_canvas.bbox("all")))
        self.gallery_inner.bind("<MouseWheel>", self.scroll_targets)
        self.target_canvas.bind("<Configure>", lambda event: self.target_canvas.itemconfigure(self.gallery_window, width=event.width))

        log_box = ttk.LabelFrame(self.left_pane, text="报警与扫描日志")
        self.left_pane.add(log_box, minsize=130)
        self.log = ttk.Treeview(log_box, columns=("time", "message"), show="headings", height=9)
        self.log.heading("time", text="时间")
        self.log.heading("message", text="事件")
        self.log.column("time", width=90, anchor="center")
        self.log.column("message", width=640)
        self.log.pack(fill="both", expand=True)

        monitor_box = ttk.LabelFrame(right, text="监控屏幕")
        monitor_box.pack(fill="x")
        self.monitor_frame = ttk.Frame(monitor_box)
        self.monitor_frame.pack(fill="x", padx=8, pady=8)
        ttk.Button(monitor_box, text="刷新屏幕", command=self.refresh_monitors).pack(fill="x", padx=8, pady=(0, 8))

        app_box = ttk.LabelFrame(right, text="监控应用")
        app_box.pack(fill="x", pady=(10, 0))
        self.window_combo = ttk.Combobox(app_box, textvariable=self.window_choice, state="readonly", values=[], height=24)
        self.window_combo.pack(fill="x", padx=8, pady=(8, 6))
        self.window_combo.bind("<<ComboboxSelected>>", self.toggle_window_choice)
        self.window_combo.bind("<Button-1>", lambda _event: self.refresh_windows())
        self.selected_app_frame = ttk.Frame(app_box)
        self.selected_app_frame.pack(fill="x", padx=8, pady=(0, 8))

        region_box = ttk.LabelFrame(right, text="区域")
        region_box.pack(fill="x", pady=10)
        for label, var in [("左", self.left), ("上", self.top), ("宽(空=全屏)", self.width), ("高(空=全屏)", self.height)]:
            row = ttk.Frame(region_box)
            row.pack(fill="x", padx=8, pady=3)
            row.columnconfigure(1, weight=1)
            ttk.Label(row, text=label, width=11).grid(row=0, column=0, sticky="w")
            self.make_entry(row, var).grid(row=0, column=1, sticky="ew")

        match_box = ttk.LabelFrame(right, text="匹配")
        match_box.pack(fill="x")
        for label, var in [("阈值", self.threshold), ("缩放", self.scales), ("间隔ms", self.interval_ms), ("同图冷却秒", self.cooldown), ("蜂鸣秒", self.beep_seconds), ("蜂鸣音量", self.beep_volume), ("模板最多张", self.max_templates), ("截图最多张", self.max_alerts)]:
            row = ttk.Frame(match_box)
            row.pack(fill="x", padx=8, pady=3)
            row.columnconfigure(1, weight=1)
            ttk.Label(row, text=label, width=11).grid(row=0, column=0, sticky="w")
            self.make_entry(row, var).grid(row=0, column=1, sticky="ew")
        self.make_check(match_box, self.beep, "命中蜂鸣").pack(anchor="w", padx=8, pady=4)

        actions = ttk.LabelFrame(right, text="运行")
        actions.pack(fill="x", pady=10)
        self.start_btn = ttk.Button(actions, text="开始监控", command=self.toggle_monitoring)
        self.start_btn.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(actions, text="扫描一次", command=self.scan_once).pack(fill="x", padx=8, pady=4)
        ttk.Button(actions, text="打开证据目录", command=self.open_evidence).pack(fill="x", padx=8, pady=(4, 8))

        ttk.Label(right, textvariable=self.status, wraplength=300).pack(fill="x", pady=8)
        self.bind_mousewheel(right, self.scroll_right)

    def make_entry(self, parent, var):
        entry = ttk.Entry(parent, textvariable=var, justify="left", width=8)
        entry.bind("<FocusIn>", self.focus_entry_end)
        for sequence in ("<ButtonPress-1>", "<Button-1>", "<ButtonRelease-1>", "<B1-Motion>", "<Double-Button-1>"):
            entry.bind(sequence, self.click_entry_end)
        return entry

    def focus_entry_end(self, event):
        self.entry_cursor_end(event.widget)
        return None

    def click_entry_end(self, event):
        try:
            event.widget.focus_set()
        except TclError:
            pass
        self.entry_cursor_end(event.widget)
        return "break"

    def entry_cursor_end(self, widget):
        def apply():
            try:
                widget.selection_clear()
                widget.icursor("end")
                widget.xview_moveto(1)
            except TclError:
                pass

        apply()
        widget.after_idle(apply)
        for delay in (1, 10, 50, 150):
            widget.after(delay, apply)

    def make_check(self, parent, var, label, command=None):
        try:
            bg = parent.cget("bg")
        except TclError:
            bg = "SystemButtonFace"
        frame = Frame(parent, bg=bg, cursor="hand2")
        box = Canvas(frame, highlightthickness=0, bd=0, bg=bg)
        text = Label(frame, text=label, font=self.fonts["TkDefaultFont"], bg=bg, anchor="w", cursor="hand2")
        box.pack(side="left", padx=(0, 4))
        text.pack(side="left", fill="x", expand=True)

        def draw(*_args):
            size = max(12, int(13 * self.last_scale))
            box.configure(width=size, height=size)
            box.delete("all")
            box.create_rectangle(1, 1, size - 2, size - 2, outline="#666666", fill="#ffffff")
            if var.get():
                box.create_line(size * 0.22, size * 0.52, size * 0.42, size * 0.72, size * 0.78, size * 0.28, width=max(2, int(2 * self.last_scale)), fill="#1573d1")

        def toggle(_event=None):
            var.set(not var.get())
            draw()
            if command:
                command()
            return "break"

        for widget in (frame, box, text):
            widget.bind("<Button-1>", toggle)
        if hasattr(self, "right_canvas") and self.is_descendant(parent, self.right_canvas):
            for widget in (frame, box, text):
                widget.bind("<MouseWheel>", self.scroll_right)
        trace_id = var.trace_add("write", draw)
        frame._check_parts = (box, var, trace_id, draw)
        self.check_widgets.append(frame)
        draw()
        return frame

    def redraw_checks(self):
        for widget in list(self.check_widgets):
            try:
                widget._check_parts[3]()
            except Exception:
                pass

    def bind_mousewheel(self, widget, handler):
        widget.bind("<MouseWheel>", handler)
        for child in widget.winfo_children():
            self.bind_mousewheel(child, handler)

    def is_descendant(self, widget, parent):
        while widget:
            if widget == parent:
                return True
            widget = getattr(widget, "master", None)
        return False

    def update_beep_volume(self):
        self.beep_volume_level = parse_volume(self.beep_volume.get())

    def scroll_targets(self, event):
        self.target_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def scroll_right(self, event):
        self.right_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def begin_layout_drag(self, _event=None):
        self.layout_active_until = time.time() + 0.8
        self.suspend_dwm_previews()

    def mark_layout_drag(self, _event=None):
        self.layout_active_until = time.time() + 0.8

    def end_layout_drag(self, _event=None):
        self.layout_active_until = 0
        self.save_state()
        self.schedule_source_previews(0)

    def layout_busy(self):
        active_until = max(getattr(self, "resize_active_until", 0), getattr(self, "layout_active_until", 0), getattr(self, "move_active_until", 0))
        return self.mouse_button_down() or time.time() < active_until

    def mouse_button_down(self):
        if os.name != "nt":
            return False
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        except Exception:
            return False

    def refresh_monitors(self):
        had_monitors = bool(self.monitor_vars)
        selected = {i for i, var in self.monitor_vars.items() if var.get()}
        for child in self.monitor_frame.winfo_children():
            child.destroy()
        self.monitor_vars.clear()
        monitors = [m for m in list_monitors() if m["index"] != 0]
        self.monitor_info = {m["index"]: m for m in monitors}
        for i, monitor in enumerate(monitors):
            var = BooleanVar(value=(monitor["index"] in selected) if selected or had_monitors else i == 0)
            self.monitor_vars[monitor["index"]] = var
            text = f"{monitor['index']}: {monitor['width']}x{monitor['height']} ({monitor['left']},{monitor['top']})"
            self.make_check(self.monitor_frame, var, text).pack(anchor="w", pady=2)
        self.status.set(f"检测到 {len(monitors)} 个物理屏。")

    def refresh_windows(self):
        self.window_choices = list_app_windows()
        self.window_info = {item["key"]: item for item in self.window_choices}
        selected = {self.app_key(app) for app in self.selected_apps}
        values = [("✓ " if item["key"] in selected else "") + item["display"] for item in self.window_choices]
        self.window_combo.configure(values=values)
        self.window_choice.set("选择应用...")
        self.reload_selected_apps()
        if self.window_choices:
            self.status.set(f"检测到 {len(self.window_choices)} 个可选择应用窗口。")

    def refresh_windows_loop(self):
        if not self.layout_busy():
            self.refresh_windows()
        self.window_refresh_job = self.root.after(2000, self.refresh_windows_loop)

    def app_key(self, app):
        return window_key(app.get("title", ""), app.get("ordinal", 1))

    def add_selected_app(self, item):
        app = {"title": item["title"], "ordinal": item.get("ordinal", 1)}
        if self.app_key(app) not in {self.app_key(x) for x in self.selected_apps}:
            self.selected_apps.append(app)
        self.refresh_windows()
        self.save_current_profile()

    def remove_selected_app(self, app):
        key = self.app_key(app)
        self.selected_apps = [item for item in self.selected_apps if self.app_key(item) != key]
        self.refresh_windows()
        self.save_current_profile()

    def toggle_window_choice(self, _event=None):
        value = self.window_choice.get()
        index = next((i for i, item in enumerate(self.window_choices) if value == item["display"] or value == "✓ " + item["display"]), None)
        if index is None:
            self.window_choice.set("选择应用...")
            return
        item = self.window_choices[index]
        key = item["key"]
        selected = {self.app_key(app) for app in self.selected_apps}
        if key in selected:
            self.remove_selected_app({"title": item["title"], "ordinal": item["ordinal"]})
        else:
            self.add_selected_app(item)
        self.window_choice.set("选择应用...")

    def reload_selected_apps(self):
        widgets = getattr(self, "selected_app_widgets", {})
        if not self.selected_apps:
            for record in list(widgets.values()):
                record["row"].destroy()
            widgets.clear()
            if not getattr(self, "selected_empty_app_label", None):
                self.selected_empty_app_label = ttk.Label(self.selected_app_frame, text="未选择应用")
                self.selected_empty_app_label.pack(anchor="w")
                self.selected_empty_app_label.bind("<MouseWheel>", self.scroll_right)
            return
        if getattr(self, "selected_empty_app_label", None):
            self.selected_empty_app_label.destroy()
            self.selected_empty_app_label = None
        selected_keys = [self.app_key(app) for app in self.selected_apps]
        for key in list(widgets):
            if key not in selected_keys:
                widgets[key]["row"].destroy()
                del widgets[key]
        for app in self.selected_apps:
            key = self.app_key(app)
            info = self.window_info.get(key)
            title = info["display"] if info else window_display(app["title"], app.get("ordinal", 1), app.get("ordinal", 1) > 1)
            text = title if info else f"{title}（未启动）"
            record = widgets.get(key)
            if not record:
                row = ttk.Frame(self.selected_app_frame)
                row.pack(fill="x", pady=2)
                button = ttk.Button(row, text="×", width=3, command=lambda a=app: self.remove_selected_app(a))
                label = ttk.Label(row)
                button.pack(side="left")
                label.pack(side="left", padx=4)
                for widget in (row, button, label):
                    widget.bind("<MouseWheel>", self.scroll_right)
                record = widgets[key] = {"row": row, "label": label}
            if record.get("text") != text:
                record["label"].configure(text=text)
                record["text"] = text

    def handle_paste_hotkey(self, _event=None):
        widget = self.root.focus_get()
        if widget:
            try:
                if widget.winfo_class() in {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}:
                    return None
            except TclError:
                pass
        self.paste_images()
        return "break"

    def add_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp")])
        for path in paths:
            with Image.open(path) as image:
                self.add_image(Path(path).stem, image)

    def paste_images(self):
        images = read_clipboard_images()
        if not images:
            self.status.set("剪贴板里没有图片；用截图工具复制后再按 Ctrl+V。")
            return
        for name, image in images:
            try:
                self.add_image(name, image)
            finally:
                try:
                    image.close()
                except Exception:
                    pass

    def capture_as_target(self):
        try:
            monitor = next(i for i, var in self.monitor_vars.items() if var.get())
            from mss import mss

            with mss() as sct:
                monitors = [{"index": i, **m} for i, m in enumerate(sct.monitors)]
                region = config_regions({"regions": [self.region_for(monitor)]}, monitors)[0]
                frame = capture_region(sct, region)
            self.add_image(f"capture-monitor-{monitor}", Image.fromarray(frame))
        except Exception as exc:
            messagebox.showerror("截图失败", str(exc))

    def add_image(self, name, image):
        max_templates = parse_positive_int(self.max_templates.get(), "max_templates")
        self.prune_targets(max_templates - 1)
        self.normalize_targets()
        path = save_template(image, self.current_profile, len(self.targets) + 1)
        thumb = save_thumb(image, path)
        with Image.open(path) as saved:
            width, height = saved.size
        self.targets.append({"name": path.stem, "path": str(path), "thumb": str(thumb), "size": f"{width}x{height}", "enabled": True})
        self.selected_target = len(self.targets) - 1
        self.reload_target_list()
        self.save_current_profile()
        self.status.set(f"已添加 {len(self.targets)} 张模板。")

    def normalize_targets(self):
        profile = getattr(self, "current_profile", int(self.profile.get()) if hasattr(self, "profile") else 1)
        self.targets, changed = normalize_target_names(self.targets, profile)
        return changed

    def prune_targets(self, keep_count):
        keep_count = max(0, int(keep_count))
        if len(self.targets) <= keep_count:
            return 0
        removed = self.targets[: len(self.targets) - keep_count]
        self.targets = self.targets[-keep_count:] if keep_count else []
        for target in removed:
            delete_target_files(target)
        return len(removed)

    def make_thumb(self, target):
        path = target.get("thumb") or target["path"]
        mtime = Path(path).stat().st_mtime if Path(path).exists() else 0
        key = (str(path), mtime, self.thumb_w, self.thumb_h)
        if key in self.thumb_cache:
            return self.thumb_cache[key]
        with Image.open(path) as opened:
            img = opened.convert("RGB")
        img.thumbnail((self.thumb_w, self.thumb_h))
        canvas = Image.new("RGB", (self.thumb_w, self.thumb_h), (245, 245, 245))
        canvas.paste(img, ((self.thumb_w - img.width) // 2, (self.thumb_h - img.height) // 2))
        self.thumb_cache[key] = ImageTk.PhotoImage(canvas)
        return self.thumb_cache[key]

    def select_target(self, index):
        old = getattr(self, "selected_target", None)
        self.selected_target = index
        self.update_target_selection(old)
        self.status.set(f"已选中 {Path(self.targets[index]['path']).name}")

    def click_target(self, index):
        last_index, last_time = getattr(self, "target_last_click", (None, 0))
        now = time.monotonic()
        self.select_target(index)
        if last_index == index and now - last_time <= 0.5:
            self.target_last_click = (None, 0)
            self.open_target_file(index)
        else:
            self.target_last_click = (index, now)
        return "break"

    def open_target_file(self, index):
        path = Path(self.targets[index]["path"])
        try:
            if path.exists():
                os.startfile(path)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def update_target_selection(self, old=None):
        for index in {old, self.selected_target}:
            if index is None:
                continue
            widgets = getattr(self, "target_cards", {}).get(index)
            if not widgets:
                continue
            bg = "#dbeafe" if index == self.selected_target else "#ffffff"
            for widget in widgets:
                widget.configure(bg=bg)

    def toggle_target(self, index, var):
        self.targets[index]["enabled"] = bool(var.get())
        self.save_current_profile()
        self.update_target_select_button()
        self.status.set(f"当前 {len(self.targets)} 张模板，启用 {len(self.enabled_targets())} 张。")

    def update_target_select_button(self):
        if hasattr(self, "target_select_btn"):
            all_selected = bool(self.targets) and all(t.get("enabled", True) for t in self.targets)
            self.target_select_btn.configure(text="反选" if all_selected else "全选")

    def toggle_all_targets(self):
        selected = bool(self.targets) and all(t.get("enabled", True) for t in self.targets)
        for target in self.targets:
            target["enabled"] = not selected
        self.reload_target_list()
        self.save_current_profile()

    def remove_selected(self):
        if self.selected_target is not None and self.selected_target < len(self.targets):
            delete_target_files(self.targets.pop(self.selected_target))
            self.selected_target = None
            self.normalize_targets()
        self.reload_target_list()
        self.save_current_profile()

    def clear_targets(self):
        for target in self.targets:
            delete_target_files(target)
        self.targets.clear()
        self.selected_target = None
        self.reload_target_list()
        self.save_current_profile()

    def selected_windows(self):
        out = []
        for app in self.selected_apps:
            item = self.window_info.get(self.app_key(app))
            if item:
                out.append({"name": f"app-{safe_name(item['display'])}", "title": item["title"], "display": item["display"], "hwnd": item["hwnd"], "key": item["key"]})
        return out

    def preview_screen_source(self, monitor):
        source = dict(monitor)
        try:
            base = self.monitor_info[int(monitor.get("monitor", 1))]
            box = {
                "left": int(base["left"]) + int(monitor.get("left", 0)),
                "top": int(base["top"]) + int(monitor.get("top", 0)),
                "width": int(monitor.get("width", base["width"])),
                "height": int(monitor.get("height", base["height"])),
            }
            source.update({"_abs_left": box["left"], "_abs_top": box["top"], "width": box["width"], "height": box["height"]})
        except Exception:
            pass
        return source

    def refresh_source_previews(self):
        self.preview_job = None
        try:
            if not getattr(self, "source_previews_enabled", True):
                return
            layout_busy = self.layout_busy()
            if layout_busy:
                return
            sources = []
            for monitor in self.selected_regions():
                sources.append({"key": f"screen:{monitor['name']}", "kind": "screen", "name": monitor["name"], "source": self.preview_screen_source(monitor), "available": True})
            for app in self.selected_apps:
                item = self.window_info.get(self.app_key(app))
                name = item["display"] if item else window_display(app["title"], app.get("ordinal", 1), app.get("ordinal", 1) > 1)
                sources.append({"key": f"app:{self.app_key(app)}", "kind": "window", "name": name, "source": item, "available": bool(item), "dwm": bool(item and os.name == "nt")})
            for source in sources:
                source["signature"] = self.preview_signature(source)
            source_keys = {item["key"] for item in sources}
            with self.preview_lock:
                self.preview_sources = [dict(item) for item in sources]
            for key in list(self.source_widgets):
                if key not in source_keys:
                    self.unregister_dwm_preview(key)
                    self.source_widgets[key]["frame"].destroy()
                    del self.source_widgets[key]
                    with self.preview_lock:
                        self.preview_frames.pop(key, None)
                        self.preview_signatures.pop(key, None)
            if not sources and "empty" not in self.source_widgets:
                frame = ttk.Frame(self.source_frame)
                frame.pack(fill="x", padx=6, pady=6)
                ttk.Label(frame, text="未选择来源").pack(anchor="w")
                self.source_widgets["empty"] = {"frame": frame}
            elif sources and "empty" in self.source_widgets:
                self.source_widgets["empty"]["frame"].destroy()
                del self.source_widgets["empty"]
            for source in sources:
                key = source["key"]
                with self.preview_lock:
                    if self.preview_signatures.get(key) != source["signature"]:
                        self.preview_frames.pop(key, None)
                        self.preview_signatures.pop(key, None)
                widgets = self.source_widgets.get(key)
                if not widgets:
                    outer = ttk.Frame(self.source_frame)
                    outer.pack(fill="x", padx=6, pady=6)
                    area = Frame(outer, bg="#141414", width=PREVIEW_W, height=PREVIEW_H)
                    area.pack(anchor="w")
                    area.pack_propagate(False)
                    image_label = Label(area, bg="#141414")
                    image_label.place(x=0, y=0, width=PREVIEW_W, height=PREVIEW_H)
                    name_label = ttk.Label(outer, wraplength=260)
                    name_label.pack(anchor="w", pady=(2, 0))
                    self.source_widgets[key] = {"frame": outer, "area": area, "image": image_label, "name": name_label}
                    widgets = self.source_widgets[key]
                available_w = max(PREVIEW_W, self.source_canvas.winfo_width() - 12)
                preview_w = min(560, available_w)
                preview_h = self.preview_height(source, preview_w)
                widgets["area"].configure(width=preview_w, height=preview_h)
                widgets["name"].configure(wraplength=preview_w)
                if source.get("dwm") and self.sync_dwm_preview(key, widgets["area"], source["source"]["hwnd"]):
                    widgets["image"].place_forget()
                    widgets["image"].image = None
                else:
                    self.unregister_dwm_preview(key)
                    widgets["image"].place(x=0, y=0, width=preview_w, height=preview_h)
                    with self.preview_lock:
                        frame = self.preview_frames.get(key)
                    size = (preview_w, preview_h)
                    frame_id = id(frame) if frame is not None else None
                    if source["available"] and frame is not None:
                        if widgets.get("photo_frame_id") != frame_id or widgets.get("photo_size") != size:
                            photo = self.photo_from_frame(frame, preview_w, preview_h)
                            widgets["image"].configure(image=photo)
                            widgets["image"].image = photo
                            widgets["photo_frame_id"] = frame_id
                            widgets["photo_size"] = size
                    elif not layout_busy or not getattr(widgets["image"], "image", None):
                        photo = self.placeholder_image("等待画面" if source["available"] else "未启动", preview_w, preview_h)
                        widgets["image"].configure(image=photo)
                        widgets["image"].image = photo
                        widgets["photo_frame_id"] = None
                        widgets["photo_size"] = size
                widgets["name"].configure(text=source["name"] if source["available"] else f"{source['name']}（未启动）")
        except Exception:
            pass
        finally:
            if getattr(self, "source_previews_enabled", True):
                self.schedule_source_previews(SOURCE_PREVIEW_SYNC_MS)
                self.ensure_dwm_sync_loop()

    def preview_height(self, source, width):
        data = source.get("source") or {}
        src_w = max(1, int(data.get("width", PREVIEW_W) or PREVIEW_W))
        src_h = max(1, int(data.get("height", PREVIEW_H) or PREVIEW_H))
        return max(120, min(360, int(width * src_h / src_w)))

    def preview_signature(self, source):
        data = source.get("source") or {}
        if source.get("kind") == "screen":
            return (source.get("kind"), data.get("monitor"), data.get("_abs_left"), data.get("_abs_top"), data.get("width"), data.get("height"))
        return (source.get("kind"), data.get("hwnd"), data.get("width"), data.get("height"))

    def preview_frame_current(self, source):
        return source.get("kind") == "screen" and self.preview_signatures.get(source["key"]) == source.get("signature") and source["key"] in self.preview_frames

    def unregister_dwm_preview(self, key):
        record = self.dwm_thumbs.pop(key, None)
        if record:
            dwm_unregister(record["thumb"])

    def suspend_dwm_previews(self):
        for key in list(getattr(self, "dwm_thumbs", {})):
            self.unregister_dwm_preview(key)

    def schedule_source_previews(self, delay):
        if getattr(self, "preview_job", None):
            try:
                self.root.after_cancel(self.preview_job)
            except TclError:
                pass
        if not getattr(self, "source_previews_enabled", True):
            self.preview_job = None
            return
        try:
            self.preview_job = self.root.after(delay, self.refresh_source_previews)
        except TclError:
            self.preview_job = None

    def enable_source_previews(self, delay=0):
        self.source_previews_enabled = True
        self.schedule_source_previews(delay)

    def disable_source_previews(self):
        self.source_previews_enabled = False
        if getattr(self, "preview_job", None):
            try:
                self.root.after_cancel(self.preview_job)
            except TclError:
                pass
            self.preview_job = None
        self.suspend_dwm_previews()

    def visible_preview_rect(self, widget):
        try:
            if self.root.state() in {"withdrawn", "iconic"} or not widget.winfo_viewable():
                return None
            x, y = widget.winfo_rootx(), widget.winfo_rooty()
            w, h = widget.winfo_width() or PREVIEW_W, widget.winfo_height() or PREVIEW_H
            bounds = [
                (self.root.winfo_rootx(), self.root.winfo_rooty(), self.root.winfo_rootx() + self.root.winfo_width(), self.root.winfo_rooty() + self.root.winfo_height()),
                (self.source_canvas.winfo_rootx(), self.source_canvas.winfo_rooty(), self.source_canvas.winfo_rootx() + self.source_canvas.winfo_width(), self.source_canvas.winfo_rooty() + self.source_canvas.winfo_height()),
            ]
            if any(x < left or y < top or x + w > right or y + h > bottom for left, top, right, bottom in bounds):
                return None
            return x, y, w, h
        except Exception:
            return None

    def sync_dwm_preview(self, key, widget, hwnd):
        rect = self.visible_preview_rect(widget)
        record = self.dwm_thumbs.get(key)
        if rect is None:
            if record:
                dwm_update(record["thumb"], 0, 0, 1, 1, visible=False)
                record["rect"] = None
                return True
            return False
        x, y, width, height = rect
        root_hwnd = hwnd_for_tk(self.root)
        rel = (x - self.root.winfo_rootx(), y - self.root.winfo_rooty(), width, height)
        if not record or record.get("hwnd") != hwnd or record.get("root") != root_hwnd:
            self.unregister_dwm_preview(key)
            thumb = dwm_register(root_hwnd, hwnd)
            if not thumb:
                return False
            self.dwm_thumbs[key] = {"thumb": thumb, "hwnd": hwnd, "root": root_hwnd, "rect": None}
            record = self.dwm_thumbs[key]
        try:
            if record.get("rect") != rel:
                ok = dwm_update(record["thumb"], rel[0], rel[1], rel[2], rel[3], visible=True)
                if ok:
                    record["rect"] = rel
                return ok
            return True
        except Exception:
            return False

    def ensure_dwm_sync_loop(self):
        if getattr(self, "dwm_sync_job", None) is None and getattr(self, "dwm_thumbs", None):
            try:
                self.dwm_sync_job = self.root.after(DWM_PREVIEW_SYNC_MS, self.sync_dwm_previews_loop)
            except TclError:
                self.dwm_sync_job = None

    def sync_dwm_previews_loop(self):
        self.dwm_sync_job = None
        try:
            for key, record in list(self.dwm_thumbs.items()):
                widgets = self.source_widgets.get(key)
                if widgets:
                    self.sync_dwm_preview(key, widgets["area"], record["hwnd"])
            if self.layout_busy() and self.dwm_thumbs:
                self.ensure_dwm_sync_loop()
        except Exception:
            pass

    def run_preview_worker(self):
        try:
            from mss import mss

            with mss() as sct:
                while not self.close_event.is_set():
                    if not getattr(self, "source_previews_enabled", True):
                        time.sleep(0.1)
                        continue
                    if self.layout_busy():
                        time.sleep(0.05)
                        continue
                    with self.preview_lock:
                        sources = [dict(item) for item in self.preview_sources]
                    for source in sources:
                        if self.close_event.is_set():
                            return
                        if source.get("dwm"):
                            continue
                        with self.preview_lock:
                            if self.preview_frame_current(source):
                                continue
                        frame = self.capture_preview_frame(sct, source)
                        if frame is not None and not mostly_black(frame):
                            with self.preview_lock:
                                self.preview_frames[source["key"]] = frame
                                self.preview_signatures[source["key"]] = source.get("signature")
                    time.sleep(SCREEN_PREVIEW_SECONDS)
        except Exception:
            pass

    def capture_preview_frame(self, sct, source):
        try:
            if not source.get("available"):
                return None
            if source["kind"] == "window":
                return capture_window_preview(sct, source["source"]["hwnd"])
            monitors = [{"index": i, **m} for i, m in enumerate(sct.monitors)]
            region = config_regions({"regions": [source["source"]]}, monitors)[0]
            return capture_region(sct, region)
        except Exception:
            return None

    def photo_from_frame(self, frame, width=PREVIEW_W, height=PREVIEW_H):
        img = Image.fromarray(frame).convert("RGB")
        img.thumbnail((width, height))
        canvas = Image.new("RGB", (width, height), (245, 245, 245))
        canvas.paste(img, ((width - img.width) // 2, (height - img.height) // 2))
        return ImageTk.PhotoImage(canvas)

    def placeholder_image(self, text, width=PREVIEW_W, height=PREVIEW_H):
        canvas = Image.new("RGB", (width, height), (245, 245, 245))
        draw = ImageDraw.Draw(canvas)
        draw.text((14, height // 2 - 8), text, fill=(70, 70, 70))
        return ImageTk.PhotoImage(canvas)

    def one_line_name(self, text, width):
        font = self.fonts.get("TkDefaultFont") if hasattr(self, "fonts") else None
        if not font or font.measure(text) <= width:
            return text
        marker = "..."
        while text and font.measure(text + marker) > width:
            text = text[:-1]
        return text + marker

    def reload_target_list(self):
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self.thumb_refs.clear()
        self.target_vars.clear()
        self.target_cards.clear()
        columns = 5
        for idx, target in enumerate(self.targets):
            row, col = divmod(idx, columns)
            bg = "#dbeafe" if idx == self.selected_target else "#ffffff"
            card = Frame(
                self.gallery_inner,
                bd=1,
                relief="solid",
                bg=bg,
                width=self.thumb_w + 12,
                height=self.thumb_h + max(24, int(22 * self.last_scale)),
            )
            card.grid(row=row, column=col, padx=6, pady=6, sticky="n")
            card.grid_propagate(False)
            enabled_var = BooleanVar(value=target.get("enabled", True))
            self.target_vars.append(enabled_var)
            thumb = self.make_thumb(target)
            self.thumb_refs.append(thumb)
            image = Label(card, image=thumb, bg=bg, width=self.thumb_w, height=self.thumb_h)
            image.place(x=6, y=6, width=self.thumb_w, height=self.thumb_h)
            check = ttk.Checkbutton(card, variable=enabled_var, command=lambda i=idx, v=enabled_var: self.toggle_target(i, v))
            check.place(x=7, y=7, width=18, height=18)
            filename = self.one_line_name(Path(target["path"]).name, self.thumb_w)
            text_font = getattr(self, "target_name_font", self.fonts.get("TkDefaultFont"))
            text = Label(card, text=filename, bg=bg, anchor="center", font=text_font)
            text.place(x=4, y=self.thumb_h + 8, width=self.thumb_w + 4, height=max(16, int(16 * self.last_scale)))
            self.target_cards[idx] = (card, image, text)
            for widget in (card, image, text):
                widget.bind("<Button-1>", lambda _event, i=idx: self.click_target(i))
            for widget in (card, check, image, text):
                widget.bind("<MouseWheel>", self.scroll_targets)
        self.target_canvas.configure(height=max(150, int((self.thumb_h + 34) * 2)))
        self.update_target_select_button()
        self.status.set(f"当前 {len(self.targets)} 张模板，启用 {len(self.enabled_targets())} 张。")

    def enabled_targets(self):
        return [t for t in self.targets if t.get("enabled", True)]

    def profile_path(self, number=None):
        return PROFILES_DIR / f"profile_{number or self.current_profile}.json"

    def save_state(self):
        self.capture_layout_ratios()
        write_json(
            STATE_PATH,
            {
                "last_profile": self.current_profile,
                "layout": {
                    "geometry": self.root.geometry(),
                    "main_ratio": self.main_ratio,
                    "right_ratio": self.right_ratio,
                    "left_ratio": self.left_ratio,
                },
                "max_alerts": self.max_alerts.get(),
            },
        )

    def capture_layout_ratios(self):
        try:
            root_w = max(1, self.root.winfo_width())
            left_h = max(1, self.left_pane.winfo_height())
            self.main_ratio = min(0.85, max(0.25, self.main_pane.sash_coord(0)[0] / root_w))
            self.right_ratio = min(0.4, max(0.12, (self.main_pane.sash_coord(1)[0] - self.main_pane.sash_coord(0)[0]) / root_w))
            self.left_ratio = min(0.8, max(0.25, self.left_pane.sash_coord(0)[1] / left_h))
        except Exception:
            pass

    def side_pane_width(self, width):
        ratio = min(0.4, max(0.12, self.right_ratio))
        max_side = max(180, (int(width) - 360) // 2)
        min_side = min(320, max_side)
        preferred = int(width * ratio)
        if ratio <= 0.18:
            preferred = min(360, preferred)
        return max(min_side, min(max_side, preferred))

    def horizontal_sashes(self, width):
        width = max(1, int(width))
        left_min, middle_min, preview_min = 360, 260, 320
        if width < left_min + middle_min + preview_min:
            middle = self.side_pane_width(width)
            first = max(left_min, width - middle * 2)
            return first, first + middle
        first = round(width * min(0.85, max(0.25, self.main_ratio)))
        second = round(width * min(0.97, max(0.37, self.main_ratio + self.right_ratio)))
        first = min(max(left_min, first), width - middle_min - preview_min)
        second = min(max(first + middle_min, second), width - preview_min)
        return first, second

    def restore_layout(self, horizontal=True, vertical=True):
        try:
            width = self.root.winfo_width()
            left_h = self.left_pane.winfo_height()
            if self.root.state() == "withdrawn":
                if not self.layout_restore_job:
                    self.layout_restore_job = self.root.after(100, lambda h=horizontal, v=vertical: self.retry_restore_layout(h, v))
                return
            retry_horizontal = horizontal and width < 400
            retry_vertical = vertical and left_h < 100
            if horizontal and not retry_horizontal:
                first, second = self.horizontal_sashes(width)
                self.main_pane.sash_place(0, first, 0)
                self.main_pane.sash_place(1, second, 0)
            if vertical and not retry_vertical:
                self.left_pane.sash_place(0, 0, int(left_h * self.left_ratio))
            if (retry_horizontal or retry_vertical) and not self.layout_restore_job:
                self.layout_restore_job = self.root.after(100, lambda h=retry_horizontal, v=retry_vertical: self.retry_restore_layout(h, v))
        except Exception:
            pass

    def retry_restore_layout(self, horizontal=True, vertical=True):
        self.layout_restore_job = None
        self.restore_layout(horizontal, vertical)

    def switch_profile(self, _event=None):
        if self.loading_profile:
            return
        self.save_current_profile()
        self.current_profile = int(self.profile.get())
        self.save_state()
        self.load_profile(self.current_profile)

    def save_current_profile(self):
        if self.loading_profile:
            return
        data = {
            "targets": self.targets,
            "monitors": [i for i, var in self.monitor_vars.items() if var.get()],
            "windows": self.selected_apps,
            "region": {"left": self.left.get(), "top": self.top.get(), "width": self.width.get(), "height": self.height.get()},
            "match": {
                "threshold": self.threshold.get(),
                "scales": self.scales.get(),
                "interval_ms": self.interval_ms.get(),
                "cooldown": self.cooldown.get(),
                "beep": self.beep.get(),
                "beep_seconds": self.beep_seconds.get(),
                "beep_volume": self.beep_volume.get(),
                "max_templates": self.max_templates.get(),
            },
        }
        write_json(self.profile_path(), data)
        self.save_state()

    def load_profile(self, number):
        self.loading_profile = True
        data = load_json(self.profile_path(number), {})
        self.targets = [ensure_thumb(t) for t in data.get("targets", []) if Path(t.get("path", "")).exists()]
        names_changed = self.normalize_targets()
        if names_changed:
            data["targets"] = self.targets
            write_json(self.profile_path(number), data)
        self.selected_target = 0 if self.targets else None
        region = data.get("region", {})
        self.left.set(region.get("left", "0"))
        self.top.set(region.get("top", "0"))
        self.width.set(region.get("width", ""))
        self.height.set(region.get("height", ""))
        match = data.get("match", {})
        self.threshold.set(match.get("threshold", 0.90))
        self.scales.set(match.get("scales", "1.0"))
        self.interval_ms.set(match.get("interval_ms", 250))
        self.cooldown.set(match.get("cooldown", 1.0))
        self.beep.set(match.get("beep", True))
        self.beep_seconds.set(match.get("beep_seconds", match.get("beep_count", 3)))
        self.beep_volume.set(match.get("beep_volume", 100))
        self.update_beep_volume()
        self.max_templates.set(match.get("max_templates", 100))
        if "monitors" in data:
            selected_monitors = set(data.get("monitors", []))
            for i, var in self.monitor_vars.items():
                var.set(i in selected_monitors)
        self.selected_apps = [app_from_legacy(item) for item in data.get("windows", []) if app_from_legacy(item).get("title")]
        self.refresh_windows()
        self.loading_profile = False
        self.reload_selected_apps()
        self.reload_target_list()
        self.status.set(f"已载入配置 {number}。")

    def on_close(self):
        self.save_current_profile()
        self.save_state()
        self.hide_to_tray()

    def exit_app(self):
        self.save_current_profile()
        self.save_state()
        self.stop_event.set()
        self.close_event.set()
        if self.instance_socket:
            try:
                self.instance_socket.close()
            except OSError:
                pass
            self.instance_socket = None
        if self.preview_job:
            try:
                self.root.after_cancel(self.preview_job)
            except TclError:
                pass
            self.preview_job = None
        if self.dwm_sync_job:
            try:
                self.root.after_cancel(self.dwm_sync_job)
            except TclError:
                pass
            self.dwm_sync_job = None
        for key in list(self.dwm_thumbs):
            self.unregister_dwm_preview(key)
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.destroy()

    def tray_image(self):
        img = Image.new("RGB", (64, 64), "#1573d1")
        fill = (48, 180, 82) if self.is_monitoring() else (255, 255, 255)
        for x in range(14, 50):
            for y in range(20, 44):
                img.putpixel((x, y), fill)
        return img

    def hide_to_tray(self):
        self.disable_source_previews()
        self.root.withdraw()
        if not self.ensure_tray_icon(show_errors=True):
            self.enable_source_previews(250)
            return
        self.status.set("已缩小到系统托盘。")

    def ensure_tray_icon(self, show_errors=False):
        if self.tray_icon:
            return True
        try:
            import pystray
        except Exception as exc:
            if show_errors:
                messagebox.showerror("托盘不可用", f"缺少托盘组件：{exc}")
                self.root.deiconify()
            return False

        def show(_icon=None, _item=None):
            self.root.after(0, self.show_window)

        def exit_(_icon=None, _item=None):
            self.root.after(0, self.exit_app)

        self.tray_icon = pystray.Icon(APP_NAME, self.tray_image(), "屏幕监控OCR", pystray.Menu(pystray.MenuItem("打开", show, default=True), pystray.MenuItem("退出", exit_)))
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        return True

    def show_window(self):
        self.ensure_tray_icon(show_errors=False)
        self.disable_source_previews()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except TclError:
            pass
        self.enable_source_previews(250)

    def start_instance_listener(self, sock):
        self.instance_socket = sock

        def listen():
            while not self.close_event.is_set():
                try:
                    conn, _addr = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    try:
                        if conn.recv(len(INSTANCE_COMMAND)) == INSTANCE_COMMAND:
                            conn.sendall(INSTANCE_ACK)
                            self.root.after(0, self.show_window)
                    except OSError:
                        pass

        threading.Thread(target=listen, daemon=True).start()

    def update_tray_icon(self):
        if self.tray_icon:
            self.tray_icon.icon = self.tray_image()

    def is_monitoring(self):
        return bool(self.worker and self.worker.is_alive() and not self.stop_event.is_set())

    def update_monitor_button(self):
        self.start_btn.configure(text="停止监控" if self.is_monitoring() else "开始监控")
        self.update_tray_icon()

    def toggle_startup(self):
        wanted = self.startup_enabled.get()
        try:
            actual = set_startup_enabled(wanted)
            self.startup_enabled.set(actual)
            self.status.set("开机自启已开启。" if actual else "开机自启已关闭。")
        except Exception as exc:
            self.startup_enabled.set(not wanted)
            messagebox.showerror("开机自启设置失败", str(exc))

    def on_resize(self, event):
        if event.widget != self.root:
            return
        size = (event.width, event.height)
        if size == self.last_root_size:
            self.move_active_until = time.time() + 0.3
            return
        self.suspend_dwm_previews()
        self.last_root_size = size
        self.resize_active_until = time.time() + 0.3
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(120, self.apply_scale)

    def apply_scale(self, force=False):
        self.resize_job = None
        if not force and self.mouse_button_down():
            self.resize_job = self.root.after(120, self.apply_scale)
            return
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        if not force and (width, height) != self.last_root_size:
            return
        scale = max(0.8, min(1.35, ((width * height) / (980 * 680)) ** 0.5))
        if not force and abs(scale - self.last_scale) < 0.08:
            self.schedule_source_previews(0)
            return
        self.last_scale = scale
        for name, font in self.fonts.items():
            font.configure(size=max(8, int(self.base_font_sizes[name] * scale)))
        self.style.configure("Treeview", rowheight=max(22, int(22 * scale)))
        if hasattr(self, "target_name_font"):
            self.target_name_font.configure(size=max(7, int(self.base_font_sizes["TkDefaultFont"] * 0.82 * scale)))
        self.thumb_w = int(104 * scale)
        self.thumb_h = int(72 * scale)
        self.redraw_checks()
        self.reload_target_list()
        self.schedule_source_previews(0)

    def selected_regions(self):
        return [self.region_for(i) for i, var in self.monitor_vars.items() if var.get()]

    def region_for(self, monitor):
        region = {"name": f"monitor-{monitor}", "monitor": monitor}
        for key, var in [("left", self.left), ("top", self.top), ("width", self.width), ("height", self.height)]:
            value = var.get().strip()
            if value:
                region[key] = int(value)
        return region

    def detector_config(self):
        targets = self.enabled_targets()
        if not self.targets:
            raise ValueError("先添加至少一张模板图片")
        if not targets:
            raise ValueError("至少勾选一张要匹配的模板图片")
        regions = self.selected_regions()
        windows = self.selected_windows()
        if not regions and not windows and not self.selected_apps:
            raise ValueError("至少选择一个屏幕或应用")
        threshold = float(self.threshold.get())
        scales = parse_scales(self.scales.get())
        beep_seconds = parse_positive_float(self.beep_seconds.get(), "beep_seconds")
        beep_volume = parse_volume(self.beep_volume.get())
        if beep_volume != int(self.beep_volume.get()):
            self.beep_volume.set(beep_volume)
        parse_positive_int(self.max_templates.get(), "max_templates")
        max_alerts = parse_positive_int(self.max_alerts.get(), "max_alerts")
        interval_ms = scan_interval_ms(self.interval_ms.get())
        if interval_ms != int(self.interval_ms.get()):
            self.interval_ms.set(interval_ms)
        return {
            "_base_dir": str(DATA_DIR),
            "regions": regions,
            "windows": windows,
            "window_apps": self.selected_apps,
            "targets": [
                {"name": t["name"], "kind": "template", "path": t["path"], "threshold": threshold, "scales": scales}
                for t in targets
            ],
            "cooldown_seconds": float(self.cooldown.get()),
            "poll_interval_seconds": interval_ms / 1000,
            "alarm": {"beep": bool(self.beep.get()), "beep_seconds": beep_seconds, "beep_volume": beep_volume, "save_dir": "screenshots", "jsonl": "alerts.jsonl", "max_alerts": max_alerts},
        }

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self.detector_config()
        except Exception as exc:
            messagebox.showerror("无法开始", str(exc))
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self.run_worker, args=(config, False), daemon=True)
        self.worker.start()
        self.status.set("监控中。")
        self.update_monitor_button()

    def toggle_monitoring(self):
        if self.is_monitoring():
            self.stop()
        else:
            self.start()

    def scan_once(self):
        try:
            config = self.detector_config()
        except Exception as exc:
            messagebox.showerror("无法扫描", str(exc))
            return
        self.stop_event.clear()
        threading.Thread(target=self.run_worker, args=(config, True), daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.status.set("正在停止。")
        self.update_monitor_button()

    def run_worker(self, config, once):
        from mss import mss

        detector = Detector(config)
        last_seen = {}
        parallel_detect = not any(t.get("kind") == "ocr_text" for t in config.get("targets", []))
        max_workers = max(1, min(4, os.cpu_count() or 1))
        window_capture_modes = {}
        try:
            with mss() as sct:
                monitors = [{"index": i, **m} for i, m in enumerate(sct.monitors)]
                regions = config_regions(config, monitors) if config.get("regions") else []
                windows = config.get("windows", [])
                window_apps = config.get("window_apps", [])
                last_window_refresh = 0
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    while not self.stop_event.is_set():
                        started = time.perf_counter()
                        hit_count = 0
                        if window_apps and time.time() - last_window_refresh >= 2:
                            lookup = {item["key"]: item for item in list_app_windows()}
                            windows = [
                                {"name": f"app-{safe_name(item['display'])}", "title": item["title"], "display": item["display"], "hwnd": item["hwnd"], "key": item["key"]}
                                for app in window_apps
                                for item in [lookup.get(self.app_key(app))]
                                if item
                            ]
                            known_hwnds = {int(window["hwnd"]) for window in windows}
                            for key in list(window_capture_modes):
                                if key not in known_hwnds:
                                    window_capture_modes.pop(key, None)
                            last_window_refresh = time.time()
                        jobs = []
                        for region in regions:
                            jobs.append((region, capture_region(sct, region)))
                        for window in windows:
                            frame = capture_window_frame(sct, window["hwnd"], window_capture_modes)
                            if frame is not None:
                                jobs.append((window, frame))
                        if parallel_detect and len(jobs) > 1:
                            detected = [(item, frame, matches) for (item, frame), matches in zip(jobs, pool.map(lambda job: detector.run(job[1]), jobs))]
                        else:
                            detected = [(item, frame, detector.run(frame)) for item, frame in jobs]
                        for item, frame, matches in detected:
                            if matches:
                                hit_count += self.emit_alert(config, last_seen, item, frame, matches)
                        elapsed = time.perf_counter() - started
                        self.events.put(("tick", f"扫描 {len(regions)} 屏 / {len(windows)} 应用 / {len(config['targets'])} 图，用时 {elapsed * 1000:.0f} ms，命中 {hit_count}"))
                        if once:
                            return
                        time.sleep(max(0.01, config["poll_interval_seconds"] - elapsed))
        finally:
            self.events.put(("stopped", "已停止"))

    def emit_alert(self, config, last_seen, region, frame, matches):
        now = time.time()
        kept = []
        for match in matches:
            key = (region["name"], match["target"])
            if now - last_seen.get(key, 0) >= config["cooldown_seconds"]:
                last_seen[key] = now
                kept.append(match)
        if not kept:
            return 0
        alert_dir = ALERTS_DIR if config["alarm"]["save_dir"] == "screenshots" else DATA_DIR / config["alarm"]["save_dir"]
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
        image_path = alert_dir / f"{stamp}-{region['name']}.png"
        save_rgb(image_path, frame, kept)
        prune_alerts(alert_dir, config["alarm"].get("max_alerts", 50))
        event = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "region": region["name"],
            "matches": kept,
            "screenshot": str(image_path.resolve()),
        }
        jsonl = DATA_DIR / config["alarm"]["jsonl"]
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        if config["alarm"]["beep"]:
            self.start_beep(config["alarm"].get("beep_seconds", 3))
        self.events.put(("hit", f"{region['name']} 命中 {', '.join(m['target'] for m in kept)} -> {image_path.name}"))
        return len(kept)

    def start_beep(self, seconds):
        now = time.time()
        with self.beep_lock:
            if now < self.beep_until:
                return
            self.beep_until = now + float(seconds)
        threading.Thread(target=beep_for, args=(seconds, lambda: self.beep_volume_level), daemon=True).start()

    def poll_events(self):
        if self.layout_busy():
            self.root.after(100, self.poll_events)
            return
        while True:
            try:
                kind, message = self.events.get_nowait()
            except queue.Empty:
                break
            self.status.set(message)
            if kind == "stopped":
                self.worker = None
                self.update_monitor_button()
            self.log.insert("", 0, values=(time.strftime("%H:%M:%S"), message))
            for item in self.log.get_children()[100:]:
                self.log.delete(item)
        self.root.after(100, self.poll_events)

    def open_evidence(self):
        path = ALERTS_DIR
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)


def smoke_test():
    assert parse_scales("1, 0.9,1.1") == [1.0, 0.9, 1.1]
    monitors = [m for m in list_monitors() if m["index"] != 0]
    assert monitors, "no physical monitor found"
    print(json.dumps({"ok": True, "monitors": len(monitors)}, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke_test:
        smoke_test()
        if getattr(sys, "frozen", False):
            os._exit(0)
        return 0
    instance_socket = claim_single_instance()
    if instance_socket is None:
        return 0
    root = Tk()
    root.withdraw()
    try:
        root.attributes("-alpha", 0.0)
    except TclError:
        pass
    app = App(root)
    if instance_socket:
        app.start_instance_listener(instance_socket)
    root.update_idletasks()
    app.last_root_size = (max(1, root.winfo_width()), max(1, root.winfo_height()))
    app.apply_scale(force=True)
    root.update_idletasks()
    root.deiconify()
    root.lift()
    root.update_idletasks()
    app.restore_layout()
    root.update_idletasks()
    try:
        root.attributes("-alpha", 1.0)
    except TclError:
        pass
    root.after(350, app.restore_layout)
    root.after(500, app.enable_source_previews)
    app.ensure_tray_icon(show_errors=False)
    root.mainloop()
    return 0
