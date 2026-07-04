import tempfile
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import screen_watch.app as appmod
import screen_watch.core as coremod
from screen_watch.app import app_from_legacy, beep_wave, black_fraction, crop_black_padding, mostly_black, normalize_profile_file, normalize_target_names, parse_positive_float, parse_positive_int, parse_scales, parse_volume, prune_alerts, scan_interval_ms, template_name, window_key
from screen_watch.core import Detector, self_test


class CoreTest(unittest.TestCase):
    def test_template_and_pixel_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = type("Args", (), {"out": Path(tmp), "ocr": False})()
            self_test(args)

    def test_parse_scales(self):
        self.assertEqual(parse_scales("1, 0.9,1.1"), [1.0, 0.9, 1.1])
        self.assertEqual(parse_scales("0.5-0.7:0.1,1"), [0.5, 0.6, 0.7, 1.0])
        self.assertEqual(parse_scales("0.1-0.13:10%"), [0.1, 0.11, 0.121, 0.13])
        with self.assertRaises(ValueError):
            parse_scales("0.5-2.0")
        with self.assertRaises(ValueError):
            parse_scales("0.1-2.0:0.001")

    def test_detector_matches_scaled_template_from_range(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            template = np.zeros((30, 40, 3), dtype=np.uint8)
            template[4:26, 6:34] = [30, 160, 240]
            template[10:20, 14:26] = [250, 230, 20]
            path = base / "target.png"
            Image.fromarray(template).save(path)

            scaled = appmod.cv2.resize(appmod.cv2.imread(str(path), appmod.cv2.IMREAD_GRAYSCALE), (52, 39), interpolation=appmod.cv2.INTER_AREA)
            frame = np.zeros((120, 160, 3), dtype=np.uint8)
            frame[50:89, 70:122] = appmod.cv2.cvtColor(scaled, appmod.cv2.COLOR_GRAY2RGB)
            detector = Detector({"_base_dir": str(base), "targets": [{"id": "target-id", "name": "target", "kind": "template", "path": str(path), "threshold": 0.99, "scales": "1.0-1.5:0.1"}]})
            matches = detector.run(frame)
            self.assertEqual(matches[0]["target"], "target")
            self.assertEqual(matches[0]["target_id"], "target-id")
            self.assertEqual(matches[0]["box"], [70, 50, 122, 89])
            self.assertEqual(matches[0]["scale"], 1.3)

    def test_detector_matches_template_on_large_frame_fast_path(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rng = np.random.default_rng(1)
            template = rng.integers(0, 256, (80, 80, 3), dtype=np.uint8)
            path = base / "target.png"
            Image.fromarray(template).save(path)

            frame = np.zeros((2160, 3840, 3), dtype=np.uint8)
            frame[1700:1780, 3000:3080] = template
            detector = Detector({"_base_dir": str(base), "targets": [{"name": "target", "kind": "template", "path": str(path), "threshold": 0.99, "scales": [1.0]}]})
            matches = detector.run(frame)
            self.assertEqual(matches[0]["box"], [3000, 1700, 3080, 1780])

    def test_detector_large_frame_does_not_skip_unaligned_templates(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rng = np.random.default_rng(2)
            frame = np.zeros((2160, 3840, 3), dtype=np.uint8)
            targets = []
            expected = {}
            positions = [(780, 80), (2180, 80), (80, 530), (780, 530), (2180, 530), (80, 980)]
            for index, (x, y) in enumerate(positions):
                template = rng.integers(0, 256, (80, 80, 3), dtype=np.uint8)
                template[8:32, 9:33] = [(40 + index * 9) % 256, 230, 60]
                path = base / f"target-{index}.png"
                Image.fromarray(template).save(path)
                frame[y : y + 80, x : x + 80] = template
                name = f"target-{index}"
                expected[name] = [x, y, x + 80, y + 80]
                targets.append({"name": name, "kind": "template", "path": str(path), "threshold": 0.99, "scales": [1.0]})

            detector = Detector({"_base_dir": str(base), "targets": targets})
            matches = detector.run(frame)
            boxes = {match["target"]: match["box"] for match in matches}
            self.assertEqual(boxes, expected)

    def test_detector_does_not_miss_when_coarse_template_loses_detail(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pattern = np.indices((80, 80)).sum(axis=0) % 2
            template = np.where(pattern[..., None], 255, 0).astype(np.uint8).repeat(3, axis=2)
            path = base / "target.png"
            Image.fromarray(template).save(path)

            frame = np.zeros((2160, 3840, 3), dtype=np.uint8)
            frame[1000:1080, 1000:1080] = template
            detector = Detector({"_base_dir": str(base), "targets": [{"name": "target", "kind": "template", "path": str(path), "threshold": 0.99, "scales": [1.0]}]})
            matches = detector.run(frame)
            self.assertEqual(matches[0]["box"], [1000, 1000, 1080, 1080])

    def test_parse_positive_int(self):
        self.assertEqual(parse_positive_int("50", "beep_count"), 50)
        with self.assertRaises(ValueError):
            parse_positive_int("0", "beep_count")

    def test_scan_interval_has_safe_floor(self):
        self.assertEqual(scan_interval_ms("1"), appmod.MIN_SCAN_INTERVAL_MS)
        self.assertEqual(scan_interval_ms("250"), 250)

    def test_mostly_black_detects_blank_preview(self):
        import numpy as np

        self.assertTrue(mostly_black(np.zeros((4, 4, 3), dtype=np.uint8)))
        self.assertFalse(mostly_black(np.full((4, 4, 3), 80, dtype=np.uint8)))
        padded = np.zeros((4, 4, 3), dtype=np.uint8)
        padded[:, :2] = 80
        self.assertGreater(black_fraction(padded), 0.4)
        self.assertEqual(crop_black_padding(padded).shape, (4, 2, 3))

    def test_window_capture_falls_back_to_visible_screen_when_printwindow_black(self):
        import numpy as np

        class Rect:
            left, top = 10, 20

        class Shot:
            width, height = 2, 2
            rgb = bytes([10, 20, 30] * 4)

        class Sct:
            def __init__(self):
                self.box = None

            def grab(self, box):
                self.box = box
                return Shot()

        old_capture, old_rect = appmod.capture_window, appmod.window_rect
        try:
            appmod.capture_window = lambda _hwnd: np.zeros((2, 2, 3), dtype=np.uint8)
            appmod.window_rect = lambda _hwnd: (Rect(), 2, 2)
            sct = Sct()
            frame = appmod.capture_window_frame(sct, 123)
            self.assertEqual(sct.box, {"left": 10, "top": 20, "width": 2, "height": 2})
            self.assertEqual(frame.tolist(), [[[10, 20, 30], [10, 20, 30]], [[10, 20, 30], [10, 20, 30]]])
        finally:
            appmod.capture_window, appmod.window_rect = old_capture, old_rect

    def test_window_capture_caches_visible_mode_after_black_printwindow(self):
        import numpy as np

        visible = np.full((2, 2, 3), 80, dtype=np.uint8)
        cache = {}
        with mock.patch.object(appmod, "capture_window", return_value=np.zeros((2, 2, 3), dtype=np.uint8)) as slow, mock.patch.object(appmod, "capture_window_visible", return_value=visible) as fast:
            self.assertIs(appmod.capture_window_frame(object(), 123, cache), visible)
            self.assertEqual(cache, {123: "visible"})
            self.assertIs(appmod.capture_window_frame(object(), 123, cache), visible)
            slow.assert_called_once()
            self.assertEqual(fast.call_count, 2)

    def test_window_capture_crops_printwindow_black_padding_before_visible_fallback(self):
        import numpy as np

        padded = np.zeros((4, 4, 3), dtype=np.uint8)
        padded[:, :2] = 80
        visible = np.full((4, 4, 3), 60, dtype=np.uint8)
        with mock.patch.object(appmod, "capture_window", return_value=padded), mock.patch.object(appmod, "capture_window_visible", return_value=visible):
            frame = appmod.capture_window_frame(object(), 123)
            self.assertEqual(frame.shape, (4, 2, 3))
            self.assertEqual(frame.tolist(), np.full((4, 2, 3), 80, dtype=np.uint8).tolist())

    def test_window_preview_prefers_fast_visible_capture(self):
        import numpy as np

        visible = np.full((4, 4, 3), 80, dtype=np.uint8)
        with mock.patch.object(appmod, "capture_window_visible", return_value=visible) as fast, mock.patch.object(appmod, "capture_window_frame") as slow:
            frame = appmod.capture_window_preview(object(), 123)
            fast.assert_called_once()
            slow.assert_not_called()
            self.assertIs(frame, visible)

    def test_dwm_preview_registers_and_reuses_thumbnail(self):
        app = object.__new__(appmod.App)
        app.dwm_thumbs = {}
        app.root = mock.Mock()
        app.root.winfo_rootx.return_value = 10
        app.root.winfo_rooty.return_value = 20
        app.visible_preview_rect = mock.Mock(return_value=(30, 50, 300, 180))
        with mock.patch.object(appmod, "hwnd_for_tk", return_value=111), mock.patch.object(appmod, "dwm_register", return_value=object()) as register, mock.patch.object(appmod, "dwm_update", return_value=True) as update:
            self.assertTrue(appmod.App.sync_dwm_preview(app, "app:x", object(), 222))
            self.assertTrue(appmod.App.sync_dwm_preview(app, "app:x", object(), 222))
            register.assert_called_once_with(111, 222)
            update.assert_called_once_with(app.dwm_thumbs["app:x"]["thumb"], 20, 30, 300, 180, visible=True)

    def test_dwm_sync_loop_schedules_once(self):
        app = object.__new__(appmod.App)
        app.dwm_sync_job = None
        app.dwm_thumbs = {"app:x": {"hwnd": 222}}
        app.root = mock.Mock()
        app.root.after.return_value = "job"
        appmod.App.ensure_dwm_sync_loop(app)
        appmod.App.ensure_dwm_sync_loop(app)
        app.root.after.assert_called_once_with(appmod.DWM_PREVIEW_SYNC_MS, app.sync_dwm_previews_loop)
        self.assertEqual(app.dwm_sync_job, "job")

    def test_dwm_preview_falls_back_when_widget_not_visible_yet(self):
        app = object.__new__(appmod.App)
        app.dwm_thumbs = {}
        app.visible_preview_rect = mock.Mock(return_value=None)
        self.assertFalse(appmod.App.sync_dwm_preview(app, "app:x", object(), 222))

    def test_dwm_sync_loop_repeats_only_while_layout_busy(self):
        app = object.__new__(appmod.App)
        app.dwm_sync_job = "job"
        app.dwm_thumbs = {"app:x": {"hwnd": 222}}
        app.source_widgets = {"app:x": {"area": object()}}
        app.sync_dwm_preview = mock.Mock()
        app.layout_busy = mock.Mock(return_value=True)
        app.ensure_dwm_sync_loop = mock.Mock()
        appmod.App.sync_dwm_previews_loop(app)
        app.sync_dwm_preview.assert_called_once_with("app:x", app.source_widgets["app:x"]["area"], 222)
        app.ensure_dwm_sync_loop.assert_called_once()

    def test_suspend_dwm_preview_unregisters_overlay(self):
        app = object.__new__(appmod.App)
        thumb = object()
        app.dwm_thumbs = {"app:x": {"thumb": thumb, "hwnd": 222, "rect": (1, 2, 3, 4)}}
        app.dwm_sync_job = None
        app.source_widgets = {}
        with mock.patch.object(appmod, "dwm_unregister") as unregister:
            appmod.App.suspend_dwm_previews(app)
            unregister.assert_called_once_with(thumb)
            self.assertEqual(app.dwm_thumbs, {})

    def test_suspend_dwm_preview_does_not_replace_underlay(self):
        app = object.__new__(appmod.App)
        thumb = object()
        image = mock.Mock()
        image.image = None
        area = mock.Mock()
        area.winfo_width.return_value = 300
        area.winfo_height.return_value = 180
        app.dwm_thumbs = {"app:x": {"thumb": thumb, "hwnd": 222, "rect": (1, 2, 3, 4)}}
        app.dwm_sync_job = "job"
        app.root = mock.Mock()
        app.source_widgets = {"app:x": {"area": area, "image": image}}
        app.placeholder_image = mock.Mock(return_value="photo")
        with mock.patch.object(appmod, "dwm_unregister") as unregister:
            appmod.App.suspend_dwm_previews(app)
            app.root.after_cancel.assert_called_once_with("job")
            image.place.assert_not_called()
            image.configure.assert_not_called()
            app.placeholder_image.assert_not_called()
            unregister.assert_called_once_with(thumb)

    def test_layout_drag_suspends_dwm_preview(self):
        app = object.__new__(appmod.App)
        app.layout_active_until = 0
        app.ensure_dwm_sync_loop = mock.Mock()
        app.suspend_dwm_previews = mock.Mock()
        appmod.App.begin_layout_drag(app)
        app.ensure_dwm_sync_loop.assert_not_called()
        app.suspend_dwm_previews.assert_called_once()

    def test_hide_to_tray_unregisters_dwm_preview_before_withdraw(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.ensure_tray_icon = mock.Mock(return_value=True)
        app.disable_source_previews = mock.Mock()
        app.status = mock.Mock()
        app.hide_to_tray_pending = False
        appmod.App.hide_to_tray(app)
        self.assertTrue(app.hide_to_tray_pending)
        app.disable_source_previews.assert_called_once()
        app.root.withdraw.assert_called_once()

    def test_hide_to_tray_reenables_previews_when_tray_unavailable(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.ensure_tray_icon = mock.Mock(return_value=False)
        app.disable_source_previews = mock.Mock()
        app.enable_source_previews = mock.Mock()
        app.status = mock.Mock()
        app.hide_to_tray_pending = False
        appmod.App.hide_to_tray(app)
        self.assertFalse(app.hide_to_tray_pending)
        app.enable_source_previews.assert_called_once_with(250)
        app.status.set.assert_not_called()

    def test_screen_preview_captures_real_frame(self):
        import numpy as np

        class Shot:
            width, height = 2, 2
            rgb = bytes([10, 20, 30] * 4)

        class Sct:
            monitors = [{}, {"left": 0, "top": 0, "width": 2, "height": 2}]

            def grab(self, box):
                self.box = box
                return Shot()

        app = object.__new__(appmod.App)
        source = {"available": True, "kind": "screen", "source": {"monitor": 1, "left": 0, "top": 0, "width": 2, "height": 2}}
        sct = Sct()
        frame = appmod.App.capture_preview_frame(app, sct, source)
        self.assertFalse(mostly_black(frame))
        self.assertEqual(frame.tolist(), np.full((2, 2, 3), [10, 20, 30], dtype=np.uint8).tolist())

    def test_screen_preview_frame_is_reused_until_source_changes(self):
        app = object.__new__(appmod.App)
        app.preview_signatures = {"screen:monitor-2": ("screen", 2, 3840, 0, 3840, 2160)}
        app.preview_frames = {"screen:monitor-2": object()}
        source = {"key": "screen:monitor-2", "kind": "screen", "signature": ("screen", 2, 3840, 0, 3840, 2160)}
        self.assertTrue(appmod.App.preview_frame_current(app, source))
        source["signature"] = ("screen", 2, 3840, 0, 100, 100)
        self.assertFalse(appmod.App.preview_frame_current(app, source))

    def test_refresh_source_previews_gives_worker_signed_sources(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.preview_job = None
        app.source_previews_enabled = True
        app.layout_busy = mock.Mock(return_value=False)
        app.selected_regions = mock.Mock(return_value=[{"name": "monitor-2", "monitor": 2}])
        app.monitor_info = {2: {"left": 3840, "top": 0, "width": 3840, "height": 2160}}
        app.selected_apps = []
        app.window_info = {}
        app.preview_sources = []
        app.preview_frames = {}
        app.preview_signatures = {}
        app.preview_lock = mock.MagicMock()
        app.source_canvas = mock.Mock()
        app.source_canvas.winfo_width.return_value = 400
        app.source_frame = mock.Mock()
        app.source_widgets = {
            "screen:monitor-2": {
                "frame": mock.Mock(),
                "area": mock.Mock(),
                "image": mock.Mock(),
                "name": mock.Mock(),
            }
        }
        app.dwm_thumbs = {}
        app.ensure_dwm_sync_loop = mock.Mock()
        app.placeholder_image = mock.Mock(return_value="photo")

        appmod.App.refresh_source_previews(app)

        self.assertEqual(app.preview_sources[0]["signature"], ("screen", 2, 3840, 0, 3840, 2160))
        app.root.after.assert_called_once_with(appmod.SOURCE_PREVIEW_SYNC_MS, app.refresh_source_previews)

    def test_refresh_source_previews_skips_full_rebuild_while_dragging(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.preview_job = None
        app.source_previews_enabled = True
        app.layout_busy = mock.Mock(return_value=True)
        app.source_widgets = {"app:x": {"frame": mock.Mock()}}
        app.ensure_dwm_sync_loop = mock.Mock()
        app.selected_regions = mock.Mock()
        appmod.App.refresh_source_previews(app)
        app.selected_regions.assert_not_called()
        app.root.after.assert_called_once_with(appmod.SOURCE_PREVIEW_SYNC_MS, app.refresh_source_previews)

    def test_refresh_source_previews_waits_while_startup_layout_is_busy(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.preview_job = None
        app.source_previews_enabled = True
        app.layout_busy = mock.Mock(return_value=True)
        app.source_widgets = {}
        app.ensure_dwm_sync_loop = mock.Mock()
        app.selected_regions = mock.Mock()
        appmod.App.refresh_source_previews(app)
        app.selected_regions.assert_not_called()
        app.root.after.assert_called_once_with(appmod.SOURCE_PREVIEW_SYNC_MS, app.refresh_source_previews)

    def test_schedule_source_previews_keeps_single_timer(self):
        app = object.__new__(appmod.App)
        app.preview_job = "old"
        app.source_previews_enabled = True
        app.root = mock.Mock()
        app.root.after.return_value = "new"
        appmod.App.schedule_source_previews(app, 25)
        app.root.after_cancel.assert_called_once_with("old")
        app.root.after.assert_called_once_with(25, app.refresh_source_previews)
        self.assertEqual(app.preview_job, "new")

    def test_schedule_source_previews_is_idle_until_enabled(self):
        app = object.__new__(appmod.App)
        app.preview_job = None
        app.source_previews_enabled = False
        app.root = mock.Mock()
        appmod.App.schedule_source_previews(app, 25)
        app.root.after.assert_not_called()
        self.assertIsNone(app.preview_job)
        appmod.App.enable_source_previews(app, 25)
        app.root.after.assert_called_once_with(25, app.refresh_source_previews)

    def test_parse_positive_float(self):
        self.assertEqual(parse_positive_float("3.5", "beep_seconds"), 3.5)
        with self.assertRaises(ValueError):
            parse_positive_float("0", "beep_seconds")

    def test_beep_volume_is_clamped_and_changes_wave_amplitude(self):
        import io
        import struct
        import wave

        def peak(data):
            with wave.open(io.BytesIO(data), "rb") as wav:
                samples = struct.unpack("<" + "h" * (wav.getnframes()), wav.readframes(wav.getnframes()))
            return max(abs(item) for item in samples)

        self.assertEqual(parse_volume("-2"), 0)
        self.assertEqual(parse_volume("150"), 100)
        self.assertGreater(peak(beep_wave(100, milliseconds=20)), peak(beep_wave(10, milliseconds=20)))

    def test_beep_for_uses_supported_winsound_flags(self):
        calls = []
        fake_winsound = types.SimpleNamespace(SND_MEMORY=4, PlaySound=lambda data, flags: calls.append((data, flags)))
        with mock.patch.dict(sys.modules, {"winsound": fake_winsound}), mock.patch.object(appmod, "beep_wave", return_value=b"wav"):
            appmod.beep_for(0.001, 100)
        self.assertTrue(calls)
        self.assertEqual({flags for _data, flags in calls}, {fake_winsound.SND_MEMORY})

    def test_template_name_uses_profile_count_date(self):
        self.assertEqual(template_name(1, 11, "20260701"), "1-11-20260701")
        self.assertRegex(template_name(5, 2), r"^5-2-\d{20}$")

    def test_normalize_target_names_fills_deleted_number_gap(self):
        old_data, old_thumbs = appmod.DATA_DIR, appmod.THUMBS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "data"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            try:
                targets = []
                for name in ("1-1-old-a", "1-3-old-b"):
                    template = appmod.DATA_DIR / "templates" / f"{name}.png"
                    thumb = appmod.THUMBS_DIR / f"{name}.png"
                    template.parent.mkdir(parents=True, exist_ok=True)
                    thumb.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (4, 4), "red").save(template)
                    Image.new("RGB", (4, 4), "blue").save(thumb)
                    targets.append({"name": name, "path": str(template), "thumb": str(thumb)})
                renamed, changed = normalize_target_names(targets, 1)
                self.assertTrue(changed)
                self.assertEqual([Path(t["path"]).stem for t in renamed], ["1-1-old-a", "1-2-old-b"])
                self.assertEqual([t["id"] for t in renamed], ["old-a", "old-b"])
                self.assertEqual([t["hit_count"] for t in renamed], [0, 0])
                self.assertTrue(Path(renamed[1]["thumb"]).exists())
                self.assertFalse((appmod.DATA_DIR / "templates" / "1-3-old-b.png").exists())
            finally:
                appmod.DATA_DIR, appmod.THUMBS_DIR = old_data, old_thumbs

    def test_normalize_profile_file_updates_saved_paths(self):
        old_data, old_profiles, old_thumbs = appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.THUMBS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "data"
            appmod.PROFILES_DIR = appmod.DATA_DIR / "profiles"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            try:
                targets = []
                for name in ("1-1-old", "1-3-old"):
                    template = appmod.DATA_DIR / "templates" / f"{name}.png"
                    thumb = appmod.THUMBS_DIR / f"{name}.png"
                    template.parent.mkdir(parents=True, exist_ok=True)
                    thumb.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (4, 4), "red").save(template)
                    Image.new("RGB", (4, 4), "blue").save(thumb)
                    targets.append({"name": name, "path": str(template), "thumb": str(thumb)})
                appmod.write_json(appmod.PROFILES_DIR / "profile_1.json", {"targets": targets})
                self.assertTrue(normalize_profile_file(1))
                data = appmod.load_json(appmod.PROFILES_DIR / "profile_1.json", {})
                self.assertEqual([Path(t["path"]).stem for t in data["targets"]], ["1-1-old", "1-2-old"])
            finally:
                appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.THUMBS_DIR = old_data, old_profiles, old_thumbs

    def test_window_selection_keys_and_legacy_profile(self):
        self.assertEqual(window_key("Demo", 2), "Demo" + "\0" + "2")
        self.assertEqual(app_from_legacy("Demo"), {"title": "Demo", "ordinal": 1})
        self.assertEqual(app_from_legacy({"title": "Demo", "ordinal": 2}), {"title": "Demo", "ordinal": 2})

    def test_profile_roundtrip(self):
        old_data, old_profiles, old_state = appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH
        old_legacy = appmod.LEGACY_DATA_DIR
        old_thumbs, old_alerts = appmod.THUMBS_DIR, appmod.ALERTS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "app_data"
            appmod.LEGACY_DATA_DIR = base / "missing_legacy"
            appmod.PROFILES_DIR = appmod.DATA_DIR / "profiles"
            appmod.STATE_PATH = appmod.DATA_DIR / "state.json"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            appmod.ALERTS_DIR = appmod.DATA_DIR / "screenshots"
            try:
                root = appmod.Tk()
                root.withdraw()
                app = appmod.App(root)
                app.current_profile = 5
                app.profile.set(5)
                app.max_alerts.set(10)
                app.max_templates.set(20)
                template = appmod.DATA_DIR / "templates" / "target.png"
                template.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (12, 10), "red").save(template)
                app.targets = [{"name": "target", "path": str(template), "size": "12x10", "enabled": False}]
                app.left.set("11")
                app.top.set("22")
                app.width.set("333")
                app.height.set("444")
                app.beep_seconds.set(5)
                app.beep_volume.set(42)
                app.save_current_profile()
                root.destroy()

                root2 = appmod.Tk()
                root2.withdraw()
                app2 = appmod.App(root2)
                self.assertEqual(app2.current_profile, 5)
                self.assertEqual(app2.left.get(), "11")
                self.assertEqual(app2.width.get(), "333")
                self.assertEqual(app2.beep_seconds.get(), 5)
                self.assertEqual(app2.beep_volume.get(), 42)
                self.assertEqual(app2.max_alerts.get(), 10)
                self.assertEqual(app2.max_templates.get(), 20)
                self.assertEqual(len(app2.targets), 1)
                self.assertFalse(app2.targets[0]["enabled"])
                self.assertIn("layout", appmod.load_json(appmod.STATE_PATH, {}))
                root2.destroy()
            finally:
                appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH = old_data, old_profiles, old_state
                appmod.LEGACY_DATA_DIR = old_legacy
                appmod.THUMBS_DIR, appmod.ALERTS_DIR = old_thumbs, old_alerts

    def test_profile_restores_no_selected_monitors(self):
        old_data, old_profiles, old_state = appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH
        old_legacy = appmod.LEGACY_DATA_DIR
        old_thumbs, old_alerts = appmod.THUMBS_DIR, appmod.ALERTS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "app_data"
            appmod.LEGACY_DATA_DIR = base / "missing_legacy"
            appmod.PROFILES_DIR = appmod.DATA_DIR / "profiles"
            appmod.STATE_PATH = appmod.DATA_DIR / "state.json"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            appmod.ALERTS_DIR = appmod.DATA_DIR / "screenshots"
            try:
                root = appmod.Tk()
                root.withdraw()
                app = appmod.App(root)
                for var in app.monitor_vars.values():
                    var.set(False)
                app.save_current_profile()
                root.destroy()

                root2 = appmod.Tk()
                root2.withdraw()
                app2 = appmod.App(root2)
                self.assertEqual([i for i, var in app2.monitor_vars.items() if var.get()], [])
                root2.destroy()
            finally:
                appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH = old_data, old_profiles, old_state
                appmod.LEGACY_DATA_DIR = old_legacy
                appmod.THUMBS_DIR, appmod.ALERTS_DIR = old_thumbs, old_alerts

    def test_entry_click_keeps_cursor_at_end(self):
        root = appmod.Tk()
        try:
            app = object.__new__(appmod.App)
            value = appmod.StringVar(value="250")
            entry = appmod.App.make_entry(app, root, value)
            self.assertEqual(entry.cget("justify"), "left")
            entry.pack()
            root.update()
            entry.event_generate("<ButtonPress-1>", x=2, y=2)
            entry.event_generate("<ButtonRelease-1>", x=2, y=2)
            root.update()
            root.after(80, root.quit)
            root.mainloop()
            self.assertEqual(entry.index("insert"), 3)
        finally:
            root.destroy()

    def test_custom_check_indicator_scales(self):
        root = appmod.Tk()
        try:
            app = object.__new__(appmod.App)
            app.last_scale = 2.0
            app.check_widgets = []
            app.fonts = {"TkDefaultFont": appmod.tkfont.nametofont("TkDefaultFont")}
            var = appmod.BooleanVar(value=True)
            check = appmod.App.make_check(app, root, var, "匹配")
            check.pack()
            root.update()
            box = check._check_parts[0]
            self.assertGreaterEqual(int(box.cget("width")), 26)
            check.event_generate("<Button-1>")
            self.assertFalse(var.get())
        finally:
            root.destroy()

    def test_resize_ignores_window_moves_without_size_change(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.state.return_value = "normal"
        app.last_root_size = (980, 680)
        app.resize_job = None
        app.move_active_until = 0
        event = type("Event", (), {"widget": app.root, "width": 980, "height": 680})()
        appmod.App.on_resize(app, event)
        app.root.after.assert_not_called()
        self.assertTrue(app.move_active_until > 0)

    def test_vertical_resize_does_not_reset_horizontal_panes(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.state.return_value = "normal"
        app.last_root_size = (980, 680)
        app.resize_job = None
        app.ensure_dwm_sync_loop = mock.Mock()
        app.suspend_dwm_previews = mock.Mock()
        event = type("Event", (), {"widget": app.root, "width": 980, "height": 720})()
        appmod.App.on_resize(app, event)
        app.ensure_dwm_sync_loop.assert_not_called()
        app.suspend_dwm_previews.assert_called_once()

        app.left_ratio = 0.5
        app.main_pane = mock.Mock()
        app.left_pane = mock.Mock()
        app.left_pane.winfo_height.return_value = 500
        appmod.App.restore_layout(app, horizontal=False)
        app.main_pane.sash_place.assert_not_called()
        app.left_pane.sash_place.assert_called_once_with(0, 0, 250)

    def test_taskbar_minimize_configure_does_not_redraw_or_save_geometry(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.state.return_value = "iconic"
        app.last_root_size = (1200, 700)
        app.resize_job = "job"
        app.resize_active_until = appmod.time.time() + 1
        app.last_window_geometry = "1200x700+30+40"
        app.ensure_dwm_sync_loop = mock.Mock()
        app.suspend_dwm_previews = mock.Mock()
        event = type("Event", (), {"widget": app.root, "width": 160, "height": 28})()
        appmod.App.on_resize(app, event)
        app.root.after_cancel.assert_called_once_with("job")
        app.root.after.assert_not_called()
        app.ensure_dwm_sync_loop.assert_not_called()
        app.suspend_dwm_previews.assert_not_called()
        self.assertIsNone(app.resize_job)
        self.assertEqual(app.last_root_size, (1200, 700))
        self.assertEqual(app.last_window_geometry, "1200x700+30+40")

    def test_restore_layout_waits_until_panes_are_mapped(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.state.return_value = "withdrawn"
        app.root.winfo_width.return_value = 1
        app.root.after.return_value = "job"
        app.layout_restore_job = None
        app.left_pane = mock.Mock()
        app.left_pane.winfo_height.return_value = 1
        app.main_pane = mock.Mock()
        appmod.App.restore_layout(app)
        app.main_pane.sash_place.assert_not_called()
        app.left_pane.sash_place.assert_not_called()
        self.assertEqual(app.layout_restore_job, "job")

    def test_restore_layout_applies_horizontal_when_vertical_not_ready(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.state.return_value = "normal"
        app.root.winfo_width.return_value = 1200
        app.root.after.return_value = "job"
        app.layout_restore_job = None
        app.main_ratio = 0.42
        app.right_ratio = 0.25
        app.left_ratio = 0.5
        app.left_pane = mock.Mock()
        app.left_pane.winfo_height.return_value = 1
        app.main_pane = mock.Mock()
        appmod.App.restore_layout(app)
        app.main_pane.sash_place.assert_has_calls([mock.call(0, 504, 0), mock.call(1, 804, 0)])
        app.left_pane.sash_place.assert_not_called()
        app.root.after.assert_called_once()

    def test_left_ratio_uses_left_pane_height(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.winfo_width.return_value = 1000
        app.main_pane = mock.Mock()
        app.main_pane.sash_coord.side_effect = [(700, 0), (900, 0), (700, 0)]
        app.left_pane = mock.Mock()
        app.left_pane.winfo_height.return_value = 500
        app.left_pane.sash_coord.return_value = (0, 300)
        appmod.App.capture_layout_ratios(app)
        self.assertEqual(app.left_ratio, 0.6)

    def test_layout_drag_release_saves_state(self):
        app = object.__new__(appmod.App)
        app.save_state = mock.Mock()
        app.schedule_source_previews = mock.Mock()
        appmod.App.end_layout_drag(app)
        app.save_state.assert_called_once()
        app.schedule_source_previews.assert_called_once_with(0)

    def test_resize_remembers_full_window_geometry(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.state.return_value = "normal"
        app.root.winfo_x.return_value = 30
        app.root.winfo_y.return_value = 40
        app.last_window_geometry = "980x680+0+0"
        appmod.App.remember_window_geometry(app, 1200, 720)
        self.assertEqual(app.last_window_geometry, "1200x720+30+40")

    def test_save_state_keeps_last_visible_geometry_when_hidden(self):
        old_state = appmod.STATE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            appmod.STATE_PATH = Path(tmp) / "state.json"
            try:
                app = object.__new__(appmod.App)
                app.root = mock.Mock()
                app.root.state.return_value = "withdrawn"
                app.root.geometry.return_value = "1x1+-32000+-32000"
                app.last_window_geometry = "1400x900+120+80"
                app.current_profile = 1
                app.main_ratio = 0.7
                app.right_ratio = 0.2
                app.left_ratio = 0.5
                app.max_alerts = mock.Mock()
                app.max_alerts.get.return_value = 10
                app.capture_layout_ratios = mock.Mock()
                appmod.App.save_state(app)
                data = appmod.load_json(appmod.STATE_PATH, {})
                self.assertEqual(data["layout"]["geometry"], "1400x900+120+80")
            finally:
                appmod.STATE_PATH = old_state

    def test_current_window_geometry_keeps_last_visible_geometry_when_iconic(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.state.return_value = "iconic"
        app.root.geometry.return_value = "160x28+-32000+-32000"
        app.last_window_geometry = "1200x700+30+40"
        self.assertEqual(appmod.App.current_window_geometry(app), "1200x700+30+40")

    def test_autohide_scrollbar_only_maps_when_needed(self):
        root = appmod.Tk()
        try:
            canvas = appmod.Canvas(root, width=100, height=100, highlightthickness=0)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar = appmod.ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
            app = object.__new__(appmod.App)
            appmod.App.configure_autohide_scrollbar(app, canvas, scrollbar, side="right", fill="y")

            canvas.configure(scrollregion=(0, 0, 100, 80))
            root.update_idletasks()
            self.assertFalse(scrollbar.winfo_ismapped())

            canvas.configure(scrollregion=(0, 0, 100, 220))
            root.update_idletasks()
            self.assertTrue(scrollbar.winfo_ismapped())

            canvas.configure(scrollregion=(0, 0, 100, 80))
            root.update_idletasks()
            self.assertFalse(scrollbar.winfo_ismapped())
        finally:
            root.destroy()

    def test_apply_scale_does_not_reset_panes_on_outer_resize(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.winfo_width.return_value = 1200
        app.root.winfo_height.return_value = 700
        app.last_root_size = (1200, 700)
        app.resize_job = object()
        app.last_scale = 1.0
        app.fonts = {}
        app.base_font_sizes = {}
        app.style = mock.Mock()
        app.redraw_checks = mock.Mock()
        app.reload_target_list = mock.Mock()
        app.restore_layout = mock.Mock()
        app.mouse_button_down = mock.Mock(return_value=False)
        appmod.App.apply_scale(app)
        app.restore_layout.assert_not_called()

    def test_apply_scale_waits_while_mouse_is_down(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.resize_job = None
        app.mouse_button_down = mock.Mock(return_value=True)
        appmod.App.apply_scale(app)
        app.root.after.assert_called_once_with(120, app.apply_scale)

    def test_apply_scale_force_runs_before_startup_show(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.root.winfo_width.return_value = 1200
        app.root.winfo_height.return_value = 700
        app.last_root_size = (1200, 700)
        app.resize_job = object()
        app.last_scale = 1.0
        app.fonts = {}
        app.base_font_sizes = {}
        app.style = mock.Mock()
        app.redraw_checks = mock.Mock()
        app.reload_target_list = mock.Mock()
        app.schedule_source_previews = mock.Mock()
        app.mouse_button_down = mock.Mock(return_value=True)
        appmod.App.apply_scale(app, force=True)
        app.root.after.assert_not_called()
        app.reload_target_list.assert_called_once()

    def test_main_restores_layout_after_window_is_shown(self):
        root = mock.Mock()
        root.winfo_width.return_value = 980
        root.winfo_height.return_value = 680
        app = mock.Mock()
        with mock.patch.object(appmod, "claim_single_instance", return_value=False), mock.patch.object(appmod, "Tk", return_value=root), mock.patch.object(appmod, "App", return_value=app):
            self.assertEqual(appmod.main([]), 0)
        root.deiconify.assert_called_once()
        root.lift.assert_called_once()
        root.attributes.assert_not_called()
        root.after.assert_any_call(350, app.restore_layout)
        root.after.assert_any_call(500, app.enable_source_previews)
        root.mainloop.assert_called_once()

    def test_main_start_minimized_stays_in_tray(self):
        root = mock.Mock()
        root.winfo_width.return_value = 980
        root.winfo_height.return_value = 680
        app = mock.Mock()
        with mock.patch.object(appmod, "claim_single_instance", return_value=False), mock.patch.object(appmod, "Tk", return_value=root), mock.patch.object(appmod, "App", return_value=app):
            self.assertEqual(appmod.main(["--start-minimized"]), 0)
        root.deiconify.assert_not_called()
        root.lift.assert_not_called()
        app.ensure_tray_icon.assert_called_once_with(show_errors=False)
        root.attributes.assert_not_called()
        root.after.assert_not_called()
        root.mainloop.assert_called_once()

    def test_core_app_forwards_start_minimized(self):
        with mock.patch.object(appmod, "main", return_value=0) as app_main:
            self.assertEqual(coremod.main(["app", "--start-minimized"]), 0)
        app_main.assert_called_once_with(["--start-minimized"])

    def test_startup_arguments_start_minimized(self):
        self.assertEqual(appmod.startup_arguments(Path("ScreenWatchOCR.exe")), "--start-minimized")
        self.assertEqual(appmod.startup_arguments(Path(sys.executable)), "-m screen_watch app --start-minimized")

    def test_main_exits_when_existing_instance_accepts_wake(self):
        with mock.patch.object(appmod, "claim_single_instance", return_value=None), mock.patch.object(appmod, "Tk") as tk:
            self.assertEqual(appmod.main([]), 0)
        tk.assert_not_called()

    def test_preview_height_tracks_source_aspect(self):
        app = object.__new__(appmod.App)
        source = {"source": {"width": 1920, "height": 1080}}
        self.assertEqual(appmod.App.preview_height(app, source, 260), 146)

    def test_side_panes_stay_equal_and_bounded(self):
        app = object.__new__(appmod.App)
        app.right_ratio = 0.5
        self.assertEqual(appmod.App.side_pane_width(app, 2388), 955)
        app.right_ratio = 0.16
        self.assertEqual(appmod.App.side_pane_width(app, 1453), 320)

    def test_horizontal_sashes_restore_saved_three_column_widths(self):
        app = object.__new__(appmod.App)
        app.main_ratio = 0.42
        app.right_ratio = 0.25
        self.assertEqual(appmod.App.horizontal_sashes(app, 1200), (504, 804))

    def test_layout_busy_covers_resize_and_pane_drag(self):
        app = object.__new__(appmod.App)
        app.resize_active_until = 0
        app.layout_active_until = 0
        app.move_active_until = 0
        app.mouse_button_down = mock.Mock(return_value=False)
        self.assertFalse(appmod.App.layout_busy(app))
        appmod.App.begin_layout_drag(app)
        self.assertTrue(appmod.App.layout_busy(app))
        app.layout_active_until = 0
        app.resize_active_until = appmod.time.time() + 1
        self.assertTrue(appmod.App.layout_busy(app))
        app.resize_active_until = 0
        app.move_active_until = appmod.time.time() + 1
        self.assertTrue(appmod.App.layout_busy(app))
        app.move_active_until = 0
        app.mouse_button_down.return_value = True
        self.assertTrue(appmod.App.layout_busy(app))

    def test_window_refresh_waits_while_layout_busy(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.layout_busy = mock.Mock(return_value=True)
        app.refresh_windows = mock.Mock()
        appmod.App.refresh_windows_loop(app)
        app.refresh_windows.assert_not_called()
        app.root.after.assert_called_once_with(2000, app.refresh_windows_loop)

    def test_poll_events_waits_while_layout_busy(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.layout_busy = mock.Mock(return_value=True)
        app.events = mock.Mock()
        appmod.App.poll_events(app)
        app.events.get_nowait.assert_not_called()
        app.root.after.assert_called_once_with(100, app.poll_events)

    def test_poll_events_records_target_hit_counts(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.layout_busy = mock.Mock(return_value=False)
        app.events = appmod.queue.Queue()
        app.events.put(("target_hits", ["a", "b", "a"]))
        app.targets = [{"id": "a", "hit_count": 2}, {"id": "b"}]
        app.thumb_cache = {"old": object()}
        app.reload_target_list = mock.Mock()
        app.save_current_profile = mock.Mock()
        app.status = mock.Mock()
        app.log = mock.Mock()
        appmod.App.poll_events(app)
        self.assertEqual([target["hit_count"] for target in app.targets], [4, 1])
        self.assertEqual(app.thumb_cache, {})
        app.reload_target_list.assert_called_once()
        app.save_current_profile.assert_called_once()
        app.status.set.assert_not_called()
        app.log.insert.assert_not_called()
        app.root.after.assert_called_once_with(100, app.poll_events)

    def test_horizontal_resize_stretches_left_pane_only(self):
        root = appmod.Tk()
        root.geometry("1000x700")
        try:
            app = object.__new__(appmod.App)
            app.root = root
            app.layout = {}
            app.right_ratio = 0.16
            app.left_ratio = 0.58
            app.window_choices = []
            app.window_info = {}
            app.window_choice = appmod.StringVar(value="选择应用...")
            app.selected_apps = []
            app.selected_app_widgets = {}
            app.selected_empty_app_label = None
            app.source_widgets = {}
            app.preview_lock = mock.Mock()
            app.preview_sources = []
            app.preview_frames = {}
            app.preview_signatures = {}
            app.monitor_vars = {}
            app.monitor_info = {}
            app.profile = appmod.IntVar(value=1)
            app.startup_enabled = appmod.BooleanVar(value=False)
            app.threshold = appmod.DoubleVar(value=0.9)
            app.scales = appmod.StringVar(value="1.0")
            app.interval_ms = appmod.IntVar(value=250)
            app.cooldown = appmod.DoubleVar(value=1.0)
            app.beep_seconds = appmod.DoubleVar(value=3.0)
            app.beep_volume = appmod.IntVar(value=100)
            app.max_templates = appmod.IntVar(value=100)
            app.max_alerts = appmod.IntVar(value=50)
            app.beep = appmod.BooleanVar(value=True)
            app.left = appmod.StringVar(value="0")
            app.top = appmod.StringVar(value="0")
            app.width = appmod.StringVar(value="")
            app.height = appmod.StringVar(value="")
            app.status = appmod.StringVar(value="")
            app.fonts = {name: appmod.tkfont.nametofont(name) for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont")}
            app.style = appmod.ttk.Style()
            app.check_widgets = []
            app.thumb_w = 128
            app.thumb_h = 88
            app.last_scale = 1.0
            app.targets = []
            app.target_vars = []
            app.thumb_refs = []
            app.thumb_cache = {}
            app.selected_target = None
            app._build()
            self.assertEqual(app.main_pane.cget("opaqueresize"), 0)
            self.assertEqual(app.main_pane.cget("proxyborderwidth"), 1)
            self.assertEqual(app.main_pane.cget("proxyrelief"), "flat")
            self.assertEqual(len(app.main_pane.panes()), 3)
            self.assertTrue(hasattr(app, "source_canvas"))
            root.update_idletasks()
            app.main_pane.sash_place(0, 500, 0)
            app.main_pane.sash_place(1, 800, 0)
            root.update_idletasks()
            before_right = app.main_pane.sash_coord(1)[0] - app.main_pane.sash_coord(0)[0]
            before_preview = app.main_pane.winfo_width() - app.main_pane.sash_coord(1)[0]
            root.geometry("1200x700")
            root.update_idletasks()
            after_right = app.main_pane.sash_coord(1)[0] - app.main_pane.sash_coord(0)[0]
            after_preview = app.main_pane.winfo_width() - app.main_pane.sash_coord(1)[0]
            self.assertAlmostEqual(after_right, before_right, delta=8)
            self.assertAlmostEqual(after_preview, before_preview, delta=8)
        finally:
            root.destroy()

    def test_selected_apps_reload_reuses_existing_rows(self):
        app = object.__new__(appmod.App)
        app.selected_app_frame = mock.Mock()
        app.scroll_right = mock.Mock()
        app.selected_apps = [{"title": "Demo", "ordinal": 1}]
        app.window_info = {window_key("Demo", 1): {"display": "Demo", "title": "Demo", "ordinal": 1}}
        row = mock.Mock()
        label = mock.Mock()
        app.selected_app_widgets = {window_key("Demo", 1): {"row": row, "label": label, "text": "Demo"}}
        app.selected_empty_app_label = None
        appmod.App.reload_selected_apps(app)
        row.destroy.assert_not_called()
        label.configure.assert_not_called()

    def test_show_window_keeps_existing_tray_icon(self):
        root = appmod.Tk()
        try:
            root.withdraw()
            app = object.__new__(appmod.App)
            app.root = root
            marker = object()
            app.tray_icon = marker
            appmod.App.show_window(app)
            self.assertIs(app.tray_icon, marker)
        finally:
            root.destroy()

    def test_window_unmap_covers_dynamic_canvases_before_taskbar_restore(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.hide_to_tray_pending = False
        app.show_restore_overlays = mock.Mock()
        app.disable_source_previews = mock.Mock()
        event = type("Event", (), {"widget": app.root})()
        appmod.App.on_window_unmapped(app, event)
        app.show_restore_overlays.assert_called_once()
        app.disable_source_previews.assert_called_once()

    def test_window_unmap_from_tray_does_not_cover_dynamic_canvases(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.hide_to_tray_pending = True
        app.clear_restore_overlays = mock.Mock()
        app.show_restore_overlays = mock.Mock()
        app.suspend_dwm_previews = mock.Mock()
        event = type("Event", (), {"widget": app.root})()
        appmod.App.on_window_unmapped(app, event)
        self.assertFalse(app.hide_to_tray_pending)
        app.clear_restore_overlays.assert_called_once()
        app.show_restore_overlays.assert_not_called()
        app.suspend_dwm_previews.assert_called_once()

    def test_restore_overlay_covers_black_canvas_with_window_background(self):
        root = appmod.Tk()
        try:
            root.withdraw()
            app = object.__new__(appmod.App)
            app.root = root
            app.restore_overlay_items = []
            app.target_canvas = appmod.Canvas(root, bg="#000000", width=120, height=80)
            app.target_canvas.pack()
            app.right_canvas = None
            app.source_canvas = None
            appmod.App.show_restore_overlays(app)
            self.assertEqual(len(app.restore_overlay_items), 1)
            overlay = app.restore_overlay_items[0]
            self.assertEqual(overlay.cget("bg"), root.cget("bg"))
            self.assertEqual(overlay.winfo_manager(), "place")
        finally:
            root.destroy()

    def test_window_map_clears_restore_overlay_after_tk_repaints(self):
        app = object.__new__(appmod.App)
        app.root = mock.Mock()
        app.restore_overlay_items = [mock.Mock()]
        app.enable_source_previews = mock.Mock()
        app.clear_restore_overlays = mock.Mock()
        event = type("Event", (), {"widget": app.root})()
        appmod.App.on_window_mapped(app, event)
        app.enable_source_previews.assert_called_once_with(0)
        app.root.after.assert_called_once_with(appmod.RESTORE_OVERLAY_CLEAR_MS, app.clear_restore_overlays)

    def test_single_instance_notification_wakes_existing_app(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((appmod.INSTANCE_HOST, 0))
            port = probe.getsockname()[1]

        app = object.__new__(appmod.App)
        app.close_event = appmod.threading.Event()
        app.root = mock.Mock()
        app.root.after.side_effect = lambda _delay, func: func()
        app.show_window = mock.Mock()
        with mock.patch.object(appmod, "INSTANCE_PORT", port):
            sock = appmod.claim_single_instance()
            try:
                appmod.App.start_instance_listener(app, sock)
                self.assertTrue(appmod.notify_existing_instance())
                app.show_window.assert_called_once()
            finally:
                app.close_event.set()
                if app.instance_socket:
                    app.instance_socket.close()

    def test_remove_selected_deletes_template_and_thumb_files(self):
        old_data, old_thumbs = appmod.DATA_DIR, appmod.THUMBS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "data"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            template = appmod.DATA_DIR / "templates" / "one.png"
            thumb = appmod.THUMBS_DIR / "one.png"
            template.parent.mkdir(parents=True)
            thumb.parent.mkdir(parents=True)
            template.write_bytes(b"x")
            thumb.write_bytes(b"x")
            try:
                app = object.__new__(appmod.App)
                app.targets = [{"path": str(template), "thumb": str(thumb)}]
                app.selected_target = 0
                app.reload_target_list = lambda: None
                app.save_current_profile = lambda: None
                appmod.App.remove_selected(app)
                self.assertFalse(template.exists())
                self.assertFalse(thumb.exists())
                self.assertEqual(app.targets, [])
            finally:
                appmod.DATA_DIR, appmod.THUMBS_DIR = old_data, old_thumbs

    def test_record_target_hits_updates_matching_template_counts(self):
        app = object.__new__(appmod.App)
        app.targets = [
            {"id": "a", "name": "1-1-a", "hit_count": 2},
            {"id": "b", "name": "1-2-b", "hit_count": 0},
        ]
        app.thumb_cache = {"old": object()}
        app.reload_target_list = mock.Mock()
        app.save_current_profile = mock.Mock()
        appmod.App.record_target_hits(app, ["a", "b", "a", "missing"])
        self.assertEqual([target["hit_count"] for target in app.targets], [4, 1])
        self.assertEqual(app.thumb_cache, {})
        app.reload_target_list.assert_called_once()
        app.save_current_profile.assert_called_once()

    def test_count_badge_marks_thumbnail_top_right(self):
        image = Image.new("RGB", (80, 50), (245, 245, 245))
        app = object.__new__(appmod.App)
        appmod.App.draw_count_badge(app, image, 12)
        pixels = [image.getpixel((x, y)) for x in range(50, 78) for y in range(4, 22)]
        self.assertTrue(any(pixel != (245, 245, 245) for pixel in pixels))

    def test_gallery_mousewheel_scrolls_canvas(self):
        app = object.__new__(appmod.App)
        app.target_canvas = mock.Mock()
        event = type("Event", (), {"delta": -120})()
        self.assertEqual(appmod.App.scroll_targets(app, event), "break")
        app.target_canvas.yview_scroll.assert_called_once_with(1, "units")

    def test_toggle_all_targets_switches_between_select_all_and_invert(self):
        app = object.__new__(appmod.App)
        app.targets = [{"enabled": True}, {"enabled": False}]
        app.reload_target_list = mock.Mock()
        app.save_current_profile = mock.Mock()
        appmod.App.toggle_all_targets(app)
        self.assertEqual([t["enabled"] for t in app.targets], [True, True])
        appmod.App.toggle_all_targets(app)
        self.assertEqual([t["enabled"] for t in app.targets], [False, False])

    def test_select_all_button_label_reflects_target_state(self):
        app = object.__new__(appmod.App)
        app.target_select_btn = mock.Mock()
        app.targets = [{"enabled": True}, {"enabled": True}]
        appmod.App.update_target_select_button(app)
        app.target_select_btn.configure.assert_called_with(text="反选")
        app.targets[1]["enabled"] = False
        appmod.App.update_target_select_button(app)
        app.target_select_btn.configure.assert_called_with(text="全选")

    def test_select_target_does_not_redraw_gallery(self):
        app = object.__new__(appmod.App)
        app.targets = [{"path": "one.png"}]
        app.status = mock.Mock()
        app.reload_target_list = mock.Mock()
        appmod.App.select_target(app, 0)
        self.assertEqual(app.selected_target, 0)
        app.reload_target_list.assert_not_called()

    def test_select_target_marks_only_old_and_new_cards(self):
        app = object.__new__(appmod.App)
        old = mock.Mock()
        new = mock.Mock()
        other = mock.Mock()
        app.target_cards = {0: (old,), 1: (new,), 2: (other,)}
        app.selected_target = 0
        appmod.App.update_target_selection(app, old=0)
        app.selected_target = 1
        appmod.App.update_target_selection(app, old=0)
        old.configure.assert_called_with(bg="#ffffff")
        new.configure.assert_called_with(bg="#dbeafe")
        other.configure.assert_not_called()

    def test_click_target_opens_on_second_click_without_preselect(self):
        app = object.__new__(appmod.App)
        app.targets = [{"path": "one.png"}, {"path": "two.png"}, {"path": "three.png"}]
        app.status = mock.Mock()
        app.target_cards = {}
        app.target_last_click = (None, 0)
        app.open_target_file = mock.Mock()
        with mock.patch.object(appmod.time, "monotonic", side_effect=[10.0, 10.2, 10.3, 10.6]):
            appmod.App.click_target(app, 0)
            appmod.App.click_target(app, 0)
            appmod.App.click_target(app, 2)
            appmod.App.click_target(app, 2)
        self.assertEqual(app.open_target_file.call_args_list, [mock.call(0), mock.call(2)])

    def test_open_target_file_opens_image_directly(self):
        app = object.__new__(appmod.App)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "one.png"
            image.write_bytes(b"x")
            app.targets = [{"path": str(image)}]
            with mock.patch.object(appmod.os, "startfile", create=True) as startfile, mock.patch.object(appmod.subprocess, "Popen") as popen:
                appmod.App.open_target_file(app, 0)
        startfile.assert_called_once_with(image)
        popen.assert_not_called()

    def test_prune_alerts_keeps_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            files = []
            for i in range(5):
                path = base / f"{i}.png"
                path.write_bytes(b"x")
                files.append(path)
            prune_alerts(base, 2)
            self.assertEqual([p.name for p in sorted(base.glob("*.png"))], ["3.png", "4.png"])

    def test_migrate_legacy_data_maps_alerts(self):
        old_data, old_legacy, old_alerts = appmod.DATA_DIR, appmod.LEGACY_DATA_DIR, appmod.ALERTS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.LEGACY_DATA_DIR = base / "legacy"
            appmod.DATA_DIR = base / "user"
            appmod.ALERTS_DIR = appmod.DATA_DIR / "screenshots"
            try:
                (appmod.LEGACY_DATA_DIR / "profiles").mkdir(parents=True)
                (appmod.LEGACY_DATA_DIR / "alerts").mkdir()
                (appmod.LEGACY_DATA_DIR / "profiles" / "profile_1.json").write_text("{}", encoding="utf-8")
                (appmod.LEGACY_DATA_DIR / "alerts" / "hit.png").write_bytes(b"x")
                appmod.migrate_legacy_data()
                self.assertTrue((appmod.DATA_DIR / "profiles" / "profile_1.json").exists())
                self.assertTrue((appmod.ALERTS_DIR / "hit.png").exists())
            finally:
                appmod.DATA_DIR, appmod.LEGACY_DATA_DIR, appmod.ALERTS_DIR = old_data, old_legacy, old_alerts

    def test_detector_config_uses_only_checked_targets(self):
        old_data, old_profiles, old_state = appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH
        old_legacy, old_thumbs, old_alerts = appmod.LEGACY_DATA_DIR, appmod.THUMBS_DIR, appmod.ALERTS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "app_data"
            appmod.LEGACY_DATA_DIR = base / "missing_legacy"
            appmod.PROFILES_DIR = appmod.DATA_DIR / "profiles"
            appmod.STATE_PATH = appmod.DATA_DIR / "state.json"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            appmod.ALERTS_DIR = appmod.DATA_DIR / "screenshots"
            try:
                root = appmod.Tk()
                root.withdraw()
                app = appmod.App(root)
                app.monitor_vars = {1: appmod.BooleanVar(value=True)}
                one = appmod.DATA_DIR / "templates" / "one.png"
                two = appmod.DATA_DIR / "templates" / "two.png"
                one.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (12, 10), "red").save(one)
                Image.new("RGB", (12, 10), "blue").save(two)
                app.targets = [
                    {"id": "one-id", "name": "one", "path": str(one), "enabled": True},
                    {"name": "two", "path": str(two), "enabled": False},
                ]
                config_targets = app.detector_config()["targets"]
                self.assertEqual([t["name"] for t in config_targets], ["one"])
                self.assertEqual([t["id"] for t in config_targets], ["one-id"])
                app.targets[0]["enabled"] = False
                with self.assertRaises(ValueError):
                    app.detector_config()
                root.destroy()
            finally:
                appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH = old_data, old_profiles, old_state
                appmod.LEGACY_DATA_DIR = old_legacy
                appmod.THUMBS_DIR, appmod.ALERTS_DIR = old_thumbs, old_alerts

    def test_detector_config_allows_window_only_source(self):
        old_data, old_profiles, old_state = appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH
        old_legacy, old_thumbs, old_alerts = appmod.LEGACY_DATA_DIR, appmod.THUMBS_DIR, appmod.ALERTS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "app_data"
            appmod.LEGACY_DATA_DIR = base / "missing_legacy"
            appmod.PROFILES_DIR = appmod.DATA_DIR / "profiles"
            appmod.STATE_PATH = appmod.DATA_DIR / "state.json"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            appmod.ALERTS_DIR = appmod.DATA_DIR / "screenshots"
            try:
                root = appmod.Tk()
                root.withdraw()
                app = appmod.App(root)
                app.monitor_vars = {1: appmod.BooleanVar(value=False)}
                app.window_info = {window_key("Demo", 1): {"title": "Demo", "display": "Demo", "hwnd": 123, "width": 200, "height": 100, "key": window_key("Demo", 1)}}
                app.selected_apps = [{"title": "Demo", "ordinal": 1}]
                one = appmod.DATA_DIR / "templates" / "one.png"
                one.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (12, 10), "red").save(one)
                app.targets = [{"name": "one", "path": str(one), "enabled": True}]
                config = app.detector_config()
                self.assertEqual(config["regions"], [])
                self.assertEqual(config["windows"][0]["title"], "Demo")
                self.assertEqual(config["window_apps"], [{"title": "Demo", "ordinal": 1}])
                app.window_info = {}
                config = app.detector_config()
                self.assertEqual(config["windows"], [])
                self.assertEqual(config["window_apps"], [{"title": "Demo", "ordinal": 1}])
                root.destroy()
            finally:
                appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH = old_data, old_profiles, old_state
                appmod.LEGACY_DATA_DIR = old_legacy
                appmod.THUMBS_DIR, appmod.ALERTS_DIR = old_thumbs, old_alerts

    def test_add_image_prunes_to_template_limit_before_naming(self):
        old_data, old_profiles, old_state = appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH
        old_legacy, old_thumbs, old_alerts = appmod.LEGACY_DATA_DIR, appmod.THUMBS_DIR, appmod.ALERTS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            appmod.DATA_DIR = base / "app_data"
            appmod.LEGACY_DATA_DIR = base / "missing_legacy"
            appmod.PROFILES_DIR = appmod.DATA_DIR / "profiles"
            appmod.STATE_PATH = appmod.DATA_DIR / "state.json"
            appmod.THUMBS_DIR = appmod.DATA_DIR / "thumbs"
            appmod.ALERTS_DIR = appmod.DATA_DIR / "screenshots"
            try:
                root = appmod.Tk()
                root.withdraw()
                app = appmod.App(root)
                app.current_profile = 1
                app.max_templates.set(2)
                app.add_image("one", Image.new("RGB", (12, 10), "red"))
                app.add_image("two", Image.new("RGB", (12, 10), "blue"))
                app.add_image("three", Image.new("RGB", (12, 10), "green"))
                self.assertEqual(len(app.targets), 2)
                self.assertTrue(Path(app.targets[0]["path"]).name.startswith("1-1-"))
                self.assertTrue(Path(app.targets[1]["path"]).name.startswith("1-2-"))
                root.destroy()
            finally:
                appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH = old_data, old_profiles, old_state
                appmod.LEGACY_DATA_DIR = old_legacy
                appmod.THUMBS_DIR, appmod.ALERTS_DIR = old_thumbs, old_alerts


if __name__ == "__main__":
    unittest.main()
