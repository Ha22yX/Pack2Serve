from __future__ import annotations

import argparse
import json
from pathlib import Path

from pack2serve.builder import ServerBuilder
from pack2serve.downloader import ArtifactCache, CurseForgeTemplateMirrorProvider
from pack2serve.installer import LoaderInstaller, load_loader_plan
from pack2serve.parser import parse_modpack
from pack2serve.validator import ServerValidator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pack2serve")
    subcommands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subcommands.add_parser("inspect", help="Inspect a modpack archive")
    inspect_parser.add_argument("pack", type=Path)

    build_parser = subcommands.add_parser("build", help="Generate a server project")
    build_parser.add_argument("pack", type=Path)
    build_parser.add_argument("--target", type=Path, required=True)
    build_parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    build_parser.add_argument("--download", action="store_true")
    build_parser.add_argument(
        "--curseforge-mirror",
        action="append",
        default=[],
        help="No-key CurseForge mirror URL template with {projectID} and {fileID}",
    )

    prepare_parser = subcommands.add_parser(
        "prepare", help="Build, install loader, and optionally validate a modpack server"
    )
    prepare_parser.add_argument("pack", type=Path)
    prepare_parser.add_argument("--target", type=Path, required=True)
    prepare_parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    prepare_parser.add_argument("--download", action="store_true")
    prepare_parser.add_argument("--curseforge-mirror", action="append", default=[])
    prepare_parser.add_argument("--execute-installers", action="store_true")
    prepare_parser.add_argument("--loader-url-override")
    prepare_parser.add_argument("--validate", action="store_true")
    prepare_parser.add_argument("--timeout", type=int, default=120)
    prepare_parser.add_argument("--validation-command", nargs="+")

    install_parser = subcommands.add_parser(
        "install-loader", help="Download and optionally execute the generated loader install plan"
    )
    install_parser.add_argument("server_dir", type=Path)
    install_parser.add_argument("--execute-installers", action="store_true")

    validate_parser = subcommands.add_parser(
        "validate-server", help="Run a first-start validation command for a generated server"
    )
    validate_parser.add_argument("server_dir", type=Path)
    validate_parser.add_argument("--timeout", type=int, default=120)
    validate_parser.add_argument("--command", dest="validation_command", nargs="+")

    prepare_existing_parser = subcommands.add_parser(
        "prepare-existing", help="Install loader and optionally validate an existing generated server"
    )
    prepare_existing_parser.add_argument("server_dir", type=Path)
    prepare_existing_parser.add_argument("--execute-installers", action="store_true")
    prepare_existing_parser.add_argument("--validate", action="store_true")
    prepare_existing_parser.add_argument("--timeout", type=int, default=120)
    prepare_existing_parser.add_argument("--validation-command", nargs="+")

    args = parser.parse_args(argv)
    if args.command == "inspect":
        pack = parse_modpack(args.pack)
        print(
            json.dumps(
                {
                    "format": pack.format,
                    "name": pack.name,
                    "version": pack.version,
                    "minecraftVersion": pack.minecraft_version,
                    "loader": {
                        "name": pack.loader.name,
                        "version": pack.loader.version,
                    },
                    "remoteFiles": len(pack.remote_files),
                    "overrideRoot": pack.override_root,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "build":
        curseforge_providers = _curseforge_providers(args.cache, args.curseforge_mirror)
        report = ServerBuilder(
            cache_dir=args.cache,
            download_remote=args.download,
            curseforge_providers=curseforge_providers,
        ).build(args.pack, args.target)
        print(
            json.dumps(
                {
                    "target": str(report.target_dir),
                    "format": report.pack.format,
                    "name": report.pack.name,
                    "minecraftVersion": report.pack.minecraft_version,
                    "loader": report.pack.loader.__dict__,
                    "remoteFiles": len(report.downloads),
                    "copiedOverrides": len(report.copied_overrides),
                    "manualActions": len(report.manual_actions),
                    "java": report.java.__dict__,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "prepare":
        curseforge_providers = _curseforge_providers(args.cache, args.curseforge_mirror)
        build_report = ServerBuilder(
            cache_dir=args.cache,
            download_remote=args.download,
            curseforge_providers=curseforge_providers,
        ).build(args.pack, args.target)
        plan_path = args.target / "pack2serve" / "loader-install-plan.json"
        if args.loader_url_override:
            _override_loader_url(plan_path, args.loader_url_override)
        plan = load_loader_plan(plan_path)
        install_result = LoaderInstaller().install(
            args.target,
            plan,
            execute_installers=args.execute_installers,
        )
        validation_result = None
        if args.validate:
            validation_result = ServerValidator().validate(
                args.target,
                command=args.validation_command,
                timeout_seconds=args.timeout,
            )
        print(
            json.dumps(
                {
                    "build": {
                        "target": str(build_report.target_dir),
                        "manualActions": len(build_report.manual_actions),
                        "remoteFiles": len(build_report.downloads),
                    },
                    "loader": install_result.to_json_dict(),
                    "validation": validation_result.to_json_dict() if validation_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "install-loader":
        plan_path = args.server_dir / "pack2serve" / "loader-install-plan.json"
        plan = load_loader_plan(plan_path)
        result = LoaderInstaller().install(
            args.server_dir,
            plan,
            execute_installers=args.execute_installers,
        )
        print(json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "validate-server":
        result = ServerValidator().validate(
            args.server_dir,
            command=args.validation_command,
            timeout_seconds=args.timeout,
        )
        print(json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "prepare-existing":
        plan_path = args.server_dir / "pack2serve" / "loader-install-plan.json"
        plan = load_loader_plan(plan_path)
        install_result = LoaderInstaller().install(
            args.server_dir,
            plan,
            execute_installers=args.execute_installers,
        )
        validation_result = None
        if args.validate:
            validation_result = ServerValidator().validate(
                args.server_dir,
                command=args.validation_command,
                timeout_seconds=args.timeout,
            )
        print(
            json.dumps(
                {
                    "loader": install_result.to_json_dict(),
                    "validation": validation_result.to_json_dict() if validation_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    return 2


def _curseforge_providers(cache_dir: Path, templates: list[str]) -> list[CurseForgeTemplateMirrorProvider]:
    cache = ArtifactCache(cache_dir)
    return [
        CurseForgeTemplateMirrorProvider(
            cache=cache,
            name=f"curseforge-mirror-{index + 1}",
            url_template=template,
        )
        for index, template in enumerate(templates)
    ]


def _override_loader_url(plan_path: Path, url: str) -> None:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    data["download_url"] = url
    if data.get("kind") == "direct-server-jar":
        data["install_command"] = ["download", url, data.get("artifact_path", "server.jar")]
    plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
