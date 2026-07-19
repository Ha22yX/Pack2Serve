from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationResult:
    status: str
    command: list[str]
    return_code: int | None
    timed_out: bool
    combined_output: str
    hints: list[str]

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


class ServerValidator:
    def validate(
        self,
        server_dir: str | Path,
        *,
        command: list[str] | None = None,
        timeout_seconds: int = 120,
    ) -> ValidationResult:
        root = Path(server_dir)
        cmd = command or _default_command(root)
        result = self._validate_process(root, cmd, timeout_seconds)
        self._write_outputs(root, result)
        return result

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
                proc.kill()
                remaining_stdout, _ = proc.communicate(timeout=5)
                if remaining_stdout:
                    output_parts.append(remaining_stdout)

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
        or "mod loading error" in lowered
        or "exception in thread" in lowered
        or "runtimeexception" in lowered
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
    if timed_out:
        hints.append("The process did not finish before the validation timeout.")
    if "unsupported class file major version" in lowered:
        hints.append("The selected Java runtime is probably too old for one or more mods.")
    if "missing mods" in lowered or "mod loading error" in lowered:
        hints.append("A dependency may be missing or a client-only mod may be present.")
    if "agree to the eula" in lowered or ("eula" in lowered and "run the server" in lowered):
        hints.append("The Minecraft EULA may need to be accepted before startup can continue.")
    if "accessdeniedexception" in lowered or "access is denied" in lowered:
        hints.append("The server process hit a filesystem permission or file-lock issue.")
    if return_code and return_code != 0 and not hints:
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
    proc.terminate()
