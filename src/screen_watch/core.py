import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def load_config(path):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_base_dir"] = str(path.parent.resolve())
    data.setdefault("poll_interval_seconds", 0.3)
    data.setdefault("cooldown_seconds", 3)
    data.setdefault("regions", [])
    data.setdefault("targets", [])
    data.setdefault("alarm", {})
    if not data["targets"]:
        raise ValueError("config.targets is empty")
    return data


def list_monitors():
    from mss import mss

    with mss() as sct:
        return [{"index": i, **m} for i, m in enumerate(sct.monitors)]


def _bbox_from_region(region, monitor):
    return {
        "left": monitor["left"] + int(region.get("left", 0)),
        "top": monitor["top"] + int(region.get("top", 0)),
        "width": int(region.get("width", monitor["width"])),
        "height": int(region.get("height", monitor["height"])),
    }


def config_regions(config, monitors):
    regions = config.get("regions") or [
        {"name": f"monitor-{m['index']}", "monitor": m["index"]}
        for m in monitors
        if m["index"] != 0
    ]
    out = []
    by_index = {m["index"]: m for m in monitors}
    for region in regions:
        monitor_id = int(region.get("monitor", 1))
        if monitor_id not in by_index or monitor_id == 0:
            raise ValueError(f"unknown monitor {monitor_id}; run list-monitors")
        out.append({**region, "_bbox": _bbox_from_region(region, by_index[monitor_id])})
    return out


def capture_region(sct, region):
    shot = sct.grab(region["_bbox"])
    return np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)


def save_rgb(path, frame, matches=None):
    img = Image.fromarray(frame)
    if matches:
        draw = ImageDraw.Draw(img)
        for match in matches:
            box = match.get("box")
            if box:
                draw.rectangle(box, outline=(255, 0, 0), width=3)
                draw.text((box[0], max(0, box[1] - 14)), match["target"], fill=(255, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:80] or "alert"


class OcrBackend:
    def __init__(self):
        try:
            from rapidocr import RapidOCR
        except Exception as exc:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except Exception:
                raise RuntimeError(
                    "OCR target needs rapidocr; run `python -m pip install -r requirements.txt`"
                ) from exc
        self.engine = RapidOCR()

    def read(self, frame):
        raw = self.engine(frame)
        if all(hasattr(raw, name) for name in ("boxes", "txts", "scores")):
            return list(zip(list(raw.txts), list(raw.scores), list(raw.boxes)))
        rows = raw[0] if isinstance(raw, tuple) else raw
        text_rows = []
        for row in rows or []:
            if isinstance(row, dict):
                text_rows.append((row.get("text", ""), float(row.get("score", 0)), row.get("box")))
            elif len(row) >= 3:
                text_rows.append((str(row[1]), float(row[2]), row[0]))
        return text_rows


class Detector:
    def __init__(self, config):
        self.base_dir = Path(config["_base_dir"])
        self.targets = config["targets"]
        self.templates = {}
        self.ocr = None
        if any(t.get("kind") == "ocr_text" for t in self.targets):
            self.ocr = OcrBackend()
        for target in self.targets:
            if target.get("kind") == "template":
                path = self._path(target["path"])
                image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    raise ValueError(f"cannot read template {path}")
                self.templates[target["name"]] = image

    def _path(self, value):
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path

    def run(self, frame):
        matches = []
        gray = None
        ocr_rows = None
        for target in self.targets:
            kind = target.get("kind")
            if kind == "pixel":
                hit = self._pixel(frame, target)
            elif kind == "template":
                if gray is None:
                    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                hit = self._template(gray, target)
            elif kind == "ocr_text":
                if ocr_rows is None:
                    ocr_rows = self.ocr.read(frame)
                hit = self._ocr(ocr_rows, target)
            else:
                raise ValueError(f"unknown target kind {kind!r}")
            if hit:
                matches.append(hit)
        return matches

    def _pixel(self, frame, target):
        x, y = int(target["x"]), int(target["y"])
        if y < 0 or y >= frame.shape[0] or x < 0 or x >= frame.shape[1]:
            return None
        expected = np.array(target["rgb"], dtype=np.int16)
        actual = frame[y, x].astype(np.int16)
        dist = int(np.max(np.abs(actual - expected)))
        if dist <= int(target.get("tolerance", 8)):
            return {"target": target["name"], "kind": "pixel", "score": 1 - dist / 255, "box": [x - 4, y - 4, x + 4, y + 4]}
        return None

    def _template(self, gray, target):
        template = self.templates[target["name"]]
        threshold = float(target.get("threshold", 0.9))
        best = None
        for scale in target.get("scales", [1.0]):
            scaled = template
            if float(scale) != 1.0:
                w = max(1, int(template.shape[1] * float(scale)))
                h = max(1, int(template.shape[0] * float(scale)))
                scaled = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA)
            if scaled.shape[0] > gray.shape[0] or scaled.shape[1] > gray.shape[1]:
                continue
            if float(np.std(scaled)) < 1:
                result = cv2.matchTemplate(gray, scaled, cv2.TM_SQDIFF_NORMED)
                min_val, _, min_loc, _ = cv2.minMaxLoc(result)
                score, loc = 1 - float(min_val), min_loc
            else:
                result = cv2.matchTemplate(gray, scaled, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                score, loc = float(max_val), max_loc
            if best is None or score > best["score"]:
                x, y = loc
                best = {
                    "target": target["name"],
                    "kind": "template",
                    "score": score,
                    "box": [x, y, x + scaled.shape[1], y + scaled.shape[0]],
                }
        return best if best and best["score"] >= threshold else None

    def _ocr(self, rows, target):
        needle = str(target["text"])
        case = bool(target.get("case_sensitive", False))
        min_score = float(target.get("min_score", 0))
        wanted = needle if case else needle.lower()
        for text, score, box in rows:
            haystack = text if case else text.lower()
            if score >= min_score and wanted in haystack:
                flat_box = None
                if box is not None:
                    pts = np.array(box, dtype=float).reshape(-1, 2)
                    x1, y1 = pts.min(axis=0)
                    x2, y2 = pts.max(axis=0)
                    flat_box = [int(x1), int(y1), int(x2), int(y2)]
                return {"target": target["name"], "kind": "ocr_text", "score": score, "text": text, "box": flat_box}
        return None


class Alarm:
    def __init__(self, config):
        alarm = config.get("alarm", {})
        self.beep = bool(alarm.get("beep", True))
        self.save_dir = Path(config["_base_dir"]) / alarm.get("save_dir", "evidence/alerts")
        self.jsonl = Path(config["_base_dir"]) / alarm.get("jsonl", "evidence/alerts.jsonl")
        self.cooldown = float(config.get("cooldown_seconds", 3))
        self.last_seen = {}

    def emit(self, region, frame, matches):
        now = time.time()
        kept = []
        for match in matches:
            key = (region.get("name", "region"), match["target"])
            if now - self.last_seen.get(key, 0) >= self.cooldown:
                self.last_seen[key] = now
                kept.append(match)
        if not kept:
            return []
        stamp = time.strftime("%Y%m%d-%H%M%S")
        event = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "region": region.get("name", "region"),
            "matches": kept,
        }
        image_path = self.save_dir / f"{stamp}-{_safe_name(event['region'])}.png"
        save_rgb(image_path, frame, kept)
        event["screenshot"] = str(image_path.resolve())
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self.beep:
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                print("\a", end="", flush=True)
        print(json.dumps(event, ensure_ascii=False), flush=True)
        return kept


def scan_frames(config, once=False, duration=None):
    from mss import mss

    detector = Detector(config)
    alarm = Alarm(config)
    deadline = time.time() + duration if duration else math.inf
    hit_count = 0
    with mss() as sct:
        regions = config_regions(config, [{"index": i, **m} for i, m in enumerate(sct.monitors)])
        while True:
            for region in regions:
                frame = capture_region(sct, region)
                matches = detector.run(frame)
                if matches:
                    hit_count += len(alarm.emit(region, frame, matches))
            if once or time.time() >= deadline:
                return hit_count
            time.sleep(float(config.get("poll_interval_seconds", 0.3)))


def screenshot(args):
    from mss import mss

    with mss() as sct:
        monitors = [{"index": i, **m} for i, m in enumerate(sct.monitors)]
        region = {"name": "manual", "monitor": args.monitor, "left": args.left, "top": args.top, "width": args.width, "height": args.height}
        frame = capture_region(sct, config_regions({"regions": [region]}, monitors)[0])
    out = Path(args.out)
    save_rgb(out, frame)
    print(str(out.resolve()))


def make_demo(out):
    out = Path(out)
    templates = out / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    frame = Image.new("RGB", (520, 220), "white")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((22, 24, 76, 78), fill=(228, 38, 38))
    draw.ellipse((36, 36, 62, 62), fill=(255, 255, 255))
    draw.rectangle((170, 76, 236, 132), outline=(0, 92, 190), width=5)
    draw.rectangle((187, 93, 219, 126), fill=(0, 92, 190))
    draw.text((260, 92), "ALERT-42", fill=(0, 0, 0), font=ImageFont.load_default())
    frame.save(out / "demo_frame.png")
    frame.crop((170, 76, 236, 132)).save(templates / "blue_mark.png")
    config = {
        "poll_interval_seconds": 0.25,
        "cooldown_seconds": 1,
        "regions": [{"name": "primary-demo", "monitor": 1, "left": 0, "top": 0, "width": 520, "height": 220}],
        "targets": [
            {"name": "red-dot", "kind": "pixel", "x": 50, "y": 50, "rgb": [255, 255, 255], "tolerance": 0},
            {"name": "blue-mark", "kind": "template", "path": "templates/blue_mark.png", "threshold": 0.95, "scales": [1.0]},
            {"name": "alert-text", "kind": "ocr_text", "text": "ALERT", "min_score": 0.3},
        ],
        "alarm": {"beep": False, "save_dir": "evidence/alerts", "jsonl": "evidence/alerts.jsonl"},
    }
    (out / "config.demo.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(str((out / "config.demo.json").resolve()))


def self_test(args):
    out = Path(args.out)
    make_demo(out)
    config = load_config(out / "config.demo.json")
    detector_targets = [t for t in config["targets"] if args.ocr or t["kind"] != "ocr_text"]
    config["targets"] = detector_targets
    detector = Detector(config)
    frame = np.array(Image.open(out / "demo_frame.png").convert("RGB"))
    matches = detector.run(frame)
    names = {m["target"] for m in matches}
    assert "red-dot" in names, matches
    assert "blue-mark" in names, matches
    if args.ocr:
        assert "alert-text" in names, matches
    save_rgb(out / "selftest_annotated.png", frame, matches)
    (out / "selftest_result.json").write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "matches": matches, "annotated": str((out / "selftest_annotated.png").resolve())}, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m screen_watch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("app")
    p.add_argument("--smoke-test", action="store_true")
    sub.add_parser("list-monitors")
    p = sub.add_parser("once")
    p.add_argument("--config", required=True)
    p = sub.add_parser("watch")
    p.add_argument("--config", required=True)
    p.add_argument("--duration", type=float)
    p = sub.add_parser("screenshot")
    p.add_argument("--monitor", type=int, default=1)
    p.add_argument("--left", type=int, default=0)
    p.add_argument("--top", type=int, default=0)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=360)
    p.add_argument("--out", default="evidence/screenshot.png")
    p = sub.add_parser("make-demo")
    p.add_argument("--out", default="demo")
    p = sub.add_parser("self-test")
    p.add_argument("--out", default="evidence/selftest")
    p.add_argument("--ocr", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "app":
        from .app import main as app_main

        return app_main(["--smoke-test"] if args.smoke_test else [])
    if args.cmd == "list-monitors":
        print(json.dumps(list_monitors(), indent=2))
        return 0
    if args.cmd == "make-demo":
        make_demo(args.out)
        return 0
    if args.cmd == "self-test":
        self_test(args)
        return 0
    if args.cmd == "screenshot":
        screenshot(args)
        return 0
    config = load_config(args.config)
    hits = scan_frames(config, once=args.cmd == "once", duration=getattr(args, "duration", None))
    return 0 if hits else 1
