from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path


def resolve_item_icon_data_url(server_dir: Path, item_id: str) -> str | None:
    namespace, item_name = _split_item_id(item_id)
    for jar_path in sorted((server_dir / "mods").glob("*.jar")):
        icon = _resolve_item_icon_from_jar(jar_path, namespace, item_name)
        if icon:
            return icon
    return None


def _resolve_item_icon_from_jar(jar_path: Path, namespace: str, item_name: str) -> str | None:
    try:
        with zipfile.ZipFile(jar_path) as jar:
            names = set(jar.namelist())
            model_path = f"assets/{namespace}/models/item/{item_name}.json"
            if model_path in names:
                texture = _texture_from_model(jar, model_path)
                if texture:
                    texture_path = _texture_reference_to_path(texture)
                    if texture_path in names:
                        return _png_data_url(jar.read(texture_path))
            for direct in (
                f"assets/{namespace}/textures/item/{item_name}.png",
                f"assets/{namespace}/textures/block/{item_name}.png",
            ):
                if direct in names:
                    return _png_data_url(jar.read(direct))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return None
    return None


def _texture_from_model(jar: zipfile.ZipFile, model_path: str) -> str | None:
    model = json.loads(jar.read(model_path).decode("utf-8", errors="replace"))
    textures = model.get("textures")
    if not isinstance(textures, dict):
        return None
    value = textures.get("layer0") or textures.get("all") or textures.get("particle")
    return str(value) if value else None


def _texture_reference_to_path(reference: str) -> str:
    namespace, texture = _split_item_id(reference)
    return f"assets/{namespace}/textures/{texture}.png"


def _split_item_id(item_id: str) -> tuple[str, str]:
    if ":" in item_id:
        namespace, name = item_id.split(":", 1)
        return namespace, name
    return "minecraft", item_id


def _png_data_url(data: bytes) -> str | None:
    if not data or len(data) > 512 * 1024:
        return None
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
