"""Check failure paths without downloading, sudo, or changing the host."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import printer


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(printer, "STATE", Path(self.temp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.app = printer.App()
        self.addCleanup(self.app.log.close)

    def test_bad_plugin_hash_leaves_file_intact(self):
        archive = Path(self.temp.name) / "plugin.run"
        archive.write_bytes(b"corrupt archive")
        with self.assertRaises(printer.Failure) as raised:
            self.app.check_plugin(archive)
        self.assertIn("не совпадает", str(raised.exception))
        self.assertEqual(archive.read_bytes(), b"corrupt archive")

    def test_timeout_is_actionable(self):
        with patch.object(printer.subprocess, "run", side_effect=subprocess.TimeoutExpired(["example"], 1)):
            with self.assertRaises(printer.Failure) as raised:
                self.app.run(["example"], timeout=1)
        self.assertIn("1 секунд", str(raised.exception))
        self.assertIn("Повторите", raised.exception.remedy)

    def test_sudo_error_points_to_interactive_terminal(self):
        result = subprocess.CompletedProcess(["sudo"], 1, "", "sudo: a password is required")
        with patch.object(printer.subprocess, "run", return_value=result):
            with self.assertRaises(printer.Failure) as raised:
                self.app.run(["sudo", "true"])
        self.assertIn("интерактивного терминала", raised.exception.remedy)

    def test_local_cups_server_is_explicit_for_sudo(self):
        with patch.object(self.app, "root") as root:
            self.app.host_run(["lpstat", "-r"])
        self.assertEqual(root.call_args.args[0], ["lpstat", "-h", "/run/cups/cups.sock", "-r"])

    def test_lock_conflict_does_not_run_deploy(self):
        with patch.object(sys, "argv", ["printer.py", "deploy"]), \
                patch.object(printer.fcntl, "flock", side_effect=BlockingIOError), \
                patch.object(printer.App, "deploy") as deploy, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(printer.main(), 1)
        deploy.assert_not_called()
        self.assertIn("Дождитесь", output.getvalue())


if __name__ == "__main__":
    unittest.main()
