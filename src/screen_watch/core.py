import argparse
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

cv2.setUseOptimized(True)
cv2.setNumThreads(1)

MAX_SCALE_COUNT = 120
COARSE_AREA = 2560 * 1440
QUARTER_AREA = 3840 * 2160
COARSE_CANDIDATES = 3
REFINE_MARGIN = 16
TEMPLATE_WORKERS = 8


def parse_scales(value):
    values = []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (int, float)):
        parts = [str(value)]
    else:
        parts = list(value)

    for part in parts:
        if not isinstance(part, str):
            scale = float(part)
            if scale <= 0:
                raise ValueError("scale must be > 0")
            values.append(scale)
            continue

        token = part.strip()
        if "-" not in token:
            scale = float(token)
            if scale <= 0:
                raise ValueError("scale must be > 0")
            values.append(scale)
            continue

        if ":" not in token:
            raise ValueError(f"scale range {token!r} needs a step, for example 0.5-2.0:0.1")
        span, step_text = token.split(":", 1)
        start_text, end_text = span.split("-", 1)
        start, end = float(start_text), float(end_text)
        if start <= 0 or end <= 0:
            raise ValueError("scale range values must be > 0")
        values.extend(_scale_range(start, end, step_text.strip()))

    out = []
    seen = set()
    for value in values:
        key = round(float(value), 6)
        if key <= 0:
            raise ValueError("scale must be > 0")
        if key not in seen:
            out.append(key)
            seen.add(key)
        if len(out) > MAX_SCALE_COUNT:
            raise ValueError(f"too many scales; keep it <= {MAX_SCALE_COUNT}")
    if not out:
        raise ValueError("scales is empty")
    return out


def _scale_range(start, end, step_text):
    percent = step_text.endswith("%")
    step = float(step_text[:-1] if percent else step_text)
    if step <= 0:
        raise ValueError("scale range step must be > 0")

    values = []
    if percent:
        factor = 1 + step / 100
        current = start
        if start <= end:
            while current <= end * 1.0000001:
                values.append(current)
                if len(values) > MAX_SCALE_COUNT:
                    raise ValueError(f"too many scales; keep it <= {MAX_SCALE_COUNT}")
                current *= factor
        else:
            while current >= end / 1.0000001:
                values.append(current)
                if len(values) > MAX_SCALE_COUNT:
                    raise ValueError(f"too many scales; keep it <= {MAX_SCALE_COUNT}")
                current /= factor
    else:
        direction = 1 if end >= start else -1
        current = start
        step *= direction
        if direction > 0:
            while current <= end + abs(step) / 1_000_000:
                values.append(current)
                if len(values) > MAX_SCALE_COUNT:
                    raise ValueError(f"too many scales; keep it <= {MAX_SCALE_COUNT}")
                current += step
        else:
            while current >= end - abs(step) / 1_000_000:
                values.append(current)
                if len(values) > MAX_SCALE_COUNT:
                    raise ValueError(f"too many scales; keep it <= {MAX_SCALE_COUNT}")
                current += step

    if values and abs(values[-1] - end) > 1e-6:
        values.append(end)
    return values


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
                scaled = []
                seen_sizes = set()
                for scale in parse_scales(target.get("scales", [1.0])):
                    if float(scale) == 1.0:
                        item = image
                    else:
                        w = max(1, int(image.shape[1] * float(scale)))
                        h = max(1, int(image.shape[0] * float(scale)))
                        item = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
                    size = item.shape[:2]
                    if size in seen_sizes:
                        continue
                    seen_sizes.add(size)
                    scaled.append({"image": item, "flat": float(np.std(item)) < 1, "scale": scale, "coarse": self._coarse_templates(item)})
                self.templates[target["name"]] = scaled

    def _path(self, value):
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path

    def run(self, frame):
        hits = [None] * len(self.targets)
        gray = None
        ocr_rows = None
        template_jobs = []
        for index, target in enumerate(self.targets):
            kind = target.get("kind")
            if kind == "pixel":
                hits[index] = self._pixel(frame, target)
            elif kind == "template":
                if gray is None:
                    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                template_jobs.append((index, target))
            elif kind == "ocr_text":
                if ocr_rows is None:
                    ocr_rows = self.ocr.read(frame)
                hits[index] = self._ocr(ocr_rows, target)
            else:
                raise ValueError(f"unknown target kind {kind!r}")
        if template_jobs:
            frames = self._frames_for(gray, template_jobs)
            if len(template_jobs) > 1:
                with ThreadPoolExecutor(max_workers=min(TEMPLATE_WORKERS, len(template_jobs))) as pool:
                    for index, hit in pool.map(lambda job: (job[0], self._template(frames, job[1])), template_jobs):
                        hits[index] = hit
            else:
                index, target = template_jobs[0]
                hits[index] = self._template(frames, target)
        return [hit for hit in hits if hit]

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

    def _template(self, frames, target):
        threshold = float(target.get("threshold", 0.9))
        best = None
        gray = frames[1.0]
        for item in self.templates[target["name"]]:
            scaled = item["image"]
            if scaled.shape[0] > gray.shape[0] or scaled.shape[1] > gray.shape[1]:
                continue
            factor = self._frame_scale(gray, scaled, item)
            if factor == 1.0:
                score, loc = self._match_one(gray, scaled, item["flat"])
            else:
                score, loc = self._match_coarse(frames, item, factor)
            if best is None or score > best["score"]:
                x, y = loc
                best = {
                    "target": target["name"],
                    "kind": "template",
                    "score": score,
                    "scale": item["scale"],
                    "box": [x, y, x + scaled.shape[1], y + scaled.shape[0]],
                }
        return best if best and best["score"] >= threshold else None

    def _coarse_templates(self, image):
        out = {}
        original_flat = float(np.std(image)) < 1
        for factor in (0.5, 0.25, 0.125):
            w = max(1, int(image.shape[1] * factor))
            h = max(1, int(image.shape[0] * factor))
            min_dim = 3 if factor == 0.5 else 4 if factor == 0.25 else 8
            if min(w, h) >= min_dim:
                coarse = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
                if original_flat or float(np.std(coarse)) >= 1:
                    out[factor] = coarse
        return out

    def _frame_scale(self, gray, scaled, item):
        area = gray.shape[0] * gray.shape[1]
        if area >= QUARTER_AREA and 0.25 in item["coarse"]:
            return 0.25
        if area >= COARSE_AREA and 0.5 in item["coarse"]:
            return 0.5
        return 1.0

    def _frames_for(self, gray, template_jobs):
        frames = {1.0: gray}
        factors = set()
        for _index, target in template_jobs:
            for item in self.templates[target["name"]]:
                factors.add(self._frame_scale(gray, item["image"], item))
        for factor in sorted(factors):
            if factor != 1.0:
                frames[factor] = cv2.resize(gray, (max(1, int(gray.shape[1] * factor)), max(1, int(gray.shape[0] * factor))), interpolation=cv2.INTER_AREA)
        return frames

    def _scaled_frame(self, frames, factor):
        if factor not in frames:
            gray = frames[1.0]
            frames[factor] = cv2.resize(gray, (max(1, int(gray.shape[1] * factor)), max(1, int(gray.shape[0] * factor))), interpolation=cv2.INTER_AREA)
        return frames[factor]

    def _match_coarse(self, frames, item, factor):
        coarse_gray = self._scaled_frame(frames, factor)
        coarse_template = item["coarse"][factor]
        if coarse_template.shape[0] > coarse_gray.shape[0] or coarse_template.shape[1] > coarse_gray.shape[1]:
            return -1.0, (0, 0)
        result = self._match_result(coarse_gray, coarse_template, item["flat"])
        best_score, best_loc = -1.0, (0, 0)
        for loc in self._candidate_locs(result, item["flat"], coarse_template.shape):
            score, hit = self._refine(frames[1.0], item["image"], item["flat"], loc, factor)
            if score > best_score:
                best_score, best_loc = score, hit
        return best_score, best_loc

    def _refine(self, gray, template, is_flat, coarse_loc, factor):
        x = int(round(coarse_loc[0] / factor))
        y = int(round(coarse_loc[1] / factor))
        margin = max(REFINE_MARGIN, int(max(template.shape[:2]) * 0.25))
        x1 = max(0, x - margin)
        y1 = max(0, y - margin)
        x2 = min(gray.shape[1], x + template.shape[1] + margin)
        y2 = min(gray.shape[0], y + template.shape[0] + margin)
        patch = gray[y1:y2, x1:x2]
        if template.shape[0] > patch.shape[0] or template.shape[1] > patch.shape[1]:
            return -1.0, (x, y)
        score, loc = self._match_one(patch, template, is_flat)
        return score, (x1 + loc[0], y1 + loc[1])

    def _match_one(self, gray, template, is_flat):
        result = self._match_result(gray, template, is_flat)
        if is_flat:
            min_val, _, min_loc, _ = cv2.minMaxLoc(result)
            return 1 - float(min_val), min_loc
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        return float(max_val), max_loc

    def _match_result(self, gray, template, is_flat):
        method = cv2.TM_SQDIFF_NORMED if is_flat else cv2.TM_CCOEFF_NORMED
        return cv2.matchTemplate(gray, template, method)

    def _candidate_locs(self, result, is_flat, template_shape):
        work = result.copy()
        locs = []
        for _ in range(COARSE_CANDIDATES):
            min_val, _, min_loc, max_loc = cv2.minMaxLoc(work)
            loc = min_loc if is_flat else max_loc
            locs.append(loc)
            radius = max(2, min(template_shape[:2]) // 2)
            x1 = max(0, loc[0] - radius)
            y1 = max(0, loc[1] - radius)
            x2 = min(work.shape[1], loc[0] + radius + 1)
            y2 = min(work.shape[0], loc[1] + radius + 1)
            work[y1:y2, x1:x2] = 2 if is_flat else -2
            if is_flat and min_val >= 1:
                break
        return locs

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
    p.add_argument("--start-minimized", action="store_true")
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

        app_args = []
        if args.smoke_test:
            app_args.append("--smoke-test")
        if args.start_minimized:
            app_args.append("--start-minimized")
        return app_main(app_args)
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
