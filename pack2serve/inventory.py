from __future__ import annotations

import gzip
import re
import struct
from pathlib import Path
from typing import Any


def minecraft_supports_inventory_view(version: str) -> bool:
    parts = _minecraft_version_parts(version)
    if len(parts) < 2:
        return False
    return (parts[0], parts[1]) >= (1, 13)


def parse_snbt_inventory_list(raw: str, section: str = "inventory") -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for compound in _split_snbt_compounds(raw):
        item_id = _snbt_string_value(compound, "id")
        if not item_id:
            continue
        slot = _snbt_int_value(compound, "Slot", default=len(items))
        count = _snbt_int_value(compound, "Count", default=_snbt_int_value(compound, "count", default=1))
        damage = _snbt_int_value(compound, "Damage", default=_snbt_int_value(compound, "damage", default=0))
        items.append(
            normalize_inventory_item(
                {
                    "slot": slot,
                    "id": item_id,
                    "count": count,
                    "damage": damage,
                    "raw": compound,
                },
                section=section,
            )
        )
    return items


def normalize_inventory_item(raw: dict[str, object], section: str) -> dict[str, object]:
    slot = int(raw.get("slot", 0) or 0)
    item_id = str(raw.get("id", "minecraft:air") or "minecraft:air")
    count = int(raw.get("count", 1) or 1)
    damage = int(raw.get("damage", 0) or 0)
    normalized_section = _slot_section(slot, section)
    tooltip = [
        _display_name_from_item_id(item_id),
        item_id,
        f"Count: {count}",
    ]
    if damage:
        tooltip.append(f"Damage: {damage}")
    raw_text = str(raw.get("raw", "") or "")
    if raw_text:
        tooltip.append(raw_text[:500])
    return {
        "slot": slot,
        "section": normalized_section,
        "id": item_id,
        "count": count,
        "damage": damage,
        "raw": raw_text,
        "iconDataUrl": None,
        "tooltip": tooltip,
    }


def read_playerdata_inventory(path: Path) -> dict[str, object]:
    data = read_nbt_gzip(path)
    return {
        "inventory": [_item_from_nbt(item, section="inventory") for item in _compound_list(data.get("Inventory"))],
        "enderChest": [_item_from_nbt(item, section="enderChest") for item in _compound_list(data.get("EnderItems"))],
        "accessories": _accessory_sections(data),
        "rawKeys": sorted(str(key) for key in data.keys()),
    }


def read_nbt_gzip(path: Path) -> dict[str, object]:
    reader = _NbtReader(gzip.decompress(path.read_bytes()))
    tag_id = reader.read_u8()
    if tag_id != 10:
        raise ValueError("NBT root must be a compound tag.")
    reader.read_string()
    value = reader.read_payload(tag_id)
    if not isinstance(value, dict):
        raise ValueError("NBT root must decode to a dictionary.")
    return value


def _minecraft_version_parts(version: str) -> tuple[int, ...]:
    match = re.match(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def _split_snbt_compounds(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    compounds: list[str] = []
    depth = 0
    start: int | None = None
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                compounds.append(text[start : index + 1])
                start = None
    return compounds


def _snbt_string_value(compound: str, key: str) -> str | None:
    match = re.search(rf"(?:^|[{{,\s]){re.escape(key)}\s*:\s*\"([^\"]+)\"", compound)
    if match:
        return match.group(1)
    match = re.search(rf"(?:^|[{{,\s]){re.escape(key)}\s*:\s*'([^']+)'", compound)
    if match:
        return match.group(1)
    return None


def _snbt_int_value(compound: str, key: str, *, default: int) -> int:
    match = re.search(rf"(?:^|[{{,\s]){re.escape(key)}\s*:\s*(-?\d+)[bBsSlL]?", compound)
    return int(match.group(1)) if match else default


def _slot_section(slot: int, fallback: str) -> str:
    if fallback not in {"inventory", ""}:
        return fallback
    if 0 <= slot <= 8:
        return "hotbar"
    if 9 <= slot <= 35:
        return "main"
    if 100 <= slot <= 103:
        return "armor"
    if slot == -106:
        return "offhand"
    return fallback


def _display_name_from_item_id(item_id: str) -> str:
    name = item_id.split(":", 1)[-1].replace("_", " ").strip()
    return " ".join(part.capitalize() for part in name.split()) or item_id


def _compound_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _item_from_nbt(item: dict[str, object], section: str) -> dict[str, object]:
    return normalize_inventory_item(
        {
            "slot": int(item.get("Slot", 0) or 0),
            "id": str(item.get("id", "minecraft:air") or "minecraft:air"),
            "count": int(item.get("Count", item.get("count", 1)) or 1),
            "damage": int(item.get("Damage", item.get("damage", 0)) or 0),
            "raw": _short_raw_item_summary(item),
        },
        section=section,
    )


def _short_raw_item_summary(item: dict[str, object]) -> str:
    parts = []
    for key in sorted(item):
        if key in {"Slot", "id", "Count", "count"}:
            continue
        parts.append(f"{key}={item[key]!r}")
    return ", ".join(parts)[:500]


def _accessory_sections(data: dict[str, object]) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    for key, value in data.items():
        lowered = str(key).lower()
        if not any(marker in lowered for marker in ("curios", "trinket", "bauble", "artifact")):
            continue
        items = _find_item_compounds(value)
        if items:
            sections.append(
                {
                    "name": str(key),
                    "items": [_item_from_nbt(item, section="accessory") for item in items],
                }
            )
    return sections


def _find_item_compounds(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if "id" in value and ("Count" in value or "count" in value):
            found.append(value)
        for child in value.values():
            found.extend(_find_item_compounds(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_item_compounds(child))
    return found


class _NbtReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read_u8(self) -> int:
        value = self.data[self.offset]
        self.offset += 1
        return value

    def read_i8(self) -> int:
        return self._unpack(">b", 1)

    def read_i16(self) -> int:
        return self._unpack(">h", 2)

    def read_u16(self) -> int:
        return self._unpack(">H", 2)

    def read_i32(self) -> int:
        return self._unpack(">i", 4)

    def read_i64(self) -> int:
        return self._unpack(">q", 8)

    def read_f32(self) -> float:
        return self._unpack(">f", 4)

    def read_f64(self) -> float:
        return self._unpack(">d", 8)

    def read_string(self) -> str:
        length = self.read_u16()
        raw = self.data[self.offset : self.offset + length]
        self.offset += length
        return raw.decode("utf-8", errors="replace")

    def read_payload(self, tag_id: int) -> Any:
        if tag_id == 1:
            return self.read_i8()
        if tag_id == 2:
            return self.read_i16()
        if tag_id == 3:
            return self.read_i32()
        if tag_id == 4:
            return self.read_i64()
        if tag_id == 5:
            return self.read_f32()
        if tag_id == 6:
            return self.read_f64()
        if tag_id == 7:
            length = self.read_i32()
            raw = self.data[self.offset : self.offset + max(length, 0)]
            self.offset += max(length, 0)
            return raw
        if tag_id == 8:
            return self.read_string()
        if tag_id == 9:
            item_type = self.read_u8()
            length = self.read_i32()
            return [self.read_payload(item_type) for _ in range(max(length, 0))]
        if tag_id == 10:
            return self.read_compound()
        if tag_id == 11:
            return [self.read_i32() for _ in range(max(self.read_i32(), 0))]
        if tag_id == 12:
            return [self.read_i64() for _ in range(max(self.read_i32(), 0))]
        raise ValueError(f"Unsupported NBT tag id: {tag_id}")

    def read_compound(self) -> dict[str, object]:
        result: dict[str, object] = {}
        while True:
            tag_id = self.read_u8()
            if tag_id == 0:
                return result
            name = self.read_string()
            result[name] = self.read_payload(tag_id)

    def _unpack(self, fmt: str, size: int):
        value = struct.unpack(fmt, self.data[self.offset : self.offset + size])[0]
        self.offset += size
        return value
