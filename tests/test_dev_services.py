"""Exercise dev.sh against real listening processes without project dependencies."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev_services", ROOT / "scripts/dev_services.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lifecycle")


def proc_child_ids_are_visible():
    """Some nested sandboxes expose host /proc IDs instead of Popen's IDs."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"],
                               start_new_session=True)
    try:
        info = dev.process_info(process.pid)
        return bool(info and info["pid"] == process.pid and info["pgid"] == process.pid
                    and info["sid"] == process.pid)
    finally:
        process.terminate()
        process.wait()


requires_visible_proc = pytest.mark.skipif(
    not proc_child_ids_are_visible(), reason="sandbox does not expose child PIDs through /proc"
)

SERVER = '''import json, os, signal, socket, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
name = args[0]
if name == "web":
    child = subprocess.Popen([sys.executable, __file__, "web-child", "--port", str(port)])
    print(json.dumps({"child": child.pid}), flush=True)
    child.wait()
    sys.exit(child.returncode)
print(json.dumps({"args": args, "harness_url": os.environ.get("HARNESS_URL"),
                  "backend_port": os.environ.get("BACKEND_PORT")}), flush=True)
if os.environ.get("FAKE_NO_LISTEN") == name:
    time.sleep(60)
else:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen()
        while True:
            connection, _ = sock.accept()
            connection.close()
'''


def free_ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def eventually(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for development services")


class Session:
    def __init__(self, root):
        self.root = root
        (root / "scripts").mkdir(parents=True)
        (root / "web/node_modules").mkdir(parents=True)
        (root / ".venv/bin").mkdir(parents=True)
        (root / ".venv/bin/python").symlink_to(sys.executable)
        for filename in ["dev.sh", "scripts/dev_services.py"]:
            shutil.copy2(ROOT / filename, root / filename)
        modules = root / "fake_modules"
        modules.mkdir()
        for name in ["fastapi", "httpx", "pydantic"]:
            (modules / f"{name}.py").touch()
        (modules / "uvicorn").mkdir()
        (modules / "uvicorn/__init__.py").touch()
        (modules / "uvicorn/__main__.py").write_text(SERVER)
        (modules / "server.py").write_text(SERVER)
        binary = root / "bin"
        binary.mkdir()
        npm = binary / "npm"
        npm.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "{modules / "server.py"}" web "$@"\n')
        npm.chmod(0o755)
        self.env = dict(os.environ, PYTHONPATH=str(modules),
                        PATH=f"{binary}:{os.environ['PATH']}", LOG_ROOT=str(root / "logs"))
        self.env.update(dict(zip(["HARNESS_PORT", "BACKEND_PORT", "WEB_PORT"],
                                 map(str, free_ports()))))
        self.processes = []
        self.outputs = []

    def command(self, action="start", **overrides):
        return subprocess.run([str(self.root / "dev.sh"), action],
                              env=dict(self.env, **overrides), capture_output=True,
                              text=True, timeout=10)

    def start(self, action="start", **overrides):
        path = self.root / f"supervisor-{len(self.processes)}.log"
        with path.open("w") as output:
            proc = subprocess.Popen([str(self.root / "dev.sh"), action],
                                    env=dict(self.env, **overrides), stdout=output,
                                    stderr=subprocess.STDOUT)
        self.processes.append(proc)
        self.outputs.append(path)
        eventually(lambda: proc.poll() is not None or "Services are listening" in path.read_text())
        assert proc.poll() is None, path.read_text()
        return proc

    def state(self):
        return json.loads((self.root / ".run/dev/services.json").read_text())

    def close(self):
        self.command("stop")
        for proc in self.processes:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=8)


@pytest.fixture
def session(tmp_path):
    current = Session(tmp_path / "project")
    try:
        yield current
    finally:
        current.close()


@requires_visible_proc
def test_start_status_restart_changed_ports_and_logs(session):
    first = session.start()
    old = session.state()
    assert "running" in session.command("status").stdout
    assert "Already running" in session.command().stdout
    assert session.state()["token"] == old["token"]
    for name in ["harness", "backend"]:
        recorded = json.loads((Path(old["logs"]) / f"{name}.log").read_text().splitlines()[0])
        assert "--reload" in recorded["args"]
        assert recorded["args"].count("--reload-dir") == 2
        assert recorded["backend_port"] == session.env["BACKEND_PORT"]
        assert recorded["harness_url"].endswith(session.env["HARNESS_PORT"])
    overrides = dict(zip(["HARNESS_PORT", "BACKEND_PORT", "WEB_PORT"], map(str, free_ports())))
    second = session.start("restart", LOG_ROOT=str(session.root / "new_logs"), **overrides)
    new = session.state()
    assert new["token"] != old["token"]
    assert "new_logs" in new["logs"]
    assert first.wait(timeout=8) == 0
    assert all(dev.process_info(item["pid"]) is None for item in old["services"])
    assert session.command("stop").returncode == 0
    assert second.wait(timeout=8) == 0
    assert not (session.root / ".run/dev/services.json").exists()
    dev.check_ports({"host": "127.0.0.1", "ports": [item["port"] for item in new["services"]]})


@requires_visible_proc
def test_ctrl_c_stops_npm_descendant(session):
    supervisor = session.start()
    state = session.state()
    child = json.loads((Path(state["logs"]) / "web.log").read_text().splitlines()[0])["child"]
    supervisor.send_signal(signal.SIGINT)
    assert supervisor.wait(timeout=8) == 0
    eventually(lambda: dev.process_info(child) is None)
    assert session.command("status").stdout.strip() == "Local services are stopped."


@requires_visible_proc
def test_restart_recovers_after_supervisor_is_killed(session):
    supervisor = session.start()
    old = session.state()
    supervisor.kill()
    supervisor.wait()
    assert all(dev.process_info(item["pid"]) for item in old["services"])
    session.start("restart", DEV_RELOAD="0")
    assert all(dev.process_info(item["pid"]) is None for item in old["services"])
    recorded = json.loads((Path(session.state()["logs"]) / "harness.log").read_text().splitlines()[0])
    assert "--reload" not in recorded["args"]


def test_unrelated_listener_is_preserved(session):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(session.env["BACKEND_PORT"])))
        listener.listen()
        result = session.command()
        assert result.returncode == 1
        assert "Cannot bind" in result.stderr
        assert listener.getsockname()[1] == int(session.env["BACKEND_PORT"])
        assert not (session.root / ".run/dev/services.json").exists()


@requires_visible_proc
def test_one_service_crash_cleans_remaining_groups(session):
    supervisor = session.start()
    state = session.state()
    os.kill(state["services"][0]["pid"], signal.SIGKILL)
    assert supervisor.wait(timeout=8) == 1
    assert all(dev.process_info(item["pid"]) is None for item in state["services"])
    assert not (session.root / ".run/dev/services.json").exists()


@requires_visible_proc
def test_changed_process_identity_refuses_to_kill(session):
    session.start()
    manager = dev.Services(session.root)
    state = manager.read()
    wrong = json.loads(json.dumps(state))
    wrong["services"][0]["start"] = "0"
    manager.write(wrong)
    try:
        result = session.command("stop")
        assert result.returncode == 1
        assert "identity changed" in result.stderr
        assert all(dev.process_info(item["pid"]) for item in state["services"])
    finally:
        manager.write(state)


@requires_visible_proc
def test_orphan_descendant_and_term_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "STOP_TIMEOUT", 0.2)
    manager = dev.Services(tmp_path)
    child_file = tmp_path / "child.pid"
    script = '''import os, signal, sys, time
pid = os.fork()
if pid:
    open(sys.argv[1], "w").write(str(pid))
    time.sleep(60)
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
'''
    config = {"logs": tmp_path / "logs", "host": "127.0.0.1", "ports": [1, 2, 3],
              "commands": [("fake", 1, [sys.executable, "-c", script, str(child_file)], tmp_path)]}
    state = manager.launch(config)
    try:
        eventually(child_file.exists)
        child = int(child_file.read_text())
        manager.children[0].kill()
        manager.children[0].wait()
        assert dev.process_info(child)
        manager.stop(state)
        eventually(lambda: dev.process_info(child) is None)
    finally:
        manager.stop(state)
        for process in manager.children:
            process.wait(timeout=3)


def test_previous_boot_state_is_ignored_and_cross_project_is_rejected(tmp_path):
    manager = dev.Services(tmp_path)
    state = {"root": str(tmp_path), "boot": "previous boot", "token": "old", "services": []}
    manager.write(state)
    assert manager.read() is None
    state.update(root="/different/project", boot=manager.boot)
    manager.write(state)
    with pytest.raises(RuntimeError, match="wrong project"):
        manager.read()


def test_configuration_reads_dotenv_and_shell_overrides(tmp_path, monkeypatch):
    for key in dev.DEV_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "HOST=0.0.0.0\n"
        "HARNESS_PORT=8101\n"
        "BACKEND_PORT=8100 # backend\n"
        "WEB_PORT='5273'\n"
        "DEV_RELOAD=0\n"
        "LOG_ROOT=var/logs\n"
        "MODEL_API_KEY=must-not-be-loaded\n"
    )
    config = dev.configuration(tmp_path)
    assert config["host"] == "0.0.0.0"
    assert config["ports"] == [8101, 8100, 5273]
    assert config["logs"] == tmp_path / "var/logs/services"
    assert "--reload" not in config["commands"][0][2]

    monkeypatch.setenv("HOST", "127.0.0.2")
    monkeypatch.setenv("BACKEND_PORT", "8200")
    override = dev.configuration(tmp_path)
    assert override["host"] == "127.0.0.2"
    assert override["ports"] == [8101, 8200, 5273]


@requires_visible_proc
def test_concurrent_start_does_not_duplicate_services(session):
    outputs = [session.root / "concurrent-1.log", session.root / "concurrent-2.log"]
    processes = []
    for path in outputs:
        with path.open("w") as output:
            processes.append(subprocess.Popen([str(session.root / "dev.sh")], env=session.env,
                                              stdout=output, stderr=subprocess.STDOUT))
    session.processes.extend(processes)
    eventually(lambda: any(proc.poll() is not None for proc in processes))
    assert sum(proc.poll() is None for proc in processes) == 1
    assert any("Already running" in path.read_text() for path in outputs)
    eventually(lambda: any("Services are listening" in path.read_text() for path in outputs))
    assert len(session.state()["services"]) == 3
