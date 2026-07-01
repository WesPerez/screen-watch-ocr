import tempfile
import unittest
from pathlib import Path

from PIL import Image

import screen_watch.app as appmod
from screen_watch.app import app_from_legacy, mostly_black, parse_positive_float, parse_positive_int, parse_scales, prune_alerts, scan_interval_ms, template_name, window_key
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

    def test_parse_positive_float(self):
        self.assertEqual(parse_positive_float("3.5", "beep_seconds"), 3.5)
        with self.assertRaises(ValueError):
            parse_positive_float("0", "beep_seconds")

    def test_template_name_uses_profile_count_date(self):
        self.assertEqual(template_name(1, 11, "20260701"), "1-11-20260701")

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
                app.save_current_profile()
                root.destroy()

                root2 = appmod.Tk()
                root2.withdraw()
                app2 = appmod.App(root2)
                self.assertEqual(app2.current_profile, 5)
                self.assertEqual(app2.left.get(), "11")
                self.assertEqual(app2.width.get(), "333")
                self.assertEqual(app2.beep_seconds.get(), 5)
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
