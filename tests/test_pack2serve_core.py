import json
import hashlib
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from pack2serve.builder import ServerBuilder
from pack2serve.cli import main
from pack2serve.downloader import ArtifactCache, CurseForgeTemplateMirrorProvider, ModrinthDirectProvider
from pack2serve.java import java_status, required_java_major
from pack2serve.loader import LoaderInstallPlan, create_loader_install_plan
from pack2serve.installer import LoaderInstaller
from pack2serve.parser import ModpackFormat, parse_modpack
from pack2serve.validator import ServerValidator


def write_zip(path: Path, files: dict[str, str | bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


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
            self.assertTrue((target / "_client-overrides/shaderpacks/client.zip").exists())
            self.assertTrue((target / "_client-overrides/options.txt").exists())
            self.assertTrue((target / "world/level.dat").exists())
            self.assertTrue((target / "pack2serve/build-report.json").exists())
            self.assertTrue((target / "pack2serve/loader-install-plan.json").exists())
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
                        "--curseforge-mirror",
                        (mirror_root / "{projectID}" / "{fileID}" / "file.jar").as_uri(),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual((target / "mods/10-20.jar").read_bytes(), b"mirror-cli")

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
