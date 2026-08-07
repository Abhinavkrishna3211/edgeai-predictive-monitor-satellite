"""
Wire codecs for the MQTT satellite link, ported from the reference
base-station implementation @ base-station/python/common/wire_protocol.py.

Only the piece telemetry_frame.py actually imports (ChannelSpectrum) is
ported here -- the command-direction ([TYPE][PAYLOAD] STATUS_LED/MOTOR_STOP
codecs in the reference file) belongs to base-station-authored commands our
gateway never sends, so it's out of scope for the ingestion-boundary swap
this module exists for (Phase 8a).

Not to be confused with components/epm_codec/include/frame_codec/
wire_protocol.h -- that's our own C header for the firmware-side frame
codec; this is an unrelated, Python-only file with the same name as the
reference implementation's.
"""
from dataclasses import dataclass
from typing import Tuple


@dataclass
class ChannelSpectrum:
    """One channel's spectrum (fs / fft_size / magnitude bins). Shared by the
    section-list codec (telemetry_frame.py) and the satellite sim."""
    fs: float
    fft_size: int
    bins: Tuple[float, ...]
