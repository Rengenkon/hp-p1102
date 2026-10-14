#!/usr/bin/env python3
"""One-command, idempotent deployment for the configured USB printer."""
import argparse
import fcntl
import grp
import hashlib
import json
import os
import platform
import re
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("ERROR: требуется Python 3.11 или новее.")

import tomllib
from runtime import atomic_write, ipp_uri, load_config, reconcile_queue, scheduler_running
from ipp import attributes, decode_prefix, printer_attributes_request

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".state"


class Failure(Exception):
    def __init__(self, message, remedy):
        super().__init__(message)
        self.remedy = remedy


class App:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.stage = "Подготовка"
        self.completed = []
        STATE.mkdir(exist_ok=True)
        directory = STATE / "logs"
        directory.mkdir(exist_ok=True)
        self.log_path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.log"
        self.log = self.log_path.open("w", buffering=1)
        self.config = load_config(ROOT / "printer.toml")
        with (ROOT / "build.lock.toml").open("rb") as stream:
            self.lock = tomllib.load(stream)
        plugin = self.lock["plugin"]
        if not re.fullmatch(r"[0-9a-f]{64}", plugin["sha256"]):
            raise ValueError("build.lock.toml: неверный SHA-256 плагина")
        if not self.lock["packages"]["hplip"].startswith(plugin["version"] + "+"):
            raise ValueError("Версии HPLIP и плагина не совпадают")
        if any(not isinstance(version, str) for version in self.lock["packages"].values()):
            raise ValueError("Версии пакетов в lock-файле должны быть строками; имена с точками заключайте в кавычки")
        self.env = {**os.environ, "LC_ALL": "C"}
        self.arch = None

    def deployed_mode(self):
        path = STATE / "compose.env"
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith("PRINTER_MODE="):
                    return line.split("=", 1)[1]
        return "local"

    def public_uri(self):
        if self.deployed_mode() == "server":
            return f"ipp://localhost:{self.config['container']['host_port']}/"
        return ipp_uri(self.config)

    def say(self, message):
        print(message, flush=True)
        self.log.write(message + "\n")

    def step(self, number, title):
        self.stage = title
        self.say(f"\n[{number}/7] {title}")

    def run(self, args, check=True, stream=False, timeout=60, env=None, preserve_terminal=False):
        args = [str(a) for a in args]
        command = shlex.join(args)
        self.log.write(f"$ {command}\n")
        if self.verbose:
            print(f"  $ {command}", flush=True)
        try:
            if stream:
                output = []
                process = subprocess.Popen(args, cwd=ROOT, env=env or self.env,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           start_new_session=not preserve_terminal)
                deadline = time.monotonic() + timeout
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                try:
                    while selector.get_map():
                        if time.monotonic() > deadline:
                            raise subprocess.TimeoutExpired(args, timeout)
                        for key, _ in selector.select(timeout=1):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            text = chunk.decode(errors="replace")
                            output.append(text)
                            self.log.write(text)
                            print(text, end="", flush=True)
                    result = subprocess.CompletedProcess(args, process.wait(timeout=max(1, deadline-time.monotonic())), "".join(output), "")
                finally:
                    selector.close()
                    try:
                        if process.poll() is None:
                            try:
                                if preserve_terminal:
                                    # sudo shares the terminal's process group.
                                    # Signal only sudo; it forwards TERM to its command.
                                    process.terminate()
                                    try:
                                        process.wait(timeout=5)
                                    except subprocess.TimeoutExpired:
                                        process.kill()
                                else:
                                    os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait()
                    finally:
                        process.stdout.close()
            else:
                result = subprocess.run(args, cwd=ROOT, env=env or self.env, text=True,
                                        capture_output=True, timeout=timeout)
                self.log.write(result.stdout + result.stderr)
        except FileNotFoundError as error:
            raise Failure(f"Команда {args[0]} не найдена.", f"Установите {args[0]} и повторите запуск.") from error
        except subprocess.TimeoutExpired as error:
            raise Failure(f"Команда не завершилась за {timeout} секунд: {command}",
                          "Проверьте доступность сервиса/сети; подробности в логе. Повторите запуск после исправления.") from error
        if check and result.returncode:
            tail = "\n".join((result.stderr or result.stdout).splitlines()[-12:])
            remedy = "Устраните указанную причину и повторите запуск. Выполненные настройки будут использованы повторно."
            if args[0] == "sudo" and any(text in (result.stderr + result.stdout).lower() for text in ("password", "terminal")):
                remedy = "Запустите deploy из интерактивного терминала и введите пароль sudo."
            raise Failure(f"Команда завершилась с кодом {result.returncode}: {command}\n{tail}",
                          remedy)
        return result

    def root(self, args, **kwargs):
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        if prefix and not shutil.which("sudo"):
            raise Failure("Для настройки хоста нужны права root, но sudo отсутствует.",
                          "Установите sudo или запустите команду от root с доступом к тому же Docker daemon.")
        if prefix:
            # Keep sudo in the caller's session so terminal-scoped credentials
            # remain valid and it can prompt again when authentication expires.
            kwargs["preserve_terminal"] = True
        return self.run(prefix + list(args), **kwargs)

    def host_run(self, args, check=True):
        # sudo may strip environment variables; explicitly select the local server.
        if "-h" not in args:
            args = [args[0], "-h", "/run/cups/cups.sock", *args[1:]]
        return self.root(args, check=check, env={**self.env, "CUPS_SERVER": "/run/cups/cups.sock"})

    def compose(self, args, **kwargs):
        if not (STATE / "compose.env").exists():
            raise Failure("Развертывание еще не подготовлено.", "Выполните python3 scripts/printer.py deploy.")
        env = {key: value for key, value in self.env.items() if key not in (
            "HP_PRINTER_GID", "PRINTER_IMAGE", "PRINTER_PORT", "PRINTER_MODE", "PRINTER_BIND", "PRINTER_CONTAINER_PORT")}
        return self.run(["docker", "compose", "--env-file", STATE / "compose.env", "-f", ROOT / "compose.yaml", *args], env=env, **kwargs)

    def preflight(self):
        if platform.system() != "Linux":
            raise Failure("USB-развертывание поддерживает только Linux.", "Запустите команду на Linux-хосте принтера.")
        for command in ("docker", "udevadm"):
            if not shutil.which(command):
                raise Failure(f"Не найден {command}.", f"Установите {command}; для USB требуется udev/eudev. Docker Engine и Compose устанавливаются заранее.")
        self.run(["docker", "compose", "version"])
        info = json.loads(self.run(["docker", "info", "--format", "{{json .}}"]).stdout)
        if any("rootless" in option for option in info.get("SecurityOptions", [])):
            raise Failure("Docker daemon работает в rootless-режиме.", "Используйте локальный rootful Docker Engine для доступа к USB.")
        context = json.loads(self.run(["docker", "context", "inspect"]).stdout)[0]
        endpoint = os.environ.get("DOCKER_HOST", context["Endpoints"]["docker"]["Host"])
        if not endpoint.startswith("unix://"):
            raise Failure("Docker daemon должен находиться на USB-хосте.", "Выберите локальный Docker context с Unix-сокетом.")
        aliases = {"x86_64": "amd64", "aarch64": "arm64", "amd64": "amd64", "arm64": "arm64"}
        self.arch = aliases.get(info["Architecture"])
        if self.arch not in self.lock["build"]["architectures"]:
            raise Failure(f"Архитектура Docker {info['Architecture']} не закреплена для этой сборки.", "Нужен совместимый HP-плагин и проверенный lock-файл для этой архитектуры.")
        self.say(f"  OK   Python {platform.python_version()}, локальный Docker ({self.arch}), Compose, udev")

    def plugin(self):
        plugin = self.lock["plugin"]
        target = ROOT / "plugins" / f"hplip-{plugin['version']}-plugin.run"
        if not target.exists():
            self.say(f"  CHANGE Загрузка плагина {plugin['version']} из {plugin['url']}")
            temp = target.with_suffix(".download")
            try:
                with urllib.request.urlopen(plugin["url"], timeout=30) as response, temp.open("wb") as output:
                    deadline = time.monotonic() + 180
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        total += len(chunk)
                        self.say(f"    Загружено {total // 1024} КиБ")
                        if total > 100 * 1024 * 1024 or time.monotonic() > deadline:
                            raise Failure("Превышен лимит загрузки плагина.", "Положите проверенный плагин в plugins/ и повторите запуск.")
                self.check_plugin(temp)
                temp.replace(target)
            except OSError as error:
                raise Failure(f"Не удалось скачать плагин: {error}", f"Скачайте {plugin['url']} вручную в {target} и повторите запуск.") from error
            finally:
                temp.unlink(missing_ok=True)
        else:
            self.check_plugin(target)
        self.say(f"  OK   Плагин {plugin['version']}: SHA-256 совпадает")

    def check_plugin(self, path):
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if actual != self.lock["plugin"]["sha256"]:
            raise Failure(f"Контрольная сумма {path} не совпадает: {actual}",
                          "Файл не будет исполнен. Замените его файлом закрепленной версии; не меняйте хеш без проверки происхождения.")

    def devices(self):
        p = self.config["printer"]
        found = []
        for entry in Path("/sys/bus/usb/devices").iterdir():
            try:
                if all((entry / field).read_text().strip() == p[key] for field, key in
                       (("idVendor", "vendor_id"), ("idProduct", "product_id"), ("serial", "serial"))):
                    bus = int((entry / "busnum").read_text())
                    device = int((entry / "devnum").read_text())
                    found.append((entry, Path(f"/dev/bus/usb/{bus:03d}/{device:03d}")))
            except (FileNotFoundError, PermissionError):
                continue
        return found

    def usb(self):
        host, p = self.config["host"], self.config["printer"]
        group = host["usb_group"]
        created = False
        self.say("  Настройка группы и правила USB при необходимости использует sudo.")
        try:
            gid = grp.getgrnam(group).gr_gid
            self.say(f"  SKIP Группа {group} существует, GID={gid}")
        except KeyError:
            if shutil.which("groupadd"):
                self.root(["groupadd", "--system", group])
            elif shutil.which("addgroup"):
                self.root(["addgroup", "-S", group])
            else:
                raise Failure("Не найдена команда создания групп.", f"Создайте системную группу {group} и повторите запуск.")
            gid = grp.getgrnam(group).gr_gid
            created = True
            self.say(f"  CHANGE Создана группа {group}, GID={gid}")
        # Escape udev strings; arbitrary serial values must never inject rules.
        serial = p["serial"]
        if any(c in serial for c in ('"', '\\', '\n', '\r', '*', '?', '[', ']')):
            raise ValueError("Серийный номер содержит недопустимые для udev символы")
        rule = (f'SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="{p["vendor_id"]}", '
                f'ATTR{{idProduct}}=="{p["product_id"]}", ATTR{{serial}}=="{serial}", GROUP="{group}", MODE="0660"\n')
        target = Path("/etc/udev/rules.d/99-hp-p1102.rules")
        changed = not target.exists() or target.read_text() != rule
        if changed:
            source = STATE / "99-hp-p1102.rules"
            atomic_write(source, rule)
            self.root(["install", "-d", "-m", "0755", target.parent])
            self.root(["install", "-m", "0644", source, str(target) + ".tmp"])
            self.root(["mv", str(target) + ".tmp", target])
            self.say("  CHANGE Правило udev установлено")
        else:
            self.say("  SKIP Правило udev соответствует конфигурации")
        devices = self.devices()
        needs_reload = changed or created or any(
            node.stat().st_gid != gid or stat.S_IMODE(node.stat().st_mode) != 0o660 for _, node in devices)
        if needs_reload:
            self.root(["udevadm", "control", "--reload-rules"])
        for entry, node in devices:
            metadata = node.stat()
            if changed or metadata.st_gid != gid or stat.S_IMODE(metadata.st_mode) != 0o660:
                self.root(["udevadm", "trigger", "--action=change", str(entry.resolve())])
                self.root(["udevadm", "settle", "--timeout=10"])
                metadata = node.stat()
                self.say(f"  FIX  Правило применено к {node}")
            if metadata.st_gid != gid or stat.S_IMODE(metadata.st_mode) != 0o660:
                raise Failure(f"USB-права {node} не соответствуют группе {group} и режиму 0660.",
                              "Переподключите принтер и повторите deploy. Проверьте конфликтующие правила udev через doctor.")
            self.say(f"  OK   {node}: группа {group}, режим 0660")
        if not devices:
            self.say("  WARN Принтер не подключен. Настройка продолжится; проверка USB и физическая печать отложены.")
        return gid

    def image(self, mode="local"):
        inputs = [ROOT / name for name in ("Dockerfile", "build.lock.toml", "scripts/runtime.py", "scripts/entrypoint.py", "config/cupsd.conf")]
        if mode == "server":
            inputs += [ROOT / name for name in ("requirements-proxy.lock", "scripts/ipp.py", "scripts/proxy.py", "config/test-print.test")]
        digest = hashlib.sha256()
        digest.update(f"{self.arch}:{mode}".encode())
        for path in inputs:
            digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
        tag = f"local/hplip-p1102:{mode}-{digest.hexdigest()[:20]}"
        if self.run(["docker", "image", "inspect", tag], check=False).returncode == 0:
            self.say(f"  SKIP Образ {tag} уже собран")
            return tag
        lock = self.lock
        args = ["docker", "build", "--target", mode, "--platform", f"linux/{self.arch}", "-t", tag]
        build_args = {
            "BASE_IMAGE": lock["build"]["base_image"], "DEBIAN_SNAPSHOT": lock["build"]["snapshot"],
            "APT_PACKAGES": " ".join(f"{k}={v}" for k, v in lock["packages"].items()),
            "PLUGIN_VERSION": lock["plugin"]["version"], "PLUGIN_SHA256": lock["plugin"]["sha256"],
        }
        if mode == "server":
            build_args["SERVER_APT_PACKAGES"] = " ".join(f"{k}={v}" for k, v in lock["server_packages"].items())
        for key, value in build_args.items():
            args += ["--build-arg", f"{key}={value}"]
        self.say("  CHANGE Сборка закрепленного образа; вывод Docker следует ниже")
        self.run(args + ["."], stream=True, timeout=1800)
        self.say(f"  OK   Образ {tag} собран")
        return tag

    def start(self, gid, image, mode="local"):
        # Mount a directory: editors/atomic writes can replace the file inode.
        # A single-file bind mount would keep showing the obsolete configuration.
        runtime_config = STATE / "runtime" / "printer.toml"
        desired = (ROOT / "printer.toml").read_text()
        if not runtime_config.exists() or runtime_config.read_text() != desired:
            atomic_write(runtime_config, desired)
        content = (f"HP_PRINTER_GID={gid}\nPRINTER_IMAGE={image}\nPRINTER_PORT={self.config['container']['host_port']}\n"
                   f"PRINTER_MODE={mode}\nPRINTER_BIND={'0.0.0.0' if mode == 'server' else '127.0.0.1'}\n"
                   f"PRINTER_CONTAINER_PORT={8631 if mode == 'server' else 631}\n")
        envfile = STATE / "compose.env"
        if not envfile.exists() or envfile.read_text() != content:
            atomic_write(envfile, content)
        # `up` reconciles without replacing unchanged running containers.
        try:
            self.compose(["up", "-d", "--wait", "--wait-timeout", "90"], stream=True, timeout=120)
            # Bind-mounted printer config can change without a container replacement.
            self.compose(["exec", "-T", "hplip", "python3", "/usr/local/bin/entrypoint.py", "--apply"], stream=True)
        except Failure:
            self.say("  Журнал контейнера перед ошибкой:")
            try:
                logs = self.compose(["logs", "--tail=40", "hplip"], check=False)
                self.say((logs.stdout + logs.stderr).strip() or "Контейнер еще не создал журнал.")
            except Failure as log_error:
                self.say(f"  Не удалось получить журнал: {log_error}")
            raise
        self.say("  OK   Контейнер работает, CUPS и управляемая очередь готовы")

    def check_proxy(self):
        uri = self.public_uri()
        request = urllib.request.Request(uri.replace("ipp://", "http://", 1),
                                         data=printer_attributes_request(uri),
                                         headers={"Content-Type": "application/ipp"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                header, records = decode_prefix(response.read())
            if struct.unpack_from(">H", header, 2)[0] >= 0x0400:
                raise ValueError("Get-Printer-Attributes отклонен")
            uris = [record.value.decode() for name, record, depth in attributes(records)
                    if not depth and name == b"printer-uri-supported"]
            if uri not in uris:
                raise ValueError(f"Прокси вернул неверные адреса принтера: {uris}")
        except (OSError, ValueError) as error:
            raise Failure(f"IPP-прокси не готов: {error}",
                          "Проверьте журнал контейнера через doctor и повторите server.") from error
        self.say(f"  OK   Корневой IPP-адрес отвечает: {uri}")

    def install_cups(self):
        managers = [
            ("apt-get", ["apt-get", "install", "-y", "cups", "cups-client"], ["apt-get", "update"]),
            ("dnf", ["dnf", "install", "-y", "cups", "cups-client"], None),
            ("pacman", ["pacman", "-S", "--needed", "--noconfirm", "cups"], None),
            ("zypper", ["zypper", "--non-interactive", "install", "cups", "cups-client"], None),
            ("apk", ["apk", "add", "cups", "cups-client", "cups-openrc"], None),
            ("xbps-install", ["xbps-install", "-S", "-y", "cups"], None),
        ]
        for executable, install, refresh in managers:
            if shutil.which(executable):
                self.say(f"  CHANGE Установка CUPS через {executable}; требуется sudo")
                if refresh:
                    self.root(refresh, stream=True, timeout=600)
                self.root(install, stream=True, timeout=900)
                return
        raise Failure("Не найден поддерживаемый пакетный менеджер.", "Установите CUPS и cups-client средствами дистрибутива, запустите CUPS и повторите deploy.")

    def ensure_cups(self):
        tools = ("lpadmin", "lpstat", "lpoptions", "cupsenable", "cupsaccept")
        if any(not shutil.which(tool) for tool in tools):
            if not self.config["host"]["install_cups"]:
                raise Failure("На хосте отсутствуют инструменты CUPS.", "Установите CUPS/cups-client или включите host.install_cups в printer.toml.")
            self.install_cups()
        if scheduler_running(self.host_run(["lpstat", "-r"], check=False)):
            self.say("  SKIP CUPS хоста уже работает")
            return
        self.say("  CHANGE Запуск CUPS хоста; требуется sudo")
        if Path("/run/systemd/system").is_dir() and shutil.which("systemctl"):
            # Unit names differ across distributions.
            unit = None
            for candidate in ("cups.service", "cupsd.service"):
                if self.run(["systemctl", "show", "-p", "LoadState", "--value", candidate], check=False).stdout.strip() == "loaded":
                    unit = candidate
                    break
            if unit:
                self.root(["systemctl", "enable", "--now", unit], stream=True)
            else:
                raise Failure("Не найдена systemd-служба CUPS.", "Установите сервер CUPS и запустите его службу.")
        elif shutil.which("rc-service"):
            self.root(["rc-update", "add", "cups", "default"])
            self.root(["rc-service", "cups", "start"], stream=True)
        elif shutil.which("sv") and Path("/etc/sv/cupsd").is_dir():
            service = Path("/var/service/cupsd")
            if not service.exists():
                self.root(["ln", "-s", "/etc/sv/cupsd", service])
            self.root(["sv", "up", service])
        else:
            raise Failure("Не найден адаптер запуска CUPS для системы инициализации хоста.", "Запустите CUPS средствами своей системы и повторите deploy.")
        deadline = time.monotonic() + 20
        while not scheduler_running(self.host_run(["lpstat", "-r"], check=False)):
            if time.monotonic() > deadline:
                raise Failure("CUPS хоста не стал доступен за 20 секунд.", "Проверьте журнал службы CUPS и повторите deploy.")
            time.sleep(1)
        self.say("  OK   CUPS хоста запущен и отвечает")

    def host_queue(self):
        self.say("  Настройка очереди хоста при необходимости использует sudo.")
        self.ensure_cups()
        config = self.config
        # Discover driverless capability instead of assuming it from the URL.
        url = ipp_uri(config)
        self.host_run(["lpstat", "-h", f"localhost:{config['container']['host_port']}", "-p", config["printer"]["name"]])
        self.say("  OK   IPP-сервер контейнера отвечает")
        name = config["host"]["queue"]
        def ppd_hash(path):
            result = self.root(["sha256sum", path], check=False)
            return result.stdout.split()[0] if result.returncode == 0 else None
        reconcile_queue(self.host_run, name, url, "everywhere", config["printer"].get("options", {}),
                        STATE / "host-queue.json", "/etc/cups/ppd", lambda line: self.say("  " + line),
                        get_ppd_hash=ppd_hash, make_default=config["host"]["make_default"])
        self.host_run(["lpstat", "-v", name])
        self.say(f"  OK   Принтер {name} подключен на хосте")

    def deploy(self, mode="local"):
        self.say(f"Режим: {'server — сетевой IPP на 0.0.0.0' if mode == 'server' else 'local — локальная печать'}")
        operations = [("Проверка хоста", self.preflight), ("Проверка HP-плагина", self.plugin),
                      ("Настройка USB", self.usb), ("Подготовка образа", lambda: self.image(mode))]
        results = []
        for number, (title, operation) in enumerate(operations, 1):
            self.step(number, title)
            results.append(operation())
            self.completed.append(title)
        self.step(5, "Запуск контейнера")
        self.start(results[2], results[3], mode)
        self.completed.append(self.stage)
        self.step(6, "Проверка IPP-прокси" if mode == "server" else "Настройка принтера на хосте")
        if mode == "server":
            self.check_proxy()
        else:
            self.host_queue()
        self.completed.append(self.stage)
        self.step(7, "Итоговая проверка")
        self.compose(["exec", "-T", "hplip", "python3", "/usr/local/bin/entrypoint.py", "--check"])
        nodes = [str(node) for _, node in self.devices()]
        self.compose(["exec", "-T", "hplip", "python3", "/usr/local/bin/entrypoint.py", "--usb-check", *nodes], stream=True)
        if mode == "local":
            self.host_run(["lpstat", "-p", self.config["host"]["queue"]])
        address = f"ipp://<адрес-сервера>:{self.config['container']['host_port']}/" if mode == "server" else ipp_uri(self.config)
        self.say(f"  OK   Развертывание завершено. IPP: {address}")
        if not self.devices():
            self.say("  WARN USB-принтер отсутствует: физическая печать пока не проверена.")
        self.say("Пробная печать: python3 scripts/printer.py test-print")

    def status(self, doctor=False):
        self.stage = "Диагностика" if doctor else "Состояние"
        problems = []
        for entry, node in self.devices():
            metadata = node.stat()
            self.say(f"USB: {node}, GID={metadata.st_gid}, режим={stat.S_IMODE(metadata.st_mode):04o}")
            try:
                gid = grp.getgrnam(self.config["host"]["usb_group"]).gr_gid
            except KeyError:
                gid = None
            if gid != metadata.st_gid or stat.S_IMODE(metadata.st_mode) != 0o660:
                problems.append(f"Неверные USB-права {node}")
        if not self.devices():
            problems.append("USB-принтер не найден")
        checks = [(["ps"], "Контейнер"), (["exec", "-T", "hplip", "lpstat", "-t"], "CUPS контейнера")]
        if doctor:
            checks += [(["exec", "-T", "hplip", "id", "lp"], "Группы lp"),
                       (["exec", "-T", "hplip", "python3", "/usr/local/bin/entrypoint.py", "--usb-check",
                         *[str(node) for _, node in self.devices()]], "Реальные права процессов CUPS и доступ к USB"),
                       (["exec", "-T", "hplip", "cat", "/var/lib/hp/hplip.state"], "HP-плагин"),
                       (["logs", "--tail=80", "hplip"], "Журнал контейнера")]
        for args, title in checks:
            self.say(f"\n{title}:")
            try:
                result = self.compose(args, check=False)
                self.say((result.stdout + result.stderr).strip() or "Нет вывода")
                if result.returncode:
                    problems.append(f"{title}: проверка завершилась ошибкой")
            except Failure as error:
                problems.append(str(error))
        if self.deployed_mode() == "server":
            self.say("\nIPP-прокси:")
            try:
                self.check_proxy()
            except Failure as error:
                problems.append(str(error))
        elif shutil.which("lpstat"):
            self.say("\nCUPS хоста:")
            result = self.run(["lpstat", "-h", "/run/cups/cups.sock", "-v", self.config["host"]["queue"]], check=False)
            self.say((result.stdout + result.stderr).strip())
            if result.returncode:
                problems.append("CUPS хоста или управляемая очередь недоступны")
            elif result.stdout.strip().split(": ", 1)[-1] != ipp_uri(self.config):
                problems.append("URI очереди хоста расходится с конфигурацией")
        else:
            problems.append("CUPS хоста не установлен")
        if doctor:
            if shutil.which("getenforce"):
                self.say("SELinux: " + self.run(["getenforce"], check=False).stdout.strip())
            self.say("Ожидаемые права USB: группа " + self.config["host"]["usb_group"] + ", 0660")
        if problems:
            raise Failure("; ".join(dict.fromkeys(problems)), "Запустите deploy для восстановления настроек; при ошибках USB проверьте подключение и журнал контейнера.")

    def test_print(self):
        self.stage = "Пробная печать"
        if not self.devices():
            raise Failure("Принтер не подключен.", "Подключите и включите принтер, затем повторите test-print.")
        sample = STATE / "test-print.txt"
        atomic_write(sample, f"HP LaserJet P1102 — test print\n{datetime.now().isoformat()}\nConfiguration as code\n")
        if self.deployed_mode() == "server":
            self.say("Отправка одной тестовой страницы через корневой IPP-адрес сервера.")
            self.compose(["exec", "-T", "hplip", "ipptool", "-tv", "-f", "/etc/hosts", "ipp://localhost:8631/",
                          "/usr/local/share/printer/test-print.test"], stream=True)
            self.say("OK Задание принято. Подтвердите выход страницы; при проблемах выполните doctor.")
            return
        self.say("Отправка одной тестовой страницы через очередь хоста.")
        result = self.run(["lp", "-d", self.config["host"]["queue"], sample],
                          env={**self.env, "CUPS_SERVER": "/run/cups/cups.sock"})
        self.say(result.stdout.strip())
        self.say("OK Задание принято. Подтвердите выход страницы; при проблемах выполните doctor.")


def main():
    parser = argparse.ArgumentParser(description="Развертывание и диагностика HP P1102. Python stdlib, без внешних зависимостей.")
    parser.add_argument("command", choices=("deploy", "server", "status", "doctor", "test-print"))
    parser.add_argument("--verbose", action="store_true", help="показывать команды и traceback")
    args = parser.parse_args()
    app = None
    lock = None
    try:
        STATE.mkdir(exist_ok=True)
        lock = (STATE / "command.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("Другая команда уже работает с этим репозиторием.", "Дождитесь ее завершения и повторите запуск.")
        app = App(args.verbose)
        if args.command in ("deploy", "server"):
            app.deploy(mode="server" if args.command == "server" else "local")
        elif args.command == "test-print":
            app.test_print()
        else:
            app.status(doctor=args.command == "doctor")
        return 0
    except Exception as error:
        message = f"\nERROR [{app.stage if app else 'Подготовка'}]\n{error}"
        if app:
            app.say(message)
            app.log.write(traceback.format_exc())
            if app.completed:
                app.say("Уже выполнено: " + ", ".join(app.completed))
        else:
            print(message, file=sys.stderr)
        remedy = error.remedy if isinstance(error, Failure) else "Проверьте конфигурацию и доступность файлов. Подробности в логе."
        print("Что сделать: " + remedy, flush=True)
        if app:
            print(f"Подробный лог: {app.log_path}", flush=True)
        if args.verbose:
            traceback.print_exc()
        return 1
    finally:
        if app:
            app.log.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
