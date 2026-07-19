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
- Generate `pack2serve/java-runtime-install-plan.json` for a project-local portable JRE.
- Install a portable Java runtime with `install-java`.
- Rewrite `start.ps1` to use the project-local Java executable after runtime installation.
- Safely extract Java runtime archives with path traversal checks.
- Download Modrinth remote files through their `downloads[]` URLs when `--download` is enabled.
- Cache downloaded artifacts under `data/cache`.
- Verify Modrinth downloads with SHA1/SHA512 and expected file size when present.
- Resolve CurseForge files through configurable no-key mirror URL templates when `--curseforge-mirror` is provided.
- Create manual action items for CurseForge files when no mirror provider resolves them.
- Generate loader-specific install plans for Fabric, Forge, and NeoForge.
- Fabric plans use the Fabric metadata API server launcher jar.
- Forge plans use the MinecraftForge Maven installer jar.
- NeoForge plans use the NeoForged Maven installer jar.
- Download loader artifacts with `install-loader`.
- Rewrite `start.ps1` automatically after direct server jar installation.
- Optionally execute Forge/NeoForge installer jars with `--execute-installers`.
- Validate generated servers with `validate-server`.
- Run build, loader install, and optional validation with `prepare`.
- Write `pack2serve/validation-report.json` and `logs/pack2serve-validation.log`.
- Detect `started`, `needs-eula`, `failed`, `crashed`, and `timed-out` validation states.
- Stream validation output and stop a long-running server after the Minecraft `Done (...)! For help` startup marker.
- Explicitly accept Minecraft EULA with `accept-eula --i-agree`.

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

Download/install the generated loader plan:

```powershell
python -m pack2serve.cli install-loader "data\servers\example"
python -m pack2serve.cli install-loader "data\servers\example" --execute-installers
```

Install the generated portable Java runtime plan:

```powershell
python -m pack2serve.cli install-java "data\servers\example"
```

Run the full local pipeline:

```powershell
python -m pack2serve.cli prepare "C:\path\to\modpack.mrpack" --target "data\servers\example" --download --install-java --validate
```

Validate an existing generated server:

```powershell
python -m pack2serve.cli validate-server "data\servers\example" --timeout 120
```

Explicitly accept the Minecraft EULA:

```powershell
python -m pack2serve.cli accept-eula "data\servers\example" --i-agree
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

## Runtime Verification

The `Into the Backrooms` Fabric sample has been through a real local first-start check:

- `install-loader` downloaded the Fabric server launcher jar.
- `validate-server` first reported `needs-eula`.
- `accept-eula --i-agree` updated `eula.txt` explicitly.
- `validate-server --timeout 120` then reached `status: started` and stopped the server cleanly after Minecraft reported startup completion.

## Current Limits

- Modrinth direct download is implemented, but it is only executed when `--download` is enabled.
- CurseForge no-key mirror resolution supports template providers, but no default public mirror is bundled yet.
- Loader artifacts can be downloaded, but Forge/NeoForge installer execution is only run when `--execute-installers` is provided.
- Minecraft EULA acceptance is explicit; Pack2Serve will not automatically accept it without `accept-eula --i-agree`.
- Java runtime installation is implemented as a project-local portable JRE flow, but it has not yet been run against a real Adoptium download in CI.

## Next Development Step

Implement installer execution and runtime hosting:

1. Real Adoptium Java runtime download verification
2. Forge/NeoForge installer execution validation with real packs
3. client-only mod detection database
4. web API and panel integration
5. runtime process/container manager
