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
            if route == "/":
                self._send_html(PANEL_HTML)
                return
            if route == "/api/servers":
                self._send_json({"servers": service.list_servers()})
                return
            if route == "/api/servers/logs":
                query = parse_qs(parsed.query)
                target_name = query.get("targetName", [""])[0]
                max_lines = int(query.get("maxLines", ["200"])[0])
                self._send_json({"log": service.server_log_tail(target_name, max_lines=max_lines)})
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

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
                if route == "/api/servers/start":
                    self._send_json({"server": service.start_server(payload["targetName"])})
                    return
                if route == "/api/servers/stop":
                    self._send_json({"server": service.stop_server(payload["targetName"])})
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
  <title>Pack2Serve</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f4ef;
      --ink: #1b1f23;
      --muted: #65717c;
      --line: #d8d3c8;
      --panel: #fffdf8;
      --accent: #1c6f6b;
      --accent-2: #b6462d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Segoe UI", Arial, sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      padding: 22px 28px;
      border-bottom: 1px solid var(--line);
      background: #fbfaf6;
    }
    h1 { margin: 0; font-size: 22px; font-weight: 750; }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 420px) 1fr;
      min-height: calc(100vh - 72px);
    }
    section {
      padding: 24px 28px;
      border-right: 1px solid var(--line);
    }
    section:last-child { border-right: 0; }
    h2 { margin: 0 0 16px; font-size: 16px; }
    label {
      display: grid;
      gap: 7px;
      margin-bottom: 14px;
      color: var(--muted);
      font-size: 13px;
    }
    input, textarea {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      padding: 9px 11px;
      font: inherit;
    }
    textarea { min-height: 88px; resize: vertical; }
    .row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 14px 0 20px;
    }
    .row input { width: 18px; min-height: 18px; }
    button {
      min-height: 40px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: white;
      padding: 9px 14px;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary { background: #2f3b43; }
    button.danger { background: #9d2f22; }
    button:disabled { cursor: progress; opacity: .62; }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 11px 12px;
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }
    th { color: var(--muted); font-size: 12px; font-weight: 700; }
    tr:last-child td { border-bottom: 0; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 3px 9px;
      background: #e6e0d5;
      color: #394149;
      font-weight: 700;
      font-size: 12px;
    }
    .status-pill.running { background: #d8eee7; color: #166047; }
    .status-pill.starting, .status-pill.stopping { background: #fff0c7; color: #77520b; }
    .status-pill.crashed, .status-pill.failed { background: #f4d8d2; color: #8d2b1f; }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .actions button { min-height: 34px; padding: 7px 10px; }
    .addr {
      font-family: Consolas, "Cascadia Mono", monospace;
      font-size: 13px;
      white-space: nowrap;
    }
    .status {
      min-height: 22px;
      margin-top: 14px;
      color: var(--accent-2);
      font-size: 13px;
      white-space: pre-wrap;
    }
    .log-panel {
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #171b1f;
      color: #e7efe9;
      overflow: hidden;
    }
    .log-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid #303942;
      color: #c7d1ca;
      font-size: 12px;
    }
    .log-body {
      height: 360px;
      overflow: auto;
      margin: 0;
      padding: 12px;
      font: 12px/1.45 Consolas, "Cascadia Mono", monospace;
      white-space: pre-wrap;
      word-break: break-word;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      section { border-right: 0; border-bottom: 1px solid var(--line); }
      th, td { padding: 9px 8px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Pack2Serve</h1>
    <button class="secondary" id="refresh">刷新</button>
  </header>
  <main>
    <section>
      <h2>导入整合包</h2>
      <label>整合包路径
        <input id="packPath" placeholder="C:\\path\\to\\modpack.mrpack">
      </label>
      <label>服务端目录名
        <input id="targetName" placeholder="my-server">
      </label>
      <label>CurseForge 镜像模板
        <textarea id="mirrors" placeholder="https://mirror.example/curseforge/{projectID}/{fileID}/file.jar"></textarea>
      </label>
      <div class="row">
        <input id="download" type="checkbox">
        <span>下载可解析远程文件</span>
      </div>
      <button id="import">导入</button>
      <div class="status" id="status"></div>
    </section>
    <section>
      <div class="toolbar">
        <h2>已验证服务端</h2>
      </div>
      <table>
        <thead>
          <tr>
            <th>目录</th>
            <th>整合包</th>
            <th>版本</th>
            <th>Loader</th>
            <th>连接地址</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody id="servers"></tbody>
      </table>
      <div class="log-panel">
        <div class="log-head">
          <strong id="logTitle">实时日志</strong>
          <span id="logMeta">请选择一个服务端</span>
        </div>
        <pre class="log-body" id="logBody">点击表格里的“日志”，或点击“启动”后自动显示该服务端日志。</pre>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let selectedLogTarget = "";
    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || response.statusText);
      return payload;
    }
    async function refresh() {
      const payload = await api("/api/servers");
      const verifiedPrefix = "startup-verification-2026-07-21/";
      const servers = payload.servers.filter((server) => server.targetName.startsWith(verifiedPrefix));
      $("servers").innerHTML = servers.map((server) => `
        <tr>
          <td>${server.targetName}</td>
          <td>${server.name}</td>
          <td>${server.minecraftVersion}</td>
          <td>${server.loader}</td>
          <td><span class="addr">${server.connectAddress}</span></td>
          <td>
            <span class="status-pill ${server.runtimeStatus}">${server.runtimeStatus}</span>
            ${server.pid ? `<div>PID ${server.pid}</div>` : ""}
          </td>
          <td>
            <div class="actions">
              <button onclick="startServer('${escapeAttr(server.targetName)}')">启动</button>
              <button class="secondary" onclick="showLogs('${escapeAttr(server.targetName)}')">日志</button>
              <button class="danger" onclick="stopServer('${escapeAttr(server.targetName)}')">停止</button>
            </div>
          </td>
        </tr>
      `).join("") || `<tr><td colspan="7">没有找到 startup-verification-2026-07-21 这批测试服务端。</td></tr>`;
    }
    function escapeAttr(value) {
      return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    }
    async function startServer(targetName) {
      selectedLogTarget = targetName;
      await refreshLogs();
      $("status").textContent = `正在启动 ${targetName}...`;
      try {
        const payload = await api("/api/servers/start", {
          method: "POST",
          body: JSON.stringify({ targetName })
        });
        $("status").textContent = `已发送启动命令：${payload.server.connectAddress}`;
        await refresh();
        await refreshLogs();
      } catch (error) {
        $("status").textContent = error.message;
      }
    }
    async function showLogs(targetName) {
      selectedLogTarget = targetName;
      await refreshLogs();
    }
    async function refreshLogs() {
      if (!selectedLogTarget) return;
      const payload = await api(`/api/servers/logs?targetName=${encodeURIComponent(selectedLogTarget)}&maxLines=300`);
      $("logTitle").textContent = selectedLogTarget;
      $("logMeta").textContent = `${payload.log.runtimeStatus} · ${payload.log.connectAddress}`;
      $("logBody").textContent = payload.log.lines.join("\n") || "暂无日志。";
      $("logBody").scrollTop = $("logBody").scrollHeight;
    }
    async function stopServer(targetName) {
      $("status").textContent = `正在停止 ${targetName}...`;
      try {
        await api("/api/servers/stop", {
          method: "POST",
          body: JSON.stringify({ targetName })
        });
        $("status").textContent = `已发送停止命令：${targetName}`;
        await refresh();
        await refreshLogs();
      } catch (error) {
        $("status").textContent = error.message;
      }
    }
    $("refresh").onclick = refresh;
    setInterval(() => refresh().catch(() => {}), 5000);
    setInterval(() => refreshLogs().catch(() => {}), 2000);
    $("import").onclick = async () => {
      $("status").textContent = "处理中...";
      try {
        const mirrors = $("mirrors").value.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);
        const payload = await api("/api/import", {
          method: "POST",
          body: JSON.stringify({
            packPath: $("packPath").value.trim(),
            targetName: $("targetName").value.trim(),
            download: $("download").checked,
            curseforgeMirrors: mirrors
          })
        });
        $("status").textContent = `已导入 ${payload.server.targetName}`;
        await refresh();
      } catch (error) {
        $("status").textContent = error.message;
      }
    };
    refresh().catch((error) => { $("status").textContent = error.message; });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
