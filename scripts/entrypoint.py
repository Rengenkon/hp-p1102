"""Container PID 1: reconcile CUPS and supervise both foreground daemons."""
import grp
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/printer")
from runtime import atomic_write, device_uri, load_config, local_run, reconcile_queue

CONFIG = Path("/etc/printer/printer.toml")


def apply():
    config = load_config(CONFIG)
    p = config["printer"]
    reconcile_queue(local_run, p["name"], device_uri(config), p["driver"],
                    {**p.get("options", {}), "printer-is-shared": "true"}, "/etc/cups/.managed-printer.json", "/etc/cups/ppd")


def main():
    if "--check" in sys.argv:
        config = load_config(CONFIG)
        local_run(["lpstat", "-p", config["printer"]["name"]])
        local_run(["lpstat", "-r"])
        return
    if "--apply" in sys.argv:
        apply()
        return
    gid = int(os.environ["HP_PRINTER_GID"])
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = "hostprinter"
        local_run(["groupadd", "-g", str(gid), group])
    if "lp" not in grp.getgrnam(group).gr_mem:
        local_run(["usermod", "-aG", group, "lp"])
    for directory in ("/run/cups", "/run/dbus"):
        Path(directory).mkdir(parents=True, exist_ok=True)
    Path("/run/dbus/pid").unlink(missing_ok=True)
    local_run(["dbus-uuidgen", "--ensure=/etc/machine-id"])
    desired = Path("/usr/local/share/printer/cupsd.conf").read_text()
    cups_config = Path("/etc/cups/cupsd.conf")
    if not cups_config.exists() or cups_config.read_text() != desired:
        atomic_write(cups_config, desired, 0o640)
    files = Path("/etc/cups/cups-files.conf")
    desired_files = Path("/usr/local/share/printer/cups-files.conf").read_text()
    if not files.exists() or files.read_text() != desired_files:
        atomic_write(files, desired_files, 0o640)
    local_run(["cupsd", "-t"])
    children = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen(["dbus-daemon", "--system", "--nofork", "--nopidfile"]))
        children.append(subprocess.Popen(["cupsd", "-f"]))
        deadline = time.monotonic() + 30
        while local_run(["lpstat", "-r"], check=False).returncode:
            if stopping or any(c.poll() is not None for c in children) or time.monotonic() > deadline:
                raise RuntimeError("CUPS не запустился за 30 секунд")
            time.sleep(0.5)
        if not stopping:
            apply()
        while not stopping:
            if any(c.poll() is not None for c in children):
                raise RuntimeError("CUPS или D-Bus завершился неожиданно")
            time.sleep(0.5)
    finally:
        stop(None, None)
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
