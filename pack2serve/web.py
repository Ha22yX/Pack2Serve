from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pack2serve.panel import PanelService


MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024


def serve(host: str = "127.0.0.1", port: int = 8765, workspace_dir: str | Path = "data") -> None:
    service = PanelService(workspace_dir=workspace_dir, advertise_host=host)

    class Pack2ServeHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            try:
                if route == "/":
                    self._send_html(PANEL_HTML)
                    return
                if route == "/api/servers":
                    include_internal = query.get("includeInternal", ["false"])[0].lower() == "true"
                    self._send_json({"servers": service.list_servers(include_internal=include_internal)})
                    return
                if route == "/api/projects/jobs":
                    job_id = query.get("jobId", [""])[0]
                    if job_id:
                        self._send_json({"job": service.project_job(job_id)})
                    else:
                        self._send_json({"jobs": service.project_jobs()})
                    return
                if route == "/api/servers/logs":
                    target_name = query.get("targetName", [""])[0]
                    max_lines = int(query.get("maxLines", ["300"])[0])
                    self._send_json({"log": service.server_log_tail(target_name, max_lines=max_lines)})
                    return
                if route == "/api/servers/properties":
                    self._send_json({"serverProperties": service.server_properties(query.get("targetName", [""])[0])})
                    return
                if route == "/api/servers/key-settings":
                    self._send_json({"keySettings": service.key_server_settings(query.get("targetName", [""])[0])})
                    return
                if route == "/api/servers/players":
                    self._send_json({"players": service.server_players(query.get("targetName", [""])[0])})
                    return
                if route == "/api/servers/player-inventory":
                    self._send_json(
                        {
                            "inventory": service.player_inventory(
                                query.get("targetName", [""])[0],
                                query.get("player", [""])[0],
                                source=query.get("source", ["online"])[0],
                            )
                        }
                    )
                    return
                if route == "/api/servers/metrics":
                    self._send_json({"metrics": service.server_metrics(query.get("targetName", [""])[0])})
                    return
                if route == "/api/servers/mods":
                    self._send_json({"mods": service.server_mods(query.get("targetName", [""])[0])})
                    return
                if route == "/api/servers/worlds":
                    self._send_json({"worlds": service.server_worlds(query.get("targetName", [""])[0])})
                    return
                if route == "/api/servers/files":
                    self._send_json(
                        {
                            "files": service.server_files(
                                query.get("targetName", [""])[0],
                                query.get("path", [""])[0],
                            )
                        }
                    )
                    return
                if route == "/api/servers/command-suggestions":
                    self._send_json(
                        {
                            "suggestions": service.command_suggestions(
                                query.get("targetName", [""])[0],
                                query.get("prefix", [""])[0],
                            )
                        }
                    )
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            try:
                if route == "/api/projects/upload":
                    fields, files = self._read_multipart_form()
                    upload = files.get("packFile")
                    if upload is None or not upload.content:
                        raise ValueError("Please choose a .mrpack or .zip modpack file.")
                    uploaded_path = _save_uploaded_pack(service.workspace_dir, upload)
                    self._send_json(
                        {
                            "job": service.create_project(
                                uploaded_path,
                                project_name=_uploaded_project_name(fields, upload),
                                accept_eula=_truthy(fields.get("acceptEula", "")),
                                download=_truthy(fields.get("download", "true")),
                            )
                        }
                    )
                    return
                if route == "/api/servers/mods/upload":
                    fields, files = self._read_multipart_form()
                    upload = files.get("modFile")
                    if upload is None or not upload.content:
                        raise ValueError("Please choose a .jar mod file.")
                    self._send_json(
                        {
                            "result": service.add_mod(
                                fields.get("targetName", ""),
                                upload.filename,
                                upload.content,
                            )
                        }
                    )
                    return
                payload = self._read_json()
                if route == "/api/import":
                    result = service.import_pack(
                        payload["packPath"],
                        target_name=payload.get("targetName"),
                        download=bool(payload.get("download", False)),
                        curseforge_mirrors=list(payload.get("curseforgeMirrors", [])),
                    )
                    self._send_json({"server": result})
                    return
                if route == "/api/projects":
                    self._send_json(
                        {
                            "job": service.create_project(
                                payload["packPath"],
                                project_name=payload.get("projectName") or payload.get("targetName") or "",
                                accept_eula=bool(payload.get("acceptEula", False)),
                                download=bool(payload.get("download", True)),
                                curseforge_mirrors=list(payload.get("curseforgeMirrors", [])),
                            )
                        }
                    )
                    return
                if route == "/api/servers/start":
                    self._send_json({"server": service.start_server(payload["targetName"])})
                    return
                if route == "/api/servers/stop":
                    self._send_json({"server": service.stop_server(payload["targetName"])})
                    return
                if route == "/api/servers/delete":
                    self._send_json({"result": service.delete_project(payload["targetName"])})
                    return
                if route == "/api/servers/command":
                    self._send_json({"result": service.send_console_command(payload["targetName"], payload["command"])})
                    return
                if route == "/api/servers/properties":
                    self._send_json(
                        {
                            "serverProperties": service.save_server_properties(
                                payload["targetName"],
                                dict(payload.get("properties", {})),
                            )
                        }
                    )
                    return
                if route == "/api/servers/key-settings":
                    self._send_json(
                        {
                            "keySettings": service.save_key_server_settings(
                                payload["targetName"],
                                dict(payload.get("settings", {})),
                            )
                        }
                    )
                    return
                if route == "/api/servers/player-action":
                    self._send_json(
                        {
                            "result": service.player_action(
                                payload["targetName"],
                                payload["action"],
                                **dict(payload.get("args", {})),
                            )
                        }
                    )
                    return
                if route == "/api/servers/mods/disable":
                    self._send_json({"result": service.disable_mod(payload["targetName"], payload["fileName"])})
                    return
                if route == "/api/servers/mods/delete":
                    self._send_json({"result": service.delete_mod(payload["targetName"], payload["fileName"])})
                    return
                if route == "/api/servers/worlds/create":
                    self._send_json(
                        {
                            "result": service.create_world(
                                payload["targetName"],
                                str(payload.get("worldName", "")),
                            )
                        }
                    )
                    return
                if route == "/api/servers/worlds/select":
                    self._send_json(
                        {
                            "result": service.select_world(
                                payload["targetName"],
                                str(payload.get("worldName", "")),
                            )
                        }
                    )
                    return
                if route == "/api/servers/worlds/backup":
                    self._send_json(
                        {
                            "result": service.backup_world(
                                payload["targetName"],
                                str(payload.get("worldName", "")) or None,
                            )
                        }
                    )
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _read_multipart_form(self) -> tuple[dict[str, str], dict[str, "UploadedFormFile"]]:
            length = _validate_upload_length(self.headers.get("Content-Length", ""))
            body = self.rfile.read(length)
            return _parse_multipart_form(self.headers.get("Content-Type", ""), body)

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Pack2ServeHandler)
    service.cleanup_stale_server_processes()
    if sys.stdout:
        print(f"Pack2Serve panel listening on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    finally:
        service.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pack2serve-panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.workspace)
    return 0


@dataclass(frozen=True)
class UploadedFormFile:
    filename: str
    content: bytes


def _parse_multipart_form(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, UploadedFormFile]]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("Expected multipart/form-data upload.")
    message = BytesParser(policy=email_default_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    if not message.is_multipart():
        raise ValueError("Invalid multipart/form-data upload.")

    fields: dict[str, str] = {}
    files: dict[str, UploadedFormFile] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")
        else:
            files[name] = UploadedFormFile(filename=Path(filename.replace("\\", "/")).name, content=payload)
    return fields, files


def _save_uploaded_pack(workspace_dir: Path, upload: UploadedFormFile) -> Path:
    safe_name = _safe_upload_name(upload.filename)
    if Path(safe_name).suffix.lower() not in {".mrpack", ".zip"}:
        raise ValueError("Uploaded modpack must be a .mrpack or .zip file.")
    upload_dir = workspace_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{uuid.uuid4().hex}-{safe_name}"
    path.write_bytes(upload.content)
    return path


def _uploaded_project_name(fields: dict[str, str], upload: UploadedFormFile) -> str:
    return fields.get("projectName", "").strip() or Path(upload.filename).stem


def _safe_upload_name(filename: str) -> str:
    original = Path(filename.replace("\\", "/")).name.strip()
    suffix = Path(original).suffix.lower()
    stem = Path(original).stem
    clean_stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" .-_")
    return f"{clean_stem or 'modpack'}{suffix or '.zip'}"


def _validate_upload_length(raw_length: str) -> int:
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise ValueError("Upload request is missing a valid Content-Length header.") from exc
    if length <= 0:
        raise ValueError("Upload request is empty.")
    if length > MAX_UPLOAD_BYTES:
        limit_gb = MAX_UPLOAD_BYTES // (1024 * 1024 * 1024)
        raise ValueError(f"Uploaded modpack is too large. Limit is {limit_gb} GB.")
    return length


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


PANEL_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pack2Serve 控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #efeee9;
      --surface: #fbfaf6;
      --surface-2: #f5f3ed;
      --ink: #171817;
      --muted: #686f67;
      --line: #d9d6cc;
      --accent: #16725f;
      --accent-ink: #ffffff;
      --warn: #a26012;
      --danger: #a43b2b;
      --ok: #177245;
      --console: #111412;
      --console-line: #263029;
      --shadow: 0 18px 50px rgb(32 35 31 / .12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100dvh;
      background:
        linear-gradient(135deg, rgb(255 255 255 / .58), transparent 38%),
        var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
    }
    button, input, textarea, select { font: inherit; }
    button {
      min-height: 38px;
      border: 0;
      border-radius: 8px;
      padding: 8px 13px;
      background: var(--ink);
      color: white;
      font-weight: 760;
      cursor: pointer;
    }
    button:hover { filter: brightness(1.05); }
    button:active { transform: translateY(1px); }
    button.primary { background: var(--accent); color: var(--accent-ink); }
    button.secondary { background: #e4e1d7; color: var(--ink); }
    button.danger { background: var(--danger); }
    button.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
    button:disabled { opacity: .55; cursor: progress; }
    input, textarea, select {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffefa;
      color: var(--ink);
      padding: 9px 11px;
      outline: none;
    }
    select {
      appearance: none;
      -webkit-appearance: none;
      background-image:
        linear-gradient(45deg, transparent 50%, #30362f 50%),
        linear-gradient(135deg, #30362f 50%, transparent 50%);
      background-position:
        calc(100% - 18px) 50%,
        calc(100% - 12px) 50%;
      background-size: 6px 6px, 6px 6px;
      background-repeat: no-repeat;
      padding-right: 34px;
      font-weight: 700;
      cursor: pointer;
    }
    select::-ms-expand { display: none; }
    input:focus, textarea:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgb(22 114 95 / .14); }
    textarea { resize: vertical; }
    .app-shell { min-height: 100dvh; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
      padding: 20px 28px;
      border-bottom: 1px solid var(--line);
      background: rgb(251 250 246 / .88);
      backdrop-filter: blur(18px);
      position: sticky;
      top: 0;
      z-index: 20;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .mark {
      width: 38px;
      height: 38px;
      border-radius: 8px;
      background: linear-gradient(135deg, #171817, #2c332e);
      color: #d9fff2;
      display: grid;
      place-items: center;
      font: 800 14px Consolas, monospace;
    }
    h1, h2, h3 { margin: 0; }
    h1 { font-size: 21px; }
    .subtle { color: var(--muted); font-size: 13px; }
    .page {
      max-width: 1440px;
      margin: 0 auto;
      padding: 24px 28px 40px;
    }
    .summary-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric, .project-card, .panel, dialog {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 1px 0 rgb(255 255 255 / .7) inset;
    }
    .metric { padding: 14px; }
    .metric strong { display: block; font-size: 24px; margin-top: 5px; }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 18px;
      margin: 18px 0 14px;
    }
    .project-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 14px;
    }
    .project-card {
      padding: 16px;
      cursor: pointer;
      transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
    }
    .project-card:hover { transform: translateY(-2px); box-shadow: var(--shadow); border-color: #beb8aa; }
    .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .project-title { font-size: 17px; font-weight: 800; line-height: 1.25; }
    .project-meta { display: flex; flex-wrap: wrap; gap: 7px; margin: 13px 0; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 3px 9px;
      background: #e7e3d8;
      color: #383b36;
      font-size: 12px;
      font-weight: 760;
    }
    .pill.running, .pill.verified-equivalent { background: #d6eee5; color: #176246; }
    .pill.starting, .pill.stopping, .pill.generated-not-validated, .pill.startable-with-differences { background: #fff0c5; color: #77520b; }
    .pill.failed, .pill.crashed, .pill.not-startable, .pill.incomplete { background: #f5d9d1; color: #8c2c1f; }
    .addr { font: 13px Consolas, "Cascadia Mono", monospace; color: #30362f; }
    .card-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
    .detail-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(320px, .7fr);
      gap: 14px;
      align-items: start;
    }
    .panel { padding: 16px; }
    .panel-head { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 12px; }
    .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 12px; }
    .tab { background: #e4e1d7; color: var(--ink); }
    .tab.active { background: var(--ink); color: white; }
    .log-box {
      height: 470px;
      margin: 0;
      overflow: auto;
      padding: 14px;
      border-radius: 8px;
      background: var(--console);
      color: #dce9df;
      border: 1px solid var(--console-line);
      font: 12px/1.48 Consolas, "Cascadia Mono", monospace;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .log-start-line {
      display: block;
      margin: 10px 0 8px;
      padding: 7px 10px;
      border-left: 4px solid #48c798;
      border-radius: 8px;
      background: rgb(72 199 152 / .16);
      color: #9af3cb;
      font-weight: 800;
    }
    .console-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; margin-top: 10px; }
    .progress-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffefa;
      padding: 14px;
      margin-bottom: 12px;
    }
    .progress-track { height: 10px; background: #e4e1d7; border-radius: 999px; overflow: hidden; }
    .progress-fill { height: 100%; width: 0; background: var(--accent); transition: width .25s ease; }
    .download-detail {
      display: grid;
      gap: 6px;
      margin-top: 12px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f7f5ee;
    }
    .download-detail .progress-track { height: 8px; }
    .download-meta { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; color: var(--muted); }
    .download-current { font: 12px Consolas, "Cascadia Mono", monospace; color: #30362f; overflow-wrap: anywhere; }
    .stage-list { display: grid; gap: 8px; margin-top: 12px; }
    .stage { display: flex; justify-content: space-between; color: var(--muted); font-size: 13px; }
    .stage.current { color: var(--ink); font-weight: 760; }
    .properties-editor { min-height: 430px; font: 13px/1.45 Consolas, "Cascadia Mono", monospace; }
    .settings-grid, .metrics-grid, .player-detail-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .mini-card {
      position: relative;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffefa;
      padding: 12px;
      min-height: 74px;
    }
    .mini-card strong { display: block; margin-top: 5px; font-size: 16px; overflow-wrap: anywhere; }
    .mini-card.with-copy { padding-right: 72px; }
    .mini-copy {
      position: absolute;
      right: 10px;
      top: 10px;
      min-height: 28px;
      border-radius: 8px;
      padding: 4px 8px;
      background: #e4e1d7;
      color: var(--ink);
      font-size: 12px;
      font-weight: 760;
    }
    .players { display: grid; gap: 8px; }
    .player-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .player-section { display: grid; gap: 8px; align-content: start; }
    .player-row { display: flex; justify-content: space-between; border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fffefa; cursor: pointer; }
    .player-row.active { border-color: var(--accent); box-shadow: 0 0 0 3px rgb(22 114 95 / .12); }
    .player-profile { display: grid; grid-template-columns: 72px 1fr; gap: 12px; align-items: start; margin-bottom: 12px; }
    .player-skin { width: 64px; height: 64px; border-radius: 8px; image-rendering: pixelated; background: #e4e1d7; }
    .inventory-panel { margin-top: 14px; display: grid; gap: 12px; }
    .inventory-panel.loading .inventory-layout { outline: 2px solid rgb(22 114 95 / .18); outline-offset: 2px; }
    .inventory-layout {
      display: grid;
      gap: 12px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #d7d2c4;
      box-shadow: inset 0 1px 0 rgb(255 255 255 / .45);
    }
    .equipment-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: start; }
    .inventory-grid, .hotbar-grid { display: grid; grid-template-columns: repeat(9, 42px); gap: 4px; }
    .hotbar-grid { margin-top: 8px; padding-top: 8px; border-top: 3px solid #f3f0e6; }
    .equipment-grid { display: grid; grid-template-columns: repeat(5, 42px); gap: 4px; }
    .accessory-grid { display: grid; grid-template-columns: repeat(auto-fill, 42px); gap: 4px; min-height: 42px; }
    .item-slot {
      position: relative;
      width: 42px;
      height: 42px;
      border: 2px solid #8b867b;
      border-top-color: #5f5a50;
      border-left-color: #5f5a50;
      background: #b9b3a5;
      display: grid;
      place-items: center;
    }
    .item-slot img { width: 32px; height: 32px; image-rendering: pixelated; object-fit: contain; }
    .item-placeholder { width: 28px; height: 28px; border-radius: 6px; background: #8f897d; color: #f8f4e9; display: grid; place-items: center; font-size: 11px; font-weight: 900; }
    .item-count { position: absolute; right: 2px; bottom: 0; color: white; text-shadow: 1px 1px 0 #222; font: 700 12px Consolas, monospace; }
    .item-tooltip {
      display: none;
      position: absolute;
      left: 36px;
      top: 0;
      z-index: 30;
      min-width: 220px;
      max-width: 360px;
      padding: 8px 10px;
      border: 1px solid #5b3c92;
      border-radius: 4px;
      background: rgba(22, 14, 32, .96);
      color: #f4ecff;
      box-shadow: 0 8px 20px rgb(0 0 0 / .28);
      font: 12px/1.4 Consolas, "Cascadia Mono", monospace;
      pointer-events: none;
      overflow-wrap: anywhere;
    }
    .item-slot:hover .item-tooltip { display: block; }
    .inventory-title { font-weight: 850; margin-bottom: 6px; }
    .mod-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
    .mod-toolbar input { width: auto; flex: 1; }
    .mod-list { display: grid; gap: 9px; }
    .mod-row, .world-row, .file-row {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffefa;
      padding: 9px;
    }
    .file-row { cursor: default; }
    .file-row.directory { cursor: pointer; }
    .file-row.directory:hover { border-color: var(--accent); background: #f1fbf6; }
    .world-row.current {
      border-color: var(--accent);
      background: #f1fbf6;
      box-shadow: 0 0 0 3px rgb(22 114 95 / .12);
    }
    .mod-icon { width: 36px; height: 36px; border-radius: 8px; background: #e4e1d7; object-fit: cover; image-rendering: pixelated; }
    .world-icon {
      width: 36px;
      height: 36px;
      border-radius: 8px;
      background: #d6eee5;
      color: #176246;
      display: grid;
      place-items: center;
      font-weight: 900;
    }
    .file-icon {
      width: 36px;
      height: 36px;
      border-radius: 8px;
      background: #e4e1d7;
      display: grid;
      place-items: center;
      font-weight: 900;
      font-size: 12px;
    }
    .file-row.directory .file-icon { background: #d6eee5; color: #176246; }
    .file-breadcrumbs { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 12px; }
    .file-breadcrumbs button { min-height: 30px; padding: 5px 9px; font-size: 12px; }
    .command-wrap { position: relative; }
    .suggestions {
      position: absolute;
      left: 0;
      right: 0;
      bottom: calc(100% + 6px);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffefa;
      box-shadow: var(--shadow);
      overflow: hidden;
      z-index: 10;
    }
    .suggestion { padding: 8px 10px; font: 12px Consolas, "Cascadia Mono", monospace; cursor: pointer; }
    .suggestion:hover { background: #e4e1d7; }
    dialog {
      width: min(620px, calc(100vw - 28px));
      padding: 0;
      color: var(--ink);
    }
    dialog::backdrop { background: rgb(23 24 23 / .42); backdrop-filter: blur(5px); }
    .modal-head, .modal-body, .modal-actions { padding: 18px; }
    .modal-head { border-bottom: 1px solid var(--line); }
    .modal-body { display: grid; gap: 13px; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 9px; border-top: 1px solid var(--line); }
    label { display: grid; gap: 7px; color: var(--muted); font-size: 13px; font-weight: 680; }
    .check-row { display: flex; align-items: flex-start; gap: 10px; color: var(--ink); font-size: 13px; }
    .check-row input { width: 18px; min-height: 18px; margin-top: 1px; }
    .inline-toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 680;
    }
    .inline-toggle input { width: 18px; min-height: 18px; }
    .toast-stack {
      position: fixed;
      right: 22px;
      bottom: 22px;
      display: grid;
      gap: 10px;
      z-index: 50;
    }
    .toast {
      min-width: 280px;
      max-width: 420px;
      padding: 13px 14px;
      border-radius: 8px;
      background: #151714;
      color: white;
      box-shadow: var(--shadow);
      font-size: 13px;
    }
    .hidden { display: none !important; }
    @media (max-width: 900px) {
      .summary-row, .detail-layout { grid-template-columns: 1fr; }
      .page, .topbar { padding-left: 16px; padding-right: 16px; }
      .console-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <div class="mark">P2S</div>
        <div>
          <h1>Pack2Serve 控制台</h1>
          <div class="subtle">整合包导入、构建、开服、日志和配置集中管理</div>
        </div>
      </div>
      <div>
        <button class="ghost" id="refresh">刷新</button>
        <button class="primary" id="openCreate">创建项目</button>
      </div>
    </header>

    <main class="page">
      <section id="homeView">
        <div class="summary-row">
          <div class="metric"><span class="subtle">项目总数</span><strong id="metricTotal">0</strong></div>
          <div class="metric"><span class="subtle">运行中</span><strong id="metricRunning">0</strong></div>
        </div>
        <div id="activeJob" class="progress-card hidden">
          <div class="panel-head">
            <div>
              <h3 id="jobTitle">正在创建项目</h3>
              <div class="subtle" id="jobMessage">准备中</div>
            </div>
            <span class="pill starting" id="jobStage">queued</span>
          </div>
          <div class="progress-track"><div class="progress-fill" id="jobFill"></div></div>
          <div class="download-detail hidden" id="downloadDetail">
            <div class="download-meta"><span id="downloadCount">下载 0/0</span><span id="downloadPercent">0%</span></div>
            <div class="progress-track"><div class="progress-fill" id="downloadFill"></div></div>
            <div class="download-current" id="downloadCurrent"></div>
          </div>
          <div class="stage-list">
            <div class="stage" data-stage="inspect"><span>读取整合包</span><span>08%</span></div>
            <div class="stage" data-stage="build"><span>解析整合包</span><span>16%</span></div>
            <div class="stage" data-stage="copy"><span>复制配置资源</span><span>18%</span></div>
            <div class="stage" data-stage="download"><span>下载模组文件</span><span>26-56%</span></div>
            <div class="stage" data-stage="java"><span>安装 Java 运行时</span><span>62%</span></div>
            <div class="stage" data-stage="loader"><span>安装服务端启动文件</span><span>76%</span></div>
            <div class="stage" data-stage="eula"><span>写入 EULA</span><span>84%</span></div>
            <div class="stage" data-stage="validate"><span>启动验证</span><span>90%</span></div>
            <div class="stage" data-stage="finalize"><span>生成摘要</span><span>97%</span></div>
            <div class="stage" data-stage="complete"><span>创建完成</span><span>100%</span></div>
          </div>
        </div>
        <div class="section-head">
          <div>
            <h2>服务器项目</h2>
            <div class="subtle">默认隐藏开发验证产生的内部测试目录</div>
          </div>
          <label class="inline-toggle">
            <input id="showInternalProjects" type="checkbox">
            <span>显示测试项目</span>
          </label>
        </div>
        <div class="project-grid" id="projectGrid"></div>
      </section>

      <section id="detailView" class="hidden">
        <div class="section-head">
          <div>
            <button class="ghost" id="backHome">返回项目列表</button>
            <h2 id="detailTitle" style="margin-top:12px">项目详情</h2>
            <div class="subtle" id="detailMeta"></div>
          </div>
          <div class="card-actions">
            <button class="primary" id="detailStart">启动</button>
            <button class="danger" id="detailStop">停止</button>
            <button class="danger" id="detailDelete">删除</button>
          </div>
        </div>
        <div class="detail-layout">
          <div class="panel">
            <div class="tabs">
              <button class="tab" data-tab="status">运行总览</button>
              <button class="tab active" data-tab="logs">日志控制台</button>
              <button class="tab" data-tab="properties">服务器参数</button>
              <button class="tab" data-tab="players">玩家</button>
              <button class="tab" data-tab="mods">模组列表</button>
              <button class="tab" data-tab="worlds">世界</button>
              <button class="tab" data-tab="files">文件管理</button>
            </div>
            <div id="tabStatus" class="hidden">
              <div class="metrics-grid" id="metricsGrid"></div>
            </div>
            <div id="tabLogs">
              <pre class="log-box" id="logBody">暂无日志。</pre>
              <div class="console-row">
                <div class="command-wrap">
                  <div class="suggestions hidden" id="commandSuggestions"></div>
                  <input id="consoleCommand" placeholder="输入控制台指令，例如 gamemode creative Steve">
                </div>
                <button class="primary" id="sendCommand">发送</button>
              </div>
            </div>
            <div id="tabProperties" class="hidden">
              <div class="settings-grid" id="keySettings"></div>
              <div class="card-actions"><button class="primary" id="saveKeySettings">保存常用参数</button></div>
              <hr style="border:0;border-top:1px solid var(--line);margin:14px 0">
              <textarea class="properties-editor" id="propertiesEditor" spellcheck="false"></textarea>
              <div class="card-actions"><button class="primary" id="saveProperties">保存 server.properties</button></div>
            </div>
            <div id="tabPlayers" class="hidden">
              <div class="players" id="playersList">
                <div class="player-columns">
                  <div class="player-section">
                    <h3>在线玩家</h3>
                    <div id="onlinePlayersList"></div>
                  </div>
                  <div class="player-section">
                    <h3>离线玩家</h3>
                    <div id="offlinePlayersList"></div>
                  </div>
                </div>
              </div>
              <div class="mini-card" id="playerDetail" style="margin-top:12px"></div>
            </div>
            <div id="tabMods" class="hidden">
              <div class="mod-toolbar">
                <input id="modFile" type="file" accept=".jar,application/java-archive">
                <button class="primary" id="addMod">添加模组</button>
              </div>
              <div class="mod-list" id="modsList"></div>
            </div>
            <div id="tabWorlds" class="hidden">
              <div class="mod-toolbar">
                <input id="worldName" placeholder="新世界名称，例如 world-2">
                <button class="primary" id="createWorld">新建世界</button>
              </div>
              <div class="mod-list" id="worldsList"></div>
            </div>
            <div id="tabFiles" class="hidden">
              <div class="file-breadcrumbs" id="filesBreadcrumbs"></div>
              <div class="mod-list" id="filesList"></div>
            </div>
          </div>
          <aside class="panel">
            <div class="panel-head"><h3>运行状态</h3><span class="pill" id="detailStatus">stopped</span></div>
            <div class="project-meta" id="detailBadges"></div>
            <p class="subtle" id="detailPath"></p>
          </aside>
        </div>
      </section>
    </main>
  </div>

  <dialog id="createDialog">
    <form method="dialog">
      <div class="modal-head">
        <h2>创建服务器项目</h2>
        <div class="subtle">导入整合包后会生成独立服务端项目</div>
      </div>
      <div class="modal-body">
        <label>整合包文件
          <input id="packFile" type="file" accept=".mrpack,.zip,application/zip">
        </label>
        <label>项目名称
          <input id="projectName" placeholder="例如 RLCraft 测试服">
        </label>
        <div class="subtle">CurseForge 文件将使用系统内置的默认无 Key 解析源，不需要单独填写镜像模板。</div>
        <label class="check-row">
          <input id="download" type="checkbox" checked>
          <span>自动下载可解析的远程模组文件</span>
        </label>
        <label class="check-row">
          <input id="acceptEula" type="checkbox">
          <span>我已阅读并同意 Minecraft EULA，允许系统为此项目写入 eula=true</span>
        </label>
      </div>
      <div class="modal-actions">
        <button class="secondary" value="cancel">取消</button>
        <button class="primary" id="createProject" value="default" disabled>创建</button>
      </div>
    </form>
  </dialog>

  <dialog id="playerActionDialog">
    <form method="dialog">
      <div class="modal-head">
        <h2 id="playerActionTitle">玩家操作</h2>
        <div class="subtle" id="playerActionSubtitle"></div>
      </div>
      <div class="modal-body" id="playerActionFields"></div>
      <div class="modal-actions">
        <button class="secondary" value="cancel">取消</button>
        <button class="primary" id="playerActionConfirm" type="button">执行</button>
      </div>
    </form>
  </dialog>
  <div class="toast-stack" id="toastStack"></div>

  <script>
    const $ = (id) => document.getElementById(id);
    const VALID_TABS = new Set(["status", "logs", "properties", "players", "mods", "worlds", "files"]);
    const HOME_ROUTE = "#/projects";
    const ACTIVE_JOB_STORAGE_KEY = "pack2serve.activeJobId";
    const state = { servers: [], selected: null, tab: "status", jobId: "", jobTimer: null, showInternal: false, players: [], onlinePlayers: [], offlinePlayers: [], selectedPlayer: "", selectedPlayerSource: "online", playerInventory: null, inventoryLoading: false, inventoryAutoTimer: null, inventoryRequestId: 0, renderedPlayerKey: "", creatingProject: false, filePath: "", logPinnedToBottom: true, pendingPlayerAction: null };

    async function api(path, options = {}) {
      const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
      const response = await fetch(path, { headers, ...options });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || response.statusText);
      return payload;
    }

    function toast(message) {
      const item = document.createElement("div");
      item.className = "toast";
      item.textContent = message;
      $("toastStack").appendChild(item);
      setTimeout(() => item.remove(), 4200);
    }

    async function runAction(action) {
      try { await action(); } catch (error) { toast(error.message); }
    }

    async function refresh() {
      const payload = await api(`/api/servers?includeInternal=${state.showInternal ? "true" : "false"}`);
      state.servers = payload.servers;
      renderHome();
      applyRoute();
    }

    function renderHome() {
      $("metricTotal").textContent = state.servers.length;
      $("metricRunning").textContent = state.servers.filter((server) => server.runtimeStatus === "running").length;
      $("projectGrid").innerHTML = state.servers.map(cardTemplate).join("") || `<div class="panel"><h3>还没有正式项目</h3><p class="subtle">点击右上角创建项目，选择 .mrpack 或 .zip 整合包生成服务端。</p></div>`;
    }

    function cardTemplate(server) {
      return `<article class="project-card" onclick="openProject('${escapeAttr(server.targetName)}')">
        <div class="card-top"><div><div class="project-title">${escapeHtml(server.name)}</div><div class="subtle">${escapeHtml(server.targetName)}</div></div><span class="pill ${escapeAttr(server.runtimeStatus)}">${escapeHtml(server.runtimeStatus)}</span></div>
        <div class="project-meta"><span class="pill">${escapeHtml(server.minecraftVersion)}</span><span class="pill">${escapeHtml(server.loader)}</span></div>
        <div class="addr">连接 IP ${escapeHtml(server.connectAddress)}</div>
        <div class="card-actions">
          <button class="secondary" onclick="event.stopPropagation(); runAction(() => copyAddress('${escapeAttr(server.connectAddress)}'))">复制地址</button>
          ${cardActionButton(server)}
        </div>
      </article>`;
    }

    function isServerActiveStatus(status) {
      return ["running", "starting", "stopping"].includes(status);
    }

    function cardActionButton(server) {
      if (isServerActiveStatus(server.runtimeStatus)) {
        return `<button class="danger" onclick="event.stopPropagation(); runAction(() => stopServer('${escapeAttr(server.targetName)}'))">停止</button>`;
      }
      return `<button class="primary" onclick="event.stopPropagation(); runAction(() => startServer('${escapeAttr(server.targetName)}'))">启动</button>`;
    }

    async function copyAddress(address) {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(address);
      } else {
        const input = document.createElement("input");
        input.value = address;
        document.body.appendChild(input);
        input.select();
        document.execCommand("copy");
        input.remove();
      }
      toast(`已复制连接地址：${address}`);
    }

    function openProject(targetName) {
      routeToProject(targetName, "status");
    }

    function normalizeTab(tab) {
      return VALID_TABS.has(tab) ? tab : "status";
    }

    function detailRoute(targetName, tab = state.tab) {
      return `#/projects/${encodeURIComponent(targetName)}?tab=${encodeURIComponent(normalizeTab(tab))}`;
    }

    function parseRoute() {
      const hash = location.hash || HOME_ROUTE;
      const [path, query = ""] = hash.slice(1).split("?");
      const parts = path.split("/").filter(Boolean);
      return {
        view: parts[0] === "projects" && parts[1] ? "detail" : "home",
        targetName: parts[0] === "projects" && parts[1] ? decodeURIComponent(parts.slice(1).join("/")) : "",
        searchParams: new URLSearchParams(query),
      };
    }

    function routeHome() {
      if (location.hash === HOME_ROUTE) {
        applyRoute();
      } else {
        location.hash = HOME_ROUTE;
      }
    }

    function routeToProject(targetName, tab = state.tab) {
      const next = detailRoute(targetName, tab);
      if (location.hash === next) {
        applyRoute();
      } else {
        location.hash = next;
      }
    }

    function applyRoute() {
      const route = parseRoute();
      if (route.view === "home") {
        state.selected = null;
        state.selectedPlayer = "";
        state.playerInventory = null;
        state.renderedPlayerKey = "";
        state.inventoryRequestId += 1;
        stopInventoryAutoRefresh();
        $("detailView").classList.add("hidden");
        $("homeView").classList.remove("hidden");
        return;
      }
      const server = state.servers.find((item) => item.targetName === route.targetName);
      if (!server) {
        routeHome();
        return;
      }
      const changedProject = state.selected?.targetName !== server.targetName;
      state.selected = server;
      if (changedProject) {
        state.selectedPlayer = "";
        state.playerInventory = null;
        state.renderedPlayerKey = "";
        state.inventoryRequestId += 1;
        stopInventoryAutoRefresh();
        state.filePath = "";
      }
      $("homeView").classList.add("hidden");
      $("detailView").classList.remove("hidden");
      renderDetail();
      const routedTab = route.searchParams.get("tab") || "status";
      setTab(routedTab, { updateRoute: false });
    }

    function renderDetail() {
      const server = state.selected;
      if (!server) return;
      $("detailTitle").textContent = server.name;
      $("detailMeta").textContent = `${server.targetName} / ${server.connectAddress}`;
      $("detailStatus").textContent = server.runtimeStatus;
      $("detailStatus").className = `pill ${server.runtimeStatus}`;
      $("detailBadges").innerHTML = `<span class="pill">${escapeHtml(server.minecraftVersion)}</span><span class="pill">${escapeHtml(server.loader)}</span><span class="pill ${escapeAttr(server.compatibilityLevel)}">${escapeHtml(server.compatibilityLevel)}</span><span class="pill">人工项 ${escapeHtml(server.manualActions)}</span>`;
      $("detailPath").textContent = server.target;
    }

    async function startServer(targetName) {
      state.logPinnedToBottom = true;
      const payload = await api("/api/servers/start", { method: "POST", body: JSON.stringify({ targetName }) });
      toast(`已发送启动命令：${payload.server.connectAddress}`);
      await refresh();
      await refreshVisibleTab();
    }

    async function stopServer(targetName) {
      await api("/api/servers/stop", { method: "POST", body: JSON.stringify({ targetName }) });
      toast("已发送停止命令");
      await refresh();
      await refreshVisibleTab();
    }

    async function deleteProject(targetName, displayName = "") {
      const server = state.servers.find((item) => item.targetName === targetName);
      const label = displayName || server?.name || targetName;
      if (!confirm(`确定删除项目“${label}”？这会停止服务器并删除整个项目目录。`)) return;
      await api("/api/servers/delete", { method: "POST", body: JSON.stringify({ targetName }) });
      toast(`已删除项目：${label}`);
      if (state.selected?.targetName === targetName) {
        state.selected = null;
        routeHome();
      }
      await refresh();
    }

    async function refreshMetrics() {
      if (!state.selected || state.tab !== "status") return;
      const payload = await api(`/api/servers/metrics?targetName=${encodeURIComponent(state.selected.targetName)}`);
      const metrics = payload.metrics;
      $("metricsGrid").innerHTML = [
        copyMetricCard("连接地址", metrics.runtime.connectAddress),
        metricCard("运行状态", metrics.runtime.runtimeStatus),
        metricCard("运行时长", formatDuration(metrics.runtime.uptimeSeconds || 0)),
        metricCard("世界时间", metrics.world.gameTime ?? "未知"),
        metricCard("已过日夜", metrics.world.days ?? "未知"),
        metricCard("世界大小", formatBytes(metrics.world.sizeBytes)),
        metricCard("项目占用", formatBytes(metrics.resources.projectSizeBytes)),
        metricCard("内存占用", formatBytes(metrics.resources.memoryBytes))
      ].join("");
    }

    function metricCard(label, value) {
      return `<div class="mini-card"><span class="subtle">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
    }

    function copyMetricCard(label, value) {
      return `<div class="mini-card with-copy"><button class="mini-copy" onclick="runAction(() => copyAddress('${escapeAttr(value)}'))">复制</button><span class="subtle">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
    }

    async function refreshLogs() {
      if (!state.selected || state.tab !== "logs") return;
      const payload = await api(`/api/servers/logs?targetName=${encodeURIComponent(state.selected.targetName)}&maxLines=500`);
      const logBody = $("logBody");
      const shouldStick = state.logPinnedToBottom || isLogNearBottom(logBody);
      logBody.innerHTML = renderLogLines(payload.log.lines);
      if (shouldStick) {
        logBody.scrollTop = logBody.scrollHeight;
        state.logPinnedToBottom = true;
      }
    }

    function renderLogLines(lines) {
      if (!lines.length) return escapeHtml("暂无日志。");
      return lines.map((line) => {
        const escaped = escapeHtml(line);
        if (line.includes("Pack2Serve panel start") || line.includes("Pack2Serve panel preflight")) {
          return `<span class="log-start-line">${escaped}</span>`;
        }
        return escaped;
      }).join("\n");
    }

    function isLogNearBottom(element) {
      return element.scrollHeight - element.scrollTop - element.clientHeight < 36;
    }

    function onLogScroll() {
      state.logPinnedToBottom = isLogNearBottom($("logBody"));
    }

    async function sendCommand() {
      if (!state.selected) return;
      const command = $("consoleCommand").value.trim();
      if (!command) return;
      await api("/api/servers/command", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, command }) });
      $("consoleCommand").value = "";
      $("commandSuggestions").classList.add("hidden");
      toast(`已发送指令：${command}`);
      await refreshLogs();
    }

    async function refreshCommandSuggestions() {
      if (!state.selected) return;
      const prefix = $("consoleCommand").value;
      const payload = await api(`/api/servers/command-suggestions?targetName=${encodeURIComponent(state.selected.targetName)}&prefix=${encodeURIComponent(prefix)}`);
      const suggestions = payload.suggestions.suggestions;
      $("commandSuggestions").innerHTML = suggestions.slice(0, 8).map((item) => `<div class="suggestion" onclick="chooseCommand('${escapeAttr(item)}')">${escapeHtml(item)}</div>`).join("");
      $("commandSuggestions").classList.toggle("hidden", suggestions.length === 0 || !prefix.trim());
    }

    function chooseCommand(command) {
      $("consoleCommand").value = command;
      $("commandSuggestions").classList.add("hidden");
      $("consoleCommand").focus();
    }

    async function loadProperties() {
      if (!state.selected) return;
      const payload = await api(`/api/servers/properties?targetName=${encodeURIComponent(state.selected.targetName)}`);
      const props = payload.serverProperties.properties;
      $("propertiesEditor").value = Object.keys(props).sort().map((key) => `${key}=${props[key]}`).join("\n");
      await loadKeySettings();
    }

    async function loadKeySettings() {
      const payload = await api(`/api/servers/key-settings?targetName=${encodeURIComponent(state.selected.targetName)}`);
      $("keySettings").innerHTML = Object.entries(payload.keySettings.settings).map(([key, setting]) => settingInput(key, setting)).join("");
    }

    function settingInput(key, setting) {
      if (setting.type === "boolean") return `<label>${escapeHtml(setting.label)}<select data-setting="${escapeAttr(key)}"><option value="true" ${setting.value === "true" ? "selected" : ""}>开启</option><option value="false" ${setting.value === "false" ? "selected" : ""}>关闭</option></select></label>`;
      if (setting.type === "select") return `<label>${escapeHtml(setting.label)}<select data-setting="${escapeAttr(key)}">${setting.options.map((option) => `<option value="${escapeAttr(option)}" ${setting.value === option ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}</select></label>`;
      return `<label>${escapeHtml(setting.label)}<input data-setting="${escapeAttr(key)}" type="${setting.type === "number" ? "number" : "text"}" value="${escapeAttr(setting.value)}"></label>`;
    }

    async function saveKeySettings() {
      const settings = {};
      document.querySelectorAll("[data-setting]").forEach((input) => settings[input.dataset.setting] = input.value);
      await api("/api/servers/key-settings", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, settings }) });
      toast("常用参数已保存");
      await loadProperties();
      await refresh();
    }

    async function saveProperties() {
      const properties = {};
      $("propertiesEditor").value.split(/\r?\n/).forEach((line) => {
        const clean = line.trim();
        if (!clean || clean.startsWith("#") || !clean.includes("=")) return;
        const index = clean.indexOf("=");
        properties[clean.slice(0, index).trim()] = clean.slice(index + 1).trim();
      });
      await api("/api/servers/properties", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, properties }) });
      toast("server.properties 已保存");
      await loadKeySettings();
      await refresh();
    }

    async function refreshPlayers() {
      if (!state.selected || state.tab !== "players") return;
      const payload = await api(`/api/servers/players?targetName=${encodeURIComponent(state.selected.targetName)}`);
      state.onlinePlayers = payload.players.onlinePlayers || payload.players.players || [];
      state.offlinePlayers = payload.players.offlinePlayers || [];
      state.players = [...state.onlinePlayers, ...state.offlinePlayers];
      $("onlinePlayersList").innerHTML = state.onlinePlayers.map((player) => playerRow(player, "online")).join("") || `<div class="subtle">当前没有在线玩家。</div>`;
      $("offlinePlayersList").innerHTML = state.offlinePlayers.map((player) => playerRow(player, "offline")).join("") || `<div class="subtle">当前世界没有可读取的离线 playerdata。</div>`;
      refreshSelectedPlayerDetail();
    }

    function playerRow(player, source) {
      const position = formatVector(player.position, "坐标未知");
      const key = player.uuid || player.name;
      const active = state.selectedPlayer === key && state.selectedPlayerSource === source ? " active" : "";
      const meta = source === "offline" ? `离线 / ${escapeHtml(player.uuid || "")}` : `${escapeHtml(player.gameMode)} / ${escapeHtml(position)}`;
      return `<div class="player-row${active}" onclick="selectPlayer('${escapeAttr(key)}', '${source}')"><strong>${escapeHtml(player.name || key)}</strong><span class="subtle">${meta}</span></div>`;
    }

    function selectPlayer(name, source = "online") {
      const nextKey = `${source}:${name}`;
      const changedPlayer = state.renderedPlayerKey !== nextKey;
      state.selectedPlayer = name;
      state.selectedPlayerSource = source;
      if (changedPlayer) {
        state.playerInventory = null;
        state.inventoryRequestId += 1;
      }
      state.inventoryLoading = false;
      renderPlayerDetail({ force: true });
      if (source === "online") runAction(() => playerAction("probe", { player: name }));
      runAction(() => loadPlayerInventory({ force: true }));
      startInventoryAutoRefresh();
    }

    function refreshSelectedPlayerDetail() {
      const player = selectedPlayerRecord();
      if (!player) {
        state.renderedPlayerKey = "";
        state.inventoryRequestId += 1;
        stopInventoryAutoRefresh();
        renderPlayerDetail({ force: true });
        return;
      }
      const key = `${state.selectedPlayerSource}:${state.selectedPlayer}`;
      if (state.renderedPlayerKey !== key || !$("playerProfileName")) {
        renderPlayerDetail({ force: true });
      } else {
        updatePlayerDetailChrome(player);
      }
    }

    function renderPlayerDetail({ force = false } = {}) {
      const player = selectedPlayerRecord();
      if (!player) {
        $("playerDetail").innerHTML = `<div class="subtle">点击玩家查看位置、朝向、皮肤、背包和管理操作。</div>`;
        return;
      }
      const key = `${state.selectedPlayerSource}:${state.selectedPlayer}`;
      if (!force && state.renderedPlayerKey === key && $("playerProfileName")) {
        updatePlayerDetailChrome(player);
        return;
      }
      state.renderedPlayerKey = key;
      const pos = formatVector(player.position, "等待探测");
      const rot = formatRotation(player.rotation, "等待探测");
      const isOnline = state.selectedPlayerSource === "online";
      const inventoryButton = `<button class="secondary" id="inventoryLoadButton" ${state.inventoryLoading ? "disabled" : ""} onclick="runAction(() => loadPlayerInventory())">${state.inventoryLoading ? "读取中" : "查看背包"}</button>`;
      $("playerDetail").innerHTML = `
        <div class="player-profile"><img class="player-skin" src="${escapeAttr(player.skinUrl)}" alt=""><div><h3 id="playerProfileName">${escapeHtml(player.name)}</h3><div class="subtle" id="playerProfileMeta">模式 ${escapeHtml(player.gameMode)} / 状态 ${escapeHtml(player.status)}</div></div></div>
        <div class="player-detail-grid">
          ${metricCardWithId("位置", pos, "playerPositionMetric")}
          ${metricCardWithId("朝向", rot, "playerRotationMetric")}
          ${metricCardWithId("UUID", player.uuid || "在线探针", "playerUuidMetric")}
          ${metricCardWithId("背包", inventorySummary(), "inventorySummaryValue")}
        </div>
        ${isOnline ? `<div class="card-actions">
          <button class="secondary" onclick="runAction(() => playerAction('probe', { player: '${escapeAttr(player.name)}' }))">刷新状态</button>
          ${inventoryButton}
          <button class="primary" onclick="runAction(() => playerAction('op', { player: '${escapeAttr(player.name)}' }))">设为 OP</button>
          <button class="secondary" onclick="openPlayerActionDialog('gamemode', '${escapeAttr(player.name)}')">改模式</button>
          <button class="secondary" onclick="openPlayerActionDialog('tp', '${escapeAttr(player.name)}')">TP</button>
          <button class="danger" onclick="openPlayerActionDialog('ban', '${escapeAttr(player.name)}')">封禁</button>
          <button class="danger" onclick="runAction(() => playerAction('kill', { player: '${escapeAttr(player.name)}' }))">杀死</button>
          <button class="danger" onclick="runAction(() => playerAction('clear', { player: '${escapeAttr(player.name)}' }))">清空背包</button>
        </div>` : `<div class="card-actions">${inventoryButton}</div>`}
        <div class="inventory-panel" id="inventoryPanel">${renderInventoryPanel()}</div>`;
      updateInventoryLoadingState();
    }

    function updatePlayerDetailChrome(player) {
      if (!$("playerProfileName")) return;
      $("playerProfileName").textContent = player.name;
      $("playerProfileMeta").textContent = `模式 ${player.gameMode} / 状态 ${player.status}`;
      $("playerPositionMetric").textContent = formatVector(player.position, "等待探测");
      $("playerRotationMetric").textContent = formatRotation(player.rotation, "等待探测");
      $("playerUuidMetric").textContent = player.uuid || "在线探针";
      updateInventorySummary();
      updateInventoryLoadingState();
    }

    function metricCardWithId(label, value, id) {
      return `<div class="mini-card"><span class="subtle">${escapeHtml(label)}</span><strong id="${escapeHtml(id)}">${escapeHtml(value)}</strong></div>`;
    }

    function selectedPlayerRecord() {
      const list = state.selectedPlayerSource === "offline" ? state.offlinePlayers : state.onlinePlayers;
      return list.find((item) => (item.uuid || item.name) === state.selectedPlayer);
    }

    async function loadPlayerInventory({ auto = false, force = false } = {}) {
      if (!state.selected || !state.selectedPlayer) return;
      if (state.inventoryLoading) return;
      if (auto && state.tab !== "players") return;
      state.inventoryLoading = true;
      const requestId = state.inventoryRequestId + 1;
      state.inventoryRequestId = requestId;
      updateInventoryLoadingState();
      try {
        const requestKey = `${state.selected.targetName}:${state.selectedPlayerSource}:${state.selectedPlayer}`;
        const payload = await api(`/api/servers/player-inventory?targetName=${encodeURIComponent(state.selected.targetName)}&player=${encodeURIComponent(state.selectedPlayer)}&source=${encodeURIComponent(state.selectedPlayerSource)}`);
        if (requestId !== state.inventoryRequestId || requestKey !== `${state.selected?.targetName}:${state.selectedPlayerSource}:${state.selectedPlayer}`) return;
        state.playerInventory = payload.inventory;
        reconcileInventoryPanel(payload.inventory, { force });
        updateInventorySummary();
        if (payload.inventory.status === "probing") setTimeout(() => loadPlayerInventory({ force }).catch(() => {}), 700);
      } finally {
        if (requestId !== state.inventoryRequestId) return;
        state.inventoryLoading = false;
        updateInventoryLoadingState();
      }
    }

    function startInventoryAutoRefresh() {
      stopInventoryAutoRefresh();
      state.inventoryAutoTimer = setInterval(() => loadPlayerInventory({ auto: true }).catch(() => {}), 10000);
    }

    function stopInventoryAutoRefresh() {
      if (state.inventoryAutoTimer) clearInterval(state.inventoryAutoTimer);
      state.inventoryAutoTimer = null;
    }

    function updateInventoryLoadingState() {
      const button = $("inventoryLoadButton");
      if (button) {
        button.disabled = state.inventoryLoading;
        button.textContent = state.inventoryLoading ? "读取中" : "查看背包";
      }
      const panel = $("inventoryPanel");
      if (panel) panel.classList.toggle("loading", state.inventoryLoading);
    }

    function updateInventorySummary() {
      const summary = $("inventorySummaryValue");
      if (summary) summary.textContent = inventorySummary();
    }

    function inventorySummary() {
      const inventory = state.playerInventory;
      if (!inventory) return "点击查看";
      if (inventory.status === "unsupported") return "版本过低不支持";
      if (inventory.status === "probing") return "探针读取中";
      const count = [...(inventory.inventory || []), ...(inventory.enderChest || [])].filter((item) => item && item.id).length;
      return `${count} 项`;
    }

    function reconcileInventoryPanel(inventory, { force = false } = {}) {
      const panel = $("inventoryPanel");
      if (!panel) return;
      const nextHtml = renderInventoryPanel();
      if (force || !panel.querySelector(".inventory-layout") || inventory?.status !== "ready") {
        panel.innerHTML = nextHtml;
        return;
      }
      const wrapper = document.createElement("div");
      wrapper.innerHTML = nextHtml;
      const currentSlots = Array.from(panel.querySelectorAll("[data-slot-key]"));
      const nextSlots = Array.from(wrapper.querySelectorAll("[data-slot-key]"));
      const currentKeys = currentSlots.map((slot) => slot.dataset.slotKey).join("|");
      const nextKeys = nextSlots.map((slot) => slot.dataset.slotKey).join("|");
      if (currentKeys !== nextKeys) {
        panel.innerHTML = nextHtml;
        return;
      }
      const currentByKey = new Map(currentSlots.map((slot) => [slot.dataset.slotKey, slot]));
      nextSlots.forEach((nextSlot) => {
        const current = currentByKey.get(nextSlot.dataset.slotKey);
        if (current && current.dataset.signature !== nextSlot.dataset.signature) {
          current.className = nextSlot.className;
          current.dataset.signature = nextSlot.dataset.signature;
          current.innerHTML = nextSlot.innerHTML;
        }
      });
    }

    function renderInventoryPanel() {
      const inventory = state.playerInventory;
      if (!inventory) return `<div class="subtle">点击“查看背包”读取玩家背包。</div>`;
      if (inventory.status === "unsupported") return `<div class="subtle">${escapeHtml(inventory.message || "当前版本不支持背包查看。")}</div>`;
      if (inventory.status === "probing") return `<div class="subtle">${escapeHtml(inventory.message || "正在等待服务器返回背包数据。")}</div>`;
      return `
        <div class="inventory-layout">
          <div>
            <div class="inventory-title">装备 / 副手</div>
            <div class="equipment-row">
              <div class="equipment-grid">${slotGrid(inventory.armor || [], 4, "armor")}${slotGrid(inventory.offhand || [], 1, "offhand")}</div>
            </div>
          </div>
          ${renderAccessories(inventory.accessories || [])}
          <div>
            <div class="inventory-title">背包</div>
            <div class="inventory-grid">${slotGrid(inventory.main || [], 27, "main")}</div>
            <div class="inventory-title" style="margin-top:10px">快捷栏</div>
            <div class="hotbar-grid">${slotGrid(inventory.hotbar || [], 9, "hotbar")}</div>
          </div>
          <div>
            <div class="inventory-title">末影箱</div>
            <div class="inventory-grid">${slotGrid(inventory.enderChest || [], 27, "enderChest")}</div>
          </div>
        </div>`;
    }

    function renderAccessories(sections) {
      if (!sections.length) return `<div><div class="inventory-title">饰品栏</div><div class="accessory-grid">${slotGrid([], 8, "accessory")}</div></div>`;
      return sections.map((section) => `<div><div class="inventory-title">${escapeHtml(section.name || "饰品栏")}</div><div class="accessory-grid">${slotGrid(section.items || [], Math.max(8, (section.items || []).length), "accessory")}</div></div>`).join("");
    }

    function slotGrid(items, size, section) {
      const bySlot = new Map((items || []).map((item, index) => [slotIndex(item, section, index), item]));
      return Array.from({ length: size }, (_, index) => itemSlot(bySlot.get(index), `${section}:${index}`)).join("");
    }

    function slotIndex(item, section, index) {
      const slot = Number(item?.slot ?? index);
      if (section === "hotbar") return slot >= 0 && slot <= 8 ? slot : index;
      if (section === "main") return slot >= 9 && slot <= 35 ? slot - 9 : index;
      if (section === "armor") return slot >= 100 && slot <= 103 ? slot - 100 : index;
      if (section === "offhand") return 0;
      return slot >= 0 ? slot : index;
    }

    function itemSlot(item, slotKey) {
      const signature = slotSignature(item);
      if (!item || !item.id) return `<div class="item-slot" data-slot-key="${escapeHtml(slotKey)}" data-signature="${escapeHtml(signature)}"></div>`;
      const icon = item.iconDataUrl ? `<img src="${escapeAttr(item.iconDataUrl)}" alt="">` : `<div class="item-placeholder">${escapeHtml(item.id.split(":")[0].slice(0, 2).toUpperCase())}</div>`;
      const count = Number(item.count || 0) > 1 ? `<span class="item-count">${escapeHtml(item.count)}</span>` : "";
      const tooltip = (item.tooltip || [item.id]).map((line) => `<div>${escapeHtml(line)}</div>`).join("");
      return `<div class="item-slot" data-slot-key="${escapeHtml(slotKey)}" data-signature="${escapeHtml(signature)}">${icon}${count}<div class="item-tooltip">${tooltip}</div></div>`;
    }

    function slotSignature(item) {
      if (!item || !item.id) return "empty";
      return [
        item.id || "",
        item.count || 0,
        item.slot ?? "",
        item.damage || 0,
        item.iconDataUrl ? "icon" : "no-icon",
        simpleHash(item.raw || "")
      ].join("|");
    }

    function simpleHash(value) {
      let hash = 0;
      const text = String(value || "");
      for (let index = 0; index < text.length; index += 1) hash = ((hash << 5) - hash + text.charCodeAt(index)) | 0;
      return String(hash);
    }

    function formatCoordinate(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number.toFixed(1) : "未知";
    }

    function formatVector(position, fallback) {
      if (!position) return fallback;
      return `${formatCoordinate(position.x)}, ${formatCoordinate(position.y)}, ${formatCoordinate(position.z)}`;
    }

    function formatRotation(rotation, fallback) {
      if (!rotation) return fallback;
      return `${formatCoordinate(rotation.yaw)}, ${formatCoordinate(rotation.pitch)}`;
    }

    async function playerAction(action, args) {
      await api("/api/servers/player-action", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, action, args }) });
      toast("玩家指令已发送");
      await refreshLogs();
      await refreshPlayers();
    }

    function openPlayerActionDialog(action, playerName) {
      const player = state.players.find((item) => item.name === playerName) || { name: playerName };
      state.pendingPlayerAction = { action, playerName };
      const titles = {
        gamemode: "修改游戏模式",
        tp: "传送玩家",
        ban: "封禁玩家",
      };
      $("playerActionTitle").textContent = titles[action] || "玩家操作";
      $("playerActionSubtitle").textContent = `${playerName} · ${state.selected?.name || ""}`;
      $("playerActionFields").innerHTML = playerActionFields(action, player);
      $("playerActionDialog").showModal();
    }

    function playerActionFields(action, player) {
      if (action === "gamemode") {
        const current = ["survival", "creative", "adventure", "spectator"].includes(player.gameMode) ? player.gameMode : "survival";
        return `
          <label>游戏模式
            <select id="playerGameMode">
              ${["survival", "creative", "adventure", "spectator"].map((mode) => `<option value="${mode}" ${mode === current ? "selected" : ""}>${mode}</option>`).join("")}
            </select>
          </label>`;
      }
      if (action === "tp") {
        const pos = player.position || { x: 0, y: 80, z: 0 };
        return `
          <div class="settings-grid">
            <label>X<input id="playerTpX" type="number" step="0.1" value="${escapeAttr(formatCoordinate(pos.x))}"></label>
            <label>Y<input id="playerTpY" type="number" step="0.1" value="${escapeAttr(formatCoordinate(pos.y))}"></label>
            <label>Z<input id="playerTpZ" type="number" step="0.1" value="${escapeAttr(formatCoordinate(pos.z))}"></label>
          </div>`;
      }
      if (action === "ban") {
        return `
          <label>封禁原因
            <input id="playerBanReason" placeholder="可留空，默认由 Pack2Serve 记录">
          </label>
          <div class="subtle">这是危险操作，执行后玩家会被加入服务器封禁列表。</div>`;
      }
      return "";
    }

    async function confirmPlayerAction() {
      const pending = state.pendingPlayerAction;
      if (!pending) return;
      const { action, playerName } = pending;
      if (action === "gamemode") {
        await playerAction("gamemode", { player: playerName, gameMode: $("playerGameMode").value });
      } else if (action === "tp") {
        const x = requireCoordinate("playerTpX");
        const y = requireCoordinate("playerTpY");
        const z = requireCoordinate("playerTpZ");
        await playerAction("tp", { player: playerName, x, y, z });
      } else if (action === "ban") {
        await playerAction("ban", { player: playerName, reason: $("playerBanReason").value.trim() });
      }
      state.pendingPlayerAction = null;
      $("playerActionDialog").close();
    }

    function requireCoordinate(id) {
      const value = $(id).value.trim();
      if (!value || !Number.isFinite(Number(value))) throw new Error("请输入有效坐标。");
      return Number(value).toFixed(1);
    }

    async function loadMods() {
      if (!state.selected || state.tab !== "mods") return;
      const payload = await api(`/api/servers/mods?targetName=${encodeURIComponent(state.selected.targetName)}`);
      $("modsList").innerHTML = payload.mods.mods.map(modRow).join("") || `<div class="subtle">当前项目没有识别到 mods 目录中的 .jar 文件。</div>`;
    }

    function modRow(mod) {
      const icon = mod.iconDataUrl ? `<img class="mod-icon" src="${escapeAttr(mod.iconDataUrl)}" alt="">` : `<div class="mod-icon"></div>`;
      return `<div class="mod-row">${icon}<div><strong>${escapeHtml(mod.title)}</strong><div class="subtle">${escapeHtml(mod.fileName)} / ${escapeHtml(mod.status)}</div></div><div class="card-actions">${mod.enabled ? `<button class="secondary" onclick="runAction(() => disableMod('${escapeAttr(mod.fileName)}'))">禁用</button>` : ""}<button class="danger" onclick="runAction(() => deleteMod('${escapeAttr(mod.fileName)}'))">删除</button></div></div>`;
    }

    async function addMod() {
      const file = $("modFile").files[0];
      if (!file) throw new Error("请选择 .jar 模组文件。");
      const form = new FormData();
      form.append("targetName", state.selected.targetName);
      form.append("modFile", file);
      await api("/api/servers/mods/upload", { method: "POST", body: form });
      $("modFile").value = "";
      toast("模组已添加，重启服务器后生效");
      await loadMods();
    }

    async function disableMod(fileName) {
      await api("/api/servers/mods/disable", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, fileName }) });
      toast("模组已禁用，重启服务器后生效");
      await loadMods();
    }

    async function deleteMod(fileName) {
      if (!confirm(`确定删除模组文件 ${fileName}？`)) return;
      await api("/api/servers/mods/delete", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, fileName }) });
      toast("模组已删除，重启服务器后生效");
      await loadMods();
    }

    async function loadWorlds() {
      if (!state.selected || state.tab !== "worlds") return;
      const payload = await api(`/api/servers/worlds?targetName=${encodeURIComponent(state.selected.targetName)}`);
      const worlds = payload.worlds.worlds;
      $("worldsList").innerHTML = worlds.map(worldRow).join("") || `<div class="subtle">还没有世界目录。新建世界后，服务器重启会生成完整世界数据。</div>`;
    }

    function worldRow(world) {
      return `<div class="world-row${world.current ? " current" : ""}">
        <div class="world-icon">W</div>
        <div>
          <strong>${escapeHtml(world.name)}</strong>
          <div class="subtle">${world.current ? "当前世界 / " : ""}${formatBytes(world.sizeBytes)}${world.hasLevelDat ? " / level.dat" : " / 新世界"}</div>
        </div>
        <div class="card-actions">
          ${world.current ? "" : `<button class="secondary" onclick="runAction(() => selectWorld('${escapeAttr(world.name)}'))">设为当前</button>`}
          <button class="primary" onclick="runAction(() => backupWorld('${escapeAttr(world.name)}'))">备份</button>
        </div>
      </div>`;
    }

    async function createWorld() {
      if (!state.selected) return;
      const worldName = $("worldName").value.trim();
      if (!worldName) throw new Error("请输入新世界名称。");
      await api("/api/servers/worlds/create", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, worldName }) });
      $("worldName").value = "";
      toast("世界已创建。选择为当前世界后，重启服务器生效。");
      await loadWorlds();
    }

    async function selectWorld(worldName) {
      await api("/api/servers/worlds/select", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, worldName }) });
      toast("当前世界已切换，重启服务器后生效。");
      await loadWorlds();
      await loadProperties();
      await refreshMetrics();
    }

    async function backupWorld(worldName) {
      const payload = await api("/api/servers/worlds/backup", { method: "POST", body: JSON.stringify({ targetName: state.selected.targetName, worldName }) });
      toast(`世界已备份：${payload.result.backupPath}`);
      await loadWorlds();
    }

    async function loadFiles(path = state.filePath) {
      if (!state.selected || state.tab !== "files") return;
      const payload = await api(`/api/servers/files?targetName=${encodeURIComponent(state.selected.targetName)}&path=${encodeURIComponent(path || "")}`);
      const files = payload.files;
      state.filePath = files.currentPath || "";
      $("filesBreadcrumbs").innerHTML = fileBreadcrumbs(files.currentPath);
      $("filesList").innerHTML = files.entries.map(fileRow).join("") || `<div class="subtle">当前目录为空。</div>`;
    }

    function fileBreadcrumbs(currentPath) {
      const parts = currentPath ? currentPath.split("/") : [];
      const crumbs = [`<button class="secondary" onclick="runAction(() => openFilePath(''))">项目根目录</button>`];
      parts.forEach((part, index) => {
        const path = parts.slice(0, index + 1).join("/");
        crumbs.push(`<button class="secondary" onclick="runAction(() => openFilePath('${escapeAttr(path)}'))">${escapeHtml(part)}</button>`);
      });
      return crumbs.join("");
    }

    function fileRow(entry) {
      const isDirectory = entry.kind === "directory";
      const action = isDirectory ? ` onclick="runAction(() => openFilePath('${escapeAttr(entry.relativePath)}'))"` : "";
      const size = isDirectory ? "文件夹" : formatBytes(entry.sizeBytes);
      const modified = entry.lastModified ? new Date(entry.lastModified * 1000).toLocaleString() : "未知时间";
      return `<div class="file-row ${escapeAttr(entry.kind)}"${action}>
        <div class="file-icon">${isDirectory ? "DIR" : "FILE"}</div>
        <div>
          <strong>${escapeHtml(entry.name)}</strong>
          <div class="subtle">${escapeHtml(entry.relativePath)}</div>
        </div>
        <div class="subtle">${escapeHtml(size)} / ${escapeHtml(modified)}</div>
      </div>`;
    }

    async function openFilePath(path) {
      state.filePath = path || "";
      await loadFiles(state.filePath);
    }

    function setTab(tab, options = { updateRoute: true }) {
      const cleanTab = normalizeTab(tab);
      if (options.updateRoute && state.selected) {
        const next = detailRoute(state.selected.targetName, cleanTab);
        if (location.hash !== next) {
          location.hash = next;
          return;
        }
      }
      state.tab = cleanTab;
      if (cleanTab === "players" && state.selectedPlayer) {
        startInventoryAutoRefresh();
      } else if (cleanTab !== "players") {
        stopInventoryAutoRefresh();
      }
      document.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === cleanTab));
      ["Status", "Logs", "Properties", "Players", "Mods", "Worlds", "Files"].forEach((name) => $(`tab${name}`).classList.toggle("hidden", cleanTab !== name.toLowerCase()));
      refreshVisibleTab();
    }

    async function refreshVisibleTab() {
      if (state.tab === "status") await refreshMetrics();
      if (state.tab === "logs") await refreshLogs();
      if (state.tab === "properties") await loadProperties();
      if (state.tab === "players") await refreshPlayers();
      if (state.tab === "mods") await loadMods();
      if (state.tab === "worlds") await loadWorlds();
      if (state.tab === "files") await loadFiles();
    }

    function updateCreateButton() {
      $("createProject").disabled = state.creatingProject || !$("download").checked || !$("acceptEula").checked;
      $("createProject").textContent = state.creatingProject ? "创建中..." : "创建";
    }

    async function createProject(event) {
      event.preventDefault();
      if (state.creatingProject) return toast("项目创建任务正在提交中，请稍等。");
      if (!$("download").checked || !$("acceptEula").checked) return toast("请勾选 EULA 和自动下载后再创建。");
      const file = $("packFile").files[0];
      if (!file) return toast("请先选择 .mrpack 或 .zip 整合包文件。");
      const form = new FormData();
      form.append("packFile", file);
      form.append("projectName", $("projectName").value.trim());
      form.append("download", $("download").checked ? "true" : "false");
      form.append("acceptEula", $("acceptEula").checked ? "true" : "false");
      state.creatingProject = true;
      updateCreateButton();
      try {
        const payload = await api("/api/projects/upload", { method: "POST", body: form });
        $("createDialog").close();
        $("packFile").value = "";
        watchJob(payload.job.jobId);
        toast("项目创建任务已开始");
      } finally {
        state.creatingProject = false;
        updateCreateButton();
      }
    }

    function watchJob(jobId) {
      state.jobId = jobId;
      localStorage.setItem(ACTIVE_JOB_STORAGE_KEY, jobId);
      $("activeJob").classList.remove("hidden");
      if (state.jobTimer) clearInterval(state.jobTimer);
      pollJob();
      state.jobTimer = setInterval(pollJob, 1200);
    }

    function clearActiveJob() {
      state.jobId = "";
      localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
      if (state.jobTimer) clearInterval(state.jobTimer);
      state.jobTimer = null;
    }

    async function restoreActiveJob() {
      const savedJobId = localStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
      if (savedJobId) {
        watchJob(savedJobId);
        return;
      }
      const payload = await api("/api/projects/jobs");
      const active = payload.jobs.find((job) => ["queued", "running"].includes(job.status));
      if (active) watchJob(active.jobId);
    }

    async function pollJob() {
      if (!state.jobId) return;
      let payload;
      try {
        payload = await api(`/api/projects/jobs?jobId=${encodeURIComponent(state.jobId)}`);
      } catch (error) {
        clearActiveJob();
        $("activeJob").classList.add("hidden");
        toast(error.message);
        return;
      }
      const job = payload.job;
      $("jobTitle").textContent = `正在创建 ${job.targetName}`;
      $("jobMessage").textContent = job.message;
      $("jobStage").textContent = job.stage;
      $("jobFill").style.width = `${job.progress}%`;
      renderDownloadProgress(job.download || {});
      document.querySelectorAll(".stage").forEach((stage) => stage.classList.toggle("current", stage.dataset.stage === job.stage));
      if (job.status === "completed" || job.status === "failed") {
        clearActiveJob();
        toast(job.status === "completed" ? "项目创建成功" : `项目创建失败：${job.error || job.message}`);
        await refresh();
        if (job.status === "completed" && job.server) openProject(job.server.targetName);
      }
    }

    function renderDownloadProgress(download) {
      const total = Number(download.total || 0);
      const completed = Number(download.completed || 0);
      const percent = Number(download.percent || 0);
      const visible = total > 0 || download.status === "running";
      $("downloadDetail").classList.toggle("hidden", !visible);
      if (!visible) return;
      $("downloadCount").textContent = `下载 ${completed}/${total}`;
      $("downloadPercent").textContent = `${percent}%`;
      $("downloadFill").style.width = `${percent}%`;
      $("downloadCurrent").textContent = download.current ? `当前：${download.current}` : "等待下载任务";
    }

    function formatBytes(value) {
      if (value === null || value === undefined) return "未知";
      const units = ["B", "KB", "MB", "GB"];
      let size = Number(value);
      let index = 0;
      while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
      return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    function formatDuration(seconds) {
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = seconds % 60;
      return h ? `${h}h ${m}m` : `${m}m ${s}s`;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }

    function escapeAttr(value) {
      return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    }

    $("openCreate").onclick = () => $("createDialog").showModal();
    $("createProject").onclick = (event) => runAction(() => createProject(event));
    $("download").onchange = updateCreateButton;
    $("acceptEula").onchange = updateCreateButton;
    $("refresh").onclick = () => runAction(refresh);
    $("showInternalProjects").onchange = () => { state.showInternal = $("showInternalProjects").checked; runAction(refresh); };
    $("backHome").onclick = () => routeHome();
    $("detailStart").onclick = () => state.selected && runAction(() => startServer(state.selected.targetName));
    $("detailStop").onclick = () => state.selected && runAction(() => stopServer(state.selected.targetName));
    $("detailDelete").onclick = () => state.selected && runAction(() => deleteProject(state.selected.targetName, state.selected.name));
    $("sendCommand").onclick = () => runAction(sendCommand);
    $("playerActionConfirm").onclick = () => runAction(confirmPlayerAction);
    $("saveProperties").onclick = () => runAction(saveProperties);
    $("saveKeySettings").onclick = () => runAction(saveKeySettings);
    $("addMod").onclick = () => runAction(addMod);
    $("createWorld").onclick = () => runAction(createWorld);
    $("consoleCommand").addEventListener("input", () => refreshCommandSuggestions().catch(() => {}));
    $("consoleCommand").addEventListener("keydown", (event) => { if (event.key === "Enter") runAction(sendCommand); if (event.key === "Escape") $("commandSuggestions").classList.add("hidden"); });
    $("logBody").addEventListener("scroll", onLogScroll);
    document.querySelectorAll(".tab").forEach((button) => button.onclick = () => setTab(button.dataset.tab));
    window.addEventListener("hashchange", () => applyRoute());
    setInterval(() => refresh().catch(() => {}), 5000);
    setInterval(() => refreshMetrics().catch(() => {}), 4000);
    setInterval(() => refreshLogs().catch(() => {}), 2000);
    setInterval(() => refreshPlayers().catch(() => {}), 3000);
    setInterval(() => loadMods().catch(() => {}), 8000);
    setInterval(() => loadWorlds().catch(() => {}), 8000);
    setInterval(() => loadFiles().catch(() => {}), 8000);
    updateCreateButton();
    if (!location.hash) location.hash = HOME_ROUTE;
    restoreActiveJob().catch((error) => toast(error.message));
    refresh().catch((error) => toast(error.message));
  </script>
  <script type="application/json" id="legacyPanelScript">
    const $ = (id) => document.getElementById(id);
    const state = { servers: [], selected: null, tab: "logs", jobId: "", jobTimer: null, showInternal: false };

    async function api(path, options = {}) {
      const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
      const response = await fetch(path, { headers, ...options });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || response.statusText);
      return payload;
    }

    function toast(message) {
      const item = document.createElement("div");
      item.className = "toast";
      item.textContent = message;
      $("toastStack").appendChild(item);
      setTimeout(() => item.remove(), 4200);
    }

    async function refresh() {
      const payload = await api(`/api/servers?includeInternal=${state.showInternal ? "true" : "false"}`);
      state.servers = payload.servers;
      renderHome();
      if (state.selected) {
        const latest = state.servers.find((server) => server.targetName === state.selected.targetName);
        if (latest) state.selected = latest;
        renderDetail();
      }
    }

    function renderHome() {
      $("metricTotal").textContent = state.servers.length;
      $("metricRunning").textContent = state.servers.filter((server) => server.runtimeStatus === "running").length;
      $("projectGrid").innerHTML = state.servers.map(cardTemplate).join("") || emptyProjects();
    }

    function cardTemplate(server) {
      return `<article class="project-card" onclick="openProject('${escapeAttr(server.targetName)}')">
        <div class="card-top">
          <div>
            <div class="project-title">${escapeHtml(server.name)}</div>
            <div class="subtle">${escapeHtml(server.targetName)}</div>
          </div>
          <span class="pill ${server.runtimeStatus}">${server.runtimeStatus}</span>
        </div>
        <div class="project-meta">
          <span class="pill">${escapeHtml(server.minecraftVersion)}</span>
          <span class="pill">${escapeHtml(server.loader)}</span>
          <span class="pill ${server.compatibilityLevel}">${escapeHtml(server.compatibilityLevel)}</span>
        </div>
        <div class="addr">${escapeHtml(server.connectAddress)}</div>
        <div class="card-actions">
          <button class="primary" onclick="event.stopPropagation(); runAction(() => startServer('${escapeAttr(server.targetName)}'))">启动</button>
          <button class="secondary" onclick="event.stopPropagation(); openProject('${escapeAttr(server.targetName)}')">详情</button>
          <button class="danger" onclick="event.stopPropagation(); runAction(() => stopServer('${escapeAttr(server.targetName)}'))">停止</button>
          <button class="danger" onclick="event.stopPropagation(); runAction(() => deleteProject('${escapeAttr(server.targetName)}'))">删除</button>
        </div>
      </article>`;
    }

    function emptyProjects() {
      return `<div class="panel"><h3>还没有正式项目</h3><p class="subtle">点击右上角创建项目，或打开“显示测试项目”查看开发验证目录。</p></div>`;
    }

    async function runAction(action) {
      try {
        await action();
      } catch (error) {
        toast(error.message);
      }
    }

    function openProject(targetName) {
      state.selected = state.servers.find((server) => server.targetName === targetName);
      if (!state.selected) return;
      $("homeView").classList.add("hidden");
      $("detailView").classList.remove("hidden");
      state.tab = "logs";
      setTab("logs");
      renderDetail();
      refreshLogs();
      refreshPlayers();
      loadProperties();
    }

    function renderDetail() {
      const server = state.selected;
      if (!server) return;
      $("detailTitle").textContent = server.name;
      $("detailMeta").textContent = `${server.targetName} · ${server.connectAddress}`;
      $("detailStatus").textContent = server.runtimeStatus;
      $("detailStatus").className = `pill ${server.runtimeStatus}`;
      $("detailBadges").innerHTML = `
        <span class="pill">${escapeHtml(server.minecraftVersion)}</span>
        <span class="pill">${escapeHtml(server.loader)}</span>
        <span class="pill ${server.compatibilityLevel}">${escapeHtml(server.compatibilityLevel)}</span>
        <span class="pill">人工项 ${server.manualActions}</span>
      `;
      $("detailPath").textContent = server.target;
    }

    async function startServer(targetName) {
      const payload = await api("/api/servers/start", {
        method: "POST",
        body: JSON.stringify({ targetName })
      });
      toast(`已发送启动命令: ${payload.server.connectAddress}`);
      await refresh();
      if (state.selected?.targetName === targetName) await refreshLogs();
    }

    async function stopServer(targetName) {
      await api("/api/servers/stop", {
        method: "POST",
        body: JSON.stringify({ targetName })
      });
      toast("已发送停止命令");
      await refresh();
    }

    async function deleteProject(targetName, displayName = "") {
      const server = state.servers.find((item) => item.targetName === targetName);
      const label = displayName || server?.name || targetName;
      if (!confirm(`确定删除项目“${label}”？这会停止服务器并删除整个项目目录。`)) return;
      await api("/api/servers/delete", {
        method: "POST",
        body: JSON.stringify({ targetName })
      });
      toast(`已删除项目: ${label}`);
      if (state.selected?.targetName === targetName) {
        $("detailView").classList.add("hidden");
        $("homeView").classList.remove("hidden");
        state.selected = null;
      }
      await refresh();
    }

    async function refreshLogs() {
      if (!state.selected || state.tab !== "logs") return;
      const payload = await api(`/api/servers/logs?targetName=${encodeURIComponent(state.selected.targetName)}&maxLines=500`);
      $("logBody").textContent = payload.log.lines.join("\n") || "暂无日志。";
      $("logBody").scrollTop = $("logBody").scrollHeight;
    }

    async function sendCommand() {
      if (!state.selected) return;
      const command = $("consoleCommand").value.trim();
      if (!command) return;
      await api("/api/servers/command", {
        method: "POST",
        body: JSON.stringify({ targetName: state.selected.targetName, command })
      });
      $("consoleCommand").value = "";
      toast(`已发送指令: ${command}`);
      await refreshLogs();
    }

    async function loadProperties() {
      if (!state.selected) return;
      const payload = await api(`/api/servers/properties?targetName=${encodeURIComponent(state.selected.targetName)}`);
      const props = payload.serverProperties.properties;
      $("propertiesEditor").value = Object.keys(props).sort().map((key) => `${key}=${props[key]}`).join("\n");
    }

    async function saveProperties() {
      if (!state.selected) return;
      const properties = {};
      $("propertiesEditor").value.split(/\r?\n/).forEach((line) => {
        const clean = line.trim();
        if (!clean || clean.startsWith("#") || !clean.includes("=")) return;
        const index = clean.indexOf("=");
        properties[clean.slice(0, index).trim()] = clean.slice(index + 1).trim();
      });
      await api("/api/servers/properties", {
        method: "POST",
        body: JSON.stringify({ targetName: state.selected.targetName, properties })
      });
      toast("server.properties 已保存");
      await refresh();
    }

    async function refreshPlayers() {
      if (!state.selected || state.tab !== "players") return;
      const payload = await api(`/api/servers/players?targetName=${encodeURIComponent(state.selected.targetName)}`);
      const players = payload.players.players;
      $("playersList").innerHTML = players.map((player) => `
        <div class="player-row">
          <strong>${escapeHtml(player.name)}</strong>
          <span class="subtle">${escapeHtml(player.gameMode)} · ${escapeHtml(player.status)}</span>
        </div>
      `).join("") || `<div class="subtle">当前没有从日志中识别到在线玩家。游戏模式需要 RCON 或插件才能稳定读取。</div>`;
    }

    function setTab(tab) {
      state.tab = tab;
      document.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
      $("tabLogs").classList.toggle("hidden", tab !== "logs");
      $("tabProperties").classList.toggle("hidden", tab !== "properties");
      $("tabPlayers").classList.toggle("hidden", tab !== "players");
      if (tab === "logs") refreshLogs();
      if (tab === "properties") loadProperties();
      if (tab === "players") refreshPlayers();
    }

    function updateCreateButton() {
      $("createProject").disabled = !$("download").checked || !$("acceptEula").checked;
    }

    async function createProject(event) {
      event.preventDefault();
      try {
        if (!$("download").checked || !$("acceptEula").checked) {
          throw new Error("请同时勾选自动下载远程模组文件和 Minecraft EULA 后再创建。");
        }
        const file = $("packFile").files[0];
        if (!file) throw new Error("请先选择 .mrpack 或 .zip 整合包文件。");
        const form = new FormData();
        form.append("packFile", file);
        form.append("projectName", $("projectName").value.trim());
        form.append("download", $("download").checked ? "true" : "false");
        form.append("acceptEula", $("acceptEula").checked ? "true" : "false");
        const payload = await api("/api/projects/upload", { method: "POST", body: form });
        $("createDialog").close();
        $("packFile").value = "";
        watchJob(payload.job.jobId);
        toast("项目创建任务已开始");
      } catch (error) {
        toast(error.message);
      }
    }

    function watchJob(jobId) {
      state.jobId = jobId;
      $("activeJob").classList.remove("hidden");
      if (state.jobTimer) clearInterval(state.jobTimer);
      pollJob();
      state.jobTimer = setInterval(pollJob, 1200);
    }

    async function pollJob() {
      if (!state.jobId) return;
      const payload = await api(`/api/projects/jobs?jobId=${encodeURIComponent(state.jobId)}`);
      const job = payload.job;
      $("jobTitle").textContent = `正在创建 ${job.targetName}`;
      $("jobMessage").textContent = job.message;
      $("jobStage").textContent = job.stage;
      $("jobFill").style.width = `${job.progress}%`;
      document.querySelectorAll(".stage").forEach((stage) => stage.classList.toggle("current", stage.dataset.stage === job.stage));
      if (job.status === "completed") {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        toast("项目创建成功");
        await refresh();
        if (job.server) openProject(job.server.targetName);
      }
      if (job.status === "failed") {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        toast(`项目创建失败: ${job.error || job.message}`);
      }
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[char]));
    }

    function escapeAttr(value) {
      return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    }

    $("openCreate").onclick = () => $("createDialog").showModal();
    $("createProject").onclick = createProject;
    $("download").onchange = updateCreateButton;
    $("acceptEula").onchange = updateCreateButton;
    $("refresh").onclick = refresh;
    $("showInternalProjects").onchange = () => {
      state.showInternal = $("showInternalProjects").checked;
      refresh().catch((error) => toast(error.message));
    };
    $("backHome").onclick = () => {
      $("detailView").classList.add("hidden");
      $("homeView").classList.remove("hidden");
      state.selected = null;
    };
    $("detailStart").onclick = () => state.selected && runAction(() => startServer(state.selected.targetName));
    $("detailStop").onclick = () => state.selected && runAction(() => stopServer(state.selected.targetName));
    $("detailDelete").onclick = () => state.selected && runAction(() => deleteProject(state.selected.targetName, state.selected.name));
    $("sendCommand").onclick = () => runAction(sendCommand);
    $("consoleCommand").addEventListener("keydown", (event) => {
      if (event.key === "Enter") runAction(sendCommand);
    });
    $("saveProperties").onclick = () => runAction(saveProperties);
    document.querySelectorAll(".tab").forEach((button) => button.onclick = () => setTab(button.dataset.tab));
    setInterval(() => refresh().catch(() => {}), 5000);
    setInterval(() => refreshLogs().catch(() => {}), 2000);
    setInterval(() => refreshPlayers().catch(() => {}), 3000);
    updateCreateButton();
    refresh().catch((error) => toast(error.message));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
