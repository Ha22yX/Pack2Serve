from __future__ import annotations

from pathlib import Path


def accept_eula(server_dir: str | Path) -> Path:
    path = Path(server_dir) / "eula.txt"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = []
    found = False
    for line in existing.splitlines():
        if line.strip().startswith("eula="):
            lines.append("eula=true")
            found = True
        else:
            lines.append(line)
    if not found:
        lines.append("eula=true")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
