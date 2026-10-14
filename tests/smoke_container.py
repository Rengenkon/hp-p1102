"""Optional Docker integration check: no USB access, print jobs, or host changes.

Usage: python3 tests/smoke_container.py local/hplip-p1102:<tag>
"""
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, check=True):
    result = subprocess.run([str(a) for a in args], text=True, capture_output=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"{args}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main(image):
    token = "hp-smoke-" + uuid.uuid4().hex[:10]
    server, client, network = token + "-server", token + "-client", token + "-network"
    (ROOT / ".state").mkdir(exist_ok=True)
    runtime = tempfile.TemporaryDirectory(prefix=token + "-", dir=ROOT / ".state")
    config = Path(runtime.name) / "printer.toml"
    original = (ROOT / "printer.toml").read_text()
    config.write_text(original)
    try:
        run("docker", "network", "create", network)
        run("docker", "run", "-d", "--name", server, "--network", network,
            "--network-alias", "printer-server", "--security-opt", "no-new-privileges:true",
            "-e", "HP_PRINTER_GID=987",
            "-v", f"{runtime.name}:/etc/printer:ro", image)
        deadline = time.monotonic() + 45
        while True:
            state = json.loads(run("docker", "inspect", server))[0]["State"]
            if not state["Running"]:
                raise RuntimeError(run("docker", "logs", server))
            check = subprocess.run(["docker", "exec", server, "python3", "/usr/local/bin/entrypoint.py", "--check"], capture_output=True)
            if check.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("CUPS startup timeout\n" + run("docker", "logs", server))
            time.sleep(0.5)
        print("OK: container starts and queue is ready")
        before = run("docker", "exec", server, "stat", "-c", "%Y", "/etc/cups/.managed-printer.json")
        repeated = run("docker", "exec", server, "python3", "/usr/local/bin/entrypoint.py", "--apply")
        assert "SKIP" in repeated, repeated
        assert before == run("docker", "exec", server, "stat", "-c", "%Y", "/etc/cups/.managed-printer.json")
        print("OK: repeated apply leaves queue and state intact")
        name = "HP_LaserJet_Professional_P1102"
        run("docker", "exec", server, "lpadmin", "-p", name, "-v", "hp:/usb/wrong", "-o", "PageSize=Letter")
        run("docker", "exec", server, "cupsdisable", name)
        run("docker", "exec", server, "python3", "/usr/local/bin/entrypoint.py", "--apply")
        uri = run("docker", "exec", server, "lpstat", "-v", name)
        assert "serial=000000000Q8D61SNPR1a" in uri, uri
        assert "*A4" in run("docker", "exec", server, "lpoptions", "-p", name, "-l")
        assert "disabled" not in run("docker", "exec", server, "lpstat", "-p", name)
        print("OK: URI, defaults and disabled queue repaired")
        replacement = config.with_suffix(".tmp")
        replacement.write_text(original.replace('PageSize = "A4"', 'PageSize = "Letter"'))
        replacement.replace(config)
        run("docker", "exec", server, "python3", "/usr/local/bin/entrypoint.py", "--apply")
        assert "*Letter" in run("docker", "exec", server, "lpoptions", "-p", name, "-l")
        replacement.write_text(original)
        replacement.replace(config)
        run("docker", "exec", server, "python3", "/usr/local/bin/entrypoint.py", "--apply")
        print("OK: atomically replaced configuration applied without container replacement")
        # A separate CUPS instance represents the host; verify real driverless
        # negotiation and reconcile twice using the same shared implementation.
        client_code = r'''
import os,sys,time,subprocess
from pathlib import Path
sys.path.insert(0,"/usr/local/lib/printer")
from runtime import local_run,reconcile_queue
os.environ["LC_ALL"]="C"
Path("/run/cups").mkdir(parents=True,exist_ok=True)
cups=subprocess.Popen(["cupsd","-f"])
try:
    for _ in range(40):
        if local_run(["lpstat","-r"],check=False).returncode == 0: break
        time.sleep(.25)
    probe=local_run(["lpstat","-h","printer-server:631","-p","HP_LaserJet_Professional_P1102"],check=False)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    for _ in range(2):
        reconcile_queue(local_run,"host_printer","ipp://printer-server:631/printers/HP_LaserJet_Professional_P1102",
                        "everywhere",{"PageSize":"A4"},"/tmp/host-state.json","/etc/cups/ppd")
finally:
    cups.terminate(); cups.wait(timeout=10)
'''
        output = run("docker", "run", "--name", client, "--network", network,
                     "--entrypoint", "python3", image, "-u", "-c", client_code)
        assert "SKIP" in output, output
        print("OK: IPP host queue created with everywhere; repeated apply is idempotent")
        run("docker", "stop", "-t", "15", server)
        state = json.loads(run("docker", "inspect", server))[0]["State"]
        assert state["ExitCode"] == 0, state
        print("OK: SIGTERM stops daemons cleanly")
    finally:
        run("docker", "rm", "-f", client, server, check=False)
        run("docker", "network", "rm", network, check=False)
        runtime.cleanup()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
