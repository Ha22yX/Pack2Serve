from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from pack2serve.builder import ServerBuilder
from pack2serve.downloader import ArtifactCache, CurseForgeTemplateMirrorProvider, default_curseforge_providers


@dataclass
class RunningServer:
    process: subprocess.Popen[str]
    status: str
    log_path: Path
    started_at: float = field(default_factory=time.time)
    stop_requested: bool = False
    last_lines: list[str] = field(default_factory=list)


class PanelService:
    def __init__(self, workspace_dir: str | Path = "data", advertise_host: str | None = None):
        self.workspace_dir = Path(workspace_dir)
        self.servers_dir = self.workspace_dir / "servers"
        self.cache_dir = self.workspace_dir / "cache"
        self.advertise_host = advertise_host or "127.0.0.1"
        self._running: dict[str, RunningServer] = {}
        self._lock = threading.RLock()

    def import_pack(
        self,
        pack_path: str | Path,
        *,
        target_name: str | None = None,
        download: bool = False,
        curseforge_mirrors: list[str] | None = None,
    ) -> dict[str, object]:
        pack_path = Path(pack_path)
        providers = self._curseforge_providers(curseforge_mirrors or [])
        parsed_name = target_name or pack_path.stem
        target_slug = _slugify(parsed_name)
        target = self.servers_dir / target_slug
        report = ServerBuilder(
            cache_dir=self.cache_dir,
            download_remote=download,
            curseforge_providers=providers,
        ).build(pack_path, target)
        return _summary_from_report(target_slug, report.to_json_dict())

    def list_servers(self) -> list[dict[str, object]]:
        if not self.servers_dir.exists():
            return []
        servers: list[dict[str, object]] = []
        for report_path in sorted(self.servers_dir.glob("**/pack2serve/build-report.json")):
            data = json.loads(report_path.read_text(encoding="utf-8"))
            server_dir = report_path.parents[1]
            target_name = server_dir.relative_to(self.servers_dir).as_posix()
            summary = _summary_from_report(target_name, data)
            summary.update(self.server_runtime_status(target_name))
            servers.append(summary)
        return servers

    def start_server(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        if not (server_dir / "start.ps1").exists() and not (server_dir / "server.jar").exists():
            raise ValueError(f"Generated server is missing a start script: {target_name}")
        with self._lock:
            existing = self._running.get(target_name)
            if existing and existing.process.poll() is None:
                return self.server_runtime_status(target_name)

            logs_dir = server_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / "panel-server.log"
            command = _default_start_command(server_dir)
            process = subprocess.Popen(
                command,
                cwd=server_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            running = RunningServer(process=process, status="starting", log_path=log_path)
            self._running[target_name] = running
            thread = threading.Thread(
                target=self._monitor_process,
                args=(target_name, running),
                daemon=True,
            )
            thread.start()
            return self.server_runtime_status(target_name)

    def stop_server(self, target_name: str) -> dict[str, object]:
        with self._lock:
            running = self._running.get(target_name)
            if not running or running.process.poll() is not None:
                return self.server_runtime_status(target_name)
            running.status = "stopping"
            running.stop_requested = True
            try:
                if running.process.stdin:
                    running.process.stdin.write("stop\n")
                    running.process.stdin.flush()
            except OSError:
                pass
        try:
            running.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_process_tree(running.process)
            running.process.wait(timeout=10)
        with self._lock:
            running.status = "stopped"
        return self.server_runtime_status(target_name)

    def server_runtime_status(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        port = _read_server_port(server_dir)
        with self._lock:
            running = self._running.get(target_name)
            status = "stopped"
            pid = None
            uptime = 0
            last_lines: list[str] = []
            if running:
                code = running.process.poll()
                if code is None:
                    status = running.status
                    pid = running.process.pid
                    uptime = int(time.time() - running.started_at)
                else:
                    status = "stopped" if running.stop_requested or running.status == "running" else running.status
                last_lines = running.last_lines[-8:]
        host = _display_host(self.advertise_host)
        return {
            "runtimeStatus": status,
            "pid": pid,
            "uptimeSeconds": uptime,
            "port": port,
            "host": host,
            "connectAddress": f"{host}:{port}",
            "logTail": last_lines,
        }

    def _curseforge_providers(self, mirrors: list[str]) -> list[object]:
        cache = ArtifactCache(self.cache_dir)
        providers: list[object] = default_curseforge_providers()
        providers.extend(
            CurseForgeTemplateMirrorProvider(
                cache=cache,
                name=f"panel-curseforge-mirror-{index + 1}",
                url_template=mirror,
            )
            for index, mirror in enumerate(mirrors)
        )
        return providers

    def _server_dir(self, target_name: str) -> Path:
        relative = Path(target_name.replace("\\", "/"))
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise ValueError("Invalid server target name.")
        server_dir = (self.servers_dir / relative).resolve()
        root = self.servers_dir.resolve()
        if root not in server_dir.parents and server_dir != root:
            raise ValueError("Invalid server target name.")
        if not server_dir.exists():
            raise ValueError(f"Unknown server: {target_name}")
        return server_dir

    def _monitor_process(self, target_name: str, running: RunningServer) -> None:
        stdout = running.process.stdout
        assert stdout is not None
        try:
            with running.log_path.open("a", encoding="utf-8", errors="replace") as log:
                _write_log_line(log, f"\n--- Pack2Serve panel start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                for line in stdout:
                    _write_log_line(log, line)
                    lowered = line.lower()
                    with self._lock:
                        running.last_lines.append(line.rstrip())
                        running.last_lines = running.last_lines[-80:]
                        if "done (" in lowered and "for help" in lowered:
                            running.status = "running"
                        elif "failed to bind to port" in lowered:
                            running.status = "failed"
                        elif "crash report" in lowered or "failed to start the minecraft server" in lowered:
                            running.status = "crashed"
                code = running.process.wait()
                with self._lock:
                    if running.stop_requested or running.status == "running":
                        running.status = "stopped"
                    elif code == 0 and running.status == "starting":
                        running.status = "exited"
                    elif running.status == "starting":
                        running.status = "failed"
        finally:
            stdout.close()
            if running.process.stdin:
                running.process.stdin.close()


def _summary_from_report(target_name: str, report: dict[str, object]) -> dict[str, object]:
    pack = report["pack"]
    loader = pack["loader"]
    return {
        "targetName": target_name,
        "target": report["target_dir"],
        "format": pack["format"],
        "name": pack["name"],
        "version": pack["version"],
        "minecraftVersion": pack["minecraft_version"],
        "loader": f"{loader['name']} {loader['version']}",
        "remoteFiles": len(report["downloads"]),
        "copiedOverrides": len(report["copied_overrides"]),
        "manualActions": len(report["manual_actions"]),
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._").lower()
    return slug or "server"


def _default_start_command(server_dir: Path) -> list[str]:
    start = server_dir / "start.ps1"
    if start.exists():
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(start.resolve())]
    return ["java", "-jar", "server.jar", "nogui"]


def _read_server_port(server_dir: Path) -> int:
    properties = server_dir / "server.properties"
    if not properties.exists():
        return 25565
    for line in properties.read_text(encoding="utf-8", errors="replace").splitlines():
        clean = line.strip().lstrip("\ufeff")
        if clean.startswith("server-port="):
            try:
                return int(clean.split("=", 1)[1])
            except ValueError:
                return 25565
    return 25565


def _display_host(configured_host: str) -> str:
    if configured_host not in {"0.0.0.0", "::", ""}:
        return configured_host
    try:
        candidates = socket.gethostbyname_ex(socket.gethostname())[2]
        return next((ip for ip in candidates if not ip.startswith("127.")), "127.0.0.1")
    except OSError:
        return "127.0.0.1"


def _write_log_line(log: TextIO, line: str) -> None:
    log.write(line)
    log.flush()


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.poll() is None:
            proc.kill()
        return
    proc.terminate()
