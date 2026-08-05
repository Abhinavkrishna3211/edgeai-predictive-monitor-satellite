"""gateway/pipeline/adaptive_control.py — adaptive-sensing reply parameters
(EPM protocol v2), extracted from recv_verify.py (Phase 8b1 task 3).

The gateway closes the AI inference loop back into the satellite's DSP
pipeline.  P(fault) drives two FFT parameters sent in the 8-byte v2 reply:

  fft_overlap_pct — Welch's windowed overlap.  Higher P(fault) → more overlap
    → higher FFT frame rate → better time resolution for transient detection.
    Variance of each spectral estimate is unchanged; what improves is
    temporal tracking of rapidly-evolving fault signatures.

  spec_avg_n — Number of FFT frames averaged before sending.  Lower N →
    faster frame delivery at the cost of a higher noise floor.  When the
    machine is healthy we want a clean, averaged baseline; when fault
    suspicion is high we want rapid response to new transients.

Operating points (fault_posterior → commanded parameters):
  p < 0.30  →  overlap=0%,  avg=8   (healthy: max averaging, no overlap cost)
  p < 0.70  →  overlap=50%, avg=4   (moderate: standard Welch, 2× frame rate)
  p ≥ 0.70  →  overlap=75%, avg=2   (high suspicion: 4× frame rate, fast reaction)
"""


def _adaptive_overlap(p_fault: float) -> int:
    if p_fault < 0.30: return 0
    if p_fault < 0.70: return 50
    return 75


def _adaptive_avg_n(p_fault: float) -> int:
    if p_fault < 0.30: return 8
    if p_fault < 0.70: return 4
    return 2
