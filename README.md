# Pack2Serve

Pack2Serve is a planned web panel for importing Minecraft modpacks, parsing their structure, generating dedicated server projects, and hosting them directly from the panel.

## Current Repository Contents

- `docs/analysis/reports/` - Markdown reports from real modpack samples.
- `docs/analysis/manifests/` - Remote dependency manifest CSV exports.
- `docs/analysis/overrides/` - Override-layer file inventory CSV exports.
- `pack2serve/` - Python backend core for parsing and generating server project candidates.
- `tests/` - Standard-library unit tests.

## Early Product Direction

The project should support Modrinth `.mrpack`, CurseForge exported `.zip`, and loader-specific server generation for Forge, Fabric, and NeoForge.

## CurseForge Download Direction

For the MVP, Pack2Serve will use a no-key, mirror-first strategy for CurseForge ZIP imports. CurseForge `manifest.json` files will be parsed into `projectID` and `fileID` entries, then resolved through configurable mirror providers and cached locally.

See `docs/architecture/0001-curseforge-no-key-mirror-strategy.md` for the current decision record.

## Current CLI

```powershell
python -m pack2serve.cli inspect "C:\path\to\modpack.mrpack"
python -m pack2serve.cli build "C:\path\to\modpack.mrpack" --target "data\servers\example"
```

Download Modrinth remote files while building:

```powershell
python -m pack2serve.cli build "C:\path\to\modpack.mrpack" --target "data\servers\example" --download
```

Use a no-key CurseForge mirror template:

```powershell
python -m pack2serve.cli build "C:\path\to\modpack.zip" --target "data\servers\example" --download --curseforge-mirror "https://mirror.example/curseforge/{projectID}/{fileID}/file.jar"
```

Download the loader artifact for a generated server project:

```powershell
python -m pack2serve.cli install-loader "data\servers\example"
```

Run Forge/NeoForge installer jars after downloading them:

```powershell
python -m pack2serve.cli install-loader "data\servers\example" --execute-installers
```

Run the full local pipeline:

```powershell
python -m pack2serve.cli prepare "C:\path\to\modpack.mrpack" --target "data\servers\example" --download --validate
```

Validate an existing generated server:

```powershell
python -m pack2serve.cli validate-server "data\servers\example" --timeout 120
```

See `docs/development/backend-mvp-status.md` for the current implementation status.
