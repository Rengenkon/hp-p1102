"""Optional Docker integration check: no USB access or host changes.

Includes a job to an isolated fake backend to verify actual CUPS credentials.

Usage: python3 tests/smoke_container.py local/hplip-p1102:<tag>
"""
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from docker_helpers import cleanup, run, wait_ready

ROOT = Path(__file__).resolve().parents[1]


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
        wait_ready(server)
        print("OK: container starts and queue is ready")
        name = "HP_LaserJet_Professional_P1102"
        ppd = f"/etc/cups/ppd/{name}.ppd"
        # Simulate persistent volumes created with the old CUPS primary group.
        run("docker", "exec", server, "chgrp", "-R", "7", "/etc/cups/ppd", "/var/spool/cups")
        run("docker", "restart", server)
        wait_ready(server, timeout=30)
        assert run("docker", "exec", server, "stat", "-c", "%g", ppd) == "987"
        assert run("docker", "exec", server, "stat", "-c", "%g", "/var/spool/cups/tmp") == "987"
        print("OK: old PPD/spool group ownership repaired on restart")
        backend = '''#!/usr/bin/python3
import os,json,sys
from pathlib import Path
if len(sys.argv) == 1:
    print('direct credentials "Unknown" "Credential probe"')
else:
    Path("/tmp/backend-credentials.json").write_text(json.dumps({"uid":os.getuid(), "gid":os.getgid(), "groups":os.getgroups()}))
'''
        install_probe = (
            "from pathlib import Path; import os; "
            f"p=Path('/usr/lib/cups/backend/credentials'); p.write_text({backend!r}); p.chmod(0o755); "
            "p=Path('/tmp/mock-usb'); p.touch(); os.chown(p,0,987); p.chmod(0o660)")
        run("docker", "exec", server, "python3", "-c", install_probe)
        run("docker", "exec", server, "python3", "/usr/local/bin/entrypoint.py", "--usb-check", "/tmp/mock-usb")
        run("docker", "exec", server, "lpadmin", "-p", "credential_probe", "-E", "-v", "credentials:/probe", "-m", "raw")
        run("docker", "exec", server, "lp", "-d", "credential_probe", "/etc/hosts")
        deadline = time.monotonic() + 20
        while True:
            exists = subprocess.run(["docker", "exec", server, "test", "-f", "/tmp/backend-credentials.json"], capture_output=True)
            if exists.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Credential probe did not run\n" + run("docker", "logs", server))
            time.sleep(0.25)
        credentials = json.loads(run("docker", "exec", server, "cat", "/tmp/backend-credentials.json"))
        assert credentials["uid"] != 0 and credentials["gid"] == 987, credentials
        assert credentials["groups"] == [987], credentials
        print("OK: actual CUPS backend uses the USB primary group, with no reliance on supplementary groups")
        before = run("docker", "exec", server, "stat", "-c", "%Y", "/etc/cups/.managed-printer.json")
        repeated = run("docker", "exec", server, "python3", "/usr/local/bin/entrypoint.py", "--apply")
        assert "SKIP" in repeated, repeated
        assert before == run("docker", "exec", server, "stat", "-c", "%Y", "/etc/cups/.managed-printer.json")
        print("OK: repeated apply leaves queue and state intact")
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
from runtime import local_run,reconcile_queue,scheduler_running
os.environ["LC_ALL"]="C"
Path("/run/cups").mkdir(parents=True,exist_ok=True)
stopped=local_run(["lpstat","-r"],check=False)
assert not scheduler_running(stopped), stopped.stdout + stopped.stderr
cups=subprocess.Popen(["cupsd","-f"])
try:
    for _ in range(40):
        if scheduler_running(local_run(["lpstat","-r"],check=False)): break
        time.sleep(.25)
    else:
        raise AssertionError("Client CUPS scheduler did not start")
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
        cleanup(client, server, network, runtime)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
