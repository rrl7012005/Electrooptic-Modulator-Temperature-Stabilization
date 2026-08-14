"""Independent TEC completion fallback for an abruptly lost supervisor."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Mapping


def _process_is_running(
    pid: int,
    *,
    started_before_utc: str | None = None,
) -> bool:
    if os.name == "nt":
        try:
            if started_before_utc is not None:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                            "if ($null -eq $p) { exit 1 }; "
                            "$p.StartTime.ToUniversalTime().ToString('o')"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5.0,
                )
                if result.returncode != 0:
                    return False
                process_started = datetime.fromisoformat(result.stdout.strip())
                marker = datetime.fromisoformat(
                    started_before_utc[:-1] + "+00:00"
                    if started_before_utc.endswith("Z")
                    else started_before_utc
                )
                return process_started <= marker
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _log(path: Path, event: str, **fields: Any) -> None:
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event": event,
        **fields,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")


def apply_emergency_completion(
    document: Mapping[str, Any],
    *,
    controller_factory: Callable[[Any], Any] | None = None,
) -> None:
    """Connect afresh, preflight, and apply the saved explicit completion action."""

    from eom_stabilisation.config.models import TemperatureControllerSettings

    if controller_factory is None:
        from eom_stabilisation.tec.mecom_adapter import MeComTecController

        controller_factory = MeComTecController
    settings = TemperatureControllerSettings(**dict(document["settings"]))
    behavior = str(document["completion_behavior"])
    controller = controller_factory(settings)
    try:
        controller.connect()
        controller.initial_target_c = document.get("initial_target_c")
        controller.initial_output_enabled = document.get("initial_output_enabled")
        controller.apply_completion_behavior(behavior)
    finally:
        controller.close()


def monitor_parent(
    *,
    parent_pid: int,
    configuration_path: Path,
    disarm_path: Path,
    log_path: Path,
    process_is_running: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    parent_start_timestamp_utc: str | None = None,
    controller_factory: Callable[[Any], Any] | None = None,
) -> int:
    """Wait for disarm or parent loss, then run the saved fallback once."""

    _log(
        log_path,
        "tec_cleanup_watchdog_started",
        parent_pid=parent_pid,
    )
    is_running = (
        (lambda pid: _process_is_running(pid, started_before_utc=parent_start_timestamp_utc))
        if process_is_running is None
        else process_is_running
    )
    while is_running(parent_pid):
        if disarm_path.is_file():
            _log(log_path, "tec_cleanup_watchdog_disarmed")
            return 0
        sleep(0.5)
    if disarm_path.is_file():
        _log(log_path, "tec_cleanup_watchdog_disarmed")
        return 0
    sleep(0.5)
    try:
        document = json.loads(configuration_path.read_text(encoding="utf-8"))
        apply_emergency_completion(document, controller_factory=controller_factory)
    except Exception as error:
        _log(
            log_path,
            "tec_emergency_completion_failed",
            error=f"{type(error).__name__}: {error}",
        )
        return 1
    _log(
        log_path,
        "tec_emergency_completion_applied",
        completion_behavior=document["completion_behavior"],
    )
    return 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--disarm-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--parent-start-timestamp-utc")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    return monitor_parent(
        parent_pid=arguments.parent_pid,
        configuration_path=arguments.configuration.resolve(),
        disarm_path=arguments.disarm_file.resolve(),
        log_path=arguments.log_file.resolve(),
        parent_start_timestamp_utc=arguments.parent_start_timestamp_utc,
    )


if __name__ == "__main__":
    raise SystemExit(main())
