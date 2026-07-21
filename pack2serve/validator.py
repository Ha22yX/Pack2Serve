from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pack2serve.compatibility import audit_generated_server


@dataclass(frozen=True)
class ValidationResult:
    status: str
    command: list[str]
    return_code: int | None
    timed_out: bool
    combined_output: str
    hints: list[str]
    repair_attempts: int = 0
    repaired_client_mods: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


class ServerValidator:
    def validate(
        self,
        server_dir: str | Path,
        *,
        command: list[str] | None = None,
        timeout_seconds: int = 120,
        max_client_repair_attempts: int = 3,
    ) -> ValidationResult:
        root = Path(server_dir)
        cmd = command or _default_command(root)
        result = self._validate_with_client_dist_repairs(
            root,
            cmd,
            timeout_seconds,
            max_client_repair_attempts=max_client_repair_attempts,
        )
        self._write_outputs(root, result)
        if (root / "pack2serve" / "build-report.json").exists():
            audit_generated_server(root)
        return result

    def _validate_with_client_dist_repairs(
        self,
        root: Path,
        cmd: list[str],
        timeout_seconds: int,
        *,
        max_client_repair_attempts: int,
    ) -> ValidationResult:
        outputs: list[str] = []
        repaired: list[str] = []
        repair_attempts = 0
        attempts = max(0, max_client_repair_attempts) + 1
        last_result: ValidationResult | None = None

        for attempt in range(1, attempts + 1):
            result = self._validate_process(root, cmd, timeout_seconds)
            last_result = result
            outputs.append(f"--- Pack2Serve validation attempt {attempt} ---\n")
            outputs.append(result.combined_output)
            if not result.combined_output.endswith("\n"):
                outputs.append("\n")
            if result.status == "started":
                break
            if repair_attempts >= max_client_repair_attempts:
                break
            moved = isolate_invalid_dist_client_mods(root, result.combined_output)
            if not moved:
                break
            repair_attempts += 1
            repaired.extend(moved)
            outputs.append(
                "--- Pack2Serve client-only mod repair ---\n"
                + "\n".join(f"isolated {item}" for item in moved)
                + "\n"
            )

        assert last_result is not None
        combined_output = "".join(outputs)
        hints = _hints(combined_output, last_result.return_code, timed_out=last_result.timed_out)
        if repaired and last_result.status == "started":
            hints.append("Pack2Serve isolated client-only mods reported by the startup crash report and retried validation.")
        return ValidationResult(
            status=last_result.status,
            command=last_result.command,
            return_code=last_result.return_code,
            timed_out=last_result.timed_out,
            combined_output=combined_output,
            hints=hints,
            repair_attempts=repair_attempts,
            repaired_client_mods=repaired,
        )

    def _validate_process(self, root: Path, cmd: list[str], timeout_seconds: int) -> ValidationResult:
        output_parts: list[str] = []
        started_at = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        status: str | None = None
        try:
            assert proc.stdout is not None
            while True:
                if time.monotonic() - started_at > timeout_seconds:
                    status = "timed-out"
                    break
                line = proc.stdout.readline()
                if line:
                    output_parts.append(line)
                    current = _classify_output("".join(output_parts), proc.poll(), timed_out=False)
                    if current in {"started", "needs-eula", "failed", "crashed"}:
                        status = current
                        break
                elif proc.poll() is not None:
                    status = _classify_output("".join(output_parts), proc.returncode, timed_out=False)
                    break
                else:
                    time.sleep(0.05)
        finally:
            if proc.poll() is None:
                _stop_process(proc, status)
            try:
                remaining_stdout, _ = proc.communicate(timeout=5)
                if remaining_stdout:
                    output_parts.append(remaining_stdout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    remaining_stdout, _ = proc.communicate(timeout=5)
                    if remaining_stdout:
                        output_parts.append(remaining_stdout)
                except subprocess.TimeoutExpired:
                    status = status or "timed-out"

        output = "".join(output_parts)
        timed_out = status == "timed-out"
        return ValidationResult(
            status=status or _classify_output(output, proc.returncode, timed_out=False),
            command=cmd,
            return_code=proc.returncode,
            timed_out=timed_out,
            combined_output=output,
            hints=_hints(output, proc.returncode, timed_out=timed_out),
        )

    def _write_outputs(self, root: Path, result: ValidationResult) -> None:
        pack2serve = root / "pack2serve"
        logs = root / "logs"
        pack2serve.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        (pack2serve / "validation-report.json").write_text(
            json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (logs / "pack2serve-validation.log").write_text(
            result.combined_output,
            encoding="utf-8",
        )


def _default_command(root: Path) -> list[str]:
    start = root / "start.ps1"
    if start.exists():
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(start.resolve())]
    run_bat = root / "run.bat"
    if run_bat.exists():
        return ["cmd", "/c", str(run_bat.resolve())]
    run_sh = root / "run.sh"
    if run_sh.exists():
        return ["sh", str(run_sh.resolve())]
    return ["java", "-jar", "server.jar", "nogui"]


def isolate_invalid_dist_client_mods(server_dir: str | Path, output: str = "") -> list[str]:
    root = Path(server_dir)
    if not _looks_like_invalid_dist_client_crash(output):
        return []
    mod_files = _invalid_dist_mod_files(root, output)
    moved: list[str] = []
    for mod_file in mod_files:
        if not mod_file.exists() or not mod_file.is_file():
            continue
        destination = _unique_destination(root / "_client-overrides" / "mods" / mod_file.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(mod_file), str(destination))
        relative_move = (
            f"{_relative_display(mod_file, root)} -> {_relative_display(destination, root)}"
        )
        moved.append(relative_move)
        _mark_build_report_client_isolated(root, mod_file, destination)
    if moved:
        _write_client_repair_report(root, moved)
    return moved


def _looks_like_invalid_dist_client_crash(output: str) -> bool:
    lowered = output.lower()
    return (
        "invalid dist dedicated_server" in lowered
        and "attempted to load class net/minecraft/client" in lowered
    )


def _invalid_dist_mod_files(root: Path, output: str) -> list[Path]:
    candidates: list[Path] = []
    for text in [output, *_recent_crash_report_texts(root)]:
        for block in re.split(r"(?im)^-- Mod loading issue for:", text):
            if not _looks_like_invalid_dist_client_crash(block):
                continue
            for match in re.finditer(r"(?im)^\s*Mod file:\s*(.+?\.jar)\s*$", block):
                resolved = _resolve_reported_mod_path(root, match.group(1))
                if resolved and _is_path_under(resolved, root / "mods"):
                    candidates.append(resolved)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _recent_crash_report_texts(root: Path) -> list[str]:
    crash_dir = root / "crash-reports"
    if not crash_dir.exists():
        return []
    texts: list[str] = []
    reports = sorted(
        [item for item in crash_dir.glob("*.txt") if item.is_file()],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for report in reports[:3]:
        try:
            texts.append(report.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def _resolve_reported_mod_path(root: Path, raw_path: str) -> Path | None:
    cleaned = raw_path.strip().strip('"')
    if cleaned.startswith("/") and len(cleaned) > 3 and cleaned[2] == ":":
        cleaned = cleaned[1:]
    path = Path(cleaned)
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    stem = destination.stem
    suffix = destination.suffix
    for index in range(2, 1000):
        candidate = destination.with_name(f"{stem}.{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to choose a unique destination for {destination}")


def _mark_build_report_client_isolated(root: Path, source: Path, destination: Path) -> None:
    report_path = root / "pack2serve" / "build-report.json"
    if not report_path.exists():
        return
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    copied = data.get("copied_overrides")
    if not isinstance(copied, list):
        return
    source_relative = _relative_display(source, root)
    destination_relative = _relative_display(destination, root)
    changed = False
    for item in copied:
        if not isinstance(item, dict):
            continue
        if str(item.get("destination", "")).replace("\\", "/") == source_relative:
            item["destination"] = destination_relative
            item["classification"] = "client-remote-isolated"
            item["validationRepair"] = "invalid-dist-dedicated-server"
            changed = True
    if not changed:
        try:
            size = destination.stat().st_size
        except OSError:
            size = 0
        copied.append(
            {
                "source": f"validation-repair/{source.name}",
                "destination": destination_relative,
                "classification": "client-remote-isolated",
                "size": size,
                "validationRepair": "invalid-dist-dedicated-server",
            }
        )
        changed = True
    if changed:
        report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_client_repair_report(root: Path, moved: list[str]) -> None:
    report_path = root / "pack2serve" / "client-mod-repairs.json"
    existing: list[dict[str, object]] = []
    if report_path.exists():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [item for item in loaded if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    existing.append(
        {
            "reason": "invalid-dist-dedicated-server",
            "createdAt": int(time.time()),
            "moved": moved,
        }
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def _relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _is_path_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _classify_output(output: str, return_code: int | None, timed_out: bool) -> str:
    lowered = output.lower()
    if timed_out:
        return "timed-out"
    if "agree to the eula" in lowered or ("eula" in lowered and "run the server" in lowered):
        return "needs-eula"
    if "crash report" in lowered or "exception in server tick loop" in lowered:
        return "crashed"
    if (
        "failed to start" in lowered
        or "failed to bind to port" in lowered
        or "mod loading error" in lowered
        or "exception in thread" in lowered
        or "invocationtargetexception" in lowered
        or "loadingfailedexception" in lowered
        or "mixintransformererror" in lowered
        or "unsupported class file major version" in lowered
        or "missingmodsexception" in lowered
    ):
        return "failed"
    if "done (" in lowered and "for help" in lowered:
        return "started"
    if return_code and return_code != 0:
        return "failed"
    return "exited"


def _hints(output: str, return_code: int | None, timed_out: bool) -> list[str]:
    lowered = output.lower()
    hints: list[str] = []
    started = "done (" in lowered and "for help" in lowered
    if timed_out:
        hints.append("The process did not finish before the validation timeout.")
    if (
        "unsupported class file major version" in lowered
        or "urlclassloader" in lowered
        or "appclassloader" in lowered
    ):
        hints.append("The selected Java runtime is incompatible with this loader/mod set. Use java-plan.json.")
    if not started and ("missing mods" in lowered or "mod loading error" in lowered):
        hints.append("A dependency may be missing or a client-only mod may be present.")
    if (
        "missingmodsexception" in lowered
        or "noclassdeffounderror" in lowered
        or "caused by: java.lang.classnotfoundexception" in lowered
    ) and not started and not any("dependency" in hint.lower() for hint in hints):
        hints.append("A dependency may be missing or a client-only mod may be present.")
    if "agree to the eula" in lowered or ("eula" in lowered and "run the server" in lowered):
        hints.append("The Minecraft EULA may need to be accepted before startup can continue.")
    if "accessdeniedexception" in lowered or "access is denied" in lowered:
        hints.append("The server process hit a filesystem permission or file-lock issue.")
    if not started and ("failed to bind to port" in lowered or "address already in use" in lowered):
        hints.append("The configured server port is already in use.")
    if not started and return_code and return_code != 0 and not hints:
        hints.append("The server process exited with a non-zero code. Check validation log.")
    return hints


def _stop_process(proc: subprocess.Popen[str], status: str | None) -> None:
    if status == "started" and proc.stdin:
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            return
        except OSError:
            pass
    _kill_process_tree(proc)


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.poll() is None:
            proc.kill()
        return
    proc.terminate()
