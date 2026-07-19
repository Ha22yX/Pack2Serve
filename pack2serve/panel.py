from __future__ import annotations

import json
import re
from pathlib import Path

from pack2serve.builder import ServerBuilder
from pack2serve.downloader import ArtifactCache, CurseForgeTemplateMirrorProvider


class PanelService:
    def __init__(self, workspace_dir: str | Path = "data"):
        self.workspace_dir = Path(workspace_dir)
        self.servers_dir = self.workspace_dir / "servers"
        self.cache_dir = self.workspace_dir / "cache"

    def import_pack(
        self,
        pack_path: str | Path,
        *,
        target_name: str | None = None,
        download: bool = False,
        curseforge_mirrors: list[str] | None = None,
    ) -> dict[str, object]:
        pack_path = Path(pack_path)
        providers = self._curseforge_providers(curseforge_mirrors or [])
        parsed_name = target_name or pack_path.stem
        target_slug = _slugify(parsed_name)
        target = self.servers_dir / target_slug
        report = ServerBuilder(
            cache_dir=self.cache_dir,
            download_remote=download,
            curseforge_providers=providers,
        ).build(pack_path, target)
        return _summary_from_report(target_slug, report.to_json_dict())

    def list_servers(self) -> list[dict[str, object]]:
        if not self.servers_dir.exists():
            return []
        servers: list[dict[str, object]] = []
        for report_path in sorted(self.servers_dir.glob("*/pack2serve/build-report.json")):
            data = json.loads(report_path.read_text(encoding="utf-8"))
            servers.append(_summary_from_report(report_path.parents[1].name, data))
        return servers

    def _curseforge_providers(self, mirrors: list[str]) -> list[CurseForgeTemplateMirrorProvider]:
        cache = ArtifactCache(self.cache_dir)
        return [
            CurseForgeTemplateMirrorProvider(
                cache=cache,
                name=f"panel-curseforge-mirror-{index + 1}",
                url_template=mirror,
            )
            for index, mirror in enumerate(mirrors)
        ]


def _summary_from_report(target_name: str, report: dict[str, object]) -> dict[str, object]:
    pack = report["pack"]
    loader = pack["loader"]
    return {
        "targetName": target_name,
        "target": report["target_dir"],
        "format": pack["format"],
        "name": pack["name"],
        "version": pack["version"],
        "minecraftVersion": pack["minecraft_version"],
        "loader": f"{loader['name']} {loader['version']}",
        "remoteFiles": len(report["downloads"]),
        "copiedOverrides": len(report["copied_overrides"]),
        "manualActions": len(report["manual_actions"]),
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._").lower()
    return slug or "server"
