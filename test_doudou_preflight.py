import tempfile
from pathlib import Path
import unittest

from minecraft_doudou.preflight import PreflightError, inspect_installation, build_java_argv


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.instance = self.root / "neoforge-26.2"
        mods = self.instance / "mods"
        mods.mkdir(parents=True)
        (self.instance / "config").mkdir()
        (self.instance / "config" / "fml.toml").write_text(
            "earlyWindowControl=false\n", encoding="utf-8"
        )
        for name in [
            "hmc-specifics-26.2-neoforge-3.0.0.jar",
            "world-bosses-2.0.1.jar",
            "geckolib-5.5.6.jar",
        ] + [f"mod-{i}.jar" for i in range(21)]:
            (mods / name).touch()

    def test_correct_cached_installation(self):
        result = inspect_installation(self.root)
        self.assertEqual(result["mods_count"], 24)
        self.assertTrue(result["early_window_disabled"])
        self.assertEqual(result["mcdir"], str(self.root.resolve()))

    def test_missing_instance_or_small_mod_set_fails_closed(self):
        with self.assertRaises(PreflightError):
            inspect_installation(self.root / "missing")
        for jar in list((self.instance / "mods").glob("mod-*.jar")):
            jar.unlink()
        with self.assertRaisesRegex(PreflightError, "不足"):
            inspect_installation(self.root)

    def test_missing_critical_mod_fails_closed(self):
        mods = self.instance / "mods"
        for removed in ("hmc-specifics-26.2-neoforge-3.0.0.jar",
                        "world-bosses-2.0.1.jar", "geckolib-5.5.6.jar"):
            item = mods / removed
            item.rename(mods / (removed + ".disabled"))
            with self.assertRaises(PreflightError):
                inspect_installation(self.root)
            (mods / (removed + ".disabled")).rename(item)

    def test_journeymap_headless_crash_guard(self):
        jar = self.instance / "mods" / "journeymap-neoforge-26.2-6.0.7.jar"
        jar.touch()
        with self.assertRaisesRegex(PreflightError, "JourneyMap"):
            inspect_installation(self.root)

    def test_early_window_guard(self):
        config = self.instance / "config" / "fml.toml"
        config.write_text("earlyWindowControl=true\n")
        with self.assertRaisesRegex(PreflightError, "earlyWindowControl"):
            inspect_installation(self.root)

    def test_exact_java_argv_and_utf8_console(self):
        base = ["/usr/bin/java", "-Xms256m", "-jar", "/opt/headlessmc.jar"]
        result = build_java_argv(base, str(self.root))
        self.assertEqual(result[0:2], base[0:2])
        self.assertEqual(result[-2:], base[-2:])
        self.assertIn("-Dhmc.mcdir=" + str(self.root), result)
        self.assertIn("-Dhmc.files.mc=" + str(self.root), result)
        self.assertIn("-Dfile.encoding=UTF-8", result)
        self.assertLess(result.index("-Dhmc.mcdir=" + str(self.root)), result.index("-jar"))
        self.assertEqual(base, ["/usr/bin/java", "-Xms256m", "-jar", "/opt/headlessmc.jar"])

    def test_reject_conflicting_or_appended_launch(self):
        for args in [
            ["java", "-Dhmc.mcdir=/wrong", "-jar", "headlessmc.jar"],
            ["java", "-Dhmc.files.mc=/wrong", "-jar", "headlessmc.jar"],
            ["java", "-jar", "headlessmc.jar", "launch", "neoforge"],
            ["java", "-jar", "other.jar"],
        ]:
            with self.subTest(args=args), self.assertRaises(PreflightError):
                build_java_argv(args, str(self.root))


if __name__ == "__main__":
    unittest.main()
