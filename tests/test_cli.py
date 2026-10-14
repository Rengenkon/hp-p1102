"""Check failure paths without downloading, sudo, or changing the host."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_stopped_cups_with_zero_exit_code_is_started_and_awaited(self):
        stopped = subprocess.CompletedProcess(["lpstat", "-r"], 0, "scheduler is not running\n", "")
        ready = subprocess.CompletedProcess(["lpstat", "-r"], 0, "scheduler is running\n", "")
        loaded = subprocess.CompletedProcess(["systemctl"], 0, "loaded\n", "")
        with patch.object(printer.shutil, "which", side_effect=lambda command: command), \
                patch.object(printer.Path, "is_dir", return_value=True), \
                patch.object(self.app, "host_run", side_effect=[stopped, stopped, ready]) as probe, \
                patch.object(self.app, "run", return_value=loaded), \
                patch.object(self.app, "root") as root, \
                patch.object(printer.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.app.ensure_cups()
        root.assert_called_once_with(["systemctl", "enable", "--now", "cups.service"], stream=True)
        self.assertEqual(probe.call_count, 3)
        self.assertNotIn("SKIP CUPS", output.getvalue())
        self.assertIn("CUPS хоста запущен", output.getvalue())

    def test_running_cups_does_not_restart_service(self):
        ready = subprocess.CompletedProcess(["lpstat", "-r"], 0, "scheduler is running\n", "")
        with patch.object(printer.shutil, "which", side_effect=lambda command: command), \
                patch.object(self.app, "host_run", return_value=ready), \
                patch.object(self.app, "root") as root, \
                contextlib.redirect_stdout(io.StringIO()):
            self.app.ensure_cups()
        root.assert_not_called()

    def test_cups_startup_timeout_is_not_mistaken_for_success(self):
        stopped = subprocess.CompletedProcess(["lpstat", "-r"], 0, "scheduler is not running\n", "")
        loaded = subprocess.CompletedProcess(["systemctl"], 0, "loaded\n", "")
        with patch.object(printer.shutil, "which", side_effect=lambda command: command), \
                patch.object(printer.Path, "is_dir", return_value=True), \
                patch.object(self.app, "host_run", return_value=stopped), \
                patch.object(self.app, "run", return_value=loaded), \
                patch.object(self.app, "root"), \
                patch.object(printer.time, "monotonic", side_effect=[10, 31]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(printer.Failure) as raised:
                self.app.ensure_cups()
        self.assertIn("не стал доступен", str(raised.exception))

    def test_lock_conflict_does_not_run_deploy(self):
        with patch.object(sys, "argv", ["printer.py", "deploy"]), \
                patch.object(printer.fcntl, "flock", side_effect=BlockingIOError), \
                patch.object(printer.App, "deploy") as deploy, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(printer.main(), 1)
        deploy.assert_not_called()
        self.assertIn("Дождитесь", output.getvalue())

    def test_server_does_not_install_or_configure_cups_on_host(self):
        with patch.object(self.app, "preflight"), patch.object(self.app, "plugin"), \
                patch.object(self.app, "usb", return_value=960), \
                patch.object(self.app, "image", return_value="server-image") as image, \
                patch.object(self.app, "start") as start, \
                patch.object(self.app, "check_proxy"), patch.object(self.app, "compose"), \
                patch.object(self.app, "devices", return_value=[]), \
                patch.object(self.app, "host_queue", side_effect=AssertionError("host queue")), \
                patch.object(self.app, "install_cups", side_effect=AssertionError("package installation")), \
                patch.object(self.app, "host_run", side_effect=AssertionError("host CUPS")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.app.deploy(mode="server")
        image.assert_called_once_with("server")
        start.assert_called_once_with(960, "server-image", "server")

    def test_correct_usb_setup_does_not_require_sudo_again(self):
        node = Path(self.temp.name) / "usb"
        node.touch()
        node.chmod(0o660)
        rule = Path(self.temp.name) / "rule"
        rule.write_text('SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="03f0", '
                        'ATTR{idProduct}=="002a", ATTR{serial}=="000000000Q8D61SNPR1a", GROUP="hpprinter", MODE="0660"\n')
        def path(value):
            return rule if str(value) == "/etc/udev/rules.d/99-hp-p1102.rules" else Path(value)
        with patch.object(printer, "Path", side_effect=path), \
                patch.object(printer.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=node.stat().st_gid)), \
                patch.object(self.app, "devices", return_value=[(Path(self.temp.name), node)]), \
                patch.object(self.app, "root", side_effect=AssertionError("unnecessary sudo")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.app.usb(), node.stat().st_gid)


if __name__ == "__main__":
    unittest.main()
