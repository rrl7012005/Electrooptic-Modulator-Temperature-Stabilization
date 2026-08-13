"""Narrow Liquid Instruments SDK adapter for the shared Moku:Go runtime.

This module is safe to import on computers without the Moku package.  The SDK
is imported only by :meth:`MokuSdkSession.connect`, which the spawned worker
calls.  All assumptions that may vary with an SDK release are kept here rather
than leaking ChannelA/ChannelB or routing vocabulary into experiment logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Callable, Mapping, Sequence

from .acquisition import MokuConnectionFactory, resolve_connection_address
from .models import CompiledRun, CompiledWaveform


# Verified for Moku:Go Multi-Instrument Mode.  Slot2 ChannelA is the external
# photodiode input and ChannelB is the slot-local copy of the AWG waveform.
MOKU_GO_MIM_CONNECTIONS = (
    {"source": "Input1", "destination": "Slot2InA"},
    {"source": "Slot1OutA", "destination": "Slot2InB"},
    {"source": "Slot1OutA", "destination": "Output2"},
)
PHOTODIODE_OSC_CHANNEL = "ChannelA"
WAVEFORM_REFERENCE_OSC_CHANNEL = "ChannelB"


@dataclass(frozen=True)
class MokuRuntimeConfiguration:
    """Verified Moku:Go MIM layout and explicit acquisition configuration."""

    address: str
    fallback_address: str | None = None
    force_connect: bool = False
    platform_id: int = 2
    awg_slot: int = 1
    oscilloscope_slot: int = 2
    output_channel: int = 2
    input_channel: int = 1
    frontend_impedance: str = "1MOhm"
    frontend_coupling: str = "DC"
    frontend_attenuation: str = "0dB"
    trigger_source: str = WAVEFORM_REFERENCE_OSC_CHANNEL
    trigger_level_v: float = 0.0
    trigger_edge: str = "Rising"
    trigger_mode: str = "Normal"
    trigger_type: str = "Edge"
    timebase_start_s: float = -45e-6
    timebase_end_s: float = 45e-6
    timebase_max_length: int = 16_384

    def __post_init__(self) -> None:
        if not isinstance(self.address, str) or not self.address.strip():
            raise ValueError("Moku address must be a non-empty string")
        if not isinstance(self.force_connect, bool):
            raise ValueError("force_connect must be true or false")
        if self.fallback_address is not None:
            if (
                not isinstance(self.fallback_address, str)
                or not self.fallback_address.strip()
            ):
                raise ValueError("fallback Moku address must be a non-empty string")
            if (
                self.fallback_address.strip().casefold()
                == self.address.strip().casefold()
            ):
                raise ValueError("fallback Moku address must differ from primary address")
        if self.platform_id != 2:
            raise ValueError("the verified Moku:Go MIM platform_id is 2")
        if (self.awg_slot, self.oscilloscope_slot) != (1, 2):
            raise ValueError("the verified layout requires AWG slot 1 and Oscilloscope slot 2")
        if (self.input_channel, self.output_channel) != (1, 2):
            raise ValueError("the verified physical routing requires Input1 and Output2")
        if self.frontend_impedance != "1MOhm":
            raise ValueError("Moku:Go MIM frontend impedance must be 1MOhm")
        if self.frontend_coupling not in {"AC", "DC"}:
            raise ValueError("frontend_coupling must be AC or DC")
        if self.frontend_attenuation not in {"0dB", "14dB"}:
            raise ValueError("frontend_attenuation must be 0dB or 14dB")
        if self.trigger_source not in {
            PHOTODIODE_OSC_CHANNEL,
            WAVEFORM_REFERENCE_OSC_CHANNEL,
        }:
            raise ValueError("trigger_source must be ChannelA or ChannelB in MIM")
        if self.trigger_edge not in {"Rising", "Falling", "Both"}:
            raise ValueError("trigger_edge must be Rising, Falling, or Both")
        if self.trigger_mode not in {"Normal", "Auto"}:
            raise ValueError("trigger_mode must be Normal or Auto")
        if self.trigger_type != "Edge":
            raise ValueError("only the verified trigger_type Edge is supported")
        if (
            isinstance(self.trigger_level_v, bool)
            or not isinstance(self.trigger_level_v, (int, float))
            or not math.isfinite(self.trigger_level_v)
            or not -5.0 <= self.trigger_level_v <= 5.0
        ):
            raise ValueError("trigger_level_v must be finite and between -5 and 5 V")
        if not (
            isinstance(self.timebase_start_s, (int, float))
            and not isinstance(self.timebase_start_s, bool)
            and isinstance(self.timebase_end_s, (int, float))
            and not isinstance(self.timebase_end_s, bool)
            and math.isfinite(self.timebase_start_s)
            and math.isfinite(self.timebase_end_s)
            and self.timebase_start_s < self.timebase_end_s
            and self.timebase_end_s > 0
        ):
            raise ValueError(
                "timebase start must be finite and below its positive end"
            )
        allowed_timebase_lengths = {
            128,
            256,
            512,
            1_024,
            2_048,
            4_096,
            8_192,
            16_384,
        }
        if (
            not isinstance(self.timebase_max_length, int)
            or isinstance(self.timebase_max_length, bool)
            or self.timebase_max_length not in allowed_timebase_lengths
        ):
            raise ValueError(
                "timebase_max_length must be one of 128, 256, 512, 1024, "
                "2048, 4096, 8192, or 16384"
            )

    @classmethod
    def from_settings(cls, settings: Any) -> "MokuRuntimeConfiguration":
        """Adapt config-layer settings without importing that package here."""

        getter = settings.get if isinstance(settings, Mapping) else lambda key, default=None: getattr(settings, key, default)
        return cls(
            address=getter("address"),
            fallback_address=getter("fallback_address"),
            force_connect=getter("force_connect", False),
            platform_id=getter("platform_id", 2),
            awg_slot=getter("awg_slot", 1),
            oscilloscope_slot=getter("oscilloscope_slot", 2),
            output_channel=getter("output_channel", 2),
            input_channel=getter("input_channel", 1),
            frontend_impedance=getter("frontend_impedance", "1MOhm"),
            frontend_coupling=getter("frontend_coupling", "DC"),
            frontend_attenuation=getter("frontend_attenuation", "0dB"),
            trigger_source=getter("trigger_source", WAVEFORM_REFERENCE_OSC_CHANNEL),
            trigger_level_v=getter("trigger_level_v", 0.0),
            trigger_edge=getter("trigger_edge", "Rising"),
            trigger_mode=getter("trigger_mode", "Normal"),
            trigger_type=getter("trigger_type", "Edge"),
            timebase_start_s=getter("timebase_start_s", -45e-6),
            timebase_end_s=getter("timebase_end_s", 45e-6),
            timebase_max_length=getter("timebase_max_length", 16_384),
        )

    def metadata(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def connection_interface_type(self) -> None:
        """No interface is inferred from a hostname or configured fallback."""

        return None

    def connection_addresses(self) -> tuple[tuple[str, str], ...]:
        addresses = [("primary", self.address.strip())]
        if self.fallback_address is not None:
            addresses.append(("fallback", self.fallback_address.strip()))
        return tuple(addresses)


class MokuConfigurationReplayError(RuntimeError):
    """A replay step failed before any command intentionally enabled output."""

    def __init__(
        self,
        step: str,
        *,
        outputs_confirmed_disabled: bool,
        cause: BaseException,
    ) -> None:
        self.step = step
        self.outputs_confirmed_disabled = outputs_confirmed_disabled
        super().__init__(f"Moku configuration replay failed at {step}: {cause}")
        self.__cause__ = cause


class MokuSdkSession:
    """One child-process-owned Moku:Go Multi-Instrument session.

    Hardware-facing SDK spellings are deliberately isolated in this class.
    In particular, MIM frontend settings belong to ``MultiInstrument``, not
    the slot Oscilloscope, and Moku:Go must not use the Pro/Delta-only
    ``MultiInstrument.set_output`` API.
    """

    def __init__(
        self,
        multi_instrument: Any,
        awg: Any,
        oscilloscope: Any,
        configuration: MokuRuntimeConfiguration,
        *,
        selected_address: str,
        selected_address_role: str = "primary",
        selected_resolved_addresses: Sequence[str] = (),
    ) -> None:
        self.multi_instrument = multi_instrument
        self.awg = awg
        self.oscilloscope = oscilloscope
        self.configuration = configuration
        self.selected_address = selected_address
        self.selected_address_role = selected_address_role
        self.selected_resolved_addresses = tuple(selected_resolved_addresses)
        self.outputs_confirmed_disabled = False

    @classmethod
    def connect(
        cls,
        configuration: MokuRuntimeConfiguration,
        *,
        sdk_types: tuple[Any, Any, Any] | None = None,
        event_writer: Any | None = None,
        address_resolver: Callable[[str], Sequence[str]] = (
            resolve_connection_address
        ),
    ) -> "MokuSdkSession":
        """Lazily import the SDK and construct the verified two-slot layout."""

        if sdk_types is None:
            # This is the only Moku import in the generalized runtime path.  The
            # method is invoked by the spawned worker, never during dry
            # compilation.  ``sdk_types`` exists solely for hardware-free tests.
            from moku.instruments import (  # type: ignore[import-not-found]
                ArbitraryWaveformGenerator,
                MultiInstrument,
                Oscilloscope,
            )
        else:
            MultiInstrument, ArbitraryWaveformGenerator, Oscilloscope = sdk_types

        def construct_multi_instrument(
            address: str,
            *,
            force_connect: bool,
        ) -> Any:
            return MultiInstrument(
                address,
                force_connect=force_connect,
                platform_id=configuration.platform_id,
            )

        connection_factory = MokuConnectionFactory(
            construct_multi_instrument,
            configuration,  # type: ignore[arg-type]
            event_writer=event_writer,
            address_resolver=address_resolver,
        )
        multi_instrument = connection_factory()
        selected_address = connection_factory.selected_address
        assert selected_address is not None

        try:
            awg = multi_instrument.set_instrument(
                configuration.awg_slot,
                ArbitraryWaveformGenerator,
            )
            # A newly deployed AWG may have enabled outputs.  Disable A and B
            # immediately, before installing any physical route.
            for channel in (1, 2):
                awg.enable_output(channel=channel, enable=False, strict=True)
            oscilloscope = multi_instrument.set_instrument(
                configuration.oscilloscope_slot,
                Oscilloscope,
            )
            session = cls(
                multi_instrument,
                awg,
                oscilloscope,
                configuration,
                selected_address=selected_address,
                selected_address_role=(
                    connection_factory.selected_address_role or "primary"
                ),
                selected_resolved_addresses=(
                    connection_factory.selected_resolved_addresses
                ),
            )
            session.outputs_confirmed_disabled = True
            session.replay_base_configuration()
            return session
        except BaseException:
            try:
                multi_instrument.relinquish_ownership()
            except BaseException:
                pass
            raise

    def disable_all_outputs(self) -> None:
        """Disable both AWG outputs; Output2 is driven only by AWG channel A."""

        self.outputs_confirmed_disabled = False
        for channel in (1, 2):
            self.awg.enable_output(channel=channel, enable=False, strict=True)
        self.outputs_confirmed_disabled = True

    def replay_base_configuration(self) -> None:
        """Replay routing/frontend/acquisition settings with outputs disabled."""

        self.disable_all_outputs()
        try:
            # The current MIM API accepts this list without a ``strict`` flag.
            self.multi_instrument.set_connections(
                connections=[dict(route) for route in MOKU_GO_MIM_CONNECTIONS]
            )
            self.multi_instrument.set_frontend(
                channel=self.configuration.input_channel,
                impedance=self.configuration.frontend_impedance,
                coupling=self.configuration.frontend_coupling,
                attenuation=self.configuration.frontend_attenuation,
            )
            self.oscilloscope.set_timebase(
                self.configuration.timebase_start_s,
                self.configuration.timebase_end_s,
                max_length=self.configuration.timebase_max_length,
            )
            self.oscilloscope.set_trigger(
                mode=self.configuration.trigger_mode,
                type=self.configuration.trigger_type,
                source=self.configuration.trigger_source,
                level=self.configuration.trigger_level_v,
                edge=self.configuration.trigger_edge,
            )
        except BaseException as error:
            raise MokuConfigurationReplayError(
                "base_configuration",
                outputs_confirmed_disabled=self.outputs_confirmed_disabled,
                cause=error,
            ) from error

    def upload_waveform(self, waveform: CompiledWaveform) -> None:
        """Upload one compiler-bounded LUT while leaving output disabled."""

        if not self.outputs_confirmed_disabled:
            raise RuntimeError("waveform upload requires confirmed-disabled outputs")
        self.awg.generate_waveform(
            channel=1,
            sample_rate=waveform.sample_rate_name,
            lut_data=waveform.normalized_lut.tolist(),
            frequency=waveform.achieved_frequency_hz,
            amplitude=waveform.amplitude_vpp,
            offset=waveform.offset_v,
            interpolation=False,
            strict=True,
        )

    def configure_run(self, run: CompiledRun) -> None:
        """Configure continuous output or a documented Manual/NCycle burst."""

        if not self.outputs_confirmed_disabled:
            raise RuntimeError("run setup requires confirmed-disabled outputs")
        self.awg.disable_modulation(channel=1, strict=True)
        if run.repeat_count is not None:
            self.awg.burst_modulate(
                channel=1,
                trigger_source="Manual",
                trigger_mode="NCycle",
                burst_cycles=run.repeat_count,
                strict=True,
            )

    def replay_configuration(
        self,
        waveform: CompiledWaveform,
        run: CompiledRun,
    ) -> Mapping[str, Any]:
        """Fully replay a session without ever enabling either physical output."""

        try:
            self.replay_base_configuration()
            self.upload_waveform(waveform)
            self.configure_run(run)
            return self.summary()
        except MokuConfigurationReplayError:
            raise
        except BaseException as error:
            raise MokuConfigurationReplayError(
                "waveform_or_run_configuration",
                outputs_confirmed_disabled=self.outputs_confirmed_disabled,
                cause=error,
            ) from error

    def activate(self, run: CompiledRun) -> None:
        """Enable AWG channel A and trigger a finite NCycle action if required."""

        if not self.outputs_confirmed_disabled:
            raise RuntimeError("activation requires a completed output-disabled replay")
        self.awg.enable_output(channel=1, enable=True, strict=True)
        self.outputs_confirmed_disabled = False
        if run.repeat_count is not None:
            self.awg.manual_trigger()

    def get_data(self, **kwargs: Any) -> Mapping[str, Any]:
        """Acquire a frame and expose physical signal semantics explicitly.

        Current Oscilloscope data dictionaries use ``ch1``/``ch2`` even though
        configuration calls use ``ChannelA``/``ChannelB``.  Both original keys
        are preserved; semantic aliases prevent experiment code from depending
        on that SDK mismatch.  This mapping needs an SDK smoke test on the
        laboratory unit after the user explicitly authorizes hardware access.
        """

        raw = self.oscilloscope.get_data(**kwargs)
        if not isinstance(raw, Mapping):
            raise ValueError("Moku Oscilloscope get_data did not return a mapping")
        result = dict(raw)
        if "ch1" in raw:
            result["photodiode_v"] = raw["ch1"]
        if "ch2" in raw:
            result["waveform_reference_v"] = raw["ch2"]
        return result

    def summary(self) -> Mapping[str, Any]:
        """Return read-only summaries used to verify replayed ownership."""

        # A plain dict is required because this result crosses a Windows-spawn
        # multiprocessing pipe.
        return {
            "selected_address": self.selected_address,
            "selected_address_role": self.selected_address_role,
            "selected_resolved_addresses": list(
                self.selected_resolved_addresses
            ),
            "multi_instrument": self.multi_instrument.summary(),
            "awg": self.awg.summary(),
            "oscilloscope": self.oscilloscope.summary(),
            "outputs_confirmed_disabled": self.outputs_confirmed_disabled,
        }

    def relinquish_ownership(self) -> None:
        """Best-effort safe output shutdown followed by MIM ownership release."""

        disable_error: BaseException | None = None
        try:
            self.disable_all_outputs()
        except BaseException as error:
            disable_error = error
        try:
            self.multi_instrument.relinquish_ownership()
        finally:
            if disable_error is not None:
                raise disable_error
