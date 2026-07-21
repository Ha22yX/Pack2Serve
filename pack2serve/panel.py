from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from pack2serve.builder import ServerBuilder
from pack2serve.downloader import ArtifactCache, CurseForgeTemplateMirrorProvider, default_curseforge_providers
from pack2serve.eula import accept_eula as accept_server_eula
from pack2serve.installer import LoaderInstaller, load_loader_plan
from pack2serve.java import JavaInstaller, load_java_runtime_install_plan


@dataclass
class RunningServer:
    process: subprocess.Popen[str]
    status: str
    log_path: Path
    started_at: float = field(default_factory=time.time)
    stop_requested: bool = False
    last_lines: list[str] = field(default_factory=list)


@dataclass
class ProjectJob:
    id: str
    target_name: str
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    message: str = "等待构建任务开始"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    log_lines: list[str] = field(default_factory=list)
    server: dict[str, object] | None = None
    error: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "jobId": self.id,
            "targetName": self.target_name,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "startedAt": int(self.started_at),
            "finishedAt": int(self.finished_at) if self.finished_at else None,
            "logLines": self.log_lines[-120:],
            "server": self.server,
            "error": self.error,
        }


class PanelService:
    def __init__(self, workspace_dir: str | Path = "data", advertise_host: str | None = None):
        self.workspace_dir = Path(workspace_dir)
        self.servers_dir = self.workspace_dir / "servers"
        self.cache_dir = self.workspace_dir / "cache"
        self.advertise_host = advertise_host or "127.0.0.1"
        self._running: dict[str, RunningServer] = {}
        self._jobs: dict[str, ProjectJob] = {}
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

    def create_project(
        self,
        pack_path: str | Path,
        *,
        project_name: str,
        accept_eula: bool,
        download: bool = True,
        curseforge_mirrors: list[str] | None = None,
    ) -> dict[str, object]:
        if not accept_eula:
            raise ValueError("You must accept the Minecraft EULA before creating a runnable server project.")
        target_name = _slugify(project_name or Path(pack_path).stem)
        job = ProjectJob(id=uuid.uuid4().hex, target_name=target_name)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run_create_project,
            args=(job.id, Path(pack_path), target_name, accept_eula, download, curseforge_mirrors or []),
            daemon=True,
        )
        thread.start()
        return job.to_json_dict()

    def project_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError(f"Unknown project job: {job_id}")
            return job.to_json_dict()

    def _run_create_project(
        self,
        job_id: str,
        pack_path: Path,
        target_name: str,
        accept_eula: bool,
        download: bool,
        mirrors: list[str],
    ) -> None:
        target = self.servers_dir / target_name
        try:
            self._update_job(job_id, status="running", stage="inspect", progress=8, message="读取整合包元数据")
            providers = self._curseforge_providers(mirrors)
            self._append_job_log(job_id, f"项目目录: {target}")
            self._append_job_log(job_id, f"整合包: {pack_path}")
            self._update_job(job_id, stage="build", progress=22, message="解析整合包并复制 overrides")
            report = ServerBuilder(
                cache_dir=self.cache_dir,
                download_remote=download,
                curseforge_providers=providers,
            ).build(pack_path, target)
            assigned_port = self._assign_server_port(target)
            self._append_job_log(job_id, f"服务端端口: {assigned_port}")
            auxiliary_ports = self._assign_auxiliary_ports(target)
            for name, port in auxiliary_ports.items():
                self._append_job_log(job_id, f"{name} 端口: {port}")
            self._append_job_log(job_id, f"远程文件: {len(report.downloads)}")
            self._append_job_log(job_id, f"人工项: {len(report.manual_actions)}")
            self._update_job(job_id, stage="java", progress=56, message="安装匹配的 Java 运行时")
            java_plan = load_java_runtime_install_plan(target / "pack2serve" / "java-runtime-install-plan.json")
            java_result = JavaInstaller().install(target, java_plan)
            self._append_job_log(job_id, f"Java 安装: {java_result.status}")
            self._update_job(job_id, stage="loader", progress=72, message="安装服务端启动文件")
            loader_plan = load_loader_plan(target / "pack2serve" / "loader-install-plan.json")
            loader_result = LoaderInstaller().install(target, loader_plan, execute_installers=True)
            self._append_job_log(job_id, f"Loader 安装: {loader_result.status}")
            if loader_result.status == "failed":
                raise RuntimeError("Loader installation failed. Check pack2serve/loader-install-result.json.")
            self._update_job(job_id, stage="eula", progress=82, message="写入 EULA 接受状态")
            if accept_eula:
                accept_server_eula(target)
            self._update_job(job_id, stage="finalize", progress=94, message="生成项目摘要")
            summary = _summary_from_report(target_name, report.to_json_dict())
            summary.update(self.server_runtime_status(target_name))
            with self._lock:
                job = self._jobs[job_id]
                job.status = "completed"
                job.stage = "complete"
                job.progress = 100
                job.message = "项目创建完成"
                job.finished_at = time.time()
                job.server = summary
                job.log_lines.append("项目创建完成")
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.stage = "failed"
                job.progress = max(job.progress, 1)
                job.message = str(exc)
                job.error = str(exc)
                job.finished_at = time.time()
                job.log_lines.append(f"失败: {exc}")

    def list_servers(self, *, include_internal: bool = False) -> list[dict[str, object]]:
        if not self.servers_dir.exists():
            return []
        servers: list[dict[str, object]] = []
        for report_path in sorted(self.servers_dir.glob("**/pack2serve/build-report.json")):
            data = json.loads(report_path.read_text(encoding="utf-8"))
            server_dir = report_path.parents[1]
            target_name = server_dir.relative_to(self.servers_dir).as_posix()
            internal_project = _is_internal_project(target_name)
            if internal_project and not include_internal:
                continue
            summary = _summary_from_report(target_name, data)
            summary["internalProject"] = internal_project
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

    def server_log_tail(self, target_name: str, max_lines: int = 200) -> dict[str, object]:
        max_lines = max(1, min(max_lines, 1000))
        server_dir = self._server_dir(target_name)
        log_path = server_dir / "logs" / "panel-server.log"
        lines: list[str] = []
        if log_path.exists():
            lines = _tail_text_file(log_path, max_lines)
        with self._lock:
            running = self._running.get(target_name)
            if running and running.last_lines and not lines:
                lines = running.last_lines[-max_lines:]
        status = self.server_runtime_status(target_name)
        return {
            "targetName": target_name,
            "connectAddress": status["connectAddress"],
            "runtimeStatus": status["runtimeStatus"],
            "pid": status["pid"],
            "lines": lines,
        }

    def send_console_command(self, target_name: str, command: str) -> dict[str, object]:
        clean = command.strip()
        if not clean:
            raise ValueError("Console command cannot be empty.")
        with self._lock:
            running = self._running.get(target_name)
            if not running or running.process.poll() is not None or not running.process.stdin:
                raise ValueError(f"Server is not running: {target_name}")
            running.process.stdin.write(clean + "\n")
            running.process.stdin.flush()
            running.last_lines.append(f"> {clean}")
        return {
            "targetName": target_name,
            "command": clean,
            "runtimeStatus": self.server_runtime_status(target_name)["runtimeStatus"],
        }

    def server_properties(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        path = server_dir / "server.properties"
        return {
            "targetName": target_name,
            "path": str(path),
            "properties": _read_properties(path),
            "raw": path.read_text(encoding="utf-8", errors="replace") if path.exists() else "",
        }

    def save_server_properties(self, target_name: str, properties: dict[str, object]) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        path = server_dir / "server.properties"
        current = _read_properties(path)
        for key, value in properties.items():
            clean_key = str(key).strip()
            if not clean_key or "\n" in clean_key or "=" in clean_key:
                raise ValueError(f"Invalid server.properties key: {key}")
            current[clean_key] = str(value).replace("\r", "").replace("\n", " ")
        _write_properties(path, current)
        return self.server_properties(target_name)

    def server_players(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        log_path = server_dir / "logs" / "panel-server.log"
        players: dict[str, dict[str, object]] = {}
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                joined = re.search(r": ([A-Za-z0-9_]{1,16}) joined the game", line)
                left = re.search(r": ([A-Za-z0-9_]{1,16}) left the game", line)
                mode = re.search(r"Set ([A-Za-z0-9_]{1,16})'s game mode to ([A-Za-z ]+)", line)
                if joined:
                    players[joined.group(1)] = {
                        "name": joined.group(1),
                        "status": "online",
                        "gameMode": "unknown",
                        "source": "log",
                    }
                if mode and mode.group(1) in players:
                    players[mode.group(1)]["gameMode"] = mode.group(2).strip().lower().replace(" ", "-")
                if left:
                    players.pop(left.group(1), None)
        return {
            "targetName": target_name,
            "players": list(players.values()),
            "capabilities": {
                "gameMode": "log-derived",
                "note": "实时玩家模式需要 RCON 或服务端插件；当前版本只能从日志中推断。",
            },
        }

    def _assign_server_port(self, target: Path) -> int:
        used_ports = {
            _read_server_port(path.parent)
            for path in self.servers_dir.glob("**/server.properties")
            if path.parent.resolve() != target.resolve()
        }
        port = _next_available_port(25565, 25665, used_ports, udp=False)
        properties = _read_properties(target / "server.properties")
        properties["server-port"] = str(port)
        if "query.port" in properties:
            properties["query.port"] = str(port)
        _write_properties(target / "server.properties", properties)
        return port

    def _assign_auxiliary_ports(self, target: Path) -> dict[str, int]:
        assigned: dict[str, int] = {}
        voicechat = target / "config" / "voicechat" / "voicechat-server.properties"
        if voicechat.exists():
            used_ports = {
                int(properties["port"])
                for properties_path in self.servers_dir.glob("**/config/voicechat/voicechat-server.properties")
                if properties_path.parent.parent.parent.resolve() != target.resolve()
                for properties in [_read_properties(properties_path)]
                if properties.get("port", "").isdigit()
            }
            port = _next_available_port(24454, 24554, used_ports, udp=True)
            properties = _read_properties(voicechat)
            properties["port"] = str(port)
            _write_properties(voicechat, properties)
            assigned["voicechat"] = port
        return assigned

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

    def _update_job(
        self,
        job_id: str,
        *,
        stage: str,
        progress: int,
        message: str,
        status: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if status is not None:
                job.status = status
            job.stage = stage
            job.progress = progress
            job.message = message
            job.log_lines.append(message)

    def _append_job_log(self, job_id: str, line: str) -> None:
        with self._lock:
            self._jobs[job_id].log_lines.append(line)

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
    compatibility = _read_compatibility_summary(Path(report["target_dir"]))
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
        "compatibilityLevel": compatibility["level"],
        "serverEquivalent": compatibility["serverEquivalent"],
        "compatibilitySummary": compatibility["summary"],
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._").lower()
    return slug or "server"


def _is_internal_project(target_name: str) -> bool:
    first = target_name.split("/", 1)[0]
    return (
        first in {"full-verification", "integration", "verification"}
        or first.startswith("startup-verification-")
        or target_name == "panel-check"
    )


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


def _next_available_port(start: int, stop: int, used_ports: set[int], *, udp: bool) -> int:
    for port in range(start, stop):
        if port in used_ports:
            continue
        if _is_port_available(port, udp=udp):
            return port
    raise RuntimeError(f"No available port found in range {start}-{stop - 1}.")


def _is_port_available(port: int, *, udp: bool = False) -> bool:
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("", port))
        except OSError:
            return False
    return True


def _write_log_line(log: TextIO, line: str) -> None:
    log.write(line)
    log.flush()


def _tail_text_file(path: Path, max_lines: int) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]


def _read_properties(path: Path) -> dict[str, str]:
    properties: dict[str, str] = {}
    if not path.exists():
        return properties
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        clean = line.strip().lstrip("\ufeff")
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        properties[key.strip()] = value.strip()
    return properties


def _write_properties(path: Path, properties: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in sorted(properties.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_compatibility_summary(server_dir: Path) -> dict[str, object]:
    path = server_dir / "pack2serve" / "compatibility-report.json"
    if not path.exists():
        return {
            "level": "unknown",
            "serverEquivalent": False,
            "summary": {},
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "level": data.get("level", "unknown"),
        "serverEquivalent": bool(data.get("serverEquivalent", False)),
        "summary": data.get("summary", {}),
    }


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
