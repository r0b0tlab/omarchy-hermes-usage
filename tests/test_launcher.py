import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import make_store  # noqa: E402

LAUNCH = Path(__file__).resolve().parent.parent / "collector" / "launch.py"

spec = importlib.util.spec_from_file_location("launch", LAUNCH)
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


class LauncherUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixed_default_validates(self):
        self.assertEqual(
            launch.validate_interpreter("/usr/bin/python3"),
            os.path.realpath("/usr/bin/python3"),
        )

    def test_relative_and_nul_rejected(self):
        self.assertIsNone(launch.validate_interpreter("python3"))
        self.assertIsNone(launch.validate_interpreter(""))
        self.assertIsNone(launch.validate_interpreter("/usr/bin/python3\x00evil"))

    def test_user_owned_tree_rejected(self):
        script = self.root / "evil"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        self.assertIsNone(launch.validate_interpreter(str(script)))

    def test_symlink_into_user_tree_rejected(self):
        script = self.root / "evil2"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        link = self.root / "link"
        os.symlink(script, link)
        self.assertIsNone(launch.validate_interpreter(str(link)))

    def test_non_executable_root_file_rejected(self):
        candidate = "/etc/hostname"
        if os.path.exists(candidate):
            self.assertIsNone(launch.validate_interpreter(candidate))

    def test_child_environment_is_closed(self):
        env = launch.child_environment({
            "HOME": "/home/x", "PATH": "/usr/bin", "HERMES_USAGE_PYTHON": "/x", "TZ": "UTC",
        })
        self.assertEqual(env, {"HOME": "/home/x", "TZ": "UTC"})

    def test_invalid_override_falls_back_with_warning(self):
        interpreter, warning = launch.select_interpreter({"HERMES_USAGE_PYTHON": "relative/path"})
        self.assertEqual(interpreter, os.path.realpath("/usr/bin/python3"))
        self.assertIn("refused", warning)

    def test_valid_override_is_used(self):
        interpreter, warning = launch.select_interpreter({"HERMES_USAGE_PYTHON": "/usr/bin/python3"})
        self.assertEqual(interpreter, os.path.realpath("/usr/bin/python3"))
        self.assertIsNone(warning)
