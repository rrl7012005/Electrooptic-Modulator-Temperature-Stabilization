"""Killable process boundary around blocking Moku SDK calls.

The parent process never owns a live Moku SDK object. A spawned child creates
the object, performs exactly one command at a time, and returns only picklable
data. If a command exceeds its parent-enforced deadline, the child can be
terminated before a replacement session is constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import multiprocessing
from multiprocessing.connection import Connection
import os
import threading
import time
import traceback
from typing import Any, Callable, Mapping

from .acquisition import (
    AcquisitionWatchdogExpired,
    AcquisitionWorkerTerminationError,
    MokuConnectionFactory,
    OscilloscopeConfiguration,
    RemoteMokuError,
)
from .models import CompiledRun, CompiledWaveform, OutputState
from .models import OscilloscopeTimebase
from .runtime import MokuRuntime
from .sdk_adapter import MokuRuntimeConfiguration


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerTimeouts:
    """Hard parent-side deadlines for the isolated SDK process."""

    get_data_s: float = 15.0
    rpc_s: float = 15.0
    startup_s: float = 30.0
    cleanup_s: float = 5.0
    terminate_grace_s: float = 3.0
    kill_grace_s: float = 3.0
    poll_interval_s: float = 0.1

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")


def _serialise_exception(error: BaseException) -> dict[str, str]:
    return {
        "module_name": type(error).__module__,
        "class_name": type(error).__name__,
        "message": str(error),
        "exception_repr": repr(error),
        "remote_traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }


def _remote_error(payload: Mapping[str, str]) -> RemoteMokuError:
    return RemoteMokuError(
        payload["module_name"],
        payload["class_name"],
        payload["message"],
        payload["exception_repr"],
        payload["remote_traceback"],
    )


class _PipeEventWriter:
    """Forward connection events from the child using pickle-safe fields."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def write(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        include_traceback: bool = False,
        **fields: Any,
    ) -> None:
        del include_traceback
        self.connection.send(
            {
                "kind": "event",
                "event": event,
                "fields": fields,
                "error": (
                    None if error is None else _serialise_exception(error)
                ),
            }
        )


_ALLOWED_METHODS = {
    "generate_waveform",
    "get_data",
    "relinquish_ownership",
    "set_frontend",
    "set_sources",
    "set_timebase",
    "set_trigger",
    "summary",
}


def moku_oscilloscope_worker_main(
    connection: Connection,
    configuration: OscilloscopeConfiguration,
) -> None:
    """Construct and serve one Oscilloscope session inside a child process."""

    try:
        from moku.instruments import Oscilloscope

        connection_factory = MokuConnectionFactory(
            Oscilloscope,
            configuration,
            event_writer=_PipeEventWriter(connection),
        )
        instrument = connection_factory()
        connection.send(
            {
                "kind": "ready",
                "worker_pid": os.getpid(),
                "selected_address": connection_factory.selected_address,
                "selected_address_role": (
                    connection_factory.selected_address_role
                ),
                "selected_resolved_addresses": list(
                    connection_factory.selected_resolved_addresses
                ),
            }
        )
    except BaseException as error:
        try:
            connection.send(
                {"kind": "startup_error", "error": _serialise_exception(error)}
            )
        finally:
            connection.close()
        return

    try:
        while True:
            message = connection.recv()
            if message.get("kind") == "shutdown":
                return
            if message.get("kind") != "call":
                raise RuntimeError("invalid worker command")

            request_id = message["request_id"]
            method_name = message["method"]
            if method_name not in _ALLOWED_METHODS:
                error = RuntimeError(
                    f"SDK worker method {method_name!r} is not allowed"
                )
                connection.send(
                    {
                        "kind": "response",
                        "request_id": request_id,
                        "ok": False,
                        "error": _serialise_exception(error),
                    }
                )
                continue

            try:
                result = getattr(instrument, method_name)(
                    *message.get("args", ()),
                    **message.get("kwargs", {}),
                )
            except BaseException as error:
                connection.send(
                    {
                        "kind": "response",
                        "request_id": request_id,
                        "ok": False,
                        "error": _serialise_exception(error),
                    }
                )
            else:
                connection.send(
                    {
                        "kind": "response",
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    }
                )
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class ProcessIsolatedOscilloscope:
    """Oscilloscope-compatible proxy with killable hard SDK deadlines."""

    def __init__(
        self,
        configuration: OscilloscopeConfiguration,
        *,
        event_writer: Any | None,
        timeouts: WorkerTimeouts | None = None,
        context_name: str = "spawn",
        worker_target: Callable[[Connection, OscilloscopeConfiguration], None] = (
            moku_oscilloscope_worker_main
        ),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.configuration = configuration
        self.event_writer = event_writer
        self.timeouts = timeouts or WorkerTimeouts()
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._request_id = 0
        self._hung_operation: str | None = None
        self._closed = False

        context = multiprocessing.get_context(context_name)
        parent_connection, child_connection = context.Pipe(duplex=True)
        self._connection = parent_connection
        self._process = context.Process(
            target=worker_target,
            args=(child_connection, configuration),
            name="moku-sdk-worker",
            daemon=True,
        )
        self._process.start()
        child_connection.close()

        try:
            ready = self._wait_for_kind("ready", self.timeouts.startup_s)
        except BaseException:
            self._terminate_worker("startup_failed")
            raise

        self.worker_pid = int(ready["worker_pid"])
        self.selected_address = ready.get("selected_address")
        self.selected_address_role = ready.get("selected_address_role")
        self.selected_resolved_addresses = tuple(
            ready.get("selected_resolved_addresses", ())
        )
        self._write_event(
            "acquisition_worker_started",
            worker_pid=self.worker_pid,
            multiprocessing_start_method=context_name,
            get_data_hard_timeout_s=self.timeouts.get_data_s,
            rpc_hard_timeout_s=self.timeouts.rpc_s,
            connection_address=self.selected_address,
            address_role=self.selected_address_role,
            resolved_addresses=list(self.selected_resolved_addresses),
        )

    @property
    def is_alive(self) -> bool:
        """Return whether the SDK child is currently alive."""

        return self._process.is_alive()

    @property
    def is_usable(self) -> bool:
        """Return whether another RPC may safely be sent to this worker."""

        return self.is_alive and not self._closed and self._hung_operation is None

    def _write_event(
        self,
        event: str,
        *,
        error: BaseException | None = None,
        **fields: Any,
    ) -> None:
        if self.event_writer is None:
            return
        try:
            self.event_writer.write(
                event,
                error=error,
                include_traceback=error is not None,
                **fields,
            )
        except Exception:
            LOGGER.exception("Could not record Moku worker event %s", event)

    def _forward_worker_event(self, message: Mapping[str, Any]) -> None:
        error_payload = message.get("error")
        error = None if error_payload is None else _remote_error(error_payload)
        fields = dict(message.get("fields", {}))
        if error is not None:
            fields["remote_traceback"] = error.remote_traceback
        self._write_event(message["event"], error=error, **fields)

    def _wait_for_kind(
        self,
        expected_kind: str,
        timeout_s: float,
        *,
        request_id: int | None = None,
        operation: str = "worker_startup",
    ) -> Mapping[str, Any]:
        deadline = self.monotonic() + timeout_s
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise AcquisitionWatchdogExpired(
                    operation,
                    timeout_s,
                    worker_pid=getattr(self, "worker_pid", self._process.pid),
                )
            if self._connection.poll(
                min(self.timeouts.poll_interval_s, remaining)
            ):
                try:
                    message = self._connection.recv()
                except EOFError as error:
                    raise ConnectionError(
                        f"Moku SDK worker exited during {operation}"
                    ) from error
                if message.get("kind") == "event":
                    self._forward_worker_event(message)
                    continue
                if message.get("kind") == "startup_error":
                    raise _remote_error(message["error"])
                if message.get("kind") != expected_kind:
                    raise RuntimeError(
                        f"unexpected worker message {message.get('kind')!r}"
                    )
                if (
                    request_id is not None
                    and message.get("request_id") != request_id
                ):
                    raise RuntimeError("worker response request ID mismatch")
                return message
            if not self._process.is_alive():
                raise ConnectionError(
                    f"Moku SDK worker exited during {operation} "
                    f"with code {self._process.exitcode}"
                )

    def _rpc(
        self,
        method_name: str,
        *args: Any,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> Any:
        timeout_s = self.timeouts.rpc_s if timeout_s is None else timeout_s
        with self._lock:
            if not self.is_usable:
                raise ConnectionError(
                    "Moku SDK worker is not available for another call"
                )
            self._request_id += 1
            request_id = self._request_id
            self._connection.send(
                {
                    "kind": "call",
                    "request_id": request_id,
                    "method": method_name,
                    "args": args,
                    "kwargs": kwargs,
                }
            )
            try:
                response = self._wait_for_kind(
                    "response",
                    timeout_s,
                    request_id=request_id,
                    operation=method_name,
                )
            except AcquisitionWatchdogExpired as error:
                self._hung_operation = method_name
                self._write_event(
                    "sdk_call_watchdog_expired",
                    error=error,
                    sdk_operation=method_name,
                    hard_timeout_s=timeout_s,
                    worker_pid=self._process.pid,
                )
                raise
            except KeyboardInterrupt:
                self._hung_operation = method_name
                self._write_event(
                    "sdk_call_interrupted",
                    sdk_operation=method_name,
                    worker_pid=self._process.pid,
                )
                self._terminate_worker("keyboard_interrupt")
                raise

            if not response["ok"]:
                raise _remote_error(response["error"])
            return response.get("result")

    def get_data(self, **kwargs: Any) -> Any:
        """Acquire a frame subject to the parent-enforced hard deadline."""

        return self._rpc(
            "get_data",
            timeout_s=self.timeouts.get_data_s,
            **kwargs,
        )

    def set_frontend(self, *args: Any, **kwargs: Any) -> Any:
        return self._rpc("set_frontend", *args, **kwargs)

    def set_sources(self, *args: Any, **kwargs: Any) -> Any:
        return self._rpc("set_sources", *args, **kwargs)

    def set_timebase(self, *args: Any, **kwargs: Any) -> Any:
        return self._rpc("set_timebase", *args, **kwargs)

    def set_trigger(self, *args: Any, **kwargs: Any) -> Any:
        return self._rpc("set_trigger", *args, **kwargs)

    def generate_waveform(self, *args: Any, **kwargs: Any) -> Any:
        timeout_s = (
            self.timeouts.cleanup_s
            if kwargs.get("type") == "Off"
            else self.timeouts.rpc_s
        )
        return self._rpc(
            "generate_waveform",
            *args,
            timeout_s=timeout_s,
            **kwargs,
        )

    def summary(self) -> Any:
        return self._rpc("summary")

    def _terminate_worker(self, reason: str) -> None:
        if self._closed and not self._process.is_alive():
            return

        forced = False
        if self._process.is_alive():
            forced = True
            self._process.terminate()
            self._process.join(self.timeouts.terminate_grace_s)
        if self._process.is_alive():
            kill = getattr(self._process, "kill", None)
            if kill is not None:
                kill()
                self._process.join(self.timeouts.kill_grace_s)
        if self._process.is_alive():
            raise AcquisitionWorkerTerminationError(
                f"Moku SDK worker {self._process.pid} is still alive after "
                "terminate and kill"
            )

        self._closed = True
        try:
            self._connection.close()
        except OSError:
            pass
        self._write_event(
            "acquisition_worker_terminated",
            worker_pid=self._process.pid,
            termination_reason=reason,
            forced=forced,
            exit_code=self._process.exitcode,
            previous_hung_operation=self._hung_operation,
        )

    def relinquish_ownership(self) -> None:
        """Bound ownership release and always confirm the child has exited."""

        if self._closed and not self._process.is_alive():
            return
        if self._hung_operation is not None:
            self._write_event(
                "ownership_release_unconfirmed",
                worker_pid=self._process.pid,
                blocked_sdk_operation=self._hung_operation,
            )
            self._terminate_worker("retire_hung_worker")
            return

        release_error: BaseException | None = None
        try:
            self._rpc(
                "relinquish_ownership",
                timeout_s=self.timeouts.cleanup_s,
            )
        except Exception as error:
            release_error = error
            self._write_event(
                "ownership_release_unconfirmed",
                error=error,
                worker_pid=self._process.pid,
            )

        if release_error is None and self._process.is_alive():
            try:
                self._connection.send({"kind": "shutdown"})
                self._process.join(self.timeouts.cleanup_s)
            except (BrokenPipeError, EOFError, OSError):
                pass
        self._terminate_worker("ownership_released")

        if release_error is not None and not isinstance(
            release_error, AcquisitionWatchdogExpired
        ):
            raise release_error

    def force_terminate(self, reason: str) -> None:
        """Stop the worker without issuing another SDK request."""

        self._terminate_worker(reason)


_RUNTIME_ALLOWED_METHODS = {
    "activate",
    "disable_all_outputs",
    "get_data",
    "relinquish_ownership",
    "replay_configuration",
    "summary",
}


def moku_runtime_worker_main(
    connection: Connection,
    configuration: MokuRuntimeConfiguration,
) -> None:
    """Own and serve the complete MIM session inside one spawned child.

    Importing :mod:`process_worker` in the parent does not import ``moku``.
    ``MokuSdkSession.connect`` performs that import here, after spawn.
    """

    try:
        from .sdk_adapter import MokuSdkSession

        session = MokuSdkSession.connect(
            configuration,
            event_writer=_PipeEventWriter(connection),
        )
        connection.send(
            {
                "kind": "ready",
                "worker_pid": os.getpid(),
                "selected_address": session.selected_address,
                "selected_address_role": session.selected_address_role,
                "selected_resolved_addresses": list(
                    session.selected_resolved_addresses
                ),
            }
        )
    except BaseException as error:
        try:
            connection.send(
                {"kind": "startup_error", "error": _serialise_exception(error)}
            )
        finally:
            connection.close()
        return

    try:
        while True:
            message = connection.recv()
            if message.get("kind") == "shutdown":
                return
            if message.get("kind") != "call":
                raise RuntimeError("invalid Moku runtime worker command")
            request_id = message["request_id"]
            method_name = message["method"]
            if method_name not in _RUNTIME_ALLOWED_METHODS:
                error = RuntimeError(
                    f"Moku runtime worker method {method_name!r} is not allowed"
                )
                connection.send(
                    {
                        "kind": "response",
                        "request_id": request_id,
                        "ok": False,
                        "error": _serialise_exception(error),
                    }
                )
                continue
            try:
                result = getattr(session, method_name)(
                    *message.get("args", ()),
                    **message.get("kwargs", {}),
                )
            except BaseException as error:
                connection.send(
                    {
                        "kind": "response",
                        "request_id": request_id,
                        "ok": False,
                        "error": _serialise_exception(error),
                    }
                )
            else:
                connection.send(
                    {
                        "kind": "response",
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    }
                )
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class _ProcessMokuSession(ProcessIsolatedOscilloscope):
    """Low-level RPC proxy implementing :class:`runtime.RuntimeSession`."""

    def __init__(
        self,
        configuration: MokuRuntimeConfiguration,
        *,
        event_writer: Any | None,
        timeouts: WorkerTimeouts,
        context_name: str,
        worker_target: Callable[[Connection, MokuRuntimeConfiguration], None],
        monotonic: Callable[[], float],
    ) -> None:
        super().__init__(
            configuration,  # type: ignore[arg-type]
            event_writer=event_writer,
            timeouts=timeouts,
            context_name=context_name,
            worker_target=worker_target,  # type: ignore[arg-type]
            monotonic=monotonic,
        )

    def replay_configuration(
        self,
        waveform: CompiledWaveform,
        run: CompiledRun,
        timebase: OscilloscopeTimebase | None = None,
    ) -> Any:
        if timebase is None:
            return self._rpc("replay_configuration", waveform, run)
        return self._rpc("replay_configuration", waveform, run, timebase)

    def activate(self, run: CompiledRun) -> Any:
        return self._rpc("activate", run)

    def disable_all_outputs(self) -> Any:
        return self._rpc(
            "disable_all_outputs",
            timeout_s=self.timeouts.cleanup_s,
        )


class ProcessIsolatedMokuRuntime(MokuRuntime):
    """Stateful MIM runtime whose SDK is owned only by a killable child."""

    def __init__(
        self,
        configuration: MokuRuntimeConfiguration,
        *,
        event_writer: Any | None = None,
        timeouts: WorkerTimeouts | None = None,
        context_name: str = "spawn",
        worker_target: Callable[
            [Connection, MokuRuntimeConfiguration], None
        ] = moku_runtime_worker_main,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeouts = timeouts or WorkerTimeouts()
        self.context_name = context_name
        self.worker_target = worker_target
        self.monotonic = monotonic

        def session_factory(
            selected_configuration: MokuRuntimeConfiguration,
        ) -> _ProcessMokuSession:
            return _ProcessMokuSession(
                selected_configuration,
                event_writer=event_writer,
                timeouts=self.timeouts,
                context_name=self.context_name,
                worker_target=self.worker_target,
                monotonic=self.monotonic,
            )

        super().__init__(
            configuration,
            session_factory,
            event_writer=event_writer,
        )

    @property
    def worker_pid(self) -> int:
        """PID of the currently active SDK child."""

        return int(self.session.worker_pid)  # type: ignore[attr-defined]

    @property
    def is_alive(self) -> bool:
        return bool(self.session.is_alive)  # type: ignore[attr-defined]

    @property
    def is_usable(self) -> bool:
        return bool(self.session.is_usable)  # type: ignore[attr-defined]

    def force_terminate(self, reason: str) -> None:
        """Terminate the current child without making another SDK call."""

        self.session.force_terminate(reason)  # type: ignore[attr-defined]
        self.state.output_state = OutputState.UNKNOWN
