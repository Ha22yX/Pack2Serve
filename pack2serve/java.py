from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from pack2serve.models import JavaPlan


@dataclass(frozen=True)
class JavaRuntimeInstallPlan:
    required_major: int
    os: str
    arch: str
    kind: str
    download_url: str
    archive_path: str
    install_dir: str
    java_executable: str
    notes: list[str]

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class JavaRuntimeInstallResult:
    status: str
    archive_path: str
    install_dir: str
    java_executable: str

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


class JavaInstaller:
    def install(self, server_dir: str | Path, plan: JavaRuntimeInstallPlan) -> JavaRuntimeInstallResult:
        root = Path(server_dir)
        archive_path = root / plan.archive_path
        install_dir = root / plan.install_dir
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        _download(plan.download_url, archive_path)

        extracted = root / "pack2serve" / "java-extract"
        if extracted.exists():
            shutil.rmtree(extracted)
        extracted.mkdir(parents=True)
        _extract_archive(archive_path, extracted)
        java_source = _find_java_executable(extracted, plan.os)
        runtime_root = java_source.parent.parent

        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(runtime_root, install_dir)
        shutil.rmtree(extracted)

        self._rewrite_start_script(root, plan.java_executable)
        result = JavaRuntimeInstallResult(
            status="installed",
            archive_path=str(archive_path.relative_to(root)).replace("\\", "/"),
            install_dir=str(install_dir.relative_to(root)).replace("\\", "/"),
            java_executable=plan.java_executable,
        )
        (root / "pack2serve" / "java-install-result.json").write_text(
            json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result

    def _rewrite_start_script(self, root: Path, java_executable: str) -> None:
        start = root / "start.ps1"
        java_path = java_executable.replace("/", "\\")
        replacement = f"$java = Join-Path $PSScriptRoot '{java_path}'"
        if start.exists():
            content = start.read_text(encoding="utf-8")
            if "$java = 'java'" in content:
                content = content.replace("$java = 'java'", replacement)
            else:
                content = replacement + "\n" + content
            start.write_text(content, encoding="utf-8")


def required_java_major(minecraft_version: str) -> int:
    parts = _version_parts(minecraft_version)
    minor = parts[1] if len(parts) > 1 else 0
    patch = parts[2] if len(parts) > 2 else 0

    if minor <= 16:
        return 8
    if minor == 17:
        return 16
    if minor == 20 and patch <= 4:
        return 17
    if minor <= 20:
        return 17
    return 21


def plan_java(minecraft_version: str) -> JavaPlan:
    required = required_java_major(minecraft_version)
    java_path = shutil.which("java")
    detected = _detect_java_major(java_path) if java_path else None
    status = java_status(required, detected)
    return JavaPlan(
        required_major=required,
        detected_major=detected,
        detected_path=java_path,
        status=status,
    )


def create_java_runtime_install_plan(
    required_major: int,
    *,
    os_name: str | None = None,
    machine: str | None = None,
) -> JavaRuntimeInstallPlan:
    runtime_os = _adoptium_os(os_name or platform.system())
    runtime_arch = _adoptium_arch(machine or platform.machine())
    extension = "zip" if runtime_os == "windows" else "tar.gz"
    download_url = (
        f"https://api.adoptium.net/v3/binary/latest/{required_major}/ga/"
        f"{runtime_os}/{runtime_arch}/jre/hotspot/normal/eclipse?project=jdk"
    )
    executable = "java.exe" if runtime_os == "windows" else "java"
    return JavaRuntimeInstallPlan(
        required_major=required_major,
        os=runtime_os,
        arch=runtime_arch,
        kind="adoptium-jre-archive",
        download_url=download_url,
        archive_path=f"pack2serve/java/jre-{required_major}-{runtime_os}-{runtime_arch}.{extension}",
        install_dir="pack2serve/runtime/java",
        java_executable=f"pack2serve/runtime/java/bin/{executable}",
        notes=[
            "Download is provided by the Eclipse Adoptium API.",
            "The runtime is installed inside the generated server directory.",
        ],
    )


def load_java_runtime_install_plan(path: str | Path) -> JavaRuntimeInstallPlan:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return JavaRuntimeInstallPlan(
        required_major=int(data["required_major"]),
        os=data["os"],
        arch=data["arch"],
        kind=data["kind"],
        download_url=data["download_url"],
        archive_path=data["archive_path"],
        install_dir=data["install_dir"],
        java_executable=data["java_executable"],
        notes=list(data.get("notes", [])),
    )


def java_status(required_major: int, detected_major: int | None) -> str:
    if detected_major is None:
        return "missing"
    if detected_major < required_major:
        return "too-old"
    if detected_major > required_major:
        return "newer-than-recommended"
    return "ok"


def _version_parts(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def _detect_java_major(java_path: str | None) -> int | None:
    if not java_path:
        return None
    try:
        proc = subprocess.run(
            [java_path, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = proc.stderr + proc.stdout
    match = re.search(r'version "([^"]+)"', text)
    if not match:
        return None
    version = match.group(1)
    if version.startswith("1."):
        return int(version.split(".")[1])
    return int(version.split(".")[0])


def _adoptium_os(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("win"):
        return "windows"
    if lowered == "darwin" or lowered.startswith("mac"):
        return "mac"
    if lowered.startswith("linux"):
        return "linux"
    raise ValueError(f"Unsupported Java runtime OS: {name}")


def _adoptium_arch(machine: str) -> str:
    lowered = machine.lower()
    if lowered in {"amd64", "x86_64"}:
        return "x64"
    if lowered in {"arm64", "aarch64"}:
        return "aarch64"
    raise ValueError(f"Unsupported Java runtime architecture: {machine}")


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Pack2Serve/0.1.0"})
    temp = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temp.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (urllib.error.URLError, OSError) as exc:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download Java runtime {url}: {exc}") from exc
    temp.replace(destination)


def _extract_archive(archive_path: Path, destination: Path) -> None:
    if archive_path.name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                _require_safe_archive_member(destination, member.filename)
            archive.extractall(destination)
        return
    if archive_path.name.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                _require_safe_archive_member(destination, member.name)
            archive.extractall(destination)
        return
    raise ValueError(f"Unsupported Java runtime archive: {archive_path}")


def _find_java_executable(root: Path, runtime_os: str) -> Path:
    executable = "java.exe" if runtime_os == "windows" else "java"
    matches = list(root.rglob(f"bin/{executable}"))
    if not matches:
        raise RuntimeError(f"Could not find bin/{executable} in Java runtime archive.")
    return matches[0]


def _require_safe_archive_member(destination: Path, member_name: str) -> None:
    member_path = (destination / member_name).resolve()
    destination_root = destination.resolve()
    try:
        member_path.relative_to(destination_root)
    except ValueError as exc:
        raise ValueError(f"Unsafe Java runtime archive path: {member_name}") from exc
