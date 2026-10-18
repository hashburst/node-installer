import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ArchiveInstallGuardTests(unittest.TestCase):
    def run_guard(self, response, code=0):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "systemctl"
            executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$GUARD_RESPONSE\"\nexit \"$GUARD_CODE\"\n")
            executable.chmod(0o755)
            return subprocess.run(
                ["bash", "-c", 'source "$1"; hashburst_require_writable_install_target',
                 "guard", str(ROOT / "lib/archive-guard.sh")],
                env={**os.environ, "PATH": directory + ":" + os.environ["PATH"],
                     "GUARD_RESPONSE": response, "GUARD_CODE": str(code)},
                capture_output=True, text=True)

    def test_terminal_archive_is_rejected(self):
        for state in ("active", "inactive"):
            result = self.run_guard(
                "LoadState=loaded\nActiveState=" + state +
                "\nExecStart={ path=/opt/release/hvm-legacy-archive ; }")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("archive detected", result.stderr)

    def test_missing_and_writable_services(self):
        for response in ("LoadState=not-found", "LoadState=loaded\nExecStart=/usr/local/bin/hashburst-node"):
            self.assertEqual(self.run_guard(response).returncode, 0)

    def test_inspection_failure_is_rejected(self):
        self.assertNotEqual(self.run_guard("", 1).returncode, 0)
        self.assertNotEqual(self.run_guard("").returncode, 0)

    def test_tmpfiles_recreates_directory_without_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["systemd-tmpfiles", "--root=" + directory, "--create",
                 str(ROOT / "ha/hashburst-ha.tmpfiles.conf")],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            path = Path(directory) / "run/hashburst-ha"
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertFalse((path / "lease.json").exists())

if __name__ == "__main__":
    unittest.main()
