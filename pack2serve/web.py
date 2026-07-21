from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pack2serve.panel import PanelService


def serve(host: str = "127.0.0.1", port: int = 8765, workspace_dir: str | Path = "data") -> None:
    service = PanelService(workspace_dir=workspace_dir, advertise_host=host)

    class Pack2ServeHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            try:
                if route == "/":
                    self._send_html(PANEL_HTML)
                    return
                if route == "/api/servers":
                    self._send_json({"servers": service.list_servers()})
                    return
                if route == "/api/projects/jobs":
                    self._send_json({"job": service.project_job(query.get("jobId", [""])[0])})
                    return
                if route == "/api/servers/logs":
                    target_name = query.get("targetName", [""])[0]
                    max_lines = int(query.get("maxLines", ["300"])[0])
                    self._send_json({"log": service.server_log_tail(target_name, max_lines=max_lines)})
                    return
                if route == "/api/servers/properties":
                    self._send_json({"serverProperties": service.server_properties(query.get("targetName", [""])[0])})
                    return
                if route == "/api/servers/players":
                    self._send_json({"players": service.server_players(query.get("targetName", [""])[0])})
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            try:
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
    print(f"Pack2Serve panel listening on http://{host}:{server.server_port}")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pack2serve-panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.workspace)
    return 0


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
    input, textarea {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffefa;
      color: var(--ink);
      padding: 9px 11px;
      outline: none;
    }
    input:focus, textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgb(22 114 95 / .14); }
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
      grid-template-columns: repeat(4, minmax(0, 1fr));
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
    .stage-list { display: grid; gap: 8px; margin-top: 12px; }
    .stage { display: flex; justify-content: space-between; color: var(--muted); font-size: 13px; }
    .stage.current { color: var(--ink); font-weight: 760; }
    .properties-editor { min-height: 430px; font: 13px/1.45 Consolas, "Cascadia Mono", monospace; }
    .players { display: grid; gap: 8px; }
    .player-row { display: flex; justify-content: space-between; border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fffefa; }
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
          <div class="metric"><span class="subtle">等价验证</span><strong id="metricVerified">0</strong></div>
          <div class="metric"><span class="subtle">需复核</span><strong id="metricReview">0</strong></div>
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
          <div class="stage-list">
            <div class="stage" data-stage="inspect"><span>读取整合包</span><span>08%</span></div>
            <div class="stage" data-stage="build"><span>解析与复制</span><span>22%</span></div>
            <div class="stage" data-stage="eula"><span>写入 EULA</span><span>76%</span></div>
            <div class="stage" data-stage="finalize"><span>生成摘要</span><span>92%</span></div>
            <div class="stage" data-stage="complete"><span>创建完成</span><span>100%</span></div>
          </div>
        </div>
        <div class="section-head">
          <div>
            <h2>服务器项目</h2>
            <div class="subtle">点击卡片进入详情面板</div>
          </div>
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
          </div>
        </div>
        <div class="detail-layout">
          <div class="panel">
            <div class="tabs">
              <button class="tab active" data-tab="logs">日志控制台</button>
              <button class="tab" data-tab="properties">服务器参数</button>
              <button class="tab" data-tab="players">在线玩家</button>
            </div>
            <div id="tabLogs">
              <pre class="log-box" id="logBody">暂无日志。</pre>
              <div class="console-row">
                <input id="consoleCommand" placeholder="输入控制台指令，例如 say hello 或 list">
                <button class="primary" id="sendCommand">发送</button>
              </div>
            </div>
            <div id="tabProperties" class="hidden">
              <textarea class="properties-editor" id="propertiesEditor" spellcheck="false"></textarea>
              <div class="card-actions"><button class="primary" id="saveProperties">保存 server.properties</button></div>
            </div>
            <div id="tabPlayers" class="hidden">
              <div class="players" id="playersList"></div>
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
        <label>整合包路径
          <input id="packPath" placeholder="C:\Users\Administrator\Downloads\example.mrpack">
        </label>
        <label>项目名称
          <input id="projectName" placeholder="例如 RLCraft 测试服">
        </label>
        <label>CurseForge 镜像模板
          <textarea id="mirrors" rows="3" placeholder="可选，每行一个 {projectID}/{fileID} 模板"></textarea>
        </label>
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
        <button class="primary" id="createProject" value="default">创建</button>
      </div>
    </form>
  </dialog>
  <div class="toast-stack" id="toastStack"></div>

  <script>
    const $ = (id) => document.getElementById(id);
    const state = { servers: [], selected: null, tab: "logs", jobId: "", jobTimer: null };

    async function api(path, options = {}) {
      const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
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
      const payload = await api("/api/servers");
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
      $("metricVerified").textContent = state.servers.filter((server) => server.serverEquivalent).length;
      $("metricReview").textContent = state.servers.filter((server) => !server.serverEquivalent).length;
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
        </div>
      </article>`;
    }

    function emptyProjects() {
      return `<div class="panel"><h3>还没有项目</h3><p class="subtle">点击右上角创建项目，导入 mrpack 或 CurseForge zip。</p></div>`;
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

    async function createProject(event) {
      event.preventDefault();
      try {
        const mirrors = $("mirrors").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
        const payload = await api("/api/projects", {
          method: "POST",
          body: JSON.stringify({
            packPath: $("packPath").value.trim(),
            projectName: $("projectName").value.trim(),
            download: $("download").checked,
            acceptEula: $("acceptEula").checked,
            curseforgeMirrors: mirrors
          })
        });
        $("createDialog").close();
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
    $("refresh").onclick = refresh;
    $("backHome").onclick = () => {
      $("detailView").classList.add("hidden");
      $("homeView").classList.remove("hidden");
      state.selected = null;
    };
    $("detailStart").onclick = () => state.selected && runAction(() => startServer(state.selected.targetName));
    $("detailStop").onclick = () => state.selected && runAction(() => stopServer(state.selected.targetName));
    $("sendCommand").onclick = () => runAction(sendCommand);
    $("consoleCommand").addEventListener("keydown", (event) => {
      if (event.key === "Enter") runAction(sendCommand);
    });
    $("saveProperties").onclick = () => runAction(saveProperties);
    document.querySelectorAll(".tab").forEach((button) => button.onclick = () => setTab(button.dataset.tab));
    setInterval(() => refresh().catch(() => {}), 5000);
    setInterval(() => refreshLogs().catch(() => {}), 2000);
    setInterval(() => refreshPlayers().catch(() => {}), 4000);
    refresh().catch((error) => toast(error.message));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
