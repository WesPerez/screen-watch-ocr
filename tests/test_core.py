import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import screen_watch.app as appmod
from screen_watch.app import app_from_legacy, beep_wave, black_fraction, crop_black_padding, mostly_black, parse_positive_float, parse_positive_int, parse_scales, parse_volume, prune_alerts, scan_interval_ms, template_name, window_key
from screen_watch.core import self_test


class CoreTest(unittest.TestCase):
    def test_template_and_pixel_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = type("Args", (), {"out": Path(tmp), "ocr": False})()
            self_test(args)

    def test_parse_scales(self):
        self.assertEqual(parse_scales("1, 0.9,1.1"), [1.0, 0.9, 1.1])

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

    def test_window_capture_crops_printwindow_black_padding_before_visible_fallback(self):
        import numpy as np

        padded = np.zeros((4, 4, 3), dtype=np.uint8)
        padded[:, :2] = 80
        visible = np.full((4, 4, 3), 60, dtype=np.uint8)
        with mock.patch.object(appmod, "capture_window", return_value=padded), mock.patch.object(appmod, "capture_window_visible", return_value=visible):
            frame = appmod.capture_window_frame(object(), 123)
            self.assertEqual(frame.shape, (4, 2, 3))
            self.assertEqual(frame.tolist(), np.full((4, 2, 3), 80, dtype=np.uint8).tolist())

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

    def test_template_name_uses_profile_count_date(self):
        self.assertEqual(template_name(1, 11, "20260701"), "1-11-20260701")
        self.assertRegex(template_name(5, 2), r"^5-2-\d{14}$")

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

    def test_entry_click_keeps_cursor_at_end(self):
        root = appmod.Tk()
        try:
            app = object.__new__(appmod.App)
            value = appmod.StringVar(value="250")
            entry = appmod.App.make_entry(app, root, value)
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
                    {"name": "one", "path": str(one), "enabled": True},
                    {"name": "two", "path": str(two), "enabled": False},
                ]
                self.assertEqual([t["name"] for t in app.detector_config()["targets"]], ["one"])
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
                self.assertTrue(Path(app.targets[-1]["path"]).name.startswith("1-2-"))
                self.assertFalse(any(Path(t["path"]).name.startswith("1-1-") for t in app.targets))
                root.destroy()
            finally:
                appmod.DATA_DIR, appmod.PROFILES_DIR, appmod.STATE_PATH = old_data, old_profiles, old_state
                appmod.LEGACY_DATA_DIR = old_legacy
                appmod.THUMBS_DIR, appmod.ALERTS_DIR = old_thumbs, old_alerts


if __name__ == "__main__":
    unittest.main()
