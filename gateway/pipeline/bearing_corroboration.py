"""gateway/pipeline/bearing_corroboration.py — physics-based corroboration
for an already-triggered "Bearing Fault" spectral classification.

_classify_fault_type() (gateway/pipeline/alerting.py) labels a frame as a
bearing fault purely from pattern matching (high-band energy ratio +
kurtosis). This module is additive, out-of-band evidence only: given a label
that already says "Bearing Fault", it checks whether the mic FFT's dominant
peak actually lands near a BPFO/BPFI/BSF/FTF marker computed from real shaft
speed and bearing geometry (bearing_math.py). It never influences the label
itself — alerting.py's branch logic is untouched, per Part 1's finding that
its branch order is already fragile to collisions and should not gain more
responsibility.

Requires an externally supplied shaft_hz + geometry (see bearing_math.py's
BearingGeometry) — there is no live shaft-speed estimate anywhere in this
pipeline, so corroboration is opportunistic: it runs only when both are
wired in (e.g. via --shaft-hz/--shaft-rpm --bearing on the gateway CLI).
"""

from typing import Optional

import numpy as np

from gateway.pipeline.bearing_math import BearingFreqs, BearingGeometry

_BEARING_LABEL_PREFIX = "Bearing Fault"


def corroborate_bearing_fault(fault_type: str, mic_fft_db, fs_hz: float,
                               shaft_hz: Optional[float],
                               geom: Optional[BearingGeometry],
                               tolerance_hz: Optional[float] = None) -> Optional[dict]:
    """Cross-check a "Bearing Fault" label against bearing physics.

    Returns None if fault_type isn't a bearing-fault label, or shaft_hz/geom
    are unavailable, or mic_fft_db is too short to find a peak. Otherwise
    returns:
        {'corroborated': bool, 'matched_marker': str | None,
         'peak_hz': float, 'peak_db': float,
         'nearest_marker_hz': float | None, 'delta_hz': float | None}
    'corroborated' is True iff the FFT's dominant spectral peak (bin 0
    included, per ADR-039) falls within tolerance_hz of some BPFO/BPFI/BSF/FTF
    marker (including 2nd harmonics — see bearing_math.BearingFreqs.markers()).
    """
    if not fault_type.startswith(_BEARING_LABEL_PREFIX):
        return None
    if shaft_hz is None or geom is None:
        return None
    if mic_fft_db is None or len(mic_fft_db) < 2:
        return None

    mic_fft_db = np.asarray(mic_fft_db, dtype=np.float64)
    n = len(mic_fft_db)
    hz_per_bin = fs_hz / 2.0 / n

    peak_bin = int(np.argmax(mic_fft_db))   # bin 0 included (ADR-039)
    peak_hz = peak_bin * hz_per_bin
    peak_db = float(mic_fft_db[peak_bin])

    if tolerance_hz is None:
        tolerance_hz = 2.0 * hz_per_bin   # within 2 FFT bins of resolution

    bf = BearingFreqs.from_shaft_hz(shaft_hz, geom)
    markers = bf.markers(fs_hz)

    matched_marker = None
    nearest_marker_hz = None
    delta_hz = None
    best_gap = float('inf')
    for label, marker_hz in markers.items():
        gap = abs(peak_hz - marker_hz)
        if gap < best_gap:
            best_gap = gap
            nearest_marker_hz = marker_hz
            delta_hz = gap
            matched_marker = label

    corroborated = matched_marker is not None and best_gap <= tolerance_hz
    if not corroborated:
        matched_marker = None

    return {
        'corroborated': corroborated,
        'matched_marker': matched_marker,
        'peak_hz': peak_hz,
        'peak_db': peak_db,
        'nearest_marker_hz': nearest_marker_hz,
        'delta_hz': delta_hz,
    }
