"""SDK-neutral state machine for waveform output and frame acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from .models import (
    CompiledRun,
    CompiledWaveform,
    OscilloscopeTimebase,
    OutputState,
    RunMode,
    WaveformContinuity,
)
from .sdk_adapter import MokuRuntimeConfiguration


LOGGER = logging.getLogger(__name__)


class RuntimeSession(Protocol):
    """Methods supplied by either a fake or a process-isolated SDK session."""

    def replay_configuration(
        self,
        waveform: CompiledWaveform,
        run: CompiledRun,
        timebase: OscilloscopeTimebase | None = None,
    ) -> Any: ...
    def activate(self, run: CompiledRun) -> Any: ...
    def get_data(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def disable_all_outputs(self) -> Any: ...
    def summary(self) -> Any: ...
    def relinquish_ownership(self) -> Any: ...


class FiniteBurstIndeterminateError(RuntimeError):
    """A finite burst lost its session, so completed-cycle count is unknowable."""


@dataclass
class MokuRuntimeState:
    """Last confirmed runtime state and scientific continuity markers."""

    session_id: int = 1
    waveform_run_id: int = 0
    active_waveform_name: str | None = None
    active_waveform_timing_sha256: str | None = None
    output_state: OutputState = OutputState.DISABLED
    continuity: WaveformContinuity = WaveformContinuity.UNCONFIRMED
    finite_burst_indeterminate: bool = False
    pending_discard_reason: str | None = None

    @property
    def waveform_session_id(self) -> int:
        """Compatibility/provenance name for the underlying session counter."""

        return self.session_id


@dataclass(frozen=True)
class RuntimeFrame:
    """A frame plus markers needed to prevent silent switch/recovery mixing."""

    data: Mapping[str, Any]
    accepted: bool
    discard_reason: str | None
    session_id: int
    waveform_run_id: int
    waveform_name: str | None
    waveform_timing_sha256: str | None
    continuity: WaveformContinuity

    @property
    def waveform_session_id(self) -> int:
        return self.session_id


class MokuRuntime:
    """Coordinate safe replay, activation, switching, and bounded recovery.

    The injected session factory is the only construction hook.  Production
    code supplies a process proxy; tests supply fakes.  No SDK is imported here.
    """

    def __init__(
        self,
        configuration: MokuRuntimeConfiguration,
        session_factory: Callable[[MokuRuntimeConfiguration], RuntimeSession],
        *,
        event_writer: Any | None = None,
    ) -> None:
        self.configuration = configuration
        self._session_factory = session_factory
        self.event_writer = event_writer
        self.session = session_factory(configuration)
        self.state = MokuRuntimeState()
        self.active_waveform: CompiledWaveform | None = None
        self.active_run: CompiledRun | None = None
        self.active_timebase: OscilloscopeTimebase | None = None
        self._closed = False

    def _event(
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
            LOGGER.exception("Could not record Moku runtime event %s", event)

    @staticmethod
    def _replay_failure_output_state(error: BaseException) -> OutputState:
        if bool(getattr(error, "outputs_confirmed_disabled", False)):
            return OutputState.DISABLED
        return OutputState.UNKNOWN

    def configure(
        self,
        waveform: CompiledWaveform,
        run: CompiledRun,
        timebase: OscilloscopeTimebase | None = None,
    ) -> Any:
        """Fully replay configuration while keeping both outputs disabled."""

        if self._closed:
            raise RuntimeError("Moku runtime is closed")
        try:
            result = (
                self.session.replay_configuration(waveform, run)
                if timebase is None
                else self.session.replay_configuration(waveform, run, timebase)
            )
        except BaseException as error:
            self.state.output_state = self._replay_failure_output_state(error)
            self.state.continuity = WaveformContinuity.UNCONFIRMED
            self._event(
                "moku_configuration_replay_failed",
                error=error,
                waveform_name=waveform.name,
                output_state=self.state.output_state.value,
            )
            raise
        self.active_waveform = waveform
        self.active_run = run
        self.active_timebase = timebase
        self.state.active_waveform_name = waveform.name
        self.state.active_waveform_timing_sha256 = waveform.timing_sha256
        self.state.output_state = OutputState.DISABLED
        self.state.continuity = WaveformContinuity.UNCONFIRMED
        self.state.finite_burst_indeterminate = False
        self.state.pending_discard_reason = (
            "waveform_start"
            if self.state.waveform_run_id == 0
            else "waveform_switch"
        )
        self._event(
            "moku_configuration_replayed_output_disabled",
            waveform_name=waveform.name,
            waveform_timing_sha256=waveform.timing_sha256,
            run=run.summary_dict(),
            timebase=None if timebase is None else timebase.summary_dict(),
        )
        return result

    def start(
        self,
        run: CompiledRun | None = None,
        *,
        preserve_waveform_run_id: bool = False,
    ) -> None:
        """Enable the configured waveform and trigger a finite burst once."""

        if self.active_waveform is None or self.active_run is None:
            raise RuntimeError("configure a waveform before starting output")
        selected_run = self.active_run if run is None else run
        if selected_run != self.active_run:
            raise ValueError("start run must match the run used during configuration")
        try:
            self.session.activate(selected_run)
        except BaseException as error:
            self.state.output_state = OutputState.UNKNOWN
            self.state.continuity = WaveformContinuity.UNCONFIRMED
            if selected_run.exact_hardware_burst or selected_run.is_bounded_uncertainty_count:
                self.state.finite_burst_indeterminate = True
            self._event(
                "moku_waveform_start_failed",
                error=error,
                waveform_name=self.active_waveform.name,
                finite_burst_indeterminate=self.state.finite_burst_indeterminate,
            )
            raise
        if not preserve_waveform_run_id:
            self.state.waveform_run_id += 1
        self.state.output_state = OutputState.ENABLED
        self.state.continuity = WaveformContinuity.RESTARTED_FROM_PHASE_ZERO
        if selected_run.exact_hardware_burst or selected_run.is_bounded_uncertainty_count:
            # Manual/NCycle supplies an explicit phase-zero boundary.  The one
            # triggered Osc frame may be the only scientifically useful frame,
            # so do not apply the continuous-switch first-frame discard rule.
            boundary_reason = self.state.pending_discard_reason
            self.state.pending_discard_reason = None
            self._event(
                "moku_finite_burst_boundary",
                boundary_reason=boundary_reason,
                waveform_name=self.active_waveform.name,
                waveform_run_id=self.state.waveform_run_id,
                burst_cycles=selected_run.repeat_count,
            )
        self._event(
            "moku_waveform_started",
            waveform_name=self.active_waveform.name,
            waveform_run_id=self.state.waveform_run_id,
            finite_cycle_count=(
                selected_run.repeat_count
                if selected_run.mode is RunMode.COUNT
                else None
            ),
            duration_cycle_equivalent_count=(
                selected_run.repeat_count
                if selected_run.mode is RunMode.DURATION
                else None
            ),
            continuity=self.state.continuity.value,
        )

    def switch_waveform(
        self,
        waveform: CompiledWaveform,
        run: CompiledRun,
        *,
        start: bool = True,
        timebase: OscilloscopeTimebase | None = None,
    ) -> None:
        """Perform an output-disabled full replay, then optionally activate it."""

        self.configure(waveform, run, timebase)
        self.state.pending_discard_reason = "waveform_switch"
        if start:
            self.start()

    def get_frame(self, **kwargs: Any) -> RuntimeFrame:
        """Acquire and mark a continuous frame after a switch/replay.

        Finite Manual/NCycle starts clear this marker at the known trigger
        boundary because their only acquired frame must not be discarded.
        """

        raw = self.session.get_data(**kwargs)
        if not isinstance(raw, Mapping):
            raise ValueError("Moku runtime session returned a non-mapping frame")
        discard_reason = self.state.pending_discard_reason
        self.state.pending_discard_reason = None
        frame = RuntimeFrame(
            data=MappingProxyType(dict(raw)),
            accepted=discard_reason is None,
            discard_reason=discard_reason,
            session_id=self.state.session_id,
            waveform_run_id=self.state.waveform_run_id,
            waveform_name=self.state.active_waveform_name,
            waveform_timing_sha256=self.state.active_waveform_timing_sha256,
            continuity=self.state.continuity,
        )
        if discard_reason is not None:
            self._event(
                "moku_frame_discarded",
                discard_reason=discard_reason,
                session_id=frame.session_id,
                waveform_run_id=frame.waveform_run_id,
                waveform_name=frame.waveform_name,
            )
        return frame

    def disable(self) -> None:
        """Disable both AWG outputs and record whether shutdown was confirmed."""

        try:
            self.session.disable_all_outputs()
        except BaseException as error:
            self.state.output_state = OutputState.UNKNOWN
            self._event("moku_output_disable_failed", error=error)
            raise
        self.state.output_state = OutputState.DISABLED
        self._event("moku_outputs_disabled")

    def _retire_current_session(self) -> None:
        try:
            self.session.relinquish_ownership()
        except BaseException as error:
            force_terminate = getattr(self.session, "force_terminate", None)
            if callable(force_terminate):
                force_terminate("runtime_recovery")
            self._event("moku_old_session_cleanup_failed", error=error)

    def recover(
        self,
        waveform: CompiledWaveform | None = None,
        run: CompiledRun | None = None,
        *,
        start: bool = True,
        tolerate_finite_ambiguity: bool = False,
    ) -> None:
        """Replace the session and replay without hiding continuity loss.

        Continuous output may restart from phase zero.  An active finite burst
        can never be resumed safely because the number of completed cycles is
        unknowable; it is replayed output-disabled and an explicit exception is
        raised instead of silently retriggering it.
        """

        selected_waveform = self.active_waveform if waveform is None else waveform
        selected_run = self.active_run if run is None else run
        finite_was_active = bool(
            self.active_run is not None
            and (
                self.active_run.exact_hardware_burst
                or self.active_run.is_bounded_uncertainty_count
            )
            and self.state.output_state is not OutputState.DISABLED
        )
        continuous_was_active = bool(
            self.active_run is not None
            and not (
                self.active_run.exact_hardware_burst
                or self.active_run.is_bounded_uncertainty_count
            )
            and self.state.output_state is not OutputState.DISABLED
        )
        self._retire_current_session()
        self.state.output_state = OutputState.UNKNOWN
        try:
            replacement = self._session_factory(self.configuration)
        except BaseException as error:
            self.state.continuity = WaveformContinuity.UNCONFIRMED
            self._event("moku_replacement_session_failed", error=error)
            raise
        self.session = replacement
        self.state.session_id += 1
        self.state.output_state = OutputState.DISABLED
        self.state.continuity = WaveformContinuity.UNCONFIRMED
        if selected_waveform is None or selected_run is None:
            self._event("moku_session_recovered_output_disabled")
            return

        self.configure(selected_waveform, selected_run, self.active_timebase)
        self.state.pending_discard_reason = "session_recovery"
        if finite_was_active and not tolerate_finite_ambiguity:
            self.state.finite_burst_indeterminate = True
            error = FiniteBurstIndeterminateError(
                "Moku session was lost during a finite burst; completed cycle "
                "count is indeterminate and the burst was not retriggered"
            )
            self._event(
                "moku_finite_burst_indeterminate",
                error=error,
                waveform_name=selected_waveform.name,
                output_state=self.state.output_state.value,
            )
            raise error
        if finite_was_active and tolerate_finite_ambiguity:
            self._event(
                "moku_bounded_count_chunk_not_replayed",
                waveform_name=selected_waveform.name,
                output_state=self.state.output_state.value,
            )
            start = False
        if start:
            self.start(preserve_waveform_run_id=continuous_was_active)
            self.state.continuity = WaveformContinuity.RESTARTED_FROM_PHASE_ZERO
        self._event(
            "moku_session_recovered",
            waveform_name=selected_waveform.name,
            waveform_run_id=self.state.waveform_run_id,
            continuity=self.state.continuity.value,
            output_state=self.state.output_state.value,
        )

    def summary(self) -> Any:
        """Request a read-only session summary."""

        return self.session.summary()

    def emergency_disable(self) -> None:
        """Retire an ambiguous session and confirm output-off in a fresh one.

        The replacement is created through the same hard-deadline process
        factory.  Its adapter disables both newly deployed AWG outputs before
        routing, and this method issues an additional explicit disable.  It
        never uploads, activates, or retriggers an active waveform.
        """

        self._retire_current_session()
        self.state.output_state = OutputState.UNKNOWN
        replacement = self._session_factory(self.configuration)
        self.session = replacement
        self.state.session_id += 1
        try:
            replacement.disable_all_outputs()
        except BaseException as error:
            self.state.output_state = OutputState.UNKNOWN
            self._event("moku_emergency_output_disable_failed", error=error)
            raise
        self.state.output_state = OutputState.DISABLED
        self.state.continuity = WaveformContinuity.UNCONFIRMED
        self._event(
            "moku_emergency_output_disabled",
            session_id=self.state.session_id,
        )

    def close(self) -> None:
        """Disable outputs, using a fresh bounded cleanup session if needed."""

        if self._closed:
            return
        try:
            try:
                self.disable()
            except Exception as error:
                self._event(
                    "moku_primary_cleanup_failed_starting_emergency_session",
                    error=error,
                )
                self.emergency_disable()
        finally:
            try:
                self.session.relinquish_ownership()
            finally:
                self._closed = True

    def relinquish_ownership(self) -> None:
        """Compatibility alias for :meth:`close`."""

        self.close()

    def __enter__(self) -> "MokuRuntime":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
