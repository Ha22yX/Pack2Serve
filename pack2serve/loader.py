from __future__ import annotations

from dataclasses import asdict, dataclass


FABRIC_INSTALLER_VERSION = "1.1.1"


@dataclass(frozen=True)
class LoaderInstallPlan:
    loader: str
    loader_version: str
    minecraft_version: str
    kind: str
    download_url: str
    artifact_name: str
    artifact_path: str
    install_command: list[str]
    launch_command: list[str]
    server_jar: str | None
    notes: list[str]

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


def create_loader_install_plan(
    loader: str,
    loader_version: str,
    minecraft_version: str,
) -> LoaderInstallPlan:
    normalized = _normalize_loader(loader)
    if normalized == "fabric":
        return _fabric_plan(loader_version, minecraft_version)
    if normalized == "forge":
        return _forge_plan(loader_version, minecraft_version)
    if normalized == "neoforge":
        return _neoforge_plan(loader_version, minecraft_version)
    return _unknown_plan(loader, loader_version, minecraft_version)


def _normalize_loader(loader: str) -> str:
    if loader in {"fabric-loader", "fabric"}:
        return "fabric"
    if loader == "quilt-loader":
        return "quilt"
    return loader


def _fabric_plan(loader_version: str, minecraft_version: str) -> LoaderInstallPlan:
    artifact_name = "server.jar"
    url = (
        "https://meta.fabricmc.net/v2/versions/loader/"
        f"{minecraft_version}/{loader_version}/{FABRIC_INSTALLER_VERSION}/server/jar"
    )
    return LoaderInstallPlan(
        loader="fabric",
        loader_version=loader_version,
        minecraft_version=minecraft_version,
        kind="direct-server-jar",
        download_url=url,
        artifact_name=artifact_name,
        artifact_path=artifact_name,
        install_command=["download", url, artifact_name],
        launch_command=["java", "-Xmx4G", "-jar", artifact_name, "nogui"],
        server_jar=artifact_name,
        notes=[
            "Fabric provides an executable server launcher jar through the metadata API.",
            "Most Fabric modpacks also require Fabric API in the mods folder.",
        ],
    )


def _forge_plan(loader_version: str, minecraft_version: str) -> LoaderInstallPlan:
    version = f"{minecraft_version}-{loader_version}"
    artifact_name = f"forge-{version}-installer.jar"
    url = (
        "https://maven.minecraftforge.net/net/minecraftforge/forge/"
        f"{version}/{artifact_name}"
    )
    return LoaderInstallPlan(
        loader="forge",
        loader_version=loader_version,
        minecraft_version=minecraft_version,
        kind="installer-jar",
        download_url=url,
        artifact_name=artifact_name,
        artifact_path=f"pack2serve/loaders/{artifact_name}",
        install_command=["java", "-jar", f"pack2serve/loaders/{artifact_name}", "--installServer"],
        launch_command=["powershell", "-ExecutionPolicy", "Bypass", "-File", "start.ps1"],
        server_jar=None,
        notes=[
            "Forge server installation requires running the installer jar inside the server directory.",
            "The installer creates the final launch files and libraries.",
        ],
    )


def _neoforge_plan(loader_version: str, minecraft_version: str) -> LoaderInstallPlan:
    artifact_name = f"neoforge-{loader_version}-installer.jar"
    url = (
        "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
        f"{loader_version}/{artifact_name}"
    )
    return LoaderInstallPlan(
        loader="neoforge",
        loader_version=loader_version,
        minecraft_version=minecraft_version,
        kind="installer-jar",
        download_url=url,
        artifact_name=artifact_name,
        artifact_path=f"pack2serve/loaders/{artifact_name}",
        install_command=["java", "-jar", f"pack2serve/loaders/{artifact_name}", "--installServer"],
        launch_command=["powershell", "-ExecutionPolicy", "Bypass", "-File", "run.bat"],
        server_jar=None,
        notes=[
            "NeoForge server installation requires running the installer jar inside the server directory.",
            "After installation, NeoForge creates run scripts and user_jvm_args.txt.",
        ],
    )


def _unknown_plan(loader: str, loader_version: str, minecraft_version: str) -> LoaderInstallPlan:
    return LoaderInstallPlan(
        loader=loader,
        loader_version=loader_version,
        minecraft_version=minecraft_version,
        kind="manual",
        download_url="",
        artifact_name="",
        artifact_path="",
        install_command=[],
        launch_command=[],
        server_jar=None,
        notes=["No automatic installer is implemented for this loader yet."],
    )
