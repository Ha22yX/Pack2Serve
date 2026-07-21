import json
import hashlib
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pack2serve.builder import ServerBuilder
from pack2serve.cli import main
from pack2serve.compatibility import audit_generated_server
from pack2serve.downloader import ArtifactCache, CurseForgeTemplateMirrorProvider, ModrinthDirectProvider
from pack2serve.java import (
    JavaInstaller,
    JavaRuntimeInstallPlan,
    create_java_runtime_install_plan,
    java_status,
    required_java_major,
)
from pack2serve.loader import LoaderInstallPlan, create_loader_install_plan
from pack2serve.installer import LoaderInstaller
from pack2serve.panel import PanelService
from pack2serve.parser import ModpackFormat, parse_modpack
from pack2serve.validator import ServerValidator
from pack2serve.web import (
    MAX_UPLOAD_BYTES,
    PANEL_HTML,
    UploadedFormFile,
    _parse_multipart_form,
    _safe_upload_name,
    _uploaded_project_name,
    _validate_upload_length,
)


def write_zip(path: Path, files: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _write_minimal_build_report(server_dir: Path, *, name: str) -> None:
    report = {
        "pack": {
            "source_path": "sample.mrpack",
            "format": "modrinth",
            "name": name,
            "version": "1.0.0",
            "minecraft_version": "1.20.1",
            "loader": {"name": "fabric-loader", "version": "0.18.4"},
            "override_root": "overrides",
            "remote_files": [],
        },
        "target_dir": str(server_dir),
        "java": {"required_major": 17, "detected_major": None, "detected_path": None, "status": "missing"},
        "downloads": [],
        "copied_overrides": [],
        "manual_actions": [],
        "curseforge_resolution": None,
    }
    (server_dir / "pack2serve/build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class Pack2ServeCoreTests(unittest.TestCase):
    def test_parse_modrinth_mrpack_reads_dependencies_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Sample MR",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.21.1", "neoforge": "21.1.233"},
                            "files": [
                                {
                                    "path": "mods/a.jar",
                                    "downloads": ["https://cdn.modrinth.com/data/a.jar"],
                                    "hashes": {"sha1": "abc"},
                                    "fileSize": 12,
                                    "env": {"client": "required", "server": "required"},
                                }
                            ],
                        }
                    ),
                    "overrides/config/example.toml": "enabled=true",
                },
            )

            result = parse_modpack(pack)

            self.assertEqual(result.format, ModpackFormat.MODRINTH)
            self.assertEqual(result.name, "Sample MR")
            self.assertEqual(result.minecraft_version, "1.21.1")
            self.assertEqual(result.loader.name, "neoforge")
            self.assertEqual(result.loader.version, "21.1.233")
            self.assertEqual(len(result.remote_files), 1)
            self.assertEqual(result.override_root, "overrides")

    def test_parse_curseforge_zip_reads_manifest_project_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.zip"
            write_zip(
                pack,
                {
                    "manifest.json": json.dumps(
                        {
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "Sample CF",
                            "version": "2.0",
                            "minecraft": {
                                "version": "1.20.1",
                                "modLoaders": [{"id": "forge-47.4.20", "primary": True}],
                            },
                            "files": [{"projectID": 100, "fileID": 200, "required": True}],
                            "overrides": "overrides",
                        }
                    ),
                    "overrides/config/server.toml": "x=1",
                },
            )

            result = parse_modpack(pack)

            self.assertEqual(result.format, ModpackFormat.CURSEFORGE)
            self.assertEqual(result.name, "Sample CF")
            self.assertEqual(result.loader.name, "forge")
            self.assertEqual(result.loader.version, "47.4.20")
            self.assertEqual(result.remote_files[0].project_id, 100)
            self.assertEqual(result.remote_files[0].file_id, 200)

    def test_parse_curseforge_zip_attaches_modlist_slugs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.zip"
            write_zip(
                pack,
                {
                    "manifest.json": json.dumps(
                        {
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "Sample CF",
                            "version": "2.0",
                            "minecraft": {
                                "version": "1.20.1",
                                "modLoaders": [{"id": "fabric-0.18.4", "primary": True}],
                            },
                            "files": [
                                {"projectID": 561885, "fileID": 6290217, "required": True},
                                {"projectID": 1009556, "fileID": 5572389, "required": True},
                            ],
                            "overrides": "overrides",
                        }
                    ),
                    "modlist.html": (
                        '<ul class="mod-list">'
                        '<li><a href="https://www.curseforge.com/minecraft/mc-mods/just-zoom">'
                        "Just Zoom (by Keksuccino)</a></li>"
                        '<li><a href="https://www.curseforge.com/minecraft/mc-mods/hide-experimental-warning">'
                        "Hide Experimental Warning (by Serilum)</a></li>"
                        "</ul>"
                    ),
                },
            )

            result = parse_modpack(pack)

            self.assertEqual(result.remote_files[0].slug, "just-zoom")
            self.assertEqual(result.remote_files[0].display_name, "Just Zoom")
            self.assertEqual(result.remote_files[1].slug, "hide-experimental-warning")
            self.assertEqual(result.remote_files[1].display_name, "Hide Experimental Warning")

    def test_required_java_major_maps_minecraft_versions(self) -> None:
        self.assertEqual(required_java_major("1.12.2"), 8)
        self.assertEqual(required_java_major("1.17.1"), 16)
        self.assertEqual(required_java_major("1.20.1"), 17)
        self.assertEqual(required_java_major("1.21.1"), 21)

    def test_java_status_marks_newer_runtime_as_not_exact(self) -> None:
        self.assertEqual(java_status(17, None), "missing")
        self.assertEqual(java_status(17, 8), "too-old")
        self.assertEqual(java_status(17, 17), "ok")
        self.assertEqual(java_status(8, 26), "newer-than-recommended")

    def test_java_runtime_install_plan_uses_adoptium_archive_for_windows(self) -> None:
        plan = create_java_runtime_install_plan(17, os_name="Windows", machine="AMD64")

        self.assertEqual(plan.required_major, 17)
        self.assertEqual(plan.os, "windows")
        self.assertEqual(plan.arch, "x64")
        self.assertIn("api.adoptium.net", plan.download_url)
        self.assertIn("/17/", plan.download_url)
        self.assertIn("/windows/x64/jre/", plan.download_url)
        self.assertEqual(plan.archive_path, "pack2serve/java/jre-17-windows-x64.zip")
        self.assertEqual(plan.java_executable, "pack2serve/runtime/java/bin/java.exe")

    def test_java_installer_extracts_archive_and_rewrites_start_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            archive_path = tmp_path / "jre.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("jdk-17/bin/java.exe", b"fake-java")
                archive.writestr("jdk-17/release", "JAVA_VERSION=\"17\"\n")
            server_dir = tmp_path / "server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "start.ps1").write_text(
                "$ErrorActionPreference = 'Stop'\n"
                "$java = 'java'\n"
                "& $java -jar 'server.jar' nogui\n",
                encoding="utf-8",
            )
            plan = JavaRuntimeInstallPlan(
                required_major=17,
                os="windows",
                arch="x64",
                kind="adoptium-jre-archive",
                download_url=archive_path.as_uri(),
                archive_path="pack2serve/java/jre.zip",
                install_dir="pack2serve/runtime/java",
                java_executable="pack2serve/runtime/java/bin/java.exe",
                notes=[],
            )

            result = JavaInstaller().install(server_dir, plan)

            self.assertEqual(result.status, "installed")
            self.assertEqual((server_dir / "pack2serve/runtime/java/bin/java.exe").read_bytes(), b"fake-java")
            start = (server_dir / "start.ps1").read_text(encoding="utf-8")
            self.assertIn("pack2serve\\runtime\\java\\bin\\java.exe", start)
            self.assertTrue((server_dir / "pack2serve/java-install-result.json").exists())

    def test_java_installer_adds_runtime_to_path_for_run_bat_start_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            archive_path = tmp_path / "jre.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("jdk-21/bin/java.exe", b"fake-java")
            server_dir = tmp_path / "server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "start.ps1").write_text(
                "$ErrorActionPreference = 'Stop'\n"
                "& (Join-Path $PSScriptRoot 'run.bat')\n",
                encoding="utf-8",
            )
            plan = JavaRuntimeInstallPlan(
                required_major=21,
                os="windows",
                arch="x64",
                kind="adoptium-jre-archive",
                download_url=archive_path.as_uri(),
                archive_path="pack2serve/java/jre.zip",
                install_dir="pack2serve/runtime/java",
                java_executable="pack2serve/runtime/java/bin/java.exe",
                notes=[],
            )

            JavaInstaller().install(server_dir, plan)

            start = (server_dir / "start.ps1").read_text(encoding="utf-8")
            self.assertIn("$env:PATH", start)
            self.assertIn("pack2serve\\runtime\\java\\bin", start)
            self.assertIn("run.bat", start)

    def test_java_installer_rejects_unsafe_archive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            archive_path = tmp_path / "jre.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("../outside/bin/java.exe", b"bad")
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            plan = JavaRuntimeInstallPlan(
                required_major=17,
                os="windows",
                arch="x64",
                kind="adoptium-jre-archive",
                download_url=archive_path.as_uri(),
                archive_path="pack2serve/java/jre.zip",
                install_dir="pack2serve/runtime/java",
                java_executable="pack2serve/runtime/java/bin/java.exe",
                notes=[],
            )

            with self.assertRaises(ValueError):
                JavaInstaller().install(server_dir, plan)
            self.assertFalse((tmp_path / "outside").exists())

    def test_loader_install_plan_generates_loader_specific_sources(self) -> None:
        fabric = create_loader_install_plan("fabric-loader", "0.18.4", "1.20.1")
        forge = create_loader_install_plan("forge", "47.4.20", "1.20.1")
        neoforge = create_loader_install_plan("neoforge", "21.1.233", "1.21.1")

        self.assertEqual(fabric.kind, "direct-server-jar")
        self.assertIn("meta.fabricmc.net", fabric.download_url)
        self.assertEqual(fabric.server_jar, "server.jar")
        self.assertEqual(forge.kind, "installer-jar")
        self.assertIn("maven.minecraftforge.net", forge.download_url)
        self.assertEqual(forge.install_command[-1], "--installServer")
        self.assertEqual(neoforge.kind, "installer-jar")
        self.assertIn("maven.neoforged.net", neoforge.download_url)
        self.assertIn("neoforge-21.1.233-installer.jar", neoforge.artifact_name)

    def test_loader_installer_downloads_direct_server_jar_and_rewrites_start_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "fabric-server.jar"
            source.write_bytes(b"fabric-server")
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            plan = LoaderInstallPlan(
                loader="fabric",
                loader_version="0.18.4",
                minecraft_version="1.20.1",
                kind="direct-server-jar",
                download_url=source.as_uri(),
                artifact_name="server.jar",
                artifact_path="server.jar",
                install_command=["download", source.as_uri(), "server.jar"],
                launch_command=["java", "-Xmx4G", "-jar", "server.jar", "nogui"],
                server_jar="server.jar",
                notes=[],
            )

            result = LoaderInstaller().install(server_dir, plan)

            self.assertEqual((server_dir / "server.jar").read_bytes(), b"fabric-server")
            self.assertEqual(result.status, "installed")
            self.assertTrue((server_dir / "start.ps1").read_text().find("server.jar") >= 0)

    def test_loader_installer_downloads_installer_jar_without_running_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "forge-installer.jar"
            source.write_bytes(b"installer")
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            plan = LoaderInstallPlan(
                loader="forge",
                loader_version="47.4.20",
                minecraft_version="1.20.1",
                kind="installer-jar",
                download_url=source.as_uri(),
                artifact_name="forge-installer.jar",
                artifact_path="pack2serve/loaders/forge-installer.jar",
                install_command=["java", "-jar", "pack2serve/loaders/forge-installer.jar", "--installServer"],
                launch_command=["powershell", "-ExecutionPolicy", "Bypass", "-File", "start.ps1"],
                server_jar=None,
                notes=[],
            )

            result = LoaderInstaller().install(server_dir, plan)

            self.assertEqual((server_dir / "pack2serve/loaders/forge-installer.jar").read_bytes(), b"installer")
            self.assertEqual(result.status, "downloaded")
            self.assertEqual(result.executed, False)

    def test_loader_installer_rewrites_start_script_to_run_bat_after_installer_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "installer.jar"
            source.write_bytes(b"installer")
            fake_installer = tmp_path / "fake_installer.py"
            fake_installer.write_text(
                "from pathlib import Path\n"
                "Path('run.bat').write_text('java @user_jvm_args.txt @libraries/example/win_args.txt nogui\\n')\n",
                encoding="utf-8",
            )
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            plan = LoaderInstallPlan(
                loader="forge",
                loader_version="47.4.20",
                minecraft_version="1.20.1",
                kind="installer-jar",
                download_url=source.as_uri(),
                artifact_name="forge-installer.jar",
                artifact_path="pack2serve/loaders/forge-installer.jar",
                install_command=["python", str(fake_installer)],
                launch_command=["powershell", "-ExecutionPolicy", "Bypass", "-File", "start.ps1"],
                server_jar=None,
                notes=[],
            )

            result = LoaderInstaller().install(server_dir, plan, execute_installers=True)

            self.assertEqual(result.status, "installed")
            self.assertIn("run.bat", (server_dir / "start.ps1").read_text(encoding="utf-8"))

    def test_loader_installer_uses_managed_java_runtime_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "installer.jar"
            source.write_bytes(b"installer")
            server_dir = tmp_path / "server"
            java_dir = server_dir / "pack2serve/runtime/java/bin"
            java_dir.mkdir(parents=True)
            java_cmd = java_dir / "java.cmd"
            java_cmd.write_text(
                "@echo off\n"
                "echo managed java\n"
                "echo java @user_jvm_args.txt @libraries/example/win_args.txt nogui> run.bat\n",
                encoding="utf-8",
            )
            plan = LoaderInstallPlan(
                loader="forge",
                loader_version="47.4.20",
                minecraft_version="1.20.1",
                kind="installer-jar",
                download_url=source.as_uri(),
                artifact_name="forge-installer.jar",
                artifact_path="pack2serve/loaders/forge-installer.jar",
                install_command=["java", "-jar", "pack2serve/loaders/forge-installer.jar", "--installServer"],
                launch_command=["powershell", "-ExecutionPolicy", "Bypass", "-File", "start.ps1"],
                server_jar=None,
                notes=[],
            )

            result = LoaderInstaller().install(server_dir, plan, execute_installers=True)

            self.assertEqual(result.status, "installed")
            self.assertIn("pack2serve", result.command[0].replace("\\", "/"))
            self.assertIn("runtime/java/bin/java.cmd", result.command[0].replace("\\", "/"))
            self.assertIn("run.bat", (server_dir / "start.ps1").read_text(encoding="utf-8"))

    def test_loader_installer_rewrites_start_script_to_legacy_forge_jar_when_no_run_bat_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "installer.jar"
            source.write_bytes(b"installer")
            fake_installer = tmp_path / "fake_installer.py"
            fake_installer.write_text(
                "from pathlib import Path\n"
                "Path('forge-1.12.2-14.23.5.2860.jar').write_bytes(b'forge')\n"
                "Path('minecraft_server.1.12.2.jar').write_bytes(b'minecraft')\n",
                encoding="utf-8",
            )
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            plan = LoaderInstallPlan(
                loader="forge",
                loader_version="14.23.5.2860",
                minecraft_version="1.12.2",
                kind="installer-jar",
                download_url=source.as_uri(),
                artifact_name="forge-installer.jar",
                artifact_path="pack2serve/loaders/forge-installer.jar",
                install_command=["python", str(fake_installer)],
                launch_command=["powershell", "-ExecutionPolicy", "Bypass", "-File", "start.ps1"],
                server_jar=None,
                notes=[],
            )

            result = LoaderInstaller().install(server_dir, plan, execute_installers=True)

            self.assertEqual(result.status, "installed")
            self.assertIn("forge-1.12.2-14.23.5.2860.jar", (server_dir / "start.ps1").read_text(encoding="utf-8"))

    def test_loader_installer_repairs_failed_maven_library_download_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "installer.jar"
            source.write_bytes(b"installer")
            library_source = tmp_path / "launchwrapper-1.12.jar"
            library_source.write_bytes(b"launchwrapper")
            fake_installer = tmp_path / "fake_installer.py"
            fake_installer.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "lib = Path('libraries/net/minecraft/launchwrapper/1.12/launchwrapper-1.12.jar')\n"
                "if not lib.exists():\n"
                "    print('Considering library net.minecraft:launchwrapper:1.12')\n"
                f"    print('  Downloading library from {library_source.as_uri()}')\n"
                "    print('Failed to establish connection')\n"
                "    print('These libraries failed to download. Try again.')\n"
                "    print('')\n"
                "    print('net.minecraft:launchwrapper:1.12')\n"
                "    sys.exit(1)\n"
                "Path('forge-1.12.2-14.23.5.2860.jar').write_bytes(b'forge')\n",
                encoding="utf-8",
            )
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            plan = LoaderInstallPlan(
                loader="forge",
                loader_version="14.23.5.2860",
                minecraft_version="1.12.2",
                kind="installer-jar",
                download_url=source.as_uri(),
                artifact_name="forge-installer.jar",
                artifact_path="pack2serve/loaders/forge-installer.jar",
                install_command=["python", str(fake_installer)],
                launch_command=["powershell", "-ExecutionPolicy", "Bypass", "-File", "start.ps1"],
                server_jar=None,
                notes=[],
            )

            result = LoaderInstaller().install(server_dir, plan, execute_installers=True)

            repaired_library = server_dir / "libraries/net/minecraft/launchwrapper/1.12/launchwrapper-1.12.jar"
            self.assertEqual(result.status, "installed")
            self.assertEqual(repaired_library.read_bytes(), b"launchwrapper")
            self.assertIn("Repaired failed installer libraries", result.stdout)

    def test_server_builder_copies_server_files_and_isolates_client_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Sample Build",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [
                                {
                                    "path": "mods/remote.jar",
                                    "downloads": ["https://cdn.modrinth.com/data/remote.jar"],
                                    "hashes": {"sha512": "abc"},
                                    "fileSize": 123,
                                }
                            ],
                        }
                    ),
                    "overrides/config/server.toml": "server=true",
                    "overrides/mods/local.jar": b"jar",
                    "overrides/kubejs/server_scripts/main.js": "ServerEvents.loaded(() => {})",
                    "overrides/resources/assets/lycanitesmobs/spawners/global.json": "{}",
                    "overrides/structures/active/tower.rcst": b"structure",
                    "overrides/shaderpacks/client.zip": b"shader",
                    "overrides/options.txt": "guiScale:2",
                    "overrides/saves/World/level.dat": b"level",
                },
            )
            target = tmp_path / "server"

            report = ServerBuilder().build(pack, target)

            self.assertEqual((target / "config/server.toml").read_text(), "server=true")
            self.assertEqual((target / "mods/local.jar").read_bytes(), b"jar")
            self.assertTrue((target / "kubejs/server_scripts/main.js").exists())
            self.assertTrue((target / "resources/assets/lycanitesmobs/spawners/global.json").exists())
            self.assertTrue((target / "structures/active/tower.rcst").exists())
            self.assertTrue((target / "_client-overrides/shaderpacks/client.zip").exists())
            self.assertTrue((target / "_client-overrides/options.txt").exists())
            self.assertTrue((target / "world/level.dat").exists())
            self.assertTrue((target / "pack2serve/build-report.json").exists())
            self.assertTrue((target / "pack2serve/loader-install-plan.json").exists())
            self.assertTrue((target / "pack2serve/java-runtime-install-plan.json").exists())
            self.assertTrue((target / "start.ps1").exists())
            self.assertTrue((target / "eula.txt").exists())
            self.assertTrue((target / "server.properties").exists())
            self.assertEqual(report.java.required_major, 17)
            self.assertEqual(report.downloads[0].target_path, "mods/remote.jar")

    def test_artifact_cache_downloads_modrinth_file_uri_and_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "remote.jar"
            source.write_bytes(b"modrinth-jar")
            sha512 = hashlib.sha512(source.read_bytes()).hexdigest()
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Download Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "forge": "47.4.20"},
                            "files": [
                                {
                                    "path": "mods/remote.jar",
                                    "downloads": [source.as_uri()],
                                    "hashes": {"sha512": sha512},
                                    "fileSize": len(b"modrinth-jar"),
                                }
                            ],
                        }
                    ),
                },
            )
            parsed = parse_modpack(pack)
            cache = ArtifactCache(tmp_path / "cache")
            provider = ModrinthDirectProvider(cache)

            artifact = provider.resolve_and_cache(parsed.remote_files[0])
            source.write_bytes(b"changed")
            cached_again = provider.resolve_and_cache(parsed.remote_files[0])

            self.assertEqual(artifact.path.read_bytes(), b"modrinth-jar")
            self.assertEqual(cached_again.path, artifact.path)

    def test_server_builder_downloads_modrinth_remote_files_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "remote.jar"
            source.write_bytes(b"remote-content")
            sha1 = hashlib.sha1(source.read_bytes()).hexdigest()
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Download Build",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "forge": "47.4.20"},
                            "files": [
                                {
                                    "path": "mods/remote.jar",
                                    "downloads": [source.as_uri()],
                                    "hashes": {"sha1": sha1},
                                    "fileSize": len(b"remote-content"),
                                }
                            ],
                        }
                    ),
                },
            )
            target = tmp_path / "server"

            report = ServerBuilder(cache_dir=tmp_path / "cache", download_remote=True).build(pack, target)

            self.assertEqual((target / "mods/remote.jar").read_bytes(), b"remote-content")
            self.assertEqual(len(report.manual_actions), 0)

    def test_server_builder_isolates_modrinth_server_unsupported_remote_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "client.jar"
            source.write_bytes(b"client-only")
            sha1 = hashlib.sha1(source.read_bytes()).hexdigest()
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Client Env Build",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "forge": "47.4.20"},
                            "files": [
                                {
                                    "path": "mods/client.jar",
                                    "downloads": [source.as_uri()],
                                    "hashes": {"sha1": sha1},
                                    "fileSize": len(b"client-only"),
                                    "env": {"client": "required", "server": "unsupported"},
                                }
                            ],
                        }
                    ),
                },
            )
            target = tmp_path / "server"

            report = ServerBuilder(cache_dir=tmp_path / "cache", download_remote=True).build(pack, target)

            self.assertFalse((target / "mods/client.jar").exists())
            self.assertEqual((target / "_client-overrides/mods/client.jar").read_bytes(), b"client-only")
            self.assertEqual(report.copied_overrides[-1].classification, "client-remote-isolated")
            self.assertEqual(len(report.manual_actions), 0)

    def test_server_builder_isolates_known_client_only_modrinth_remote_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "【无缝音乐】moremusic-0.1.4+1.20.1.jar"
            source.write_bytes(b"moremusic")
            sha1 = hashlib.sha1(source.read_bytes()).hexdigest()
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Known Client Build",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "forge": "47.4.20"},
                            "files": [
                                {
                                    "path": "mods/【无缝音乐】moremusic-0.1.4+1.20.1.jar",
                                    "downloads": [source.as_uri()],
                                    "hashes": {"sha1": sha1},
                                    "fileSize": len(b"moremusic"),
                                }
                            ],
                        }
                    ),
                },
            )
            target = tmp_path / "server"

            report = ServerBuilder(cache_dir=tmp_path / "cache", download_remote=True).build(pack, target)

            self.assertFalse((target / "mods/【无缝音乐】moremusic-0.1.4+1.20.1.jar").exists())
            self.assertTrue((target / "_client-overrides/mods/【无缝音乐】moremusic-0.1.4+1.20.1.jar").exists())
            self.assertEqual(report.copied_overrides[-1].classification, "client-remote-isolated")

    def test_panel_service_imports_pack_and_lists_generated_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "panel-sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Panel Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                },
            )
            service = PanelService(workspace_dir=tmp_path / "workspace")

            imported = service.import_pack(pack, target_name="My Server")
            servers = service.list_servers()

            self.assertEqual(imported["name"], "Panel Sample")
            self.assertEqual(imported["targetName"], "my-server")
            self.assertTrue((tmp_path / "workspace/servers/my-server/pack2serve/build-report.json").exists())
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["targetName"], "my-server")

    def test_panel_service_lists_nested_generated_servers_with_connection_address(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/startup-verification/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "server.properties").write_text("server-port=25610\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            servers = service.list_servers()

            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["targetName"], "startup-verification/sample-server")
            self.assertEqual(servers[0]["connectAddress"], "127.0.0.1:25610")
            self.assertEqual(servers[0]["runtimeStatus"], "stopped")

    def test_panel_service_hides_internal_verification_projects_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            public_server = tmp_path / "workspace/servers/my-public-server"
            internal_server = tmp_path / "workspace/servers/full-verification/sample-server"
            public_server.joinpath("pack2serve").mkdir(parents=True)
            internal_server.joinpath("pack2serve").mkdir(parents=True)
            _write_minimal_build_report(public_server, name="Public Server")
            _write_minimal_build_report(internal_server, name="Internal Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            public = service.list_servers()
            all_servers = service.list_servers(include_internal=True)

            self.assertEqual([server["targetName"] for server in public], ["my-public-server"])
            self.assertEqual({server["targetName"] for server in all_servers}, {"my-public-server", "full-verification/sample-server"})
            self.assertFalse(public[0]["internalProject"])

    def test_panel_service_starts_and_stops_generated_server_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "server.properties").write_text("server-port=25655\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Runnable Sample")
            fake_server = tmp_path / "fake_server.py"
            fake_server.write_text(
                "import sys\n"
                "print('Done (0.1s)! For help, type \"help\"', flush=True)\n"
                "for line in sys.stdin:\n"
                "    if line.strip() == 'stop':\n"
                "        print('Stopping server', flush=True)\n"
                "        break\n",
                encoding="utf-8",
            )
            (server_dir / "start.ps1").write_text(f"& python '{fake_server}'\n", encoding="utf-8")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            started = service.start_server("sample-server")
            self.assertEqual(started["runtimeStatus"], "starting")

            deadline = time.time() + 10
            status = service.server_runtime_status("sample-server")
            while status["runtimeStatus"] != "running" and time.time() < deadline:
                time.sleep(0.05)
                status = service.server_runtime_status("sample-server")

            self.assertEqual(status["runtimeStatus"], "running")
            self.assertEqual(status["connectAddress"], "127.0.0.1:25655")

            stopped = service.stop_server("sample-server")
            self.assertEqual(stopped["runtimeStatus"], "stopped")

    def test_panel_service_reads_server_log_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/startup-verification/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "logs").mkdir()
            (server_dir / "server.properties").write_text("server-port=25610\n", encoding="utf-8")
            (server_dir / "logs/panel-server.log").write_text(
                "\n".join(f"line {number}" for number in range(1, 8)) + "\n",
                encoding="utf-8",
            )
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            log = service.server_log_tail("startup-verification/sample-server", max_lines=3)

            self.assertEqual(log["targetName"], "startup-verification/sample-server")
            self.assertEqual(log["connectAddress"], "127.0.0.1:25610")
            self.assertEqual(log["runtimeStatus"], "stopped")
            self.assertEqual(log["lines"], ["line 5", "line 6", "line 7"])

    def test_panel_service_updates_server_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "server.properties").write_text("server-port=25565\nmotd=Old\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            saved = service.save_server_properties("sample-server", {"motd": "New", "difficulty": "hard"})

            self.assertEqual(saved["properties"]["motd"], "New")
            self.assertEqual(saved["properties"]["difficulty"], "hard")
            self.assertIn("motd=New", (server_dir / "server.properties").read_text(encoding="utf-8"))

    def test_panel_service_updates_key_server_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "server.properties").write_text(
                "server-port=25565\nonline-mode=true\nmax-players=20\nmotd=Old\n",
                encoding="utf-8",
            )
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            saved = service.save_key_server_settings(
                "sample-server",
                {"server-port": 25666, "online-mode": False, "max-players": 8},
            )

            self.assertEqual(saved["settings"]["server-port"]["value"], "25666")
            self.assertEqual(saved["settings"]["online-mode"]["value"], "false")
            self.assertEqual(saved["settings"]["max-players"]["value"], "8")
            content = (server_dir / "server.properties").read_text(encoding="utf-8")
            self.assertIn("server-port=25666", content)
            self.assertIn("online-mode=false", content)
            self.assertIn("max-players=8", content)
            self.assertIn("motd=Old", content)

    def test_panel_service_sends_console_command_to_running_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "server.properties").write_text("server-port=25655\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Runnable Sample")
            commands_file = tmp_path / "commands.txt"
            fake_server = tmp_path / "fake_server.py"
            fake_server.write_text(
                "import pathlib, sys\n"
                f"commands = pathlib.Path({str(commands_file)!r})\n"
                "print('Done (0.1s)! For help, type \"help\"', flush=True)\n"
                "for line in sys.stdin:\n"
                "    commands.write_text(commands.read_text() + line, encoding='utf-8') if commands.exists() else commands.write_text(line, encoding='utf-8')\n"
                "    print('ran ' + line.strip(), flush=True)\n"
                "    if line.strip() == 'stop':\n"
                "        break\n",
                encoding="utf-8",
            )
            (server_dir / "start.ps1").write_text(f"& python '{fake_server}'\n", encoding="utf-8")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            service.start_server("sample-server")
            deadline = time.time() + 10
            while service.server_runtime_status("sample-server")["runtimeStatus"] != "running" and time.time() < deadline:
                time.sleep(0.05)

            result = service.send_console_command("sample-server", "say hello")
            service.stop_server("sample-server")

            self.assertEqual(result["command"], "say hello")
            self.assertIn("say hello", commands_file.read_text(encoding="utf-8"))

    def test_panel_service_sends_player_management_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "server.properties").write_text("server-port=25655\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Runnable Sample")
            commands_file = tmp_path / "commands.txt"
            fake_server = tmp_path / "fake_server.py"
            fake_server.write_text(
                "import pathlib, sys\n"
                f"commands = pathlib.Path({str(commands_file)!r})\n"
                "print('Done (0.1s)! For help, type \"help\"', flush=True)\n"
                "for line in sys.stdin:\n"
                "    commands.write_text(commands.read_text() + line, encoding='utf-8') if commands.exists() else commands.write_text(line, encoding='utf-8')\n"
                "    if line.strip() == 'stop':\n"
                "        break\n",
                encoding="utf-8",
            )
            (server_dir / "start.ps1").write_text(f"& python '{fake_server}'\n", encoding="utf-8")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            service.start_server("sample-server")
            deadline = time.time() + 10
            while service.server_runtime_status("sample-server")["runtimeStatus"] != "running" and time.time() < deadline:
                time.sleep(0.05)

            service.player_action("sample-server", "op", player="Alice")
            service.player_action("sample-server", "gamemode", player="Alice", gameMode="creative")
            service.player_action("sample-server", "tp", player="Alice", x=1, y=70, z=-2)
            service.player_action("sample-server", "ban", player="Alice", reason="test only")
            service.player_action("sample-server", "kill", player="Alice")
            service.player_action("sample-server", "clear", player="Alice")
            service.stop_server("sample-server")

            commands = commands_file.read_text(encoding="utf-8")
            self.assertIn("op Alice\n", commands)
            self.assertIn("gamemode creative Alice\n", commands)
            self.assertIn("tp Alice 1 70 -2\n", commands)
            self.assertIn("ban Alice test only\n", commands)
            self.assertIn("kill Alice\n", commands)
            self.assertIn("clear Alice\n", commands)

    def test_panel_service_creates_project_job_and_accepts_eula(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Job Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                    "overrides/config/voicechat/voicechat-server.properties": "port=24454\nbind_address=\n",
                },
            )
            existing = tmp_path / "workspace/servers/existing-server"
            (existing / "pack2serve").mkdir(parents=True)
            (existing / "config/voicechat").mkdir(parents=True)
            (existing / "server.properties").write_text("server-port=25565\n", encoding="utf-8")
            (existing / "config/voicechat/voicechat-server.properties").write_text("port=24454\n", encoding="utf-8")
            _write_minimal_build_report(existing, name="Existing Server")
            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")

            def install_loader(server_dir: Path, plan: LoaderInstallPlan, *, execute_installers: bool = False):
                (Path(server_dir) / plan.artifact_path).write_bytes(b"fabric-server")
                return type(
                    "Result",
                    (),
                    {
                        "status": "installed",
                        "to_json_dict": lambda self: {
                            "status": "installed",
                            "artifact_path": plan.artifact_path,
                            "executed": execute_installers,
                        },
                    },
                )()

            def install_java(server_dir: Path, plan: JavaRuntimeInstallPlan):
                java = Path(server_dir) / plan.java_executable
                java.parent.mkdir(parents=True, exist_ok=True)
                java.write_bytes(b"java")
                return type(
                    "Result",
                    (),
                    {
                        "status": "installed",
                        "to_json_dict": lambda self: {
                            "status": "installed",
                            "java_executable": plan.java_executable,
                        },
                    },
                )()

            with patch("pack2serve.panel.JavaInstaller") as java_installer_class, patch(
                "pack2serve.panel.LoaderInstaller"
            ) as installer_class, patch("pack2serve.panel._is_port_available", return_value=True), patch.object(
                ServerValidator,
                "validate",
                return_value=type("ValidationResult", (), {"status": "started", "to_json_dict": lambda self: {}})(),
            ):
                java_installer_class.return_value.install.side_effect = install_java
                installer_class.return_value.install.side_effect = install_loader
                job = service.create_project(pack, project_name="Job Server", accept_eula=True, download=True)
                deadline = time.time() + 10
                current = service.project_job(job["jobId"])
                while current["status"] in {"queued", "running"} and time.time() < deadline:
                    time.sleep(0.05)
                    current = service.project_job(job["jobId"])

                self.assertEqual(current["status"], "completed")
                self.assertEqual(current["progress"], 100)
                self.assertEqual(current["server"]["name"], "Job Server")
                listed = next(server for server in service.list_servers() if server["targetName"] == "job-server")
                self.assertEqual(listed["name"], "Job Server")
                self.assertTrue((tmp_path / "workspace/servers/job-server/eula.txt").read_text(encoding="utf-8").strip().endswith("eula=true"))
                self.assertIn(
                    "server-port=25566",
                    (tmp_path / "workspace/servers/job-server/server.properties").read_text(encoding="utf-8"),
                )
                voice_config = (
                    tmp_path / "workspace/servers/job-server/config/voicechat/voicechat-server.properties"
                ).read_text(encoding="utf-8")
                self.assertIn("port=24455", voice_config)
                self.assertEqual((tmp_path / "workspace/servers/job-server/server.jar").read_bytes(), b"fabric-server")
                self.assertTrue((tmp_path / "workspace/servers/job-server/pack2serve/runtime/java/bin/java.exe").exists())
                java_installer_class.return_value.install.assert_called_once()
                installer_class.return_value.install.assert_called_once()

    def test_panel_service_create_project_auto_validates_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Job Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                },
            )
            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")

            def install_loader(server_dir: Path, plan: LoaderInstallPlan, *, execute_installers: bool = False):
                (Path(server_dir) / plan.artifact_path).write_bytes(b"fabric-server")
                return type("Result", (), {"status": "installed", "to_json_dict": lambda self: {}})()

            def install_java(server_dir: Path, plan: JavaRuntimeInstallPlan):
                java = Path(server_dir) / plan.java_executable
                java.parent.mkdir(parents=True, exist_ok=True)
                java.write_bytes(b"java")
                return type("Result", (), {"status": "installed", "to_json_dict": lambda self: {}})()

            validation_result = type(
                "ValidationResult",
                (),
                {"status": "started", "to_json_dict": lambda self: {"status": "started"}},
            )()

            with patch("pack2serve.panel.JavaInstaller") as java_installer_class, patch(
                "pack2serve.panel.LoaderInstaller"
            ) as installer_class, patch.object(ServerValidator, "validate", return_value=validation_result) as validate:
                java_installer_class.return_value.install.side_effect = install_java
                installer_class.return_value.install.side_effect = install_loader
                job = service.create_project(pack, project_name="Job Server", accept_eula=True, download=True)
                deadline = time.time() + 10
                current = service.project_job(job["jobId"])
                while current["status"] in {"queued", "running"} and time.time() < deadline:
                    time.sleep(0.05)
                    current = service.project_job(job["jobId"])

            self.assertEqual(current["status"], "completed")
            self.assertEqual(current["stage"], "complete")
            self.assertTrue(any("验证" in line or "validation" in line.lower() for line in current["logLines"]))
            validate.assert_called_once()
            self.assertEqual(Path(validate.call_args.args[0]), tmp_path / "workspace/servers/job-server")
            self.assertEqual(validate.call_args.kwargs["timeout_seconds"], 300)

    def test_panel_service_create_project_fails_when_auto_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Job Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                },
            )
            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")

            def install_loader(server_dir: Path, plan: LoaderInstallPlan, *, execute_installers: bool = False):
                (Path(server_dir) / plan.artifact_path).write_bytes(b"fabric-server")
                return type("Result", (), {"status": "installed", "to_json_dict": lambda self: {}})()

            def install_java(server_dir: Path, plan: JavaRuntimeInstallPlan):
                java = Path(server_dir) / plan.java_executable
                java.parent.mkdir(parents=True, exist_ok=True)
                java.write_bytes(b"java")
                return type("Result", (), {"status": "installed", "to_json_dict": lambda self: {}})()

            validation_result = type(
                "ValidationResult",
                (),
                {"status": "failed", "to_json_dict": lambda self: {"status": "failed", "hints": ["boom"]}},
            )()

            with patch("pack2serve.panel.JavaInstaller") as java_installer_class, patch(
                "pack2serve.panel.LoaderInstaller"
            ) as installer_class, patch.object(ServerValidator, "validate", return_value=validation_result):
                java_installer_class.return_value.install.side_effect = install_java
                installer_class.return_value.install.side_effect = install_loader
                job = service.create_project(pack, project_name="Job Server", accept_eula=True, download=True)
                deadline = time.time() + 10
                current = service.project_job(job["jobId"])
                while current["status"] in {"queued", "running"} and time.time() < deadline:
                    time.sleep(0.05)
                    current = service.project_job(job["jobId"])

            self.assertEqual(current["status"], "failed")
            self.assertEqual(current["stage"], "failed")
            self.assertIn("Startup validation failed", current["error"])

    def test_panel_service_create_project_requires_download_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Job Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                },
            )
            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")

            with self.assertRaises(ValueError) as raised:
                service.create_project(pack, project_name="Job Server", accept_eula=True, download=False)

            self.assertIn("automatic remote file downloads", str(raised.exception))

    def test_panel_service_writes_accepted_eula_before_loader_install_can_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Job Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                },
            )
            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")

            def install_java(server_dir: Path, plan: JavaRuntimeInstallPlan):
                java = Path(server_dir) / plan.java_executable
                java.parent.mkdir(parents=True, exist_ok=True)
                java.write_bytes(b"java")
                return type("Result", (), {"status": "installed", "to_json_dict": lambda self: {}})()

            def fail_loader(server_dir: Path, plan: LoaderInstallPlan, *, execute_installers: bool = False):
                return type(
                    "Result",
                    (),
                    {"status": "failed", "to_json_dict": lambda self: {"status": "failed"}},
                )()

            with patch("pack2serve.panel.JavaInstaller") as java_installer_class, patch(
                "pack2serve.panel.LoaderInstaller"
            ) as installer_class, patch("pack2serve.panel._is_port_available", return_value=True):
                java_installer_class.return_value.install.side_effect = install_java
                installer_class.return_value.install.side_effect = fail_loader
                job = service.create_project(pack, project_name="Job Server", accept_eula=True, download=True)
                deadline = time.time() + 10
                current = service.project_job(job["jobId"])
                while current["status"] in {"queued", "running"} and time.time() < deadline:
                    time.sleep(0.05)
                    current = service.project_job(job["jobId"])

            self.assertEqual(current["status"], "failed")
            self.assertTrue(
                (tmp_path / "workspace/servers/job-server/eula.txt")
                .read_text(encoding="utf-8")
                .strip()
                .endswith("eula=true")
            )

    def test_panel_service_reads_players_from_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "logs").mkdir()
            (server_dir / "server.properties").write_text("server-port=25565\n", encoding="utf-8")
            (server_dir / "logs/panel-server.log").write_text(
                "[Server thread/INFO]: Alice joined the game\n"
                "[Server thread/INFO]: Bob joined the game\n"
                "[Server thread/INFO]: Alice left the game\n",
                encoding="utf-8",
            )
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            players = service.server_players("sample-server")

            self.assertEqual([player["name"] for player in players["players"]], ["Bob"])
            self.assertEqual(players["players"][0]["gameMode"], "unknown")

    def test_panel_service_parses_player_details_from_command_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "logs").mkdir()
            (server_dir / "server.properties").write_text("server-port=25565\n", encoding="utf-8")
            (server_dir / "logs/panel-server.log").write_text(
                "[Server thread/INFO]: Alice joined the game\n"
                "[Server thread/INFO]: Set Alice's game mode to Creative Mode\n"
                "[Server thread/INFO]: Alice has the following entity data: [1.5d, 70.0d, -2.25d]\n"
                "[Server thread/INFO]: Alice has the following entity data: [180.0f, 20.0f]\n"
                "[Server thread/INFO]: The time is 49000\n",
                encoding="utf-8",
            )
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            players = service.server_players("sample-server")
            metrics = service.server_metrics("sample-server")

            self.assertEqual(players["players"][0]["name"], "Alice")
            self.assertEqual(players["players"][0]["gameMode"], "creative")
            self.assertEqual(players["players"][0]["position"], {"x": 1.5, "y": 70.0, "z": -2.25})
            self.assertEqual(players["players"][0]["rotation"], {"yaw": 180.0, "pitch": 20.0})
            self.assertEqual(metrics["world"]["gameTime"], 49000)
            self.assertEqual(metrics["world"]["days"], 2)

    def test_panel_service_lists_and_manages_mod_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "mods").mkdir(parents=True)
            (server_dir / "server.properties").write_text("server-port=25565\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Sample Server")
            mod_path = server_dir / "mods/example-mod.jar"
            write_zip(
                mod_path,
                {
                    "fabric.mod.json": json.dumps(
                        {
                            "id": "examplemod",
                            "name": "Example Mod",
                            "version": "1.2.3",
                            "icon": "assets/examplemod/icon.png",
                        }
                    ),
                    "assets/examplemod/icon.png": b"\x89PNG\r\n\x1a\n",
                },
            )

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            mods = service.server_mods("sample-server")
            disabled = service.disable_mod("sample-server", "example-mod.jar")

            self.assertEqual(mods["mods"][0]["title"], "Example Mod")
            self.assertEqual(mods["mods"][0]["id"], "examplemod")
            self.assertEqual(mods["mods"][0]["fileName"], "example-mod.jar")
            self.assertTrue(str(mods["mods"][0]["iconDataUrl"]).startswith("data:image/png;base64,"))
            self.assertEqual(disabled["status"], "disabled")
            self.assertFalse((server_dir / "mods/example-mod.jar").exists())
            self.assertTrue((server_dir / "disabled-mods/example-mod.jar").exists())
            deleted = service.delete_mod("sample-server", "example-mod.jar")
            self.assertEqual(deleted["status"], "deleted")
            self.assertFalse((server_dir / "disabled-mods/example-mod.jar").exists())

    def test_panel_service_returns_command_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "logs").mkdir()
            (server_dir / "server.properties").write_text("server-port=25565\n", encoding="utf-8")
            (server_dir / "logs/panel-server.log").write_text(
                "[Server thread/INFO]: Alice joined the game\n",
                encoding="utf-8",
            )
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            suggestions = service.command_suggestions("sample-server", "gam")

            self.assertIn("gamemode creative Alice", suggestions["suggestions"])
            self.assertIn("gamemode survival Alice", suggestions["suggestions"])

    def test_panel_service_manages_worlds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "World").mkdir(parents=True)
            (server_dir / "OldWorld").mkdir(parents=True)
            (server_dir / "World/level.dat").write_bytes(b"world")
            (server_dir / "OldWorld/level.dat").write_bytes(b"old")
            (server_dir / "server.properties").write_text("level-name=World\nserver-port=25565\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Sample Server")

            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")
            worlds = service.server_worlds("sample-server")
            created = service.create_world("sample-server", "New World")
            selected = service.select_world("sample-server", "New World")
            backup = service.backup_world("sample-server", "New World")

            self.assertEqual(worlds["currentWorld"], "World")
            self.assertEqual([world["name"] for world in worlds["worlds"]], ["OldWorld", "World"])
            self.assertEqual(created["world"]["name"], "New World")
            self.assertTrue((server_dir / "New World").exists())
            self.assertEqual(selected["currentWorld"], "New World")
            self.assertIn("level-name=New World", (server_dir / "server.properties").read_text(encoding="utf-8"))
            self.assertEqual(backup["status"], "backed-up")
            self.assertTrue(Path(backup["backupPath"]).exists())
            with zipfile.ZipFile(backup["backupPath"]) as archive:
                self.assertIn("New World/", archive.namelist())

    def test_panel_service_deletes_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "workspace/servers/sample-server"
            (server_dir / "pack2serve").mkdir(parents=True)
            (server_dir / "server.properties").write_text("server-port=25655\n", encoding="utf-8")
            _write_minimal_build_report(server_dir, name="Sample Server")
            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")

            result = service.delete_project("sample-server")

            self.assertEqual(result["targetName"], "sample-server")
            self.assertEqual(result["status"], "deleted")
            self.assertFalse(server_dir.exists())
            self.assertEqual(service.list_servers(), [])

    def test_panel_service_delete_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            outside = tmp_path / "outside"
            outside.mkdir()
            service = PanelService(tmp_path / "workspace", advertise_host="127.0.0.1")

            with self.assertRaises(ValueError):
                service.delete_project("../outside")

            self.assertTrue(outside.exists())

    def test_panel_html_preserves_javascript_backslash_escaping(self) -> None:
        self.assertIn('replace(/\\\\/g, "\\\\\\\\")', PANEL_HTML)
        self.assertNotIn("replace(/\\/g, \"\\\\\")", PANEL_HTML)
        self.assertGreaterEqual(PANEL_HTML.count("split(/\\r?\\n/)"), 1)
        self.assertIn('id="projectGrid"', PANEL_HTML)
        self.assertIn('id="createDialog"', PANEL_HTML)
        self.assertIn('id="consoleCommand"', PANEL_HTML)
        self.assertIn('id="metricsGrid"', PANEL_HTML)
        self.assertIn('id="playerDetail"', PANEL_HTML)
        self.assertIn('id="modsList"', PANEL_HTML)
        self.assertIn('id="worldsList"', PANEL_HTML)
        self.assertIn('id="createWorld"', PANEL_HTML)
        self.assertIn('id="keySettings"', PANEL_HTML)
        self.assertIn('id="commandSuggestions"', PANEL_HTML)
        self.assertIn('data-stage="validate"', PANEL_HTML)
        self.assertIn('id="showInternalProjects"', PANEL_HTML)
        self.assertIn('id="detailDelete"', PANEL_HTML)
        self.assertIn('id="packFile"', PANEL_HTML)
        self.assertIn("updateCreateButton", PANEL_HTML)
        self.assertIn('type="file"', PANEL_HTML)
        self.assertIn("/api/projects/upload", PANEL_HTML)
        self.assertIn("/api/servers/delete", PANEL_HTML)
        self.assertIn("/api/servers/metrics", PANEL_HTML)
        self.assertIn("/api/servers/mods", PANEL_HTML)
        self.assertIn("/api/servers/worlds", PANEL_HTML)
        self.assertIn("/api/servers/key-settings", PANEL_HTML)
        self.assertIn("/api/servers/command-suggestions", PANEL_HTML)
        self.assertIn("button, input, textarea, select", PANEL_HTML)
        self.assertIn("appearance: none", PANEL_HTML)
        self.assertIn("background-image:", PANEL_HTML)
        self.assertNotIn('id="packPath"', PANEL_HTML)
        self.assertNotIn('id="mirrors"', PANEL_HTML)

    def test_panel_home_project_cards_show_start_stop_and_copy_address_actions(self) -> None:
        card_template = PANEL_HTML.split("function cardTemplate(server)", 1)[1].split("function openProject", 1)[0]

        self.assertEqual(card_template.count("<button"), 3)
        self.assertIn("startServer('${escapeAttr(server.targetName)}')", card_template)
        self.assertIn("stopServer('${escapeAttr(server.targetName)}')", card_template)
        self.assertIn("copyAddress('${escapeAttr(server.connectAddress)}')", card_template)
        self.assertIn("连接 IP", card_template)
        self.assertNotIn("server.compatibilityLevel", card_template)
        self.assertNotIn("deleteProject('${escapeAttr(server.targetName)}')", card_template)
        self.assertNotIn("event.stopPropagation(); openProject('${escapeAttr(server.targetName)}')", card_template)

    def test_panel_runtime_overview_connection_address_can_be_copied(self) -> None:
        refresh_metrics = PANEL_HTML.split("async function refreshMetrics()", 1)[1].split("async function refreshLogs", 1)[0]
        copy_metric_card = PANEL_HTML.split("function copyMetricCard(label, value)", 1)[1].split("async function refreshLogs", 1)[0]

        self.assertIn("copyMetricCard(\"连接地址\", metrics.runtime.connectAddress)", refresh_metrics)
        self.assertIn("copyAddress('${escapeAttr(value)}')", refresh_metrics)
        self.assertIn("mini-copy", copy_metric_card)
        self.assertNotIn("card-actions", copy_metric_card)
        self.assertIn(">复制</button>", refresh_metrics)

    def test_panel_upload_multipart_parser_reads_pack_file_and_fields(self) -> None:
        boundary = "pack2serve-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="projectName"\r\n\r\n'
            "Uploaded Server\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="packFile"; filename="sample.mrpack"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + b"pack-bytes" + (
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")

        fields, files = _parse_multipart_form(f"multipart/form-data; boundary={boundary}", body)

        self.assertEqual(fields["projectName"], "Uploaded Server")
        self.assertEqual(files["packFile"].filename, "sample.mrpack")
        self.assertEqual(files["packFile"].content, b"pack-bytes")

    def test_panel_upload_uses_original_file_name_when_project_name_is_blank(self) -> None:
        upload = UploadedFormFile(filename="Example Pack.mrpack", content=b"pack")

        self.assertEqual(_uploaded_project_name({"projectName": "  "}, upload), "Example Pack")
        self.assertEqual(_uploaded_project_name({"projectName": "Named Server"}, upload), "Named Server")

    def test_panel_upload_safe_name_preserves_extension_for_chinese_pack_names(self) -> None:
        self.assertEqual(_safe_upload_name("乌托邦探险之旅3.5.2.mrpack"), "3.5.2.mrpack")
        self.assertEqual(_safe_upload_name("测试.zip"), "modpack.zip")
        self.assertEqual(_safe_upload_name("bad/../name.exe"), "name.exe")

    def test_panel_upload_length_limit_rejects_missing_or_oversized_requests(self) -> None:
        self.assertEqual(_validate_upload_length(str(MAX_UPLOAD_BYTES)), MAX_UPLOAD_BYTES)
        with self.assertRaises(ValueError):
            _validate_upload_length("")
        with self.assertRaises(ValueError):
            _validate_upload_length(str(MAX_UPLOAD_BYTES + 1))

    def test_compatibility_audit_requires_startup_validation_for_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            (server_dir / "pack2serve").mkdir(parents=True)
            _write_minimal_build_report(server_dir, name="Sample Server")

            report = audit_generated_server(server_dir)

            self.assertEqual(report["level"], "generated-not-validated")
            self.assertFalse(report["serverEquivalent"])
            self.assertTrue(any(check["id"] == "startup-validation" for check in report["checks"]))

    def test_compatibility_audit_marks_unknown_overrides_as_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            (server_dir / "pack2serve").mkdir(parents=True)
            _write_minimal_build_report(server_dir, name="Sample Server")
            build_path = server_dir / "pack2serve/build-report.json"
            data = json.loads(build_path.read_text(encoding="utf-8"))
            data["copied_overrides"].append(
                {
                    "source": "overrides/unknown/file.txt",
                    "destination": "_unknown-overrides/file.txt",
                    "classification": "unknown-isolated",
                    "size": 10,
                }
            )
            build_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            (server_dir / "pack2serve/validation-report.json").write_text(
                json.dumps(
                    {
                        "status": "started",
                        "command": ["fake"],
                        "return_code": None,
                        "timed_out": False,
                        "combined_output": "Done (0.1s)! For help, type help",
                        "hints": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = audit_generated_server(server_dir)

            self.assertEqual(report["level"], "startable-with-differences")
            self.assertFalse(report["serverEquivalent"])
            unknown = next(check for check in report["checks"] if check["id"] == "unknown-overrides")
            self.assertEqual(unknown["status"], "warning")

    def test_cli_audit_server_prints_compatibility_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            (server_dir / "pack2serve").mkdir(parents=True)
            _write_minimal_build_report(server_dir, name="Sample Server")

            output = StringIO()
            with redirect_stdout(output):
                code = main(["audit-server", str(server_dir)])

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["level"], "generated-not-validated")
            self.assertTrue((server_dir / "pack2serve/compatibility-report.json").exists())

    def test_curseforge_template_mirror_downloads_project_file_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            mirror_root = tmp_path / "mirror"
            artifact_path = mirror_root / "100" / "200" / "mod.jar"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_bytes(b"curseforge-mirror")
            cache = ArtifactCache(tmp_path / "cache")
            provider = CurseForgeTemplateMirrorProvider(
                cache=cache,
                name="local-mirror",
                url_template=(mirror_root / "{projectID}" / "{fileID}" / "mod.jar").as_uri(),
                file_name_template="{projectID}-{fileID}.jar",
            )
            pack = tmp_path / "sample.zip"
            write_zip(
                pack,
                {
                    "manifest.json": json.dumps(
                        {
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "CF Mirror",
                            "version": "1.0.0",
                            "minecraft": {
                                "version": "1.20.1",
                                "modLoaders": [{"id": "fabric-0.18.4", "primary": True}],
                            },
                            "files": [{"projectID": 100, "fileID": 200, "required": True}],
                            "overrides": "overrides",
                        }
                    ),
                },
            )
            target = tmp_path / "server"

            report = ServerBuilder(
                cache_dir=tmp_path / "cache",
                download_remote=True,
                curseforge_providers=[provider],
            ).build(pack, target)

            self.assertEqual((target / "mods/100-200.jar").read_bytes(), b"curseforge-mirror")
            self.assertEqual(len(report.manual_actions), 0)

    def test_server_builder_reports_curseforge_resolution_summary(self) -> None:
        from pack2serve.downloader import ResolvedCurseForgeArtifact

        class Provider:
            name = "summary-provider"

            def __init__(self, url: str):
                self.url = url

            def resolve(self, context):
                return ResolvedCurseForgeArtifact(
                    provider=self.name,
                    project_id=context.project_id,
                    file_id=context.file_id,
                    file_name="summary.jar",
                    download_url=self.url,
                )

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "summary.jar"
            source.write_bytes(b"summary")
            pack = tmp_path / "sample.zip"
            write_zip(
                pack,
                {
                    "manifest.json": json.dumps(
                        {
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "CF Summary",
                            "version": "1.0.0",
                            "minecraft": {
                                "version": "1.20.1",
                                "modLoaders": [{"id": "fabric-0.18.4", "primary": True}],
                            },
                            "files": [{"projectID": 100, "fileID": 200, "required": True}],
                            "overrides": "overrides",
                        }
                    ),
                },
            )
            target = tmp_path / "server"

            report = ServerBuilder(
                cache_dir=tmp_path / "cache",
                download_remote=True,
                curseforge_providers=[Provider(source.as_uri())],
            ).build(pack, target)
            persisted = json.loads((target / "pack2serve/build-report.json").read_text(encoding="utf-8"))

            self.assertEqual((target / "mods/summary.jar").read_bytes(), b"summary")
            self.assertEqual(len(report.manual_actions), 0)
            self.assertEqual(persisted["curseforge_resolution"]["resolved"], 1)
            self.assertEqual(persisted["curseforge_resolution"]["unresolved"], 0)
            self.assertEqual(persisted["curseforge_resolution"]["providers"]["summary-provider"], 1)

    def test_server_builder_isolates_curseforge_non_jar_mod_artifacts(self) -> None:
        from pack2serve.downloader import ResolvedCurseForgeArtifact

        class Provider:
            name = "zip-provider"

            def __init__(self, url: str):
                self.url = url

            def resolve(self, context):
                return ResolvedCurseForgeArtifact(
                    provider=self.name,
                    project_id=context.project_id,
                    file_id=context.file_id,
                    file_name="LycanitesMobs32X.zip",
                    download_url=self.url,
                )

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "LycanitesMobs32X.zip"
            source.write_bytes(b"resource-pack")
            pack = tmp_path / "sample.zip"
            write_zip(
                pack,
                {
                    "manifest.json": json.dumps(
                        {
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "CF Zip Resource Pack",
                            "version": "1.0.0",
                            "minecraft": {
                                "version": "1.12.2",
                                "modLoaders": [{"id": "forge-14.23.5.2860", "primary": True}],
                            },
                            "files": [{"projectID": 224770, "fileID": 4486512, "required": True}],
                            "overrides": "overrides",
                        }
                    ),
                },
            )
            target = tmp_path / "server"

            report = ServerBuilder(
                cache_dir=tmp_path / "cache",
                download_remote=True,
                curseforge_providers=[Provider(source.as_uri())],
            ).build(pack, target)

            self.assertFalse((target / "mods/LycanitesMobs32X.zip").exists())
            self.assertEqual((target / "_client-overrides/mods/LycanitesMobs32X.zip").read_bytes(), b"resource-pack")
            self.assertEqual(report.copied_overrides[-1].classification, "client-remote-isolated")
            self.assertEqual(len(report.manual_actions), 0)

    def test_curseforge_api_provider_resolves_download_url(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        from pack2serve.downloader import CurseForgeApiProvider, CurseForgeFileContext

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/mods/561885/files/6290217/download-url":
                    body = json.dumps({"data": "https://files.example/example.jar"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = CurseForgeApiProvider(
                name="test-api",
                base_url=f"http://127.0.0.1:{server.server_port}",
            )

            artifact = provider.resolve(
                CurseForgeFileContext(project_id=561885, file_id=6290217, required=True)
            )

            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertEqual(artifact.file_name, "example.jar")
            self.assertEqual(artifact.download_url, "https://files.example/example.jar")
            self.assertEqual(artifact.provider, "test-api")
        finally:
            server.shutdown()
            server.server_close()

    def test_curseforge_maven_provider_resolves_slug_url(self) -> None:
        from pack2serve.downloader import CurseForgeFileContext, CurseForgeMavenProvider

        provider = CurseForgeMavenProvider(
            name="test-maven",
            base_url="https://www.cursemaven.com",
        )

        artifact = provider.resolve(
            CurseForgeFileContext(
                project_id=561885,
                file_id=6290217,
                required=True,
                slug="just-zoom",
            )
        )

        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual(artifact.provider, "test-maven")
        self.assertEqual(artifact.file_name, "just-zoom-561885-6290217.jar")
        self.assertEqual(
            artifact.download_url,
            "https://www.cursemaven.com/curse/maven/"
            "just-zoom-561885/6290217/just-zoom-561885-6290217.jar",
        )

    def test_curseforge_resolver_caches_resolved_artifact_metadata(self) -> None:
        from pack2serve.downloader import (
            ArtifactCache,
            CurseForgeResolver,
            ResolvedCurseForgeArtifact,
        )
        from pack2serve.models import RemoteFile

        class Provider:
            name = "fake-provider"

            def __init__(self, url: str):
                self.url = url

            def resolve(self, context):
                return ResolvedCurseForgeArtifact(
                    provider=self.name,
                    project_id=context.project_id,
                    file_id=context.file_id,
                    file_name="resolved.jar",
                    download_url=self.url,
                )

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "resolved.jar"
            source.write_bytes(b"resolved")
            resolver = CurseForgeResolver(
                ArtifactCache(tmp_path / "cache"),
                providers=[Provider(source.as_uri())],
            )

            artifact = resolver.resolve_and_cache(
                RemoteFile(
                    provider="curseforge",
                    target_path="mods",
                    project_id=561885,
                    file_id=6290217,
                    required=True,
                    slug="just-zoom",
                )
            )

            metadata = json.loads((artifact.path.with_suffix(".jar.pack2serve.json")).read_text(encoding="utf-8"))
            self.assertEqual(artifact.path.read_bytes(), b"resolved")
            self.assertEqual(artifact.path.parts[-4:], ("curseforge", "561885", "6290217", "resolved.jar"))
            self.assertEqual(metadata["provider"], "fake-provider")
            self.assertEqual(metadata["projectID"], 561885)
            self.assertEqual(metadata["fileID"], 6290217)
            self.assertEqual(metadata["downloadUrl"], source.as_uri())

    def test_curseforge_resolver_falls_through_to_second_provider(self) -> None:
        from pack2serve.downloader import (
            ArtifactCache,
            CurseForgeResolver,
            DownloadError,
            ResolvedCurseForgeArtifact,
        )
        from pack2serve.models import RemoteFile

        class FailingProvider:
            name = "first"

            def resolve(self, context):
                raise DownloadError("first failed")

        class WorkingProvider:
            name = "second"

            def __init__(self, url: str):
                self.url = url

            def resolve(self, context):
                return ResolvedCurseForgeArtifact(
                    provider=self.name,
                    project_id=context.project_id,
                    file_id=context.file_id,
                    file_name="second.jar",
                    download_url=self.url,
                )

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "second.jar"
            source.write_bytes(b"second")
            resolver = CurseForgeResolver(
                ArtifactCache(tmp_path / "cache"),
                providers=[FailingProvider(), WorkingProvider(source.as_uri())],
            )

            artifact = resolver.resolve_and_cache(
                RemoteFile(provider="curseforge", target_path="mods", project_id=1, file_id=2)
            )

            self.assertEqual(artifact.provider, "second")
            self.assertEqual(artifact.path.read_bytes(), b"second")

    def test_curseforge_resolver_reports_provider_errors_when_unresolved(self) -> None:
        from pack2serve.downloader import ArtifactCache, CurseForgeResolutionError, CurseForgeResolver, DownloadError
        from pack2serve.models import RemoteFile

        class FailingProvider:
            name = "broken"

            def resolve(self, context):
                raise DownloadError("network failed")

        with tempfile.TemporaryDirectory() as temp:
            resolver = CurseForgeResolver(ArtifactCache(Path(temp) / "cache"), providers=[FailingProvider()])

            with self.assertRaises(CurseForgeResolutionError) as raised:
                resolver.resolve_and_cache(
                    RemoteFile(provider="curseforge", target_path="mods", project_id=1, file_id=2)
                )

            self.assertEqual(raised.exception.provider_errors, ["broken: network failed"])

    def test_curseforge_resolver_reuses_project_file_cache_before_providers(self) -> None:
        from pack2serve.downloader import ArtifactCache, CurseForgeResolver
        from pack2serve.models import RemoteFile

        class NetworkProvider:
            name = "network"

            def resolve(self, context):
                raise AssertionError("provider should not be called when project/file cache exists")

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            cache_root = tmp_path / "cache"
            cached = cache_root / "curseforge" / "1" / "2" / "cached.jar"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"cached")
            resolver = CurseForgeResolver(ArtifactCache(cache_root), providers=[NetworkProvider()])

            artifact = resolver.resolve_and_cache(
                RemoteFile(provider="curseforge", target_path="mods", project_id=1, file_id=2)
            )

            self.assertEqual(artifact.provider, "cache")
            self.assertEqual(artifact.path, cached)
            self.assertEqual(artifact.path.read_bytes(), b"cached")

    def test_curseforge_resolver_reuses_non_jar_project_file_cache_before_providers(self) -> None:
        from pack2serve.downloader import ArtifactCache, CurseForgeResolver
        from pack2serve.models import RemoteFile

        class NetworkProvider:
            name = "network"

            def resolve(self, context):
                raise AssertionError("provider should not be called when non-jar project/file cache exists")

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            cache_root = tmp_path / "cache"
            cached = cache_root / "curseforge" / "1" / "2" / "resource-pack.zip"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"cached-zip")
            resolver = CurseForgeResolver(ArtifactCache(cache_root), providers=[NetworkProvider()])

            artifact = resolver.resolve_and_cache(
                RemoteFile(provider="curseforge", target_path="mods", project_id=1, file_id=2)
            )

            self.assertEqual(artifact.provider, "cache")
            self.assertEqual(artifact.path, cached)
            self.assertEqual(artifact.path.read_bytes(), b"cached-zip")

    def test_cli_inspect_returns_success_for_modrinth_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.mrpack"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "CLI Sample",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "forge": "47.4.20"},
                            "files": [],
                        }
                    ),
                },
            )

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["inspect", str(pack)])

            self.assertEqual(exit_code, 0)
            self.assertIn("CLI Sample", output.getvalue())

    def test_cli_build_download_flag_downloads_modrinth_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "remote.jar"
            source.write_bytes(b"cli-remote")
            sha1 = hashlib.sha1(source.read_bytes()).hexdigest()
            pack = tmp_path / "sample.mrpack"
            target = tmp_path / "server"
            cache = tmp_path / "cache"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "CLI Download",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "forge": "47.4.20"},
                            "files": [
                                {
                                    "path": "mods/remote.jar",
                                    "downloads": [source.as_uri()],
                                    "hashes": {"sha1": sha1},
                                    "fileSize": len(b"cli-remote"),
                                }
                            ],
                        }
                    ),
                },
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "build",
                        str(pack),
                        "--target",
                        str(target),
                        "--cache",
                        str(cache),
                        "--download",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual((target / "mods/remote.jar").read_bytes(), b"cli-remote")

    def test_cli_build_accepts_curseforge_mirror_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            mirror_root = tmp_path / "mirror"
            artifact_path = mirror_root / "10" / "20" / "file.jar"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_bytes(b"mirror-cli")
            pack = tmp_path / "sample.zip"
            target = tmp_path / "server"
            write_zip(
                pack,
                {
                    "manifest.json": json.dumps(
                        {
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "CF CLI",
                            "version": "1.0.0",
                            "minecraft": {
                                "version": "1.20.1",
                                "modLoaders": [{"id": "forge-47.4.20", "primary": True}],
                            },
                            "files": [{"projectID": 10, "fileID": 20, "required": True}],
                            "overrides": "overrides",
                        }
                    ),
                },
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "build",
                        str(pack),
                        "--target",
                        str(target),
                        "--download",
                        "--no-default-curseforge-providers",
                        "--curseforge-mirror",
                        (mirror_root / "{projectID}" / "{fileID}" / "file.jar").as_uri(),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual((target / "mods/10-20.jar").read_bytes(), b"mirror-cli")

    def test_cli_curseforge_providers_include_default_no_key_providers(self) -> None:
        from pack2serve.cli import _curseforge_providers

        with tempfile.TemporaryDirectory() as temp:
            providers = _curseforge_providers(Path(temp) / "cache", [])

        self.assertEqual([provider.name for provider in providers[:2]], ["curse-tools", "curse-maven"])

    def test_cli_no_default_curseforge_providers_disables_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            pack = tmp_path / "sample.zip"
            target = tmp_path / "server"
            cache = tmp_path / "cache"
            write_zip(
                pack,
                {
                    "manifest.json": json.dumps(
                        {
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "No Defaults",
                            "version": "1.0.0",
                            "minecraft": {
                                "version": "1.20.1",
                                "modLoaders": [{"id": "fabric-0.18.4", "primary": True}],
                            },
                            "files": [{"projectID": 10, "fileID": 20, "required": True}],
                            "overrides": "overrides",
                        }
                    ),
                },
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "build",
                        str(pack),
                        "--target",
                        str(target),
                        "--cache",
                        str(cache),
                        "--download",
                        "--no-default-curseforge-providers",
                    ]
                )

            persisted = json.loads((target / "pack2serve/build-report.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(persisted["manual_actions"]), 1)
            self.assertEqual(persisted["curseforge_resolution"]["providers"], {})

    def test_panel_service_uses_default_curseforge_providers_when_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = PanelService(workspace_dir=Path(temp) / "workspace")

            providers = service._curseforge_providers([])

        self.assertEqual([provider.name for provider in providers[:2]], ["curse-tools", "curse-maven"])

    def test_cli_install_loader_reads_plan_and_downloads_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            source = tmp_path / "server-source.jar"
            source.write_bytes(b"server")
            server_dir = tmp_path / "server"
            plan_dir = server_dir / "pack2serve"
            plan_dir.mkdir(parents=True)
            plan = LoaderInstallPlan(
                loader="fabric",
                loader_version="0.18.4",
                minecraft_version="1.20.1",
                kind="direct-server-jar",
                download_url=source.as_uri(),
                artifact_name="server.jar",
                artifact_path="server.jar",
                install_command=["download", source.as_uri(), "server.jar"],
                launch_command=["java", "-Xmx4G", "-jar", "server.jar", "nogui"],
                server_jar="server.jar",
                notes=[],
            )
            (plan_dir / "loader-install-plan.json").write_text(
                json.dumps(plan.to_json_dict()),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                exit_code = main(["install-loader", str(server_dir)])

            self.assertEqual(exit_code, 0)
            self.assertEqual((server_dir / "server.jar").read_bytes(), b"server")
            self.assertTrue((plan_dir / "loader-install-result.json").exists())

    def test_cli_install_java_reads_plan_downloads_runtime_and_rewrites_start_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            archive_path = tmp_path / "jre.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("jdk-17/bin/java.exe", b"fake-java")
            server_dir = tmp_path / "server"
            plan_dir = server_dir / "pack2serve"
            plan_dir.mkdir(parents=True)
            (server_dir / "start.ps1").write_text("$java = 'java'\n& $java -jar 'server.jar' nogui\n", encoding="utf-8")
            plan = create_java_runtime_install_plan(17, os_name="Windows", machine="AMD64")
            plan_data = plan.to_json_dict()
            plan_data["download_url"] = archive_path.as_uri()
            (plan_dir / "java-runtime-install-plan.json").write_text(json.dumps(plan_data), encoding="utf-8")

            with redirect_stdout(StringIO()):
                exit_code = main(["install-java", str(server_dir)])

            self.assertEqual(exit_code, 0)
            self.assertTrue((server_dir / "pack2serve/runtime/java/bin/java.exe").exists())
            self.assertIn("pack2serve\\runtime\\java\\bin\\java.exe", (server_dir / "start.ps1").read_text())

    def test_cli_validate_server_runs_custom_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_server.py"
            fake.write_text("print('Done (0.1s)! For help, type \"help\"')\n", encoding="utf-8")

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "validate-server",
                        str(server_dir),
                        "--command",
                        "python",
                        str(fake),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((server_dir / "pack2serve/validation-report.json").exists())

    def test_cli_reconfigures_stdout_for_replacement_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "bad_output.py"
            fake.write_text("import sys; sys.stdout.buffer.write(b'bad\\xae\\n')", encoding="utf-8")
            output_path = tmp_path / "stdout.txt"

            with output_path.open("w", encoding="ascii", errors="strict") as output:
                with redirect_stdout(output):
                    exit_code = main(
                        [
                            "validate-server",
                            str(server_dir),
                            "--command",
                            "python",
                            str(fake),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            self.assertIn("bad", output_path.read_text(encoding="utf-8"))

    def test_cli_accept_eula_requires_flag_and_updates_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            (server_dir / "eula.txt").write_text("eula=false\n", encoding="utf-8")

            with redirect_stdout(StringIO()):
                declined = main(["accept-eula", str(server_dir)])
            with redirect_stdout(StringIO()):
                accepted = main(["accept-eula", str(server_dir), "--i-agree"])

            self.assertEqual(declined, 1)
            self.assertEqual(accepted, 0)
            self.assertIn("eula=true", (server_dir / "eula.txt").read_text(encoding="utf-8"))

    def test_cli_prepare_builds_installs_and_validates_with_custom_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            remote = tmp_path / "server-source.jar"
            remote.write_bytes(b"server")
            fake = tmp_path / "fake_server.py"
            fake.write_text("print('Done (0.1s)! For help, type \"help\"')\n", encoding="utf-8")
            pack = tmp_path / "sample.mrpack"
            target = tmp_path / "server"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Prepare Pack",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                },
            )
            # Build first so the test can patch the generated plan to a local file URL.
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["build", str(pack), "--target", str(target)]), 0)
            plan_path = target / "pack2serve/loader-install-plan.json"
            plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
            plan_data["download_url"] = remote.as_uri()
            plan_data["install_command"] = ["download", remote.as_uri(), "server.jar"]
            plan_path.write_text(json.dumps(plan_data), encoding="utf-8")

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "prepare-existing",
                        str(target),
                        "--validate",
                        "--validation-command",
                        "python",
                        str(fake),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((target / "server.jar").exists())
            self.assertTrue((target / "pack2serve/validation-report.json").exists())

    def test_cli_prepare_builds_pack_installs_loader_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            loader_source = tmp_path / "fabric-server.jar"
            loader_source.write_bytes(b"server")
            fake = tmp_path / "fake_server.py"
            fake.write_text("print('Done (0.1s)! For help, type \"help\"')\n", encoding="utf-8")
            pack = tmp_path / "sample.mrpack"
            target = tmp_path / "server"
            write_zip(
                pack,
                {
                    "modrinth.index.json": json.dumps(
                        {
                            "formatVersion": 1,
                            "game": "minecraft",
                            "name": "Prepare Full",
                            "versionId": "1.0.0",
                            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.18.4"},
                            "files": [],
                        }
                    ),
                },
            )

            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "prepare",
                        str(pack),
                        "--target",
                        str(target),
                        "--loader-url-override",
                        loader_source.as_uri(),
                        "--validate",
                        "--validation-command",
                        "python",
                        str(fake),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual((target / "server.jar").read_bytes(), b"server")
            report = json.loads((target / "pack2serve/validation-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "started")

    def test_server_validator_detects_successful_start_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_server.py"
            fake.write_text(
                "print('Starting minecraft server version 1.20.1')\n"
                "print('Done (0.123s)! For help, type \"help\"')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "started")
            self.assertTrue((server_dir / "pack2serve/validation-report.json").exists())
            self.assertTrue((server_dir / "logs/pack2serve-validation.log").exists())

    def test_server_validator_stops_long_running_server_after_done_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_long_server.py"
            fake.write_text(
                "import time\n"
                "print('Done (0.1s)! For help, type \"help\"', flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=5,
            )

            self.assertEqual(result.status, "started")
            self.assertFalse(result.timed_out)

    def test_server_validator_detects_crash_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_crash.py"
            fake.write_text(
                "import sys\n"
                "print('Crash report saved to crash-reports/crash.txt')\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "crashed")
            self.assertIn("Crash report", result.combined_output)

    def test_server_validator_detects_java_exception_even_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_exception.py"
            fake.write_text(
                "print('Exception in thread \"main\" java.lang.RuntimeException: failed')\n"
                "print('Caused by: java.nio.file.AccessDeniedException: server.jar')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "failed")
            self.assertTrue(any("permission" in hint.lower() for hint in result.hints))

    def test_server_validator_detects_legacy_forge_java_incompatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_legacy_forge.py"
            fake.write_text(
                "print('A problem occurred running the Server launcher.java.lang.reflect.InvocationTargetException')\n"
                "print('Caused by: java.lang.ClassCastException: AppClassLoader cannot be cast to URLClassLoader')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "failed")
            self.assertTrue(any("Java runtime" in hint for hint in result.hints))

    def test_server_validator_hints_java_runtime_incompatibility_for_major_version_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_major.py"
            fake.write_text(
                "print('java.lang.IllegalArgumentException: Unsupported class file major version 70')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "failed")
            self.assertTrue(any("Java runtime" in hint for hint in result.hints))

    def test_server_validator_hints_missing_mod_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_missing_mods.py"
            fake.write_text(
                "print('net.minecraftforge.fml.common.MissingModsException: Mod requires [librarymod]')\n"
                "print('Caused by: java.lang.NoClassDefFoundError: net/fabricmc/fabric/api/client/screen/v1/ScreenEvents')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "failed")
            self.assertTrue(any("dependency" in hint.lower() for hint in result.hints))

    def test_server_validator_detects_port_binding_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_port.py"
            fake.write_text(
                "print('**** FAILED TO BIND TO PORT!')\n"
                "print('java.net.BindException: Address already in use: bind')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "failed")
            self.assertTrue(any("port" in hint.lower() for hint in result.hints))

    def test_server_validator_does_not_keep_port_hint_after_successful_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_port_then_started.py"
            fake.write_text(
                "print('Address already in use appeared in an old diagnostic')\n"
                "print('Done (1.0s)! For help, type \"help\"')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "started")
            self.assertFalse(any("port" in hint.lower() for hint in result.hints))

    def test_server_validator_does_not_fail_on_optional_mixin_class_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_optional_probe.py"
            fake.write_text(
                "print('Error loading class: net/coderbot/iris/Iris "
                "(java.lang.ClassNotFoundException: net/coderbot/iris/Iris)')\n"
                "print('Done (1.0s)! For help, type \"help\"')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "started")

    def test_server_validator_does_not_fail_on_invalid_dist_probe_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_invalid_dist_probe.py"
            fake.write_text(
                "print('Attempted to load class net/minecraft/client/MouseHandler for invalid dist DEDICATED_SERVER')\n"
                "print('java.lang.RuntimeException: Attempted to load class net/minecraft/client/MouseHandler')\n"
                "print('Done (1.0s)! For help, type \"help\"')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "started")

    def test_server_validator_does_not_hint_dependency_for_unrelated_missing_words(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_missing_words.py"
            fake.write_text(
                "print('RLMixins Early Loading: Missing Particle Rendering')\n"
                "print('Done (1.0s)! For help, type \"help\"')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "started")
            self.assertFalse(any("dependency" in hint.lower() for hint in result.hints))

    def test_server_validator_does_not_hint_dependency_for_successful_forge_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_forge_handshake.py"
            fake.write_text(
                "print('Attempting connection with missing mods [minecraft, forge] at SERVER')\n"
                "print('Done (1.0s)! For help, type \"help\"')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "started")
            self.assertFalse(any("dependency" in hint.lower() for hint in result.hints))

    def test_server_validator_replaces_invalid_output_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()

            result = ServerValidator().validate(
                server_dir,
                command=["python", "-c", "import sys; sys.stdout.buffer.write(b'bad\\xae\\n')"],
                timeout_seconds=10,
            )

            self.assertIn("bad", result.combined_output)
            self.assertEqual(result.status, "exited")

    def test_server_validator_detects_eula_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            server_dir = tmp_path / "server"
            server_dir.mkdir()
            fake = tmp_path / "fake_eula.py"
            fake.write_text(
                "print('You need to agree to the EULA in order to run the server.')\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(
                server_dir,
                command=["python", str(fake)],
                timeout_seconds=10,
            )

            self.assertEqual(result.status, "needs-eula")
            self.assertTrue(any("EULA" in hint for hint in result.hints))

    def test_server_validator_default_command_handles_relative_server_dir(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            server_dir = Path(temp)
            relative_server_dir = server_dir.relative_to(Path.cwd())
            (server_dir / "start.ps1").write_text(
                "Write-Output 'Done (0.1s)! For help, type help'\n",
                encoding="utf-8",
            )

            result = ServerValidator().validate(relative_server_dir, timeout_seconds=10)

            self.assertEqual(result.status, "started")


if __name__ == "__main__":
    unittest.main()
