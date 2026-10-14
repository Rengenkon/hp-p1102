"""Shared, standard-library-only configuration and CUPS reconciliation."""
import hashlib
import json
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import quote


def scheduler_running(result):
    # lpstat -r can return 0 even when cupsd is stopped. All callers use LC_ALL=C.
    return result.returncode == 0 and result.stdout.strip() == "scheduler is running"


def cups_files_config(template, group):
    # CUPS sets its workers' primary group and clears supplementary groups.
    # Membership in /etc/group alone does not grant the backend USB access.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", group):
        raise ValueError(f"Недопустимая группа CUPS: {group!r}")
    pattern = r"(?m)^[ \t]*#?Group[ \t]+[^\n]+$"
    if re.search(pattern, template):
        return re.sub(pattern, f"Group {group}", template)
    return template.rstrip() + f"\nGroup {group}\n"


def load_config(path):
    with Path(path).open("rb") as stream:
        config = tomllib.load(stream)
    printer, host = config["printer"], config["host"]
    for name in (printer["name"], host["queue"], host["usb_group"]):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,126}", name):
            raise ValueError(f"Недопустимое имя: {name!r}")
    for key in ("vendor_id", "product_id"):
        if not re.fullmatch(r"[0-9a-f]{4}", printer[key]):
            raise ValueError(f"printer.{key}: требуется четыре строчных hex-цифры")
    if not 1024 <= config["container"]["host_port"] <= 65535:
        raise ValueError("container.host_port: требуется порт 1024–65535")
    if not printer["serial"] or not printer["device_model"]:
        raise ValueError("Серийный номер и модель принтера обязательны")
    if any(c in printer["serial"] for c in ('"', '\\', '\n', '\r', '*', '?', '[', ']')):
        raise ValueError("Серийный номер содержит недопустимые для udev символы")
    for key in ("make_default", "install_cups"):
        if not isinstance(host[key], bool):
            raise ValueError(f"host.{key}: требуется true или false")
    for key, value in printer.get("options", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or not isinstance(value, str):
            raise ValueError(f"Недопустимый параметр печати: {key}")
    return config


def device_uri(config):
    p = config["printer"]
    return f"hp:/usb/{quote(p['device_model'], safe='_')}?serial={quote(p['serial'], safe='')}"


def ipp_uri(config):
    return f"ipp://localhost:{config['container']['host_port']}/printers/{config['printer']['name']}"


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(content)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def reconcile_queue(run, name, uri, model, options, state_path, ppd_dir, emit=print, get_ppd_hash=None, make_default=True):
    """Preserve queue/jobs; change only managed defaults and detect PPD drift."""
    state_path = Path(state_path)
    ppd = Path(ppd_dir) / f"{name}.ppd"
    desired = {"uri": uri, "model": model, "options": options}
    try:
        previous = json.loads(state_path.read_text())
    except (OSError, ValueError):
        previous = {}
    current = run(["lpstat", "-v", name], check=False)
    exists = current.returncode == 0
    current_options = {}
    if exists:
        result = run(["lpoptions", "-p", name])
        current_options = dict(token.split("=", 1) for token in shlex.split(result.stdout) if "=" in token)
        # PPD defaults (e.g. PageSize) are not included in plain lpoptions output.
        choices = run(["lpoptions", "-p", name, "-l"])
        for line in choices.stdout.splitlines():
            if ":" not in line:
                continue
            title, values = line.split(":", 1)
            for value in values.split():
                if value.startswith("*"):
                    current_options[title.split("/", 1)[0]] = value[1:]
    def read_hash():
        if get_ppd_hash:
            return get_ppd_hash(ppd)
        return hashlib.sha256(ppd.read_bytes()).hexdigest() if ppd.exists() else None

    ppd_hash = read_hash()
    driver_changed = not exists or previous.get("desired", {}).get("model") != model
    driver_changed |= ppd_hash is None or previous.get("ppd_hash") != ppd_hash
    uri_changed = not exists or current.stdout.strip().split(": ", 1)[-1] != uri
    options_changed = driver_changed or any(current_options.get(k) != v for k, v in options.items())
    removed = set(previous.get("desired", {}).get("options", {})) - set(options)
    if driver_changed or uri_changed or options_changed or removed:
        args = ["lpadmin", "-p", name]
        if not exists:
            args += ["-E"]
        if uri_changed:
            args += ["-v", uri]
        if driver_changed:
            args += ["-m", model]
        if options_changed:
            for key, value in sorted(options.items()):
                args += ["-o", f"{key}={value}"]
        for key in sorted(removed):
            args += ["-R", key]
        run(args)
        emit(f"{'CHANGE' if exists else 'FIX'} Очередь {name} настроена")
    else:
        emit(f"SKIP Очередь {name} соответствует конфигурации")
    # Explicit deployment repairs paused/rejecting managed queues after startup.
    status = run(["lpstat", "-p", name]).stdout
    if "disabled" in status:
        run(["cupsenable", name])
        emit(f"FIX Очередь {name} включена; при повторной остановке проверьте doctor")
    if "not accepting" in run(["lpstat", "-a", name]).stdout:
        run(["cupsaccept", name])
        emit(f"FIX Очередь {name} принимает задания")
    default = run(["lpstat", "-d"], check=False).stdout.strip()
    if make_default and default.split(": ", 1)[-1] != name:
        run(["lpadmin", "-d", name])
    state = {"desired": desired, "ppd_hash": read_hash()}
    if previous != state:
        atomic_write(state_path, json.dumps(state, indent=2) + "\n")


def local_run(args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=30,
                          env={**os.environ, "LC_ALL": "C", "CUPS_SERVER": "/run/cups/cups.sock"})
