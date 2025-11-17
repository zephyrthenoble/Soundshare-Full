#!/usr/bin/env python3
"""Cross-platform SoundShare server manager.

This script mirrors the capabilities of the original PowerShell helper while
adding first-class support for macOS/Linux. It can start, stop, restart, and
inspect the backend and frontend servers from a single CLI entry point.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import tomllib as toml

import psutil 


@dataclass(frozen=True)
class ServerConfig:
    key: str
    directory: Path
    command: Sequence[str]
    executable: str
    log_file: Path
    pid_file: Path
    ports: Sequence[int]


BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR
CONFIG_PATH = REPO_ROOT / "config.toml"


def load_server_configs() -> dict[str, ServerConfig]:
    if toml is None:
        print(
            "Error: toml parser not available. Use Python 3.11+ or install the 'tomli' package.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not CONFIG_PATH.exists():
        print(f"Error: configuration file not found at {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    with CONFIG_PATH.open("rb") as config_file:
        data = toml.load(config_file)

    servers_section = data.get("servers")
    if not isinstance(servers_section, Mapping):
        print("Error: [servers] section missing or invalid in config.toml", file=sys.stderr)
        sys.exit(1)

    configs: dict[str, ServerConfig] = {}
    for key, raw_entry in servers_section.items():
        if not isinstance(raw_entry, Mapping):
            print(f"Error: server '{key}' entry must be a table", file=sys.stderr)
            sys.exit(1)
        configs[key] = parse_server_entry(key, raw_entry)

    if not configs:
        print("Error: no server definitions found in config.toml", file=sys.stderr)
        sys.exit(1)

    return configs


def parse_server_entry(key: str, entry: Mapping[str, Any]) -> ServerConfig:
    missing = [field for field in ("directory", "command", "executable", "log_file", "pid_file", "ports") if field not in entry]
    if missing:
        print(f"Error: server '{key}' missing required fields: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    directory = resolve_path(entry["directory"])
    command = normalize_command(entry["command"], key)
    executable = str(entry["executable"])
    log_file = resolve_path(entry["log_file"])
    pid_file = resolve_path(entry["pid_file"])
    ports = normalize_ports(entry["ports"], key)

    return ServerConfig(
        key=key,
        directory=directory,
        command=command,
        executable=executable,
        log_file=log_file,
        pid_file=pid_file,
        ports=ports,
    )


def resolve_path(raw_path: Any) -> Path:
    if not isinstance(raw_path, str):
        raise SystemExit(f"Error: path values must be strings (got {type(raw_path).__name__})")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = (REPO_ROOT / candidate).resolve()
    return candidate


def normalize_command(raw_command: Any, key: str) -> Sequence[str]:
    if isinstance(raw_command, str):
        parts = tuple(shlex.split(raw_command))
    elif isinstance(raw_command, Sequence):
        parts = tuple(str(part) for part in raw_command)
    else:
        raise SystemExit("Error: command must be a string or an array of strings in config.toml")

    if not parts:
        raise SystemExit(f"Error: server '{key}' command cannot be empty")
    return parts


def normalize_ports(raw_ports: Any, key: str) -> Sequence[int]:
    if not isinstance(raw_ports, Sequence) or isinstance(raw_ports, (str, bytes)):
        raise SystemExit(f"Error: server '{key}' ports must be an array of integers")
    try:
        return tuple(int(port) for port in raw_ports)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        raise SystemExit(f"Error: server '{key}' ports entries must be integers")


SERVERS: dict[str, ServerConfig] = load_server_configs()
SERVER_ORDER: tuple[str, ...] = tuple(SERVERS.keys())


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    server_choices = SERVER_ORDER
    parser = argparse.ArgumentParser(
        description="Unified backend/frontend server manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              python server_manager.py start backend
              python server_manager.py restart both --background
              python server_manager.py status frontend
            """
        ),
    )
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    parser.add_argument(
        "target",
        choices=server_choices + ("both",),
        nargs="?",
        default="both",
        help="Which server to control (from config.toml) or 'both'",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Run servers in the background (default when managing multiple targets)",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=10,
        help="Number of log lines to print for status (default: 10)",
    )
    return parser.parse_args(argv)


def ensure_command_available(executable: str) -> None:
    if shutil.which(executable):
        return
    print(f"Error: required executable '{executable}' is not on PATH", file=sys.stderr)
    sys.exit(1)


def read_pid(pid_file: Path) -> int | None:
    try:
        pid_text = pid_file.read_text(encoding="utf-8").strip()
        return int(pid_text)
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        return None


def write_pid(pid_file: Path, pid: int) -> None:
    pid_file.write_text(str(pid), encoding="utf-8")


def remove_pid_file(pid_file: Path) -> None:
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False

    return psutil.pid_exists(pid)


def terminate_process_tree(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    children = proc.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(children, timeout=3)
    for survivor in alive:
        try:
            survivor.kill()
        except psutil.Error:
            pass
    try:
        proc.terminate()
    except psutil.Error:
        return False
    try:
        proc.wait(timeout=5)
        return True
    except psutil.TimeoutExpired:
        proc.kill()
        return False


def ensure_directory(config: ServerConfig) -> None:
    if config.directory.is_dir():
        return
    print(f"Error: directory '{config.directory}' not found", file=sys.stderr)
    sys.exit(1)


def prepare_log_file(log_file: Path) -> tuple[Path, bool]:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_file.exists():
            log_file.unlink()
            return log_file, False
    except OSError:
        pass
    return log_file, True


def spawn_process(config: ServerConfig, background: bool, append_logs: bool) -> subprocess.Popen:
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if background:
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        start_new_session = True

    if background:
        mode = "ab" if append_logs else "wb"
        log_handle = open(config.log_file, mode)
        process = subprocess.Popen(
            config.command,
            cwd=config.directory,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        log_handle.close()
        return process

    process = subprocess.Popen(
        config.command,
        cwd=config.directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    return process


def stream_foreground_output(process: subprocess.Popen, log_file: Path) -> int:
    assert process.stdout is not None
    mode = "ab" if log_file.exists() else "wb"
    with open(log_file, mode) as log_handle:
        for line in process.stdout:
            encoded = line.encode("utf-8", errors="replace")
            log_handle.write(encoded)
            print(line, end="")
    return process.wait()


def start_server(config: ServerConfig, background: bool) -> None:
    ensure_directory(config)
    ensure_command_available(config.executable)

    existing_pid = read_pid(config.pid_file)
    if existing_pid and process_is_running(existing_pid):
        print(f"[{config.key}] Already running (PID {existing_pid})")
        return

    log_file, append_logs = prepare_log_file(config.log_file)
    print(f"[{config.key}] Starting {'in background' if background else 'in foreground'}...")
    print(f"[{config.key}] Command: {' '.join(config.command)}")
    print(f"[{config.key}] Logs -> {log_file}")

    process = spawn_process(config, background, append_logs)
    write_pid(config.pid_file, process.pid)

    if background:
        time.sleep(2)
        if process.poll() is None:
            print(f"[{config.key}] Running (PID {process.pid})")
        else:
            print(f"[{config.key}] Warning: process exited immediately. Check logs.")
        return

    exit_code = stream_foreground_output(process, log_file)
    remove_pid_file(config.pid_file)
    if exit_code == 0:
        print(f"[{config.key}] Process exited cleanly")
    else:
        print(f"[{config.key}] Process exited with code {exit_code}")


def stop_server(config: ServerConfig) -> None:
    pid = read_pid(config.pid_file)
    stopped = False
    if pid and process_is_running(pid):
        print(f"[{config.key}] Stopping PID {pid}...")
        stopped = terminate_process_tree(pid)
    if stopped:
        remove_pid_file(config.pid_file)
        print(f"[{config.key}] Stopped successfully")
        return

    if pid and not process_is_running(pid):
        remove_pid_file(config.pid_file)

    # Fallback: scan listening ports to locate stray processes
    candidates = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port in config.ports and conn.pid:
                candidates.add(conn.pid)
    except psutil.Error:
        pass

    if not candidates:
        print(f"[{config.key}] No running process detected on expected ports")
        return

    for candidate in candidates:
        print(f"[{config.key}] Terminating process {candidate} detected on expected port")
        terminate_process_tree(candidate)


def restart_server(config: ServerConfig, background: bool) -> None:
    stop_server(config)
    time.sleep(2)
    start_server(config, background)


def port_is_listening(port: int) -> bool:
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return True
    except psutil.Error:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        result = sock.connect_ex(("127.0.0.1", port))
        return result == 0


def tail_log(log_file: Path, lines: int) -> list[str]:
    if not log_file.exists():
        return []
    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        content = handle.readlines()
    return content[-lines:]


def show_status(config: ServerConfig, tail_lines: int) -> None:
    print(f"=== {config.key.upper()} ===")
    pid = read_pid(config.pid_file)
    if pid and process_is_running(pid):
        print(f"✓ Running (PID {pid})")
        try:
            proc = psutil.Process(pid)
            mem = proc.memory_info().rss / (1024 * 1024)
            cpu = proc.cpu_percent(interval=0.1)
            print(f"  CPU: {cpu:.1f}%  RAM: {mem:.1f} MiB")
        except psutil.Error:
            pass
    else:
        print("✗ Not running")

    listening = [port for port in config.ports if port_is_listening(port)]
    if listening:
        ports = ", ".join(map(str, listening))
        print(f"  Ports in use: {ports}")
    else:
        print(f"  No expected ports ({', '.join(map(str, config.ports))}) are listening")

    logs = tail_log(config.log_file, tail_lines)
    if logs:
        print(f"  Last {min(tail_lines, len(logs))} log lines from {config.log_file.name}:")
        for line in logs:
            print(f"    {line.rstrip()}")
    else:
        print(f"  No log file at {config.log_file}")
    print()


def resolve_targets(target: str) -> Iterable[ServerConfig]:
    if target == "both":
        return tuple(SERVERS[name] for name in SERVER_ORDER)
    if target not in SERVERS:
        available = ", ".join(SERVER_ORDER)
        raise SystemExit(f"Unknown server '{target}'. Available: {available}")
    return (SERVERS[target],)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    configs = list(resolve_targets(args.target))

    background = args.background
    if len(configs) > 1 and args.action in {"start", "restart"} and not background:
        print("Multiple servers requested; automatically enabling background mode so they can start together.")
        background = True

    action = args.action
    if action == "start":
        for config in configs:
            start_server(config, background)
            if len(configs) > 1:
                print()
        if len(configs) > 1 and background:
            print("All selected servers started in background. Use 'python server_manager.py status' to monitor.")
        return 0

    if action == "stop":
        for config in configs:
            stop_server(config)
            if len(configs) > 1:
                print()
        return 0

    if action == "restart":
        for config in configs:
            restart_server(config, background)
            if len(configs) > 1:
                print()
        if len(configs) > 1 and background:
            print("All selected servers restarted in background.")
        return 0

    if action == "status":
        for config in configs:
            show_status(config, tail_lines=args.tail)
        return 0

    print(f"Unknown action: {action}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
