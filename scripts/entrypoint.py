"""Container PID 1: reconcile CUPS and supervise both foreground daemons."""
import grp
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/printer")
from runtime import atomic_write, cups_files_config, device_uri, load_config, local_run, reconcile_queue, scheduler_running

CONFIG = Path("/etc/printer/printer.toml")


def proxy_check():
    with urllib.request.urlopen("http://127.0.0.1:8631/health", timeout=3) as response:
        if response.status != 200 or response.read() != b"OK\n":
            raise RuntimeError("IPP-прокси не готов")


def apply():
    config = load_config(CONFIG)
    p = config["printer"]
    reconcile_queue(local_run, p["name"], device_uri(config), p["driver"],
                     {**p.get("options", {}), "printer-is-shared": "true"}, "/etc/cups/.managed-printer.json", "/etc/cups/ppd")


def usb_check(nodes):
    config = load_config(CONFIG)
    files = Path("/etc/cups/cups-files.conf").read_text()
    match = re.search(r"(?m)^Group[ \t]+([^\s#]+)", files)
    group = match.group(1) if match else "lp"
    gid = grp.getgrnam(group).gr_gid
    expected = int(os.environ["HP_PRINTER_GID"])
    print(f"Группа процессов CUPS: {group} (GID={gid}); USB требует GID={expected}", flush=True)
    if gid != expected:
        raise RuntimeError("Основная группа CUPS не соответствует USB. Выполните deploy с обновленным образом.")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().split(b"\0", 1)[0].decode()
            if command != device_uri(config):
                continue
            status = (proc / "status").read_text()
            lines = [line for line in status.splitlines() if line.startswith(("Uid:", "Gid:", "Groups:"))]
            print(f"HP backend PID={proc.name}: " + "; ".join(lines), flush=True)
            effective_gid = int(re.search(r"(?m)^Gid:\s+\d+\s+(\d+)", status).group(1))
            if effective_gid != gid:
                raise RuntimeError(f"HP backend PID={proc.name} запущен с неверной группой {effective_gid}")
        except (FileNotFoundError, ProcessLookupError):
            continue
    if nodes:
        # Reproduce CUPS's credentials, rather than `id lp` or a root USB probe.
        probe = subprocess.run(
            [sys.executable, "-c", "import os,sys; "
             "[(os.close(os.open(p, os.O_RDWR)), print('OK USB-доступ: ' + p, flush=True)) for p in sys.argv[1:]]", *nodes],
            user="lp", group=group, extra_groups=[group], text=True, capture_output=True, timeout=10)
        print(probe.stdout, end="", flush=True)
        if probe.returncode:
            raise RuntimeError("Backend не может открыть USB для чтения/записи:\n" + probe.stderr)


def repair_worker_files(gid):
    # Named volumes may still contain PPD/job files from the old CUPS group.
    # Preserve owners, modes and contents; only reconcile group ownership.
    repaired = 0
    for directory in (Path("/etc/cups/ppd"), Path("/var/spool/cups")):
        if not directory.exists():
            continue
        for path in (directory, *directory.rglob("*")):
            if not path.is_symlink() and path.stat().st_gid != gid:
                os.chown(path, -1, gid)
                repaired += 1
    if repaired:
        print(f"FIX Группа {repaired} сохраненных файлов/каталогов CUPS приведена к GID={gid}", flush=True)


def main():
    if "--usb-check" in sys.argv:
        usb_check(sys.argv[sys.argv.index("--usb-check") + 1:])
        return
    if "--check" in sys.argv:
        if not scheduler_running(local_run(["lpstat", "-r"], check=False)):
            raise RuntimeError("CUPS scheduler не работает")
        config = load_config(CONFIG)
        local_run(["lpstat", "-p", config["printer"]["name"]])
        if os.environ.get("PRINTER_MODE", "local") == "server":
            proxy_check()
        return
    if "--apply" in sys.argv:
        apply()
        return
    gid = int(os.environ["HP_PRINTER_GID"])
    if gid <= 0:
        raise RuntimeError("Для USB требуется отдельная группа с ненулевым GID")
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
    if os.environ.get("PRINTER_MODE", "local") == "server":
        desired = desired.replace("Listen 0.0.0.0:631", "Listen 127.0.0.1:631")
    cups_config = Path("/etc/cups/cupsd.conf")
    if not cups_config.exists() or cups_config.read_text() != desired:
        atomic_write(cups_config, desired, 0o640)
    files = Path("/etc/cups/cups-files.conf")
    desired_files = cups_files_config(Path("/usr/local/share/printer/cups-files.conf").read_text(), group)
    if not files.exists() or files.read_text() != desired_files:
        atomic_write(files, desired_files, 0o640)
    repair_worker_files(gid)
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
        while not scheduler_running(local_run(["lpstat", "-r"], check=False)):
            if stopping or any(c.poll() is not None for c in children) or time.monotonic() > deadline:
                raise RuntimeError("CUPS не запустился за 30 секунд")
            time.sleep(0.5)
        if not stopping:
            apply()
            if os.environ.get("PRINTER_MODE", "local") == "server":
                children.append(subprocess.Popen(["/opt/proxy-venv/bin/python", "-u", "/usr/local/lib/printer/proxy.py"]))
        while not stopping:
            if any(c.poll() is not None for c in children):
                raise RuntimeError("CUPS, D-Bus или IPP-прокси завершился неожиданно")
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
