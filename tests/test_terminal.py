"""Exercise streamed sudo authentication and timeouts in a real pseudo-terminal."""
import errno
import os
import pty
import select
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        sudo = self.directory / "sudo"
        sudo.write_text(f"#!{sys.executable}\n" + '''
import os,sys,termios
if sys.argv[1:] == ["-v"]:
    sys.exit(0)
if "-n" in sys.argv[1:]:
    sys.exit("sudo: a password is required")
fd = os.open("/dev/tty", os.O_RDWR)
original = termios.tcgetattr(fd)
hidden = termios.tcgetattr(fd)
hidden[3] &= ~termios.ECHO
termios.tcsetattr(fd, termios.TCSANOW, hidden)
try:
    os.write(fd, b"TEST_PASSWORD_PROMPT: ")
    password = os.read(fd, 1024).strip()
finally:
    termios.tcsetattr(fd, termios.TCSANOW, original)
    os.close(fd)
if password != b"test-password":
    sys.exit("authentication failed")
os.execvp(sys.argv[1], sys.argv[1:])
''')
        sudo.chmod(0o755)

    def terminal(self, body):
        code = f'''
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, {str(ROOT / "scripts")!r})
import printer
printer.STATE = Path({str(self.directory / "state")!r})
with patch.object(printer.os, "geteuid", return_value=1000):
    app = printer.App()
{body}
'''
        pid, fd = pty.fork()
        if pid == 0:
            env = {**os.environ, "PATH": str(self.directory) + os.pathsep + os.environ["PATH"]}
            os.execve(sys.executable, [sys.executable, "-c", code], env)
        output = bytearray()
        answered = False
        try:
            deadline = time.monotonic() + 15
            while True:
                if time.monotonic() >= deadline:
                    self.fail(f"Pseudo-terminal timed out: {output.decode(errors='replace')}")
                readable, _, _ = select.select([fd], [], [], 0.1)
                if not readable:
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
                if not answered and b"TEST_PASSWORD_PROMPT:" in output:
                    os.write(fd, b"test-password\n")
                    answered = True
            _, status = os.waitpid(pid, 0)
            pid = None
            text = output.decode(errors="replace")
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, text)
            self.assertTrue(answered, text)
            self.assertNotIn("test-password", text)
            return text
        finally:
            os.close(fd)
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)

    def test_streamed_sudo_can_prompt_and_run_command(self):
        output = self.terminal('''    app.root([sys.executable, "-c", "print('COMMAND_COMPLETED')"], stream=True)
''')
        self.assertIn("COMMAND_COMPLETED", output)

    def test_sudo_timeout_does_not_kill_callers_terminal_group(self):
        output = self.terminal('''    try:
        app.root([sys.executable, "-c", "import time; time.sleep(60)"], stream=True, timeout=0.5)
    except printer.Failure as error:
        assert "не завершилась" in str(error), str(error)
        print("TIMEOUT_RECOVERED", flush=True)
    else:
        raise AssertionError("Expected timeout")
''')
        self.assertIn("TIMEOUT_RECOVERED", output)


if __name__ == "__main__":
    unittest.main()
