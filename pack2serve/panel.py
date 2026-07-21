from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from pack2serve.builder import ServerBuilder
from pack2serve.downloader import ArtifactCache, CurseForgeTemplateMirrorProvider, default_curseforge_providers
from pack2serve.eula import accept_eula as accept_server_eula
from pack2serve.installer import LoaderInstaller, load_loader_plan
from pack2serve.java import JavaInstaller, load_java_runtime_install_plan
from pack2serve.validator import ServerValidator


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
        if not download:
            raise ValueError("You must enable automatic remote file downloads before creating a runnable server project.")
        target_name = _slugify(project_name or Path(pack_path).stem)
        job = ProjectJob(id=uuid.uuid4().hex, target_name=target_name)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run_create_project,
            args=(job.id, Path(pack_path), target_name, project_name.strip() or Path(pack_path).stem, accept_eula, download, curseforge_mirrors or []),
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
        display_name: str,
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
            _write_project_metadata(target, display_name)
            assigned_port = self._assign_server_port(target)
            self._append_job_log(job_id, f"服务端端口: {assigned_port}")
            auxiliary_ports = self._assign_auxiliary_ports(target)
            for name, port in auxiliary_ports.items():
                self._append_job_log(job_id, f"{name} 端口: {port}")
            if accept_eula:
                accept_server_eula(target)
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
            self._update_job(job_id, stage="validate", progress=88, message="启动服务端并验证 Done 状态")
            validation_result = ServerValidator().validate(target, timeout_seconds=300)
            self._append_job_log(job_id, f"启动验证: {validation_result.status}")
            if validation_result.status != "started":
                raise RuntimeError(
                    "Startup validation failed. Check pack2serve/validation-report.json and logs/pack2serve-validation.log."
                )
            self._update_job(job_id, stage="finalize", progress=96, message="生成项目摘要")
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

    def delete_project(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        with self._lock:
            running = self._running.get(target_name)
            is_running = bool(running and running.process.poll() is None)
        if is_running:
            self.stop_server(target_name)
        root = self.servers_dir.resolve()
        resolved = server_dir.resolve()
        if root not in resolved.parents:
            raise ValueError("Invalid server target name.")
        terminated_processes = _terminate_external_processes_for_path(resolved)
        _remove_tree_with_retries(resolved)
        with self._lock:
            self._running.pop(target_name, None)
        return {
            "targetName": target_name,
            "target": str(server_dir),
            "status": "deleted",
            "terminatedProcesses": terminated_processes,
        }

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

    def key_server_settings(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        properties = _read_properties(server_dir / "server.properties")
        return {
            "targetName": target_name,
            "settings": {
                key: {
                    **definition,
                    "value": properties.get(key, definition["default"]),
                }
                for key, definition in _KEY_SERVER_SETTINGS.items()
            },
        }

    def save_key_server_settings(self, target_name: str, settings: dict[str, object]) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        path = server_dir / "server.properties"
        current = _read_properties(path)
        for key, value in settings.items():
            if key not in _KEY_SERVER_SETTINGS:
                raise ValueError(f"Unsupported key server setting: {key}")
            current[key] = _normalize_server_setting(key, value)
        _write_properties(path, current)
        return self.key_server_settings(target_name)

    def server_players(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        log_path = server_dir / "logs" / "panel-server.log"
        players = _players_from_log(log_path)
        return {
            "targetName": target_name,
            "players": list(players.values()),
            "capabilities": {
                "gameMode": "log-derived",
                "position": "log-derived-after-probe",
                "rotation": "log-derived-after-probe",
                "inventory": "requires-management-probe",
                "note": "玩家状态先从控制台日志与探测命令推断；完整背包与模组命令树需要服务端管理探针或 RCON 扩展。",
            },
        }

    def server_metrics(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        properties = _read_properties(server_dir / "server.properties")
        world_name = properties.get("level-name", "world")
        world_dir = server_dir / world_name
        runtime = self.server_runtime_status(target_name)
        game_time = _latest_world_time(server_dir / "logs" / "panel-server.log")
        return {
            "targetName": target_name,
            "runtime": runtime,
            "world": {
                "name": world_name,
                "path": str(world_dir),
                "sizeBytes": _directory_size(world_dir),
                "gameTime": game_time,
                "days": game_time // 24000 if game_time is not None else None,
            },
            "resources": {
                "projectSizeBytes": _directory_size(server_dir),
                "memoryBytes": _process_memory_bytes(runtime.get("pid")),
            },
        }

    def player_action(self, target_name: str, action: str, **kwargs: object) -> dict[str, object]:
        action = action.strip().lower()
        player = _safe_player_name(str(kwargs.get("player", "")))
        if action == "op":
            commands = [f"op {player}"]
        elif action == "deop":
            commands = [f"deop {player}"]
        elif action == "gamemode":
            commands = [f"gamemode {_safe_game_mode(str(kwargs.get('gameMode', '')))} {player}"]
        elif action == "tp":
            commands = [
                f"tp {player} {_safe_coordinate(kwargs.get('x'))} {_safe_coordinate(kwargs.get('y'))} {_safe_coordinate(kwargs.get('z'))}"
            ]
        elif action == "ban":
            reason = _safe_command_tail(str(kwargs.get("reason", ""))).strip() or "Banned by Pack2Serve"
            commands = [f"ban {player} {reason}"]
        elif action == "kill":
            commands = [f"kill {player}"]
        elif action == "clear":
            commands = [f"clear {player}"]
        elif action == "probe":
            commands = [f"data get entity {player} Pos", f"data get entity {player} Rotation"]
        else:
            raise ValueError(f"Unsupported player action: {action}")
        for command in commands:
            self.send_console_command(target_name, command)
        return {"targetName": target_name, "action": action, "player": player, "commands": commands}

    def server_mods(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        mods: list[dict[str, object]] = []
        for directory, enabled in ((server_dir / "mods", True), (server_dir / "disabled-mods", False)):
            if directory.exists():
                mods.extend(_read_mod_entry(path, enabled=enabled) for path in sorted(directory.glob("*.jar")))
        return {
            "targetName": target_name,
            "mods": mods,
            "counts": {
                "enabled": sum(1 for mod in mods if mod["enabled"]),
                "disabled": sum(1 for mod in mods if not mod["enabled"]),
            },
        }

    def add_mod(self, target_name: str, filename: str, content: bytes) -> dict[str, object]:
        if not content:
            raise ValueError("Uploaded mod file is empty.")
        safe_name = _safe_mod_filename(filename)
        server_dir = self._server_dir(target_name)
        target = server_dir / "mods" / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {"targetName": target_name, "status": "added", "mod": _read_mod_entry(target, enabled=True)}

    def disable_mod(self, target_name: str, file_name: str) -> dict[str, object]:
        safe_name = _safe_mod_filename(file_name)
        server_dir = self._server_dir(target_name)
        source = server_dir / "mods" / safe_name
        target = server_dir / "disabled-mods" / safe_name
        if not source.exists():
            if target.exists():
                return {"targetName": target_name, "fileName": safe_name, "status": "disabled"}
            raise ValueError(f"Unknown mod file: {safe_name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return {"targetName": target_name, "fileName": safe_name, "status": "disabled"}

    def delete_mod(self, target_name: str, file_name: str) -> dict[str, object]:
        safe_name = _safe_mod_filename(file_name)
        server_dir = self._server_dir(target_name)
        for directory in (server_dir / "mods", server_dir / "disabled-mods"):
            path = directory / safe_name
            if path.exists():
                path.unlink()
                return {"targetName": target_name, "fileName": safe_name, "status": "deleted"}
        raise ValueError(f"Unknown mod file: {safe_name}")

    def command_suggestions(self, target_name: str, prefix: str = "") -> dict[str, object]:
        players = [str(player["name"]) for player in self.server_players(target_name)["players"]]
        candidates = _command_suggestion_candidates(players)
        clean_prefix = prefix.strip().lower()
        suggestions = [item for item in candidates if item.lower().startswith(clean_prefix)] if clean_prefix else candidates
        return {
            "targetName": target_name,
            "prefix": prefix,
            "suggestions": suggestions[:60],
            "capabilities": {
                "vanilla": True,
                "moddedCommandTree": "requires-rcon-or-management-probe",
            },
        }

    def server_worlds(self, target_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        properties = _read_properties(server_dir / "server.properties")
        current_world = properties.get("level-name", "world")
        worlds = [_world_entry(path, current_world=current_world) for path in _world_directories(server_dir)]
        return {
            "targetName": target_name,
            "currentWorld": current_world,
            "worlds": worlds,
            "note": "切换当前世界会写入 server.properties 的 level-name，重启服务器后生效。",
        }

    def create_world(self, target_name: str, world_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        clean_name = _safe_world_name(world_name)
        world_dir = server_dir / clean_name
        if world_dir.exists():
            raise ValueError(f"World already exists: {clean_name}")
        world_dir.mkdir(parents=True)
        return {
            "targetName": target_name,
            "status": "created",
            "world": _world_entry(world_dir, current_world=_read_properties(server_dir / "server.properties").get("level-name", "world")),
        }

    def select_world(self, target_name: str, world_name: str) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        clean_name = _safe_world_name(world_name)
        world_dir = server_dir / clean_name
        if not world_dir.exists() or not world_dir.is_dir():
            raise ValueError(f"Unknown world: {clean_name}")
        properties_path = server_dir / "server.properties"
        properties = _read_properties(properties_path)
        properties["level-name"] = clean_name
        _write_properties(properties_path, properties)
        return {
            "targetName": target_name,
            "status": "selected",
            "currentWorld": clean_name,
            "requiresRestart": True,
            "note": "重启服务器后会加载所选世界。",
        }

    def backup_world(self, target_name: str, world_name: str | None = None) -> dict[str, object]:
        server_dir = self._server_dir(target_name)
        properties = _read_properties(server_dir / "server.properties")
        clean_name = _safe_world_name(world_name or properties.get("level-name", "world"))
        world_dir = server_dir / clean_name
        if not world_dir.exists() or not world_dir.is_dir():
            raise ValueError(f"Unknown world: {clean_name}")
        backups_dir = server_dir / "backups" / "worlds"
        backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = backups_dir / f"{_backup_file_stem(clean_name)}-{timestamp}.zip"
        _zip_directory(world_dir, backup_path, root_name=clean_name)
        return {
            "targetName": target_name,
            "status": "backed-up",
            "worldName": clean_name,
            "backupPath": str(backup_path),
            "sizeBytes": backup_path.stat().st_size,
        }

    def _legacy_server_players(self, target_name: str) -> dict[str, object]:
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
        if not target_name.strip():
            raise ValueError("Invalid server target name.")
        relative = Path(target_name.replace("\\", "/"))
        if relative == Path(".") or relative.is_absolute() or any(part == ".." for part in relative.parts):
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


_KEY_SERVER_SETTINGS: dict[str, dict[str, object]] = {
    "server-port": {
        "label": "服务器端口",
        "type": "number",
        "default": "25565",
        "min": 1,
        "max": 65535,
        "description": "玩家连接服务器使用的端口。",
    },
    "online-mode": {
        "label": "正版验证",
        "type": "boolean",
        "default": "true",
        "description": "开启后服务器会验证 Mojang/Microsoft 正版登录。",
    },
    "max-players": {
        "label": "最大玩家数",
        "type": "number",
        "default": "20",
        "min": 1,
        "max": 100000,
        "description": "同时在线玩家上限。",
    },
    "difficulty": {
        "label": "游戏难度",
        "type": "select",
        "default": "easy",
        "options": ["peaceful", "easy", "normal", "hard"],
        "description": "世界默认难度。",
    },
    "gamemode": {
        "label": "默认游戏模式",
        "type": "select",
        "default": "survival",
        "options": ["survival", "creative", "adventure", "spectator"],
        "description": "新玩家默认游戏模式。",
    },
    "motd": {
        "label": "服务器描述",
        "type": "text",
        "default": "A Minecraft Server",
        "description": "服务器列表中显示的 MOTD。",
    },
    "level-name": {
        "label": "世界文件夹",
        "type": "text",
        "default": "world",
        "description": "服务器加载的世界目录名称。",
    },
}


def _summary_from_report(target_name: str, report: dict[str, object]) -> dict[str, object]:
    pack = report["pack"]
    loader = pack["loader"]
    server_dir = Path(report["target_dir"])
    compatibility = _read_compatibility_summary(server_dir)
    project = _read_project_metadata(server_dir)
    return {
        "targetName": target_name,
        "target": report["target_dir"],
        "format": pack["format"],
        "name": project.get("displayName") or pack["name"],
        "packName": pack["name"],
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


def _write_project_metadata(server_dir: Path, display_name: str) -> None:
    metadata_path = server_dir / "pack2serve" / "project.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "displayName": display_name.strip() or server_dir.name,
                "targetName": server_dir.name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_project_metadata(server_dir: Path) -> dict[str, str]:
    metadata_path = server_dir / "pack2serve" / "project.json"
    if not metadata_path.exists():
        return {}
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    display_name = str(data.get("displayName", "")).strip()
    return {"displayName": display_name} if display_name else {}


def _normalize_server_setting(key: str, value: object) -> str:
    definition = _KEY_SERVER_SETTINGS[key]
    kind = definition["type"]
    if kind == "boolean":
        return "true" if str(value).strip().lower() in {"1", "true", "yes", "on"} or value is True else "false"
    if kind == "number":
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be a number.") from exc
        minimum = int(definition.get("min", number))
        maximum = int(definition.get("max", number))
        if number < minimum or number > maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}.")
        return str(number)
    if kind == "select":
        clean = str(value).strip().lower()
        options = set(str(option) for option in definition.get("options", []))
        if clean not in options:
            raise ValueError(f"{key} must be one of: {', '.join(sorted(options))}.")
        return clean
    return str(value).replace("\r", "").replace("\n", " ").strip()


def _players_from_log(log_path: Path) -> dict[str, dict[str, object]]:
    players: dict[str, dict[str, object]] = {}
    if not log_path.exists():
        return players
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        joined = re.search(r": ([A-Za-z0-9_]{1,16}) joined the game", line)
        left = re.search(r": ([A-Za-z0-9_]{1,16}) left the game", line)
        mode = re.search(r"Set ([A-Za-z0-9_]{1,16})'s game mode to ([A-Za-z ]+)", line)
        entity = re.search(r": ([A-Za-z0-9_]{1,16}) has the following entity data: \[(.+)\]", line)
        if joined:
            name = joined.group(1)
            players[name] = {
                "name": name,
                "status": "online",
                "gameMode": players.get(name, {}).get("gameMode", "unknown"),
                "position": players.get(name, {}).get("position"),
                "rotation": players.get(name, {}).get("rotation"),
                "respawnPoint": players.get(name, {}).get("respawnPoint"),
                "skinUrl": f"https://minotar.net/avatar/{name}/64",
                "inventory": [],
                "source": "log",
            }
        if mode and mode.group(1) in players:
            players[mode.group(1)]["gameMode"] = _normalize_game_mode(mode.group(2))
        if entity and entity.group(1) in players:
            vector = _parse_minecraft_vector(entity.group(2))
            if len(vector) == 3:
                players[entity.group(1)]["position"] = {"x": vector[0], "y": vector[1], "z": vector[2]}
            elif len(vector) == 2:
                players[entity.group(1)]["rotation"] = {"yaw": vector[0], "pitch": vector[1]}
        if left:
            players.pop(left.group(1), None)
    return players


def _normalize_game_mode(value: str) -> str:
    clean = value.strip().lower().replace(" mode", "").replace(" ", "-")
    for mode in ("survival", "creative", "adventure", "spectator"):
        if mode in clean:
            return mode
    return clean or "unknown"


def _parse_minecraft_vector(value: str) -> list[float]:
    parts = []
    for raw in value.split(","):
        number = re.sub(r"[A-Za-z]$", "", raw.strip())
        try:
            parts.append(float(number))
        except ValueError:
            return []
    return parts


def _latest_world_time(log_path: Path) -> int | None:
    if not log_path.exists():
        return None
    game_time: int | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.search(r"The time is (\d+)", line)
        if match:
            game_time = int(match.group(1))
    return game_time


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _process_memory_bytes(pid: object) -> int | None:
    if not pid:
        return None
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0 or "No tasks" in result.stdout:
                return None
            columns = next(iter(result.stdout.splitlines()), "").split('","')
            if len(columns) < 5:
                return None
            raw = columns[4].strip('"').replace(",", "").replace("K", "").strip()
            return int(raw) * 1024 if raw.isdigit() else None
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        raw = result.stdout.strip()
        return int(raw) * 1024 if raw.isdigit() else None
    except (OSError, ValueError):
        return None


def _safe_player_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", value.strip()):
        raise ValueError(f"Invalid player name: {value}")
    return value.strip()


def _safe_game_mode(value: str) -> str:
    clean = value.strip().lower()
    if clean not in {"survival", "creative", "adventure", "spectator"}:
        raise ValueError(f"Invalid game mode: {value}")
    return clean


def _safe_coordinate(value: object) -> str:
    raw = str(value).strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        raise ValueError(f"Invalid coordinate: {value}")
    return raw


def _safe_command_tail(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()[:160]


def _safe_mod_filename(filename: str) -> str:
    safe_name = Path(filename.replace("\\", "/")).name.strip()
    if not safe_name.lower().endswith(".jar"):
        raise ValueError("Mod file must be a .jar file.")
    if not re.fullmatch(r"[A-Za-z0-9._+() -]+\.jar", safe_name):
        raise ValueError(f"Invalid mod file name: {filename}")
    return safe_name


_NON_WORLD_DIRS = {
    ".fabric",
    ".mixin.out",
    "backups",
    "config",
    "crash-reports",
    "defaultconfigs",
    "disabled-mods",
    "libraries",
    "logs",
    "mods",
    "pack2serve",
    "runtime",
    "versions",
}


def _safe_world_name(value: str) -> str:
    clean = value.strip()
    if not clean or clean in {".", ".."}:
        raise ValueError("World name cannot be empty.")
    if any(char in clean for char in '<>:"/\\|?*') or any(ord(char) < 32 for char in clean):
        raise ValueError(f"Invalid world name: {value}")
    if clean.endswith(".") or clean.endswith(" "):
        raise ValueError(f"Invalid world name: {value}")
    return clean


def _world_directories(server_dir: Path) -> list[Path]:
    if not server_dir.exists():
        return []
    worlds: list[Path] = []
    for path in server_dir.iterdir():
        if not path.is_dir():
            continue
        if path.name.lower() in _NON_WORLD_DIRS:
            continue
        if (path / "level.dat").exists() or not any(path.iterdir()):
            worlds.append(path)
    return sorted(worlds, key=lambda item: item.name.lower())


def _world_entry(path: Path, *, current_world: str) -> dict[str, object]:
    return {
        "name": path.name,
        "path": str(path),
        "current": path.name == current_world,
        "exists": path.exists(),
        "sizeBytes": _directory_size(path),
        "hasLevelDat": (path / "level.dat").exists(),
        "lastModified": int(path.stat().st_mtime) if path.exists() else None,
    }


def _backup_file_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "world"


def _zip_directory(source: Path, destination: Path, *, root_name: str) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{root_name}/", b"")
        for path in sorted(source.rglob("*")):
            archive.write(path, Path(root_name) / path.relative_to(source))


def _read_mod_entry(path: Path, *, enabled: bool) -> dict[str, object]:
    metadata: dict[str, object] = {}
    icon_data_url: str | None = None
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names:
                metadata = json.loads(jar.read("fabric.mod.json").decode("utf-8", errors="replace"))
                icon_path = str(metadata.get("icon", "")).strip()
                if icon_path in names:
                    icon_data_url = _jar_icon_data_url(jar, icon_path)
            elif "mcmod.info" in names:
                parsed = json.loads(jar.read("mcmod.info").decode("utf-8", errors="replace"))
                if isinstance(parsed, list) and parsed:
                    metadata = parsed[0]
            elif "META-INF/mods.toml" in names:
                metadata = _parse_mods_toml(jar.read("META-INF/mods.toml").decode("utf-8", errors="replace"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
        metadata = {}
    title = str(metadata.get("name") or metadata.get("displayName") or path.stem)
    mod_id = str(metadata.get("id") or metadata.get("modid") or path.stem)
    return {
        "fileName": path.name,
        "title": title,
        "id": mod_id,
        "version": str(metadata.get("version", "")),
        "enabled": enabled,
        "status": "enabled" if enabled else "disabled",
        "sizeBytes": path.stat().st_size,
        "iconDataUrl": icon_data_url,
    }


def _jar_icon_data_url(jar: zipfile.ZipFile, icon_path: str) -> str | None:
    try:
        data = jar.read(icon_path)
    except KeyError:
        return None
    if len(data) > 256 * 1024:
        return None
    suffix = Path(icon_path).suffix.lower()
    media_type = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _parse_mods_toml(content: str) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key, target in (("modId", "id"), ("displayName", "name"), ("version", "version")):
        match = re.search(rf"^\s*{key}\s*=\s*\"([^\"]+)\"", content, flags=re.MULTILINE)
        if match:
            metadata[target] = match.group(1)
    return metadata


def _command_suggestion_candidates(players: list[str]) -> list[str]:
    player_targets = players or ["<player>"]
    commands = [
        "help",
        "list",
        "time query daytime",
        "time query gametime",
        "say ",
        "save-all",
        "stop",
    ]
    for player in player_targets:
        commands.extend(
            [
                f"gamemode survival {player}",
                f"gamemode creative {player}",
                f"gamemode adventure {player}",
                f"gamemode spectator {player}",
                f"op {player}",
                f"deop {player}",
                f"tp {player} ",
                f"kill {player}",
                f"clear {player}",
                f"ban {player} ",
                f"data get entity {player} Pos",
                f"data get entity {player} Rotation",
            ]
        )
    return commands


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


def _remove_tree_with_retries(path: Path, *, attempts: int = 6, delay_seconds: float = 0.5) -> None:
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    raise ValueError(f"Project directory is still locked by another process: {path}. Last error: {last_error}") from last_error


def _terminate_external_processes_for_path(project_dir: Path) -> list[int]:
    if os.name != "nt":
        return []
    target = str(project_dir)
    script = (
        f"$target = {json.dumps(target)}; "
        "$own = $PID; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.ProcessId -ne $own -and "
        "$_.CommandLine.IndexOf($target, [StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    process_ids: list[int] = []
    for line in result.stdout.splitlines():
        raw = line.strip()
        if raw.isdigit():
            pid = int(raw)
            if pid != os.getpid() and pid not in process_ids:
                process_ids.append(pid)
    for pid in process_ids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return process_ids


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
