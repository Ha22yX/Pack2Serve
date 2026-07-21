from __future__ import annotations

import hashlib
import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from pack2serve.models import RemoteFile


@dataclass(frozen=True)
class CachedArtifact:
    provider: str
    key: str
    path: Path
    size: int


@dataclass(frozen=True)
class CurseForgeFileContext:
    project_id: int
    file_id: int
    required: bool
    slug: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class ResolvedCurseForgeArtifact:
    provider: str
    project_id: int
    file_id: int
    file_name: str
    download_url: str
    size: int | None = None
    hashes: dict[str, str] = field(default_factory=dict)


class DownloadError(RuntimeError):
    pass


class ArtifactCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path_for(self, provider: str, key: str, file_name: str) -> Path:
        safe_name = file_name.replace("/", "_").replace("\\", "_")
        return self.root / provider / key / safe_name

    def metadata_path_for(self, artifact_path: Path) -> Path:
        return artifact_path.with_suffix(artifact_path.suffix + ".pack2serve.json")

    def has_valid(self, path: Path, remote: RemoteFile) -> bool:
        if not path.exists():
            return False
        if remote.size is not None and path.stat().st_size != remote.size:
            return False
        return _hashes_match(path, remote.hashes)

    def remember(self, provider: str, key: str, artifact_path: Path, remote: RemoteFile) -> CachedArtifact:
        metadata = {
            "provider": provider,
            "key": key,
            "targetPath": remote.target_path,
            "size": artifact_path.stat().st_size,
            "hashes": remote.hashes,
        }
        self.metadata_path_for(artifact_path).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return CachedArtifact(
            provider=provider,
            key=key,
            path=artifact_path,
            size=artifact_path.stat().st_size,
        )

    def curseforge_path_for(self, project_id: int, file_id: int, file_name: str) -> Path:
        return self.root / "curseforge" / str(project_id) / str(file_id) / _safe_file_name(file_name)

    def remember_curseforge(self, artifact: ResolvedCurseForgeArtifact, artifact_path: Path) -> CachedArtifact:
        metadata = {
            "provider": artifact.provider,
            "projectID": artifact.project_id,
            "fileID": artifact.file_id,
            "fileName": artifact.file_name,
            "downloadUrl": artifact.download_url,
            "size": artifact_path.stat().st_size,
            "hashes": artifact.hashes,
            "downloadedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        self.metadata_path_for(artifact_path).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return CachedArtifact(
            provider=artifact.provider,
            key=f"{artifact.project_id}-{artifact.file_id}",
            path=artifact_path,
            size=artifact_path.stat().st_size,
        )

    def find_curseforge(self, project_id: int, file_id: int) -> CachedArtifact | None:
        root = self.root / "curseforge" / str(project_id) / str(file_id)
        if not root.exists():
            return None
        for path in sorted(root.iterdir()):
            if path.is_file() and path.stat().st_size > 0:
                if path.name.endswith(".pack2serve.json"):
                    continue
                return CachedArtifact(
                    provider="cache",
                    key=f"{project_id}-{file_id}",
                    path=path,
                    size=path.stat().st_size,
                )
        return None


class ModrinthDirectProvider:
    name = "modrinth-direct"

    def __init__(self, cache: ArtifactCache):
        self.cache = cache

    def resolve_and_cache(self, remote: RemoteFile) -> CachedArtifact:
        if not remote.downloads:
            raise DownloadError(f"Modrinth file has no download URL: {remote.target_path}")
        file_name = Path(remote.target_path).name
        key = _cache_key(remote)
        destination = self.cache.path_for(self.name, key, file_name)
        if self.cache.has_valid(destination, remote):
            return self.cache.remember(self.name, key, destination, remote)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".tmp")
        _download(remote.downloads[0], temp_path)
        if remote.size is not None and temp_path.stat().st_size != remote.size:
            temp_path.unlink(missing_ok=True)
            raise DownloadError(
                f"Downloaded size mismatch for {remote.target_path}: "
                f"expected {remote.size}, got {temp_path.stat().st_size}"
            )
        if not _hashes_match(temp_path, remote.hashes):
            temp_path.unlink(missing_ok=True)
            raise DownloadError(f"Downloaded hash mismatch for {remote.target_path}")
        temp_path.replace(destination)
        return self.cache.remember(self.name, key, destination, remote)


class CurseForgeApiProvider:
    def __init__(self, name: str = "curse-tools", base_url: str = "https://api.curse.tools/v1/cf"):
        self.name = name
        self.base_url = base_url.rstrip("/")

    def resolve(self, context: CurseForgeFileContext) -> ResolvedCurseForgeArtifact | None:
        url = f"{self.base_url}/mods/{context.project_id}/files/{context.file_id}/download-url"
        try:
            payload = _read_json_url(url)
        except DownloadError:
            raise
        download_url = payload.get("data")
        if not isinstance(download_url, str) or not download_url:
            raise DownloadError(f"{self.name} returned no download URL for {context.project_id}/{context.file_id}")
        return ResolvedCurseForgeArtifact(
            provider=self.name,
            project_id=context.project_id,
            file_id=context.file_id,
            file_name=Path(urlparse(download_url).path).name,
            download_url=download_url,
        )


class CurseForgeMavenProvider:
    def __init__(self, name: str = "curse-maven", base_url: str = "https://www.cursemaven.com"):
        self.name = name
        self.base_url = base_url.rstrip("/")

    def resolve(self, context: CurseForgeFileContext) -> ResolvedCurseForgeArtifact | None:
        if not context.slug:
            return None
        artifact_name = f"{context.slug}-{context.project_id}-{context.file_id}.jar"
        download_url = (
            f"{self.base_url}/curse/maven/"
            f"{context.slug}-{context.project_id}/{context.file_id}/{artifact_name}"
        )
        return ResolvedCurseForgeArtifact(
            provider=self.name,
            project_id=context.project_id,
            file_id=context.file_id,
            file_name=artifact_name,
            download_url=download_url,
        )


def default_curseforge_providers() -> list[object]:
    return [CurseForgeApiProvider(), CurseForgeMavenProvider()]


class CurseForgeResolutionError(DownloadError):
    def __init__(self, message: str, provider_errors: list[str]):
        super().__init__(message)
        self.provider_errors = provider_errors


class CurseForgeResolver:
    def __init__(self, cache: ArtifactCache, providers: list[object]):
        self.cache = cache
        self.providers = providers

    def resolve_and_cache(self, remote: RemoteFile) -> CachedArtifact:
        if remote.project_id is None or remote.file_id is None:
            raise CurseForgeResolutionError(
                "CurseForge remote file is missing projectID or fileID",
                ["manifest: missing projectID or fileID"],
            )
        context = CurseForgeFileContext(
            project_id=remote.project_id,
            file_id=remote.file_id,
            required=remote.required,
            slug=remote.slug,
            display_name=remote.display_name,
        )
        cached = self.cache.find_curseforge(context.project_id, context.file_id)
        if cached is not None:
            return cached

        provider_errors: list[str] = []
        for provider in self.providers:
            name = getattr(provider, "name", provider.__class__.__name__)
            try:
                resolved = provider.resolve(context)
                if resolved is None:
                    provider_errors.append(f"{name}: not resolved")
                    continue
                artifact = self._download_resolved(resolved)
                if artifact.size <= 0:
                    provider_errors.append(f"{name}: downloaded empty artifact")
                    artifact.path.unlink(missing_ok=True)
                    continue
                return artifact
            except DownloadError as exc:
                provider_errors.append(f"{name}: {exc}")
        raise CurseForgeResolutionError(
            f"Could not resolve CurseForge artifact {remote.project_id}/{remote.file_id}",
            provider_errors,
        )

    def _download_resolved(self, resolved: ResolvedCurseForgeArtifact) -> CachedArtifact:
        destination = self.cache.curseforge_path_for(resolved.project_id, resolved.file_id, resolved.file_name)
        if destination.exists() and destination.stat().st_size > 0:
            return self.cache.remember_curseforge(resolved, destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".tmp")
        _download(resolved.download_url, temp_path)
        if resolved.size is not None and temp_path.stat().st_size != resolved.size:
            temp_path.unlink(missing_ok=True)
            raise DownloadError(
                f"Downloaded size mismatch for {resolved.file_name}: "
                f"expected {resolved.size}, got {temp_path.stat().st_size}"
            )
        if not _hashes_match(temp_path, resolved.hashes):
            temp_path.unlink(missing_ok=True)
            raise DownloadError(f"Downloaded hash mismatch for {resolved.file_name}")
        temp_path.replace(destination)
        return self.cache.remember_curseforge(resolved, destination)


class CurseForgeTemplateMirrorProvider:
    def __init__(
        self,
        cache: ArtifactCache,
        name: str,
        url_template: str,
        file_name_template: str = "{projectID}-{fileID}.jar",
    ):
        self.cache = cache
        self.name = name
        self.url_template = url_template
        self.file_name_template = file_name_template

    def resolve(self, context: CurseForgeFileContext) -> ResolvedCurseForgeArtifact:
        values = {
            "projectID": context.project_id,
            "fileID": context.file_id,
            "project_id": context.project_id,
            "file_id": context.file_id,
            "slug": context.slug or "",
        }
        url = self.url_template.replace("%7B", "{").replace("%7D", "}").format(**values)
        file_name = self.file_name_template.format(**values)
        return ResolvedCurseForgeArtifact(
            provider=self.name,
            project_id=context.project_id,
            file_id=context.file_id,
            file_name=file_name,
            download_url=url,
        )

    def resolve_and_cache(self, remote: RemoteFile) -> CachedArtifact:
        if remote.project_id is None or remote.file_id is None:
            raise DownloadError("CurseForge mirror provider requires projectID and fileID")
        resolved = self.resolve(
            CurseForgeFileContext(
                project_id=remote.project_id,
                file_id=remote.file_id,
                required=remote.required,
                slug=remote.slug,
                display_name=remote.display_name,
            )
        )
        url = resolved.download_url
        file_name = resolved.file_name
        key = f"{remote.project_id}-{remote.file_id}"
        destination = self.cache.path_for(self.name, key, file_name)
        if destination.exists():
            return self.cache.remember(self.name, key, destination, remote)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".tmp")
        _download(url, temp_path)
        temp_path.replace(destination)
        return self.cache.remember(self.name, key, destination, remote)


def copy_cached_artifact(artifact: CachedArtifact, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact.path, target)


def _cache_key(remote: RemoteFile) -> str:
    if remote.hashes:
        algo, value = sorted(remote.hashes.items())[0]
        return f"{algo}-{value}"
    parsed = urlparse(remote.downloads[0])
    return hashlib.sha256(f"{remote.target_path}:{parsed.geturl()}".encode("utf-8")).hexdigest()


def _safe_file_name(file_name: str) -> str:
    return file_name.replace("/", "_").replace("\\", "_")


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Pack2Serve/0.1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (urllib.error.URLError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise DownloadError(f"Failed to download {url}: {exc}") from exc


def _read_json_url(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "Pack2Serve/0.1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DownloadError(f"URL not found: {url}") from exc
        raise DownloadError(f"Failed to read {url}: {exc}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Failed to read {url}: {exc}") from exc


def _hashes_match(path: Path, hashes: dict[str, str]) -> bool:
    for algo, expected in hashes.items():
        if algo.lower() not in {"sha1", "sha512"}:
            continue
        digest = hashlib.new(algo.lower())
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != expected.lower():
            return False
    return True
