<h1 align="center">Pack2Serve</h1>

<p align="center">
  Import a Minecraft modpack file, generate a ready-to-run dedicated server folder, and start hosting from the browser. In the fastest local path, Pack2Serve can go from only a modpack archive to a running server in about 2 minutes.
</p>

<p align="center">
  <a href="README.zh-CN.md">中文</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="#features">Features</a> ·
  <a href="#status-and-limits">Status</a>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-205A4B?style=for-the-badge&logo=python&logoColor=white" />
  <img alt="Minecraft" src="https://img.shields.io/badge/Minecraft-server%20panel-1F7A64?style=for-the-badge" />
  <img alt="Modpacks" src="https://img.shields.io/badge/Modrinth%20%2B%20CurseForge-imports-6B7FD7?style=for-the-badge" />
  <img alt="Status" src="https://img.shields.io/badge/status-active%20prototype-F0B429?style=for-the-badge" />
</p>

<p align="center">
  <img src=".github/assets/readme-hero-unified.svg" alt="Pack2Serve project overview image" />
</p>

## Screenshots

<table>
  <tr>
    <td colspan="2" align="center">
      <img src=".github/assets/screenshots/dashboard-projects.png" alt="Pack2Serve dashboard with project cards and build progress" />
      <br />
      <strong>Project dashboard.</strong> Import a pack, watch build and validation progress, copy the connection address, and start or stop servers.
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/runtime-overview.png" alt="Runtime overview tab with address, uptime, disk, and memory metrics" />
      <br />
      <strong>Runtime overview.</strong> Track status, uptime, world size, project size, memory usage, and connection address.
    </td>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/live-console.png" alt="Live Minecraft server console and command input" />
      <br />
      <strong>Live console.</strong> View server logs and send Minecraft console commands from the browser.
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/player-management.png" alt="Online player management panel with position and action buttons" />
      <br />
      <strong>Player tools.</strong> Refresh player state, inspect position, and send common moderation commands.
    </td>
    <td width="50%" align="center">
      <img src=".github/assets/screenshots/server-properties.png" alt="Server properties editor with Chinese labels and raw text editor" />
      <br />
      <strong>Server settings.</strong> Edit common server.properties values through Chinese fields or the raw properties file.
    </td>
  </tr>
</table>

## Why This Exists

Minecraft modpacks are easy to launch as a client profile, but turning that same `.mrpack` or CurseForge exported `.zip` into a dedicated server folder is still awkward: remote mod files need to be resolved, client-only files need to be separated, loader installers need to run, Java versions need to match, EULA must be explicit, and startup failures need readable logs.

Pack2Serve is built for local server hosts who want a panel-driven flow from a single modpack file to a playable server:

1. Choose a `.mrpack` or CurseForge exported `.zip` directly in the web panel.
2. Click once to parse the pack, download resolvable remote files, copy server resources, install Java and loader assets, write EULA only after consent, and run startup validation.
3. Get a generated server project folder, start the server from the project card, and manage logs, players, worlds, mods, files, and settings from the browser.

For small packs or already cached dependencies, the fastest path is roughly 2 minutes from "I only have the modpack file" to "the server is running." Larger packs still depend on download speed, loader installation, and startup validation time.

## Features

- Import Modrinth `.mrpack` archives and CurseForge exported `.zip` modpacks.
- Parse pack metadata, Minecraft version, loader type, loader version, remote mod entries, and override files.
- Download Modrinth files directly and resolve CurseForge files through pluggable no-key mirror providers where available.
- Cache downloaded artifacts under `data/cache` and reuse them across builds.
- Generate server project directories with `mods/`, copied server resources, `server.properties`, `eula.txt`, start scripts, and Pack2Serve reports.
- Install project-local Java runtimes based on the Minecraft version requirement.
- Install Fabric, Forge, and NeoForge server loader artifacts, including Forge/NeoForge installer execution when enabled.
- Run startup validation and write `pack2serve/validation-report.json` plus `logs/pack2serve-validation.log`.
- Start, stop, and monitor generated servers from the web panel.
- Show live logs, command input, project routes, server status, disk usage, memory usage, world time, and connection addresses.
- Manage `server.properties` with common Chinese-labeled fields and a raw text editor.
- List online players from logs and command probes, with common actions such as OP, gamemode, teleport, ban, kill, and clear inventory.
- Browse project files, manage worlds, back up worlds, and inspect/enable/disable/delete mod files.
- Hide validation/test projects from the main dashboard unless explicitly requested.

## Compatibility Model

Pack2Serve aims to produce a dedicated server that is functionally close to opening the same modpack for multiplayer, but Minecraft modpacks are not perfectly uniform. The builder uses a layered approach:

- Archive parser: detects Modrinth and CurseForge pack structure.
- Artifact resolver: downloads remote files or records clear manual actions when a file cannot be resolved.
- Server filter: copies server-oriented overrides and isolates known client-only files.
- Loader installer: creates the proper Fabric, Forge, or NeoForge launch path.
- Runtime validator: starts the generated server, watches the logs, and records whether it reached the Minecraft startup marker.

The reports in `docs/analysis/` and `docs/development/` contain real-pack analysis and verification notes.

## Quickstart

Prerequisites:

- Windows, PowerShell, and Python 3.10 or newer.
- Network access if Java, loader artifacts, or remote mod files need to be downloaded.
- A local Minecraft modpack archive.

Run the web panel:

```powershell
git clone https://github.com/Ha22yX/Pack2Serve.git
cd Pack2Serve
python -m pack2serve.cli serve-panel --host 127.0.0.1 --port 8766 --workspace data
```

Or use the included PowerShell launcher:

```powershell
.\scripts\start-panel.ps1 -HostName 127.0.0.1 -Port 8766 -Workspace data
```

Then open:

```text
http://127.0.0.1:8766/
```

## Panel Workflow

1. Click `创建项目`.
2. Select a `.mrpack` or CurseForge `.zip` modpack file directly.
3. Enter the project name.
4. Confirm that you have read and accepted the Minecraft EULA and allow Pack2Serve to download resolvable remote mod files.
5. Wait for Pack2Serve to generate the server folder: parsing, downloads, Java setup, loader setup, EULA writing, startup validation, and summary generation are shown in the build progress.
6. Open the project card, click start, copy the connection address, and manage logs, players, worlds, mods, files, and settings from the same panel.

## CLI Usage

Inspect a pack:

```powershell
python -m pack2serve.cli inspect "C:\path\to\modpack.mrpack"
```

Prepare a complete server project:

```powershell
python -m pack2serve.cli prepare "C:\path\to\modpack.mrpack" --target "data\servers\example" --download --install-java --validate
```

Build a CurseForge ZIP with bundled no-key providers:

```powershell
python -m pack2serve.cli build "C:\path\to\modpack.zip" --target "data\servers\example" --download
```

Validate an existing generated server:

```powershell
python -m pack2serve.cli validate-server "data\servers\example" --timeout 120
```

Accept the Minecraft EULA explicitly:

```powershell
python -m pack2serve.cli accept-eula "data\servers\example" --i-agree
```

## Tech Stack

| Layer | Technology | Purpose |
| --- | --- | --- |
| Backend | Python standard library | Pack parsing, file generation, process control, HTTP API, and panel serving |
| Web UI | Server-rendered HTML, CSS, and vanilla JavaScript | Local browser panel without a frontend build step |
| Minecraft runtime | Fabric, Forge, NeoForge, Java runtimes | Dedicated server installation and startup |
| Storage | Local filesystem under `data/` | Uploaded packs, generated servers, caches, logs, and reports |
| Tests | `unittest` | Core parser, builder, panel, validation, and process-management coverage |

## Project Structure

```text
pack2serve/                 Python package
  builder.py                Server project generation
  parser.py                 Modrinth and CurseForge archive parsing
  downloader.py             Artifact cache and download providers
  installer.py              Fabric, Forge, and NeoForge installer planning/execution
  java.py                   Java version detection and portable runtime setup
  validator.py              Startup validation and client-only repair helpers
  panel.py                  Panel service layer and server process management
  web.py                    Local HTTP panel and browser UI
docs/                       Architecture notes, pack analysis, and verification reports
scripts/start-panel.ps1     Convenience launcher for the local panel
tests/                      Standard-library unit tests
data/                       Local runtime workspace, ignored by git
```

## Status And Limits

Pack2Serve is an active prototype. It has been developed against real Modrinth and CurseForge samples, but it should still be treated as a local tool that validates each generated server instead of assuming every modpack is automatically equivalent.

Known limits:

- CurseForge no-key mode depends on third-party mirror coverage. Some files may still need manual handling.
- Some client-only mods can only be discovered after a dedicated-server crash report; Pack2Serve can isolate certain known invalid distribution cases and retry validation.
- Player position and rotation can be probed through server commands, but stable inventory, respawn-point, and deep player state inspection may require RCON support or a companion server-side mod.
- Forge/NeoForge installer behavior can vary by Minecraft generation and mod loader release.
- Public hosting, authentication, permissions, and multi-user security are not the current focus; this is designed as a local administration panel.

## Development

Run the test suite:

```powershell
python -m unittest tests.test_pack2serve_core
```

Check the current architecture notes:

- `docs/architecture/0001-curseforge-no-key-mirror-strategy.md`
- `docs/development/backend-mvp-status.md`
- `docs/development/startup-verification-2026-07-21.md`

## License

No license file has been added yet.
