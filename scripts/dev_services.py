"""Foreground Linux dev supervisor with restart-safe, project-owned process groups."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
MARKER = "AGENT_X_DEV_SESSION"
STOP_TIMEOUT = 5.0
START_TIMEOUT = 30.0
DEV_ENV_KEYS = {
    "AGENT_X_HOST", "HARNESS_PORT", "BACKEND_PORT", "WEB_PORT", "DEV_RELOAD", "LOG_ROOT"
}


def process_info(pid: int) -> dict | None:
    try:
        proc = Path(f"/proc/{pid}")
        # comm can contain spaces and parentheses; fields after its closing ')'
        # start with state (field 3), ppid, pgrp, session, ... starttime (field 22).
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z" or proc.stat().st_uid != os.getuid():
            return None
        return {"pid": pid, "pgid": int(fields[2]), "sid": int(fields[3]),
                "start": fields[19]}
    except (OSError, ValueError, IndexError):
        return None


class Services:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.runtime = self.root / ".run" / "dev"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.state_path = self.runtime / "services.json"
        self.boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.children: list[subprocess.Popen] = []

    @contextmanager
    def locked(self):
        # Never unlink the lock file: concurrent callers must use the same inode.
        with (self.runtime / "control.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self) -> dict | None:
        if not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text())
            if (state["root"] != str(self.root)
                    or not isinstance(state["token"], str)
                    or not state["token"] or not isinstance(state["services"], list)):
                raise ValueError("wrong project or boot")
            # No process from a previous boot can still belong to this session.
            if state["boot"] != self.boot:
                return None
            for service in state["services"]:
                if (not isinstance(service["pid"], int) or service["pid"] <= 1
                        or service["pgid"] != service["pid"]):
                    raise ValueError("invalid process group")
            return state
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Invalid/stale state at {self.state_path}: {exc}. "
                               "Inspect it before removing it; no processes were stopped.") from exc

    def write(self, state: dict):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def members(self, state: dict, service: dict) -> list[dict]:
        members = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            info = process_info(int(proc.name))
            if info and info["pgid"] == service["pgid"]:
                members.append(info)
        if not members:
            return []
        marker = f"{MARKER}={state['token']}".encode()
        # Validate EVERY live group member. The inherited marker lets us recover
        # npm/Vite or reload children even if their original leader has exited.
        for info in members:
            try:
                env = Path(f"/proc/{info['pid']}/environ").read_bytes().split(b"\0")
            except OSError:
                if process_info(info["pid"]) is None:
                    continue
                raise RuntimeError(f"Cannot verify PID {info['pid']}; refusing to stop it.")
            if (marker not in env or info["sid"] != service["pgid"]
                    or (info["pid"] == service["pid"] and info["start"] != service["start"])):
                raise RuntimeError(f"Process group {service['pgid']} identity changed; "
                                   "refusing to stop it.")
        return members

    def active(self, state: dict | None) -> bool:
        return bool(state and any(self.members(state, item) for item in state["services"]))

    def signal_group(self, state: dict, item: dict, sig: int):
        if self.members(state, item):
            try:
                os.killpg(item["pgid"], sig)
            except ProcessLookupError:
                pass

    def stop(self, state: dict | None):
        if not state:
            return
        # Verify all groups before sending any signals.
        for item in state["services"]:
            self.members(state, item)
        print("Stopping local services…", flush=True)
        for item in state["services"]:
            self.signal_group(state, item, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT
        while self.active(state) and time.monotonic() < deadline:
            time.sleep(0.1)
        for item in state["services"]:
            self.signal_group(state, item, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while self.active(state) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.active(state):
            raise RuntimeError("Services did not stop; keeping their state for another stop attempt.")
        self.state_path.unlink(missing_ok=True)

    def status(self):
        state = self.read()
        if not state:
            print("Local services are stopped.")
            return
        for item in state["services"]:
            members = self.members(state, item)
            print(f"{item['name']}: {'running' if members else 'stopped'} "
                  f"(port {item['port']}, process group {item['pgid']})")
        print(f"Service logs: {state['logs']}")

    def launch(self, config: dict) -> dict:
        token = uuid.uuid4().hex
        logs = config["logs"]
        logs.mkdir(parents=True, exist_ok=True)
        state = {"root": str(self.root), "boot": self.boot, "token": token,
                 "logs": str(logs), "services": []}
        self.write(state)
        try:
            for name, port, command, cwd in config["commands"]:
                env = dict(os.environ, **{MARKER: token,
                           "HARNESS_URL": f"http://127.0.0.1:{config['ports'][0]}",
                           "BACKEND_PORT": str(config["ports"][1])})
                with (logs / f"{name}.log").open("w") as log:
                    child = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                self.children.append(child)
                info = process_info(child.pid)
                if info is None:
                    raise RuntimeError(f"{name} exited during startup; see {logs / (name + '.log')}")
                state["services"].append(dict(info, name=name, port=port))
                self.write(state)
                print(f"Starting {name} on http://{config['host']}:{port}", flush=True)
        except BaseException:
            self.stop(state)
            raise
        return state


def dotenv_settings(root: Path) -> dict[str, str]:
    """Read only dev supervisor settings without evaluating shell syntax."""
    path = root / ".env"
    if not path.is_file():
        return {}
    values = {}
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in DEV_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif re.search(r"\s+#", value):
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if not value:
            raise RuntimeError(f"{path}:{line_number}: {key} must not be empty.")
        values[key] = value
    return values


def configuration(root: Path) -> dict:
    file_values = dotenv_settings(root)

    def setting(name: str, default: str) -> str:
        return os.environ[name] if name in os.environ else file_values.get(name, default)

    host = setting("AGENT_X_HOST", "127.0.0.1")
    ports = [int(setting(key, default)) for key, default in
             [("HARNESS_PORT", "8001"), ("BACKEND_PORT", "8000"), ("WEB_PORT", "5173")]]
    if len(set(ports)) != 3 or any(not 1 <= port <= 65535 for port in ports):
        raise RuntimeError("HARNESS_PORT, BACKEND_PORT and WEB_PORT must be distinct valid ports.")
    reload = setting("DEV_RELOAD", "1")
    if reload not in {"0", "1"}:
        raise RuntimeError("DEV_RELOAD must be 0 or 1.")
    logs = Path(setting("LOG_ROOT", ".data/logs"))
    if not logs.is_absolute():
        logs = root / logs
    commands = []
    for name, port in zip(["harness", "backend"], ports):
        command = [sys.executable, "-m", "uvicorn", f"{name}.app:app", "--host", host,
                   "--port", str(port)]
        if reload == "1":
            command += ["--reload", "--reload-dir", str(root / name),
                        "--reload-dir", str(root / "contracts")]
        commands.append((name, port, command, root))
    commands.append(("web", ports[2], ["npm", "run", "dev", "--", "--host", host,
                                     "--port", str(ports[2]), "--strictPort"], root / "web"))
    return {"host": host, "ports": ports, "logs": logs / "services", "commands": commands}


def check_dependencies(root: Path):
    for name in ["fastapi", "httpx", "pydantic", "uvicorn"]:
        if importlib.util.find_spec(name) is None:
            raise RuntimeError(f"Missing {name}. Run: {sys.executable} -m pip install -e '.[dev]'")
    if not shutil.which("npm"):
        raise RuntimeError("npm is required to start the Web UI.")
    if not (root / "web/node_modules").is_dir():
        raise RuntimeError("Web dependencies are missing. Run: (cd web && npm install)")


def check_ports(config: dict):
    for port in config["ports"]:
        for family, kind, proto, _, address in socket.getaddrinfo(
                config["host"], port, type=socket.SOCK_STREAM):
            with socket.socket(family, kind, proto) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(address)
                except OSError as exc:
                    if shutil.which("ss"):
                        subprocess.run(["ss", "-ltnp", f"sport = :{port}"], check=False)
                    raise RuntimeError(f"Cannot bind {config['host']}:{port}: {exc}. "
                                       "No unrelated processes were stopped. Change the corresponding "
                                       "HARNESS_PORT, BACKEND_PORT or WEB_PORT, or inspect the listener.") from exc


def ready(config: dict) -> bool:
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(config["host"], config["host"])
    for port in config["ports"]:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                pass
        except OSError:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the foreground Linux development session.")
    parser.add_argument("action", nargs="?", default="start",
                        choices=["start", "restart", "stop", "status"])
    args = parser.parse_args()
    manager = Services()
    interrupted = False

    def on_signal(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    state = None
    try:
        with manager.locked():
            if args.action == "status":
                manager.status()
                return 0
            previous = manager.read()
            if args.action == "stop":
                manager.stop(previous)
                if previous is None:
                    manager.state_path.unlink(missing_ok=True)
                print("Local services are stopped.")
                return 0
            if args.action == "start" and manager.active(previous):
                manager.status()
                print("Already running. Use ./dev.sh restart to restart this project's services.")
                return 0
            config = configuration(manager.root)
            check_dependencies(manager.root)
            manager.stop(previous)
            check_ports(config)
            if interrupted:
                return 130
            state = manager.launch(config)
        deadline = time.monotonic() + START_TIMEOUT
        announced = False
        while not interrupted:
            for child in manager.children:
                code = child.poll()
                if code is not None:
                    with manager.locked():
                        current = manager.read()
                        if not current or current["token"] != state["token"]:
                            return 0  # Another terminal stopped or restarted us.
                    raise RuntimeError(f"A service exited (status {code}). See {state['logs']} for logs.")
            if not announced:
                if ready(config):
                    print(f"Services are listening. Open http://{config['host']}:{config['ports'][2]}\n"
                          f"Service logs: {state['logs']}\nPress Ctrl+C to stop all three services.",
                          flush=True)
                    announced = True
                elif time.monotonic() >= deadline:
                    raise RuntimeError(f"Startup timed out. See {state['logs']} for logs.")
            time.sleep(0.1)
        return 0
    finally:
        if state:
            with manager.locked():
                current = manager.read()
                # A restart can already have installed a new session. An old
                # supervisor must never stop or remove that session's state.
                if current and current["token"] == state["token"]:
                    manager.stop(current)
            for child in manager.children:
                child.wait(timeout=2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
