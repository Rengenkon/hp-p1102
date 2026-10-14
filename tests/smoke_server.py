"""Real CUPS/IPP integration with a fake output backend, never physical USB.

Usage: python3 tests/smoke_server.py local/hplip-p1102:server-<hash>
"""
import json
import struct
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from docker_helpers import cleanup, container_logs, run, wait_ready

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from ipp import Attribute, attributes, decode_prefix, encode_prefix


def main(image):
    token = "ipp-smoke-" + uuid.uuid4().hex[:10]
    server, client, network = token + "-server", token + "-client", token + "-network"
    (ROOT / ".state").mkdir(exist_ok=True)
    runtime = tempfile.TemporaryDirectory(prefix=token, dir=ROOT / ".state")
    (Path(runtime.name) / "printer.toml").write_text((ROOT / "printer.toml").read_text())
    try:
        run("docker", "network", "create", network)
        run("docker", "run", "-d", "--name", server, "--network", network, "--network-alias", "printer-server",
            "--security-opt", "no-new-privileges:true", "-e", "HP_PRINTER_GID=987",
            "-v", f"{runtime.name}:/etc/printer:ro", "-p", "127.0.0.1::8631", image)
        wait_ready(server)
        binding = json.loads(run("docker", "inspect", server))[0]["NetworkSettings"]["Ports"]["8631/tcp"][0]
        base = f"http://127.0.0.1:{binding['HostPort']}"
        public = base.replace("http://", "ipp://") + "/"
        assert urllib.request.urlopen(base + "/health", timeout=10).read() == b"OK\n"
        print("OK: server health checks CUPS through the IPP proxy")

        def exchange(operation, extra=(), job_uri=None, document=b""):
            header = struct.pack(">BBHI", 2, 0, operation, 123)
            records = [1, Attribute(0x47, b"attributes-charset", b"utf-8"),
                       Attribute(0x48, b"attributes-natural-language", b"en"),
                       Attribute(0x45, b"job-uri" if job_uri else b"printer-uri", (job_uri or public).encode()),
                       Attribute(0x42, b"requesting-user-name", b"root"), *extra, 3]
            uri = job_uri or public
            http = uri.replace("ipp://", "http://", 1)
            request = urllib.request.Request(http, data=encode_prefix(header, records) + document,
                                             headers={"Content-Type": "application/ipp"})
            with urllib.request.urlopen(request, timeout=30) as response:
                reply, values = decode_prefix(response.read())
            status = struct.unpack_from(">H", reply, 2)[0]
            assert status < 0x0400, (status, values)
            return {name: attr.value for name, attr, depth in attributes(values) if not depth}

        caps = exchange(0x000b)
        assert caps[b"printer-uri-supported"] == public.encode(), caps
        print("OK: root URI returns real printer capabilities with public addresses")
        backend = '''#!/usr/bin/python3
import sys,os,json,hashlib
from pathlib import Path
if len(sys.argv)==1:
    print('direct proof "Unknown" "Isolated test output"')
else:
    data=Path(sys.argv[6]).read_bytes() if len(sys.argv)>6 else sys.stdin.buffer.read()
    Path('/tmp/proof-'+sys.argv[1]+'.json').write_text(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}))
'''
        run("docker", "exec", server, "python3", "-c",
            f"from pathlib import Path; p=Path('/usr/lib/cups/backend/proof'); p.write_text({backend!r}); p.chmod(0o755)")
        name = "HP_LaserJet_Professional_P1102"
        run("docker", "exec", server, "lpadmin", "-p", name, "-v", "proof:/target")

        # Hold the direct Print-Job so status/cancel are deterministic.
        job = exchange(2, [Attribute(0x49, b"document-format", b"text/plain"), 2,
                           Attribute(0x44, b"job-hold-until", b"indefinite")], document=b"held proxy test\n")
        job_uri = job[b"job-uri"].decode()
        assert job_uri.startswith(public + "jobs/"), job
        state = exchange(9, job_uri=job_uri)
        assert struct.unpack(">i", state[b"job-state"])[0] == 4, state
        exchange(8, job_uri=job_uri)
        state = exchange(9, job_uri=job_uri)
        assert struct.unpack(">i", state[b"job-state"])[0] == 7, state
        print("OK: Print-Job, Get-Job-Attributes and Cancel-Job use public job URIs")

        job = exchange(5)
        job_uri = job[b"job-uri"].decode()
        exchange(6, [Attribute(0x49, b"document-format", b"text/plain"),
                     Attribute(0x22, b"last-document", b"\x01")], job_uri=job_uri, document=b"Create-Job / Send-Document\n")
        deadline = time.monotonic() + 30
        while True:
            state = exchange(9, job_uri=job_uri)
            code = struct.unpack(">i", state[b"job-state"])[0]
            if code == 9:
                break
            if code in (6, 7, 8) or time.monotonic() > deadline:
                raise RuntimeError(f"Job failed: {state}\n" + run("docker", "logs", server))
            time.sleep(0.25)
        job_id = job_uri.rsplit("/", 1)[1]
        proof = json.loads(run("docker", "exec", server, "cat", f"/tmp/proof-{job_id}.json"))
        assert proof["bytes"] > 0 and proof["gid"] == 987 and proof["uid"] != 0, proof
        print("OK: Create-Job / Send-Document renders and completes through the real CUPS pipeline")

        client_code = r'''
import sys,time,subprocess,socket
from pathlib import Path
sys.path.insert(0,"/usr/local/lib/printer")
from runtime import local_run as cups_run,scheduler_running
def local_run(args,check=True):
    result=cups_run(args,check=False)
    if check and result.returncode:
        raise RuntimeError(str(args)+"\n"+result.stdout+result.stderr)
    return result
# Only the proxy is reachable from another container.
try:
    socket.create_connection(("printer-server",631),timeout=2)
except OSError:
    pass
else:
    raise AssertionError("Internal CUPS is exposed")
Path("/run/cups").mkdir(parents=True,exist_ok=True)
cups=subprocess.Popen(["cupsd","-f"])
try:
    for _ in range(80):
        if scheduler_running(local_run(["lpstat","-r"],check=False)): break
        time.sleep(.25)
    else: raise AssertionError("Client CUPS startup timeout")
    local_run(["lpadmin","-p","client_print","-E","-v","ipp://printer-server:8631","-m","everywhere"])
    local_run(["lp","-d","client_print","/etc/hosts"])
    for _ in range(120):
        result=local_run(["lpstat","-W","not-completed","-o","client_print"])
        if not result.stdout.strip(): break
        time.sleep(.25)
    else: raise AssertionError(result.stdout)
    print("CLIENT_PRINT_COMPLETED",flush=True)
finally:
    cups.terminate(); cups.wait(timeout=10)
'''
        output = run("docker", "run", "--name", client, "--network", network, "--entrypoint", "python3", image,
                     "-u", "-c", client_code)
        assert "CLIENT_PRINT_COMPLETED" in output, output
        print("OK: separate CUPS client connects to ipp://server:8631 and prints using everywhere")
        run("docker", "stop", "-t", "15", server)
        assert json.loads(run("docker", "inspect", server))[0]["State"]["ExitCode"] == 0
        print("OK: SIGTERM stops the proxy and both daemons cleanly")
    except Exception:
        print(container_logs(server))
        raise
    finally:
        cleanup(client, server, network, runtime)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
