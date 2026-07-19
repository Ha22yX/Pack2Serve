# Backend MVP Status

## Implemented

The first Python backend core is in place.

Current capabilities:

- Detect Modrinth `.mrpack` archives through `modrinth.index.json`.
- Detect CurseForge exported ZIP archives through `manifest.json`.
- Parse Minecraft version, pack name, pack version, loader name, and loader version.
- Parse Modrinth remote files with target path, downloads, hashes, size, and env metadata.
- Parse CurseForge remote files with `projectID`, `fileID`, and `required`.
- Generate a server project candidate directory.
- Copy server-oriented override content into the generated server project.
- Isolate obvious client-only content into `_client-overrides/`.
- Map a single `saves/<world>` template to `world/`.
- Generate `eula.txt`, `server.properties`, `start.ps1`, `pack2serve/build-report.json`, `pack2serve/download-plan.json`, `pack2serve/java-plan.json`, and `pack2serve/loader-install-plan.json`.
- Plan Java major version requirements from Minecraft version.
- Detect local Java runtime and report whether it matches the recommended major version.
- Download Modrinth remote files through their `downloads[]` URLs when `--download` is enabled.
- Cache downloaded artifacts under `data/cache`.
- Verify Modrinth downloads with SHA1/SHA512 and expected file size when present.
- Resolve CurseForge files through configurable no-key mirror URL templates when `--curseforge-mirror` is provided.
- Create manual action items for CurseForge files when no mirror provider resolves them.
- Generate loader-specific install plans for Fabric, Forge, and NeoForge.
- Fabric plans use the Fabric metadata API server launcher jar.
- Forge plans use the MinecraftForge Maven installer jar.
- NeoForge plans use the NeoForged Maven installer jar.

## CLI

Inspect a pack:

```powershell
python -m pack2serve.cli inspect "C:\path\to\modpack.mrpack"
```

Build a server project candidate:

```powershell
python -m pack2serve.cli build "C:\path\to\modpack.mrpack" --target "data\servers\example"
```

Build and download remote Modrinth artifacts:

```powershell
python -m pack2serve.cli build "C:\path\to\modpack.mrpack" --target "data\servers\example" --download
```

Build a CurseForge ZIP with a no-key mirror template:

```powershell
python -m pack2serve.cli build "C:\path\to\modpack.zip" --target "data\servers\example" --download --curseforge-mirror "https://mirror.example/curseforge/{projectID}/{fileID}/file.jar"
```

## Integration Samples

The following sample packs were parsed and built into `data/servers/integration/`:

| Sample | Format | Loader | Remote files | Copied overrides | Manual actions |
|---|---|---|---:|---:|---:|
| BattleArmory TACZ | Modrinth | Forge 47.4.20 | 90 | 1619 | 0 |
| 乌托邦探险之旅 | Modrinth | Fabric Loader 0.18.4 | 413 | 2694 | 0 |
| RLCraft | CurseForge | Forge 14.23.5.2860 | 187 | 3381 | 187 |
| Into the Backrooms | CurseForge | Fabric 0.18.4 | 36 | 39 | 36 |
| Re-Console LTS NeoForge | Modrinth | NeoForge 21.1.233 | 115 | 1855 | 0 |

## Current Limits

- Modrinth direct download is implemented, but it is only executed when `--download` is enabled.
- CurseForge no-key mirror resolution supports template providers, but no default public mirror is bundled yet.
- Loader installers are planned in `loader-install-plan.json`, including download URL and install command, but installer execution is not automated yet.
- `start.ps1` is a placeholder that expects `server.jar` to exist after loader installation.
- Java is detected locally, but Pack2Serve does not yet install Java distributions.

## Next Development Step

Implement installer execution and runtime hosting:

1. download loader installer/server launcher artifacts
2. execute Fabric/Forge/NeoForge installer flows
3. rewrite `start.ps1` after installer completion
4. Java distribution installer/selector
5. first-run validation and log analysis
6. web API and panel integration
