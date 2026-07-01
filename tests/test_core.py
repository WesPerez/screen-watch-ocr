import tempfile
import unittest
from pathlib import Path

from PIL import Image

import screen_watch.app as appmod
from screen_watch.app import parse_positive_float, parse_positive_int, parse_scales, prune_alerts
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

    def test_parse_positive_float(self):
        self.assertEqual(parse_positive_float("3.5", "beep_seconds"), 3.5)
        with self.assertRaises(ValueError):
            parse_positive_float("0", "beep_seconds")

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


if __name__ == "__main__":
    unittest.main()
