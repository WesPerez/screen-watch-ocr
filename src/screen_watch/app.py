import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import BooleanVar, Canvas, DoubleVar, Frame, IntVar, Label, PanedWindow, StringVar, Tk, filedialog, messagebox, ttk
import tkinter.font as tkfont

import cv2
import numpy as np
from PIL import Image, ImageGrab, ImageTk

from .core import Detector, capture_region, config_regions, list_monitors, save_rgb


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
STARTUP_LINK_NAME = "屏幕监控OCR.lnk"


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


def parse_scales(text):
    values = [float(x.strip()) for x in text.split(",") if x.strip()]
    if not values:
        raise ValueError("scales is empty")
    return values


def parse_positive_int(value, name):
    number = int(value)
    if number < 1:
        raise ValueError(f"{name} must be >= 1")
    return number


def parse_positive_float(value, name):
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} must be > 0")
    return number


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_template(image, name):
    target_dir = DATA_DIR / "templates"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
    path = target_dir / f"{stamp}-{safe_name(name)}.png"
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
        target["thumb"] = str(save_thumb(Image.open(path), path))
    return target


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


def beep_for(seconds):
    try:
        import winsound

        deadline = time.time() + float(seconds)
        while time.time() < deadline:
            winsound.Beep(1200, 180)
            time.sleep(0.02)
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Watch OCR")
        migrate_legacy_data()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.state = load_json(STATE_PATH, {"last_profile": 1, "layout": {}})
        self.layout = self.state.get("layout", {})
        self.main_ratio = float(self.layout.get("main_ratio", 0.72))
        self.left_ratio = float(self.layout.get("left_ratio", 0.58))
        self.root.geometry(self.layout.get("geometry", "980x680"))
        self.current_profile = int(self.state.get("last_profile", 1))
        self.current_profile = min(PROFILE_COUNT, max(1, self.current_profile))
        self.loading_profile = False
        self.targets = []
        self.thumb_refs = []
        self.target_vars = []
        self.thumb_cache = {}
        self.selected_target = None
        self.thumb_w = 128
        self.thumb_h = 88
        self.last_scale = 1.0
        self.resize_job = None
        self.layout_restore_job = None
        self.monitor_vars = {}
        self.worker = None
        self.tray_icon = None
        self.stop_event = threading.Event()
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
        self.max_alerts = IntVar(value=int(self.state.get("max_alerts", 50)))
        self.beep = BooleanVar(value=True)
        self.left = StringVar(value="0")
        self.top = StringVar(value="0")
        self.width = StringVar(value="")
        self.height = StringVar(value="")
        self.status = StringVar(value="添加图片或 Ctrl+V 粘贴截图，然后开始监控。")
        self.fonts = {name: tkfont.nametofont(name) for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont")}
        self.base_font_sizes = {name: font.cget("size") for name, font in self.fonts.items()}
        self.style = ttk.Style()
        self._build()
        self.refresh_monitors()
        self.load_profile(self.current_profile)
        self.root.bind("<Control-v>", lambda _event: self.paste_images())
        self.root.bind("<Configure>", self.on_resize)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(250, self.restore_layout)
        self.root.after(100, self.poll_events)

    def _build(self):
        self.main_pane = PanedWindow(self.root, orient="horizontal", sashwidth=8, sashrelief="raised", bd=0)
        self.main_pane.pack(fill="both", expand=True, padx=12, pady=12)
        left = ttk.Frame(self.main_pane)
        right = ttk.Frame(self.main_pane, width=330)
        self.main_pane.add(left, minsize=360)
        self.main_pane.add(right, minsize=260)
        self.main_pane.bind("<ButtonRelease-1>", lambda _event: self.capture_layout_ratios())

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
        self.left_pane.bind("<ButtonRelease-1>", lambda _event: self.capture_layout_ratios())

        gallery_box = ttk.LabelFrame(self.left_pane, text="匹配图片")
        self.left_pane.add(gallery_box, minsize=170)
        self.target_canvas = Canvas(gallery_box, highlightthickness=0, height=260)
        self.target_canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(gallery_box, orient="vertical", command=self.target_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.target_canvas.configure(yscrollcommand=scroll.set)
        self.gallery_inner = ttk.Frame(self.target_canvas)
        self.gallery_window = self.target_canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")
        self.gallery_inner.bind("<Configure>", lambda _event: self.target_canvas.configure(scrollregion=self.target_canvas.bbox("all")))
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

        region_box = ttk.LabelFrame(right, text="区域")
        region_box.pack(fill="x", pady=10)
        for label, var in [("左", self.left), ("上", self.top), ("宽(空=全屏)", self.width), ("高(空=全屏)", self.height)]:
            row = ttk.Frame(region_box)
            row.pack(fill="x", padx=8, pady=3)
            ttk.Label(row, text=label, width=12).pack(side="left")
            self.make_entry(row, var).pack(side="right", fill="x", expand=True)

        match_box = ttk.LabelFrame(right, text="匹配")
        match_box.pack(fill="x")
        for label, var in [("阈值", self.threshold), ("缩放", self.scales), ("间隔ms", self.interval_ms), ("同图冷却秒", self.cooldown), ("蜂鸣秒", self.beep_seconds), ("截图最多张", self.max_alerts)]:
            row = ttk.Frame(match_box)
            row.pack(fill="x", padx=8, pady=3)
            ttk.Label(row, text=label, width=12).pack(side="left")
            self.make_entry(row, var).pack(side="right", fill="x", expand=True)
        self.make_check(match_box, self.beep, "命中蜂鸣").pack(anchor="w", padx=8, pady=4)

        actions = ttk.LabelFrame(right, text="运行")
        actions.pack(fill="x", pady=10)
        self.start_btn = ttk.Button(actions, text="开始监控", command=self.start)
        self.start_btn.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(actions, text="扫描一次", command=self.scan_once).pack(fill="x", padx=8, pady=4)
        ttk.Button(actions, text="停止", command=self.stop).pack(fill="x", padx=8, pady=4)
        ttk.Button(actions, text="打开证据目录", command=self.open_evidence).pack(fill="x", padx=8, pady=(4, 8))

        ttk.Label(right, textvariable=self.status, wraplength=300).pack(fill="x", pady=8)

    def make_entry(self, parent, var):
        entry = ttk.Entry(parent, textvariable=var)
        entry.bind("<FocusIn>", lambda event: event.widget.after_idle(event.widget.icursor, "end"))
        return entry

    def make_check(self, parent, var, label, command=None):
        return ttk.Checkbutton(parent, text=label, variable=var, command=command)

    def refresh_monitors(self):
        selected = {i for i, var in self.monitor_vars.items() if var.get()}
        for child in self.monitor_frame.winfo_children():
            child.destroy()
        self.monitor_vars.clear()
        monitors = [m for m in list_monitors() if m["index"] != 0]
        for i, monitor in enumerate(monitors):
            var = BooleanVar(value=(monitor["index"] in selected) if selected else i == 0)
            self.monitor_vars[monitor["index"]] = var
            text = f"{monitor['index']}: {monitor['width']}x{monitor['height']} ({monitor['left']},{monitor['top']})"
            self.make_check(self.monitor_frame, var, text).pack(anchor="w", pady=2)
        self.status.set(f"检测到 {len(monitors)} 个物理屏。")

    def add_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp")])
        for path in paths:
            self.add_image(Path(path).stem, Image.open(path))

    def paste_images(self):
        images = read_clipboard_images()
        if not images:
            self.status.set("剪贴板里没有图片；用截图工具复制后再按 Ctrl+V。")
            return
        for name, image in images:
            self.add_image(name, image)

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
        path = save_template(image, name)
        thumb = save_thumb(image, path)
        width, height = Image.open(path).size
        self.targets.append({"name": path.stem, "path": str(path), "thumb": str(thumb), "size": f"{width}x{height}", "enabled": True})
        self.selected_target = len(self.targets) - 1
        self.reload_target_list()
        self.save_current_profile()
        self.status.set(f"已添加 {len(self.targets)} 张模板。")

    def make_thumb(self, target):
        path = target.get("thumb") or target["path"]
        mtime = Path(path).stat().st_mtime if Path(path).exists() else 0
        key = (str(path), mtime, self.thumb_w, self.thumb_h)
        if key in self.thumb_cache:
            return self.thumb_cache[key]
        img = Image.open(path).convert("RGB")
        img.thumbnail((self.thumb_w, self.thumb_h))
        canvas = Image.new("RGB", (self.thumb_w, self.thumb_h), (245, 245, 245))
        canvas.paste(img, ((self.thumb_w - img.width) // 2, (self.thumb_h - img.height) // 2))
        self.thumb_cache[key] = ImageTk.PhotoImage(canvas)
        return self.thumb_cache[key]

    def select_target(self, index):
        self.selected_target = index
        self.reload_target_list()

    def toggle_target(self, index, var):
        self.targets[index]["enabled"] = bool(var.get())
        self.save_current_profile()
        self.status.set(f"当前 {len(self.targets)} 张模板，启用 {len(self.enabled_targets())} 张。")

    def remove_selected(self):
        if self.selected_target is not None and self.selected_target < len(self.targets):
            self.targets.pop(self.selected_target)
            self.selected_target = None
        self.reload_target_list()
        self.save_current_profile()

    def clear_targets(self):
        self.targets.clear()
        self.selected_target = None
        self.reload_target_list()
        self.save_current_profile()

    def reload_target_list(self):
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self.thumb_refs.clear()
        self.target_vars.clear()
        columns = 5
        for idx, target in enumerate(self.targets):
            row, col = divmod(idx, columns)
            selected = idx == self.selected_target
            card = Frame(
                self.gallery_inner,
                bd=2 if selected else 1,
                relief="solid",
                bg="#cfe8ff" if selected else "#f6f6f6",
                width=self.thumb_w + 16,
                height=self.thumb_h + int(58 * self.last_scale),
            )
            card.grid(row=row, column=col, padx=6, pady=6, sticky="n")
            card.grid_propagate(False)
            enabled_var = BooleanVar(value=target.get("enabled", True))
            self.target_vars.append(enabled_var)
            ttk.Checkbutton(card, variable=enabled_var, text="匹配", command=lambda i=idx, v=enabled_var: self.toggle_target(i, v)).pack(anchor="w", padx=4, pady=(3, 0))
            thumb = self.make_thumb(target)
            self.thumb_refs.append(thumb)
            image = Label(card, image=thumb, bg=card["bg"], width=self.thumb_w, height=self.thumb_h)
            image.pack(pady=(6, 2))
            text = Label(card, text=Path(target["path"]).name, bg=card["bg"], wraplength=self.thumb_w, justify="center")
            text.pack(fill="x", padx=4)
            for widget in (card, image, text):
                widget.bind("<Button-1>", lambda _event, i=idx: self.select_target(i))
        self.target_canvas.configure(height=max(180, int((self.thumb_h + 54) * 2)))
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
                    "left_ratio": self.left_ratio,
                },
                "max_alerts": self.max_alerts.get(),
            },
        )

    def capture_layout_ratios(self):
        try:
            root_w = max(1, self.root.winfo_width())
            root_h = max(1, self.root.winfo_height())
            self.main_ratio = min(0.85, max(0.45, self.main_pane.sash_coord(0)[0] / root_w))
            self.left_ratio = min(0.8, max(0.25, self.left_pane.sash_coord(0)[1] / root_h))
        except Exception:
            pass

    def restore_layout(self):
        try:
            self.main_pane.sash_place(0, int(self.root.winfo_width() * self.main_ratio), 0)
            self.left_pane.sash_place(0, 0, int(self.root.winfo_height() * self.left_ratio))
        except Exception:
            pass

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
            "region": {"left": self.left.get(), "top": self.top.get(), "width": self.width.get(), "height": self.height.get()},
            "match": {
                "threshold": self.threshold.get(),
                "scales": self.scales.get(),
                "interval_ms": self.interval_ms.get(),
                "cooldown": self.cooldown.get(),
                "beep": self.beep.get(),
                "beep_seconds": self.beep_seconds.get(),
            },
        }
        write_json(self.profile_path(), data)
        self.save_state()

    def load_profile(self, number):
        self.loading_profile = True
        data = load_json(self.profile_path(number), {})
        self.targets = [ensure_thumb(t) for t in data.get("targets", []) if Path(t.get("path", "")).exists()]
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
        selected_monitors = set(data.get("monitors", []))
        if selected_monitors:
            for i, var in self.monitor_vars.items():
                var.set(i in selected_monitors)
        self.loading_profile = False
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
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.destroy()

    def tray_image(self):
        img = Image.new("RGB", (64, 64), "#1573d1")
        for x in range(14, 50):
            for y in range(20, 44):
                img.putpixel((x, y), (255, 255, 255))
        return img

    def hide_to_tray(self):
        self.root.withdraw()
        if self.tray_icon:
            return
        try:
            import pystray
        except Exception as exc:
            messagebox.showerror("托盘不可用", f"缺少托盘组件：{exc}")
            self.root.deiconify()
            return

        def show(_icon=None, _item=None):
            self.root.after(0, self.show_window)

        def exit_(_icon=None, _item=None):
            self.root.after(0, self.exit_app)

        self.tray_icon = pystray.Icon(APP_NAME, self.tray_image(), "屏幕监控OCR", pystray.Menu(pystray.MenuItem("打开", show), pystray.MenuItem("退出", exit_)))
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self.status.set("已缩小到系统托盘。")

    def show_window(self):
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.deiconify()
        self.root.lift()

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
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(450, self.apply_scale)

    def apply_scale(self):
        self.resize_job = None
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        scale = max(0.8, min(1.8, ((width * height) / (980 * 680)) ** 0.5))
        if abs(scale - self.last_scale) < 0.08:
            self.restore_layout()
            return
        self.last_scale = scale
        for name, font in self.fonts.items():
            font.configure(size=max(8, int(self.base_font_sizes[name] * scale)))
        self.style.configure("Treeview", rowheight=max(22, int(22 * scale)))
        self.thumb_w = int(128 * scale)
        self.thumb_h = int(88 * scale)
        self.reload_target_list()
        self.restore_layout()

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
        if not regions:
            raise ValueError("至少选择一个屏幕")
        threshold = float(self.threshold.get())
        scales = parse_scales(self.scales.get())
        beep_seconds = parse_positive_float(self.beep_seconds.get(), "beep_seconds")
        max_alerts = parse_positive_int(self.max_alerts.get(), "max_alerts")
        return {
            "_base_dir": str(DATA_DIR),
            "regions": regions,
            "targets": [
                {"name": t["name"], "kind": "template", "path": t["path"], "threshold": threshold, "scales": scales}
                for t in targets
            ],
            "cooldown_seconds": float(self.cooldown.get()),
            "poll_interval_seconds": int(self.interval_ms.get()) / 1000,
            "alarm": {"beep": bool(self.beep.get()), "beep_seconds": beep_seconds, "save_dir": "screenshots", "jsonl": "alerts.jsonl", "max_alerts": max_alerts},
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

    def run_worker(self, config, once):
        from mss import mss

        detector = Detector(config)
        last_seen = {}
        with mss() as sct:
            monitors = [{"index": i, **m} for i, m in enumerate(sct.monitors)]
            regions = config_regions(config, monitors)
            while not self.stop_event.is_set():
                started = time.perf_counter()
                hit_count = 0
                for region in regions:
                    frame = capture_region(sct, region)
                    matches = detector.run(frame)
                    if matches:
                        hit_count += self.emit_alert(config, last_seen, region, frame, matches)
                elapsed = time.perf_counter() - started
                self.events.put(("tick", f"扫描 {len(regions)} 屏 / {len(config['targets'])} 图，用时 {elapsed * 1000:.0f} ms，命中 {hit_count}"))
                if once:
                    return
                time.sleep(max(0.01, config["poll_interval_seconds"] - elapsed))
        self.events.put(("tick", "已停止"))

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
        threading.Thread(target=beep_for, args=(seconds,), daemon=True).start()

    def poll_events(self):
        while True:
            try:
                kind, message = self.events.get_nowait()
            except queue.Empty:
                break
            self.status.set(message)
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
        return 0
    root = Tk()
    App(root)
    root.mainloop()
    return 0
