from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from pack2serve.downloader import (
    ArtifactCache,
    CurseForgeTemplateMirrorProvider,
    DownloadError,
    ModrinthDirectProvider,
    copy_cached_artifact,
)
from pack2serve.java import plan_java
from pack2serve.loader import create_loader_install_plan
from pack2serve.models import BuildReport, CopiedOverride, ManualAction, ModpackFormat, RemoteFile
from pack2serve.parser import parse_modpack
from pack2serve.zip_utils import copy_member, require_safe_zip


SERVER_DIRECTORIES = {
    "config",
    "defaultconfigs",
    "kubejs",
    "scripts",
    "datapacks",
    "mods",
    "world",
}

CLIENT_DIRECTORIES = {
    "shaderpacks",
    "resourcepacks",
    "xaero",
    "PCL",
    "CustomSkinLoader",
    "screenshots",
}

CLIENT_FILES = {
    "options.txt",
    "optionsof.txt",
    "icon.png",
    "profileImage",
    "TrashSlotSaveState.json",
}


class ServerBuilder:
    def __init__(
        self,
        cache_dir: str | Path = "data/cache",
        download_remote: bool = False,
        curseforge_providers: list[CurseForgeTemplateMirrorProvider] | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.download_remote = download_remote
        self.curseforge_providers = curseforge_providers or []

    def build(self, pack_path: str | Path, target_dir: str | Path) -> BuildReport:
        pack = parse_modpack(pack_path)
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        (target / "mods").mkdir()
        (target / "pack2serve").mkdir()

        java = plan_java(pack.minecraft_version)
        copied: list[CopiedOverride] = []
        manual_actions: list[ManualAction] = []

        with zipfile.ZipFile(pack.source_path) as archive:
            require_safe_zip(archive)
            copied = self._copy_overrides(archive, pack.override_root, target)

        manual_actions.extend(self._resolve_remote_files(pack.format, pack.remote_files, target))

        report = BuildReport(
            pack=pack,
            target_dir=target,
            java=java,
            downloads=pack.remote_files,
            copied_overrides=copied,
            manual_actions=manual_actions,
        )
        self._write_project_files(report)
        return report

    def _resolve_remote_files(
        self, pack_format: ModpackFormat, remote_files: list[RemoteFile], target: Path
    ) -> list[ManualAction]:
        if pack_format == ModpackFormat.CURSEFORGE:
            return self._resolve_curseforge_files(remote_files, target)
        if not self.download_remote:
            return []

        provider = ModrinthDirectProvider(ArtifactCache(self.cache_dir))
        actions: list[ManualAction] = []
        for remote in remote_files:
            try:
                artifact = provider.resolve_and_cache(remote)
                copy_cached_artifact(artifact, target / Path(remote.target_path))
            except DownloadError as exc:
                actions.append(
                    ManualAction(
                        type="download-failed",
                        message=str(exc),
                        details={"targetPath": remote.target_path, "provider": remote.provider},
                    )
                )
        return actions

    def _resolve_curseforge_files(self, remote_files: list[RemoteFile], target: Path) -> list[ManualAction]:
        actions: list[ManualAction] = []
        if not self.download_remote:
            providers: list[CurseForgeTemplateMirrorProvider] = []
        else:
            providers = self.curseforge_providers
        for remote in remote_files:
            resolved = False
            last_error: str | None = None
            for provider in providers:
                try:
                    artifact = provider.resolve_and_cache(remote)
                    copy_cached_artifact(artifact, target / "mods" / artifact.path.name)
                    resolved = True
                    break
                except DownloadError as exc:
                    last_error = str(exc)
            if not resolved:
                actions.append(
                    ManualAction(
                        type="missing-curseforge-artifact",
                        message="No-key CurseForge mode requires a configured mirror provider or manual jar upload.",
                        details={
                            "projectID": remote.project_id,
                            "fileID": remote.file_id,
                            "required": remote.required,
                            "lastError": last_error,
                        },
                    )
                )
        return actions

    def _copy_overrides(
        self, archive: zipfile.ZipFile, override_root: str, target: Path
    ) -> list[CopiedOverride]:
        prefix = override_root.strip("/").rstrip("/") + "/"
        members = [member for member in archive.infolist() if not member.is_dir() and member.filename.startswith(prefix)]
        save_roots = self._save_roots(members, prefix)
        copied: list[CopiedOverride] = []
        for member in members:
            relative = member.filename[len(prefix) :]
            if not relative:
                continue
            destination, classification = self._classify_destination(relative, save_roots, target)
            copy_member(archive, member, destination)
            copied.append(
                CopiedOverride(
                    source=member.filename,
                    destination=str(destination.relative_to(target)).replace("\\", "/"),
                    classification=classification,
                    size=member.file_size,
                )
            )
        return copied

    def _save_roots(self, members: list[zipfile.ZipInfo], prefix: str) -> set[str]:
        roots: set[str] = set()
        for member in members:
            relative = member.filename[len(prefix) :]
            parts = PurePosixPath(relative).parts
            if len(parts) >= 2 and parts[0] == "saves":
                roots.add(parts[1])
        return roots

    def _classify_destination(
        self, relative: str, save_roots: set[str], target: Path
    ) -> tuple[Path, str]:
        parts = PurePosixPath(relative).parts
        top = parts[0]
        if top == "saves" and len(save_roots) == 1 and len(parts) >= 3:
            return target / "world" / Path(*parts[2:]), "world-template"
        if top == "saves":
            return target / "_world-templates" / Path(*parts[1:]), "world-template"
        if top in CLIENT_DIRECTORIES or top in CLIENT_FILES:
            return target / "_client-overrides" / Path(*parts), "client-isolated"
        if top in SERVER_DIRECTORIES:
            return target / Path(*parts), "server-copied"
        if relative == "server.properties" or relative.lower().endswith(".properties"):
            return target / Path(*parts), "server-copied"
        return target / "_unknown-overrides" / Path(*parts), "unknown-isolated"

    def _write_project_files(self, report: BuildReport) -> None:
        pack2serve = report.target_dir / "pack2serve"
        self._ensure_server_defaults(report.target_dir)
        (pack2serve / "build-report.json").write_text(
            json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (pack2serve / "download-plan.json").write_text(
            json.dumps([_remote_to_dict(remote) for remote in report.downloads], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (pack2serve / "java-plan.json").write_text(
            json.dumps(report.java.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (pack2serve / "loader-install-plan.json").write_text(
            json.dumps(_loader_plan(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (report.target_dir / "start.ps1").write_text(_start_script(report), encoding="utf-8")

    def _ensure_server_defaults(self, target: Path) -> None:
        eula = target / "eula.txt"
        if not eula.exists():
            eula.write_text(
                "# Set eula=true after reading https://aka.ms/MinecraftEULA\n"
                "eula=false\n",
                encoding="utf-8",
            )
        server_properties = target / "server.properties"
        if not server_properties.exists():
            server_properties.write_text(
                "server-port=25565\n"
                "level-name=world\n"
                "online-mode=true\n"
                "motd=Pack2Serve generated server\n",
                encoding="utf-8",
            )


def _remote_to_dict(remote: object) -> dict[str, object]:
    return dict(remote.__dict__)


def _loader_plan(report: BuildReport) -> dict[str, object]:
    loader = report.pack.loader
    return create_loader_install_plan(
        loader.name,
        loader.version,
        report.pack.minecraft_version,
    ).to_json_dict()


def _start_script(report: BuildReport) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$java = 'java'\n"
        "$args = @('-Xms1G', '-Xmx4G')\n"
        "# Pack2Serve will replace this placeholder after loader installation.\n"
        "$serverJar = 'server.jar'\n"
        "if (!(Test-Path $serverJar)) {\n"
        "  Write-Error 'server.jar is missing. Run the loader installation step first.'\n"
        "}\n"
        "& $java @args -jar $serverJar nogui\n"
    )
