from __future__ import annotations

import base64
import json
import os
import zipfile
from pathlib import Path


_ICON_CACHE: dict[tuple[str, str, str, str], str | None] = {}
_ARCHIVE_NAMES_CACHE: dict[tuple[str, int, int], set[str]] = {}


def resolve_item_icon_data_url(server_dir: Path, item_id: str) -> str | None:
    cache_key = (
        str(server_dir.resolve()),
        item_id,
        os.environ.get("PACK2SERVE_MINECRAFT_CLIENT_JAR", ""),
        os.environ.get("PACK2SERVE_MINECRAFT_HOME", ""),
    )
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]
    namespace, item_name = _split_item_id(item_id)
    for root in _resource_directories(server_dir):
        icon = _resolve_item_icon_from_directory(root, namespace, item_name)
        if icon:
            _ICON_CACHE[cache_key] = icon
            return icon
    if namespace == "minecraft":
        for archive_path in _minecraft_client_archives(server_dir):
            icon = _resolve_item_icon_from_archive(archive_path, namespace, item_name)
            if icon:
                _ICON_CACHE[cache_key] = icon
                return icon
    for archive_path in _resource_archives(server_dir):
        icon = _resolve_item_icon_from_archive(archive_path, namespace, item_name)
        if icon:
            _ICON_CACHE[cache_key] = icon
            return icon
    _ICON_CACHE[cache_key] = None
    return None


def _resource_archives(server_dir: Path) -> list[Path]:
    archives: list[Path] = []
    for pattern in ("mods/*.jar", "resourcepacks/*.zip", "resourcepacks/*.jar"):
        archives.extend(server_dir.glob(pattern))
    return sorted(path for path in archives if path.is_file())


def _resource_directories(server_dir: Path) -> list[Path]:
    candidates = [
        server_dir,
        server_dir / "kubejs",
        server_dir / "_client-overrides",
        server_dir / "_unknown-overrides",
    ]
    candidates.extend(path for path in (server_dir / "resourcepacks").glob("*") if path.is_dir())
    return [path for path in candidates if path.exists()]


def _minecraft_client_archives(server_dir: Path) -> list[Path]:
    archives: list[Path] = []
    configured = os.environ.get("PACK2SERVE_MINECRAFT_CLIENT_JAR", "")
    for raw in configured.split(os.pathsep):
        if raw.strip():
            path = Path(raw.strip())
            if path.is_file():
                archives.append(path)
    version = _minecraft_version(server_dir)
    pack_name = _pack_name(server_dir)
    for home in _minecraft_homes():
        if version:
            archives.extend(home.glob(f"libraries/net/minecraft/client/{version}-*/client-{version}-*-extra.jar"))
            archives.extend(home.glob(f"libraries/net/minecraft/client/{version}-*/client-{version}-*.jar"))
        if pack_name:
            version_dir = home / "versions" / pack_name
            archives.extend(version_dir.glob("*.jar"))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in archives:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _minecraft_homes() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("PACK2SERVE_MINECRAFT_HOME", "")
    if configured:
        candidates.append(Path(configured))
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(Path(appdata) / ".minecraft")
    candidates.extend(
        [
            Path.home() / "AppData/Roaming/.minecraft",
            Path("D:/Games/PCL/.minecraft"),
        ]
    )
    return [path for path in candidates if path.exists()]


def _minecraft_version(server_dir: Path) -> str:
    report_path = server_dir / "pack2serve" / "build-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    pack = report.get("pack") if isinstance(report, dict) else None
    if isinstance(pack, dict):
        return str(pack.get("minecraft_version") or "")
    return ""


def _pack_name(server_dir: Path) -> str:
    report_path = server_dir / "pack2serve" / "build-report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    pack = report.get("pack") if isinstance(report, dict) else None
    if isinstance(pack, dict):
        return str(pack.get("name") or "")
    return ""


def _resolve_item_icon_from_archive(archive_path: Path, namespace: str, item_name: str) -> str | None:
    try:
        names = _archive_names(archive_path)
        model_path = f"assets/{namespace}/models/item/{item_name}.json"
        direct_match = next((path for path in _candidate_texture_paths(namespace, item_name) if path in names), "")
        suffix = f"/{item_name}.png"
        loose_match = ""
        if not direct_match and model_path not in names:
            loose_match = next(
                (
                    name
                    for name in sorted(names)
                    if name.startswith(f"assets/{namespace}/") and "/textures/" in name and name.endswith(suffix)
                ),
                "",
            )
        if model_path not in names and not direct_match and not loose_match:
            return None
        with zipfile.ZipFile(archive_path) as archive:
            if model_path in names:
                for texture in _textures_from_model(archive, model_path):
                    texture_path = _texture_reference_to_path(texture)
                    if texture_path in names:
                        return _png_data_url(archive.read(texture_path))
            if direct_match:
                return _png_data_url(archive.read(direct_match))
            if loose_match:
                return _png_data_url(archive.read(loose_match))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return None
    return None


def _archive_names(archive_path: Path) -> set[str]:
    stat = archive_path.stat()
    key = (str(archive_path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    cached = _ARCHIVE_NAMES_CACHE.get(key)
    if cached is not None:
        return cached
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    _ARCHIVE_NAMES_CACHE[key] = names
    return names


def _resolve_item_icon_from_directory(root: Path, namespace: str, item_name: str) -> str | None:
    model_path = root / "assets" / namespace / "models" / "item" / f"{item_name}.json"
    if model_path.exists():
        for texture in _textures_from_directory_model(root, model_path):
            texture_path = root / _texture_reference_to_path(texture)
            if texture_path.exists():
                return _png_data_url(texture_path.read_bytes())
    for relative in _candidate_texture_paths(namespace, item_name):
        path = root / relative
        if path.exists():
            return _png_data_url(path.read_bytes())
    texture_root = root / "assets" / namespace
    if texture_root.exists():
        for path in sorted(texture_root.rglob(f"{item_name}.png")):
            if "textures" in path.parts:
                return _png_data_url(path.read_bytes())
    return None


def _textures_from_model(archive: zipfile.ZipFile, model_path: str, seen: set[str] | None = None) -> list[str]:
    seen = seen or set()
    if model_path in seen:
        return []
    seen.add(model_path)
    model = json.loads(archive.read(model_path).decode("utf-8", errors="replace"))
    textures = _texture_values(model)
    parent = str(model.get("parent", "")).strip()
    parent_path = _model_reference_to_path(parent)
    if parent_path and parent_path in archive.namelist():
        textures.extend(_textures_from_model(archive, parent_path, seen))
    return textures


def _textures_from_directory_model(root: Path, model_path: Path, seen: set[Path] | None = None) -> list[str]:
    seen = seen or set()
    if model_path in seen:
        return []
    seen.add(model_path)
    model = json.loads(model_path.read_text(encoding="utf-8", errors="replace"))
    textures = _texture_values(model)
    parent = str(model.get("parent", "")).strip()
    parent_reference = _model_reference_to_path(parent)
    if parent_reference:
        parent_path = root / parent_reference
        if parent_path.exists():
            textures.extend(_textures_from_directory_model(root, parent_path, seen))
    return textures


def _texture_values(model: dict[str, object]) -> list[str]:
    textures = model.get("textures")
    if not isinstance(textures, dict):
        return []
    preferred = ["layer0", "all", "particle"]
    ordered = [textures[key] for key in preferred if key in textures]
    ordered.extend(value for key, value in textures.items() if key not in preferred)
    return [str(value) for value in ordered if value and not str(value).startswith("#")]


def _texture_reference_to_path(reference: str) -> str:
    namespace, texture = _split_item_id(reference)
    return f"assets/{namespace}/textures/{texture}.png"


def _model_reference_to_path(reference: str) -> str | None:
    if not reference or reference.startswith("builtin/"):
        return None
    namespace, model = _split_item_id(reference)
    if not model.startswith("models/"):
        model = f"models/{model}"
    return f"assets/{namespace}/{model}.json"


def _candidate_texture_paths(namespace: str, item_name: str) -> tuple[str, ...]:
    return (
        f"assets/{namespace}/textures/item/{item_name}.png",
        f"assets/{namespace}/textures/items/{item_name}.png",
        f"assets/{namespace}/textures/block/{item_name}.png",
        f"assets/{namespace}/textures/blocks/{item_name}.png",
        f"assets/{namespace}/textures/item/models/{item_name}.png",
        f"assets/{namespace}/textures/entity/{item_name}.png",
    )


def _split_item_id(item_id: str) -> tuple[str, str]:
    if ":" in item_id:
        namespace, name = item_id.split(":", 1)
        return namespace, name
    return "minecraft", item_id


def _png_data_url(data: bytes) -> str | None:
    if not data or len(data) > 512 * 1024:
        return None
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
