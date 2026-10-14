"""Shared command execution and lifecycle helpers for Docker smoke checks."""
import json
import subprocess
import time


def run(*args, check=True):
    result = subprocess.run([str(a) for a in args], text=True, capture_output=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"{args}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def container_logs(container):
    result = subprocess.run(["docker", "logs", "--tail", "100", container],
                            text=True, capture_output=True, timeout=10)
    return result.stdout + result.stderr


def wait_ready(container, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        state = json.loads(run("docker", "inspect", container))[0]["State"]
        if not state["Running"]:
            raise RuntimeError("Container stopped\n" + container_logs(container))
        result = subprocess.run(
            ["docker", "exec", container, "python3", "/usr/local/bin/entrypoint.py", "--check"],
            capture_output=True, timeout=10)
        if result.returncode == 0:
            return
        if time.monotonic() > deadline:
            raise RuntimeError("Container startup timeout\n" + container_logs(container))
        time.sleep(0.25)


def cleanup(client, server, network, runtime):
    try:
        run("docker", "rm", "-f", client, server, check=False)
    finally:
        try:
            run("docker", "network", "rm", network, check=False)
        finally:
            runtime.cleanup()
