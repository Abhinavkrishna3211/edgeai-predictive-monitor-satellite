"""gateway/api/live_plot.py — live local matplotlib dashboard (FFT panels,
waterfall, crest/kurtosis history), extracted from recv_verify.py (Phase 8c
task 1).

Placed under gateway/api/ alongside dashboard.py and reports.py: all three
are visualization surfaces over the same live pipeline state, just with
different renderers (HTTP/JSON+HTML, print-ready HTML, local matplotlib
window) rather than different subject matter. "api" here means "the
gateway's presentation layer," not strictly "HTTP endpoint" — dashboard.py
and reports.py already establish that reading.

_sat_count is imported directly from gateway.registry.satellite_state (same
pattern dashboard.py/reports.py use for _sat_lock/_satellites): it is a
plain, dependency-free function at module load time, so no cycle risk.

Everything else this module touches (`CREST_WARN`, `CREST_FAULT`,
`HISTORY_LEN`, `WATERFALL_ROWS`, `MARKER_COLORS`, `_display`) is owned by
recv_verify.py and read through a *lazy* `import recv_verify as _rv` inside
run_plot()'s body instead of at module level. recv_verify.py does not import
this module (only gateway/main.py does), so there is no circular-import
deadlock risk here the way there is for dashboard.py/reports.py/etc. — the
lazy import is used anyway, for the same reason given in
gateway/api/reports.py's docstring: `CREST_WARN`/`CREST_FAULT` are
CLI-mutable via gateway/main.py's `rv.CREST_WARN = args.crest_warn`
assignments, and only a live module-attribute read (not a value copied at
import time) tracks that reassignment correctly.

`_DisplayState`/`_display` deliberately did NOT move here despite being
read only by run_plot(): it is written by recv_verify.py's own
_process_satellite_frame(), so moving it would make that shared,
transport-agnostic pipeline function reach out to this leaf visualization
module instead of the reverse — the same "shared step shouldn't depend on
one specific consumer's module" reasoning ADR-028 already applied when it
kept `_log_security_event` in recv_verify.py. See
docs/decisions/ADR-029-recv-verify-fate-and-main-py-split.md for why
recv_verify.py still owns this state.
"""

import collections

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from gateway.registry.satellite_state import _sat_count


def run_plot(fft_mic_n, fft_imu_n, mic_fs=16000, imu_fs=25600, shaft_hz=None,
             bearing_freqs_mic=None, bearing_freqs_imu=None, bf=None):
    """bf: the BearingFreqs object itself (not just a precomputed markers()
    dict), needed because the 3 envelope panels' fs (~3200 Hz, from
    IMU_FS_HZ/IMU_ENVELOPE_DECIM) is only known once a real frame arrives and
    reports it (Phase 11b) -- unlike mic_fs/imu_fs, it cannot be baked into a
    markers() dict at startup the way bearing_freqs_mic/bearing_freqs_imu are."""
    import recv_verify as _rv

    mic_bins  = fft_mic_n // 2
    imu_bins  = fft_imu_n // 2
    mic_freqs = np.linspace(0, mic_fs / 2, mic_bins)
    imu_freqs = np.linspace(0, imu_fs / 2, imu_bins)

    HISTORY_LEN    = _rv.HISTORY_LEN
    WATERFALL_ROWS = _rv.WATERFALL_ROWS

    # Per-satellite history — keyed by satellite name so alternating frames
    # from different satellites don't clear each other's accumulated history.
    _sat_crest_mic: dict = {}
    _sat_crest_imu: dict = {}
    _sat_kurt_mic:  dict = {}
    _sat_wf_buf:    dict = {}

    def _ensure_sat_history(name):
        if name not in _sat_crest_mic:
            _sat_crest_mic[name] = collections.deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
            _sat_crest_imu[name] = collections.deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
            _sat_kurt_mic[name]  = collections.deque([3.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
            _sat_wf_buf[name]    = np.full((WATERFALL_ROWS, mic_bins), -120.0, dtype=np.float32)

    # Waterfall: rows=time (newest at top), cols=frequency bins
    wf_buf = np.full((WATERFALL_ROWS, mic_bins), -120.0, dtype=np.float32)

    plt.ion()
    fig = plt.figure(figsize=(14, 16))
    fig.patch.set_facecolor('#0d0d0d')

    # Layout: 5 rows × 6 cols (6 is lcm(2, 3), so the 2-panel raw rows and the
    # 3-panel envelope row both divide evenly into whole-column spans)
    #  Row 0: MIC FFT (cols 0-2)         | IMU X radial (cols 3-5)
    #  Row 1: MIC Waterfall (full width)
    #  Row 2: IMU Y radial (cols 0-2)    | IMU Z axial (cols 3-5)
    #  Row 3: ENV X (cols 0-1) | ENV Y (cols 2-3) | ENV Z (cols 4-5)  -- Phase 11b
    #  Row 4: Crest & Kurtosis history (full width)
    gs = gridspec.GridSpec(5, 6, figure=fig,
                           height_ratios=[1.0, 0.65, 1.0, 1.0, 0.65],
                           hspace=0.62, wspace=0.35)

    ax_mic   = fig.add_subplot(gs[0, 0:3])
    ax_x     = fig.add_subplot(gs[0, 3:6])
    ax_wf    = fig.add_subplot(gs[1, :])   # waterfall spans full width
    ax_y     = fig.add_subplot(gs[2, 0:3])
    ax_z     = fig.add_subplot(gs[2, 3:6])
    ax_env_x = fig.add_subplot(gs[3, 0:2])
    ax_env_y = fig.add_subplot(gs[3, 2:4])
    ax_env_z = fig.add_subplot(gs[3, 4:6])
    ax_cr    = fig.add_subplot(gs[4, :])

    def _style(ax, grid=True):
        ax.set_facecolor('#111111')
        ax.tick_params(colors='#aaaaaa', labelsize=7)
        for sp in ax.spines.values():
            sp.set_edgecolor('#333333')
        if grid:
            ax.grid(True, alpha=0.15, color='gray')

    for ax in (ax_mic, ax_x, ax_y, ax_z, ax_env_x, ax_env_y, ax_env_z, ax_cr):
        _style(ax)
    _style(ax_wf, grid=False)  # waterfall has no grid

    def _draw_bearing_markers(ax, bearing_freqs, fs):
        """Colored vertical lines + rotated labels at each bearing fault
        frequency below fs/2. Returns the created artists so a caller that
        redraws markers once a real (not placeholder) fs becomes known --
        the envelope panels below -- can remove the old set first."""
        artists = []
        if not bearing_freqs:
            return artists
        _DFLT_C = '#aaaaaa'
        for label, freq in bearing_freqs.items():
            if 0 < freq < fs / 2:
                c = _rv.MARKER_COLORS.get(label, _DFLT_C)
                artists.append(ax.axvline(freq, color=c, alpha=0.55, lw=0.9, ls='-.'))
                artists.append(ax.text(freq + fs / 2 * 0.005, 5, label,
                               color=c, fontsize=5, rotation=90,
                               va='top', ha='left', alpha=0.85))
        return artists

    def _fft_panel(ax, freqs, color, title, fs, bearing_freqs=None):
        (line,) = ax.plot(freqs, np.full(len(freqs), -130.0), lw=0.8, color=color)
        ax.set_xlim(0, fs / 2)
        ax.set_ylim(-130, 10)
        ax.set_ylabel('dBFS', color='#aaaaaa', fontsize=7)
        ax.set_xlabel('Hz',   color='#aaaaaa', fontsize=7)
        ax.set_title(title,   color='white',   fontsize=8)
        if shaft_hz and shaft_hz > 0:
            for h in range(1, 11):
                f = shaft_hz * h
                if f < fs / 2:
                    ax.axvline(f, color='#ffff44', alpha=0.3, lw=0.6, ls='--')
        _draw_bearing_markers(ax, bearing_freqs, fs)
        return line

    line_mic = _fft_panel(ax_mic, mic_freqs, 'cyan',
                          f'MIC FFT  {fft_mic_n}-pt  {mic_fs//1000} kHz', mic_fs,
                          bearing_freqs=bearing_freqs_mic)
    line_x   = _fft_panel(ax_x, imu_freqs, '#ff7f0e',
                          f'IMU X  radial  {fft_imu_n}-pt  {imu_fs//1000} kHz', imu_fs,
                          bearing_freqs=bearing_freqs_imu)
    line_y   = _fft_panel(ax_y, imu_freqs, '#2ca02c',
                          f'IMU Y  radial  {fft_imu_n}-pt  {imu_fs//1000} kHz', imu_fs,
                          bearing_freqs=bearing_freqs_imu)
    line_z   = _fft_panel(ax_z, imu_freqs, '#d62728',
                          f'IMU Z  axial   {fft_imu_n}-pt  {imu_fs//1000} kHz', imu_fs,
                          bearing_freqs=bearing_freqs_imu)

    # Envelope panels (Phase 11b / ADR-032): fs (~3200 Hz, decimated) is not
    # known until the first real frame reports it (see decoded_to_frame_dict()
    # in mqtt_subscriber.py), so these start with an empty line and a title
    # that says so. No bearing markers are drawn here -- those wait for the
    # real fs too, in the frame loop below.
    line_env_x = _fft_panel(ax_env_x, np.array([]), '#ff7f0e',
                            'IMU X ENVELOPE spectrum -- awaiting first frame', imu_fs / 8)
    line_env_y = _fft_panel(ax_env_y, np.array([]), '#2ca02c',
                            'IMU Y ENVELOPE spectrum -- awaiting first frame', imu_fs / 8)
    line_env_z = _fft_panel(ax_env_z, np.array([]), '#d62728',
                            'IMU Z ENVELOPE spectrum -- awaiting first frame', imu_fs / 8)
    _env_state = {'fs': None, 'n_bins': None, 'artists': []}

    # Stub signal verification markers
    for f, ax in ((50, ax_x), (50, ax_y), (150, ax_y), (100, ax_z)):
        ax.axvline(f, color='white', alpha=0.35, lw=0.6, ls=':')

    # ── Waterfall ─────────────────────────────────────────────────────────────
    frame_period_s = 1.0 / 2.2            # ~0.45 s per frame
    wf_duration_s  = WATERFALL_ROWS * frame_period_s
    img_wf = ax_wf.imshow(
        wf_buf,
        aspect='auto',
        origin='upper',                    # row 0 = newest (top)
        extent=[0, mic_fs / 2, wf_duration_s, 0],
        vmin=-120, vmax=-50,
        cmap='inferno',
        interpolation='nearest',
    )
    cbar = plt.colorbar(img_wf, ax=ax_wf, fraction=0.015, pad=0.01)
    cbar.set_label('dBFS', color='#aaaaaa', fontsize=7)
    cbar.ax.tick_params(colors='#aaaaaa', labelsize=7)
    ax_wf.set_xlabel('Hz',         color='#aaaaaa', fontsize=7)
    ax_wf.set_ylabel('Time (s) ↓', color='#aaaaaa', fontsize=7)
    ax_wf.set_title(
        f'MIC Waterfall  —  last {WATERFALL_ROWS} frames  (~{wf_duration_s:.0f} s, newest at top)',
        color='white', fontsize=8)
    ax_wf.tick_params(colors='#aaaaaa', labelsize=7)
    if shaft_hz and shaft_hz > 0:
        for h in range(1, 11):
            f = shaft_hz * h
            if f < mic_fs / 2:
                ax_wf.axvline(f, color='#ffff44', alpha=0.25, lw=0.8, ls='--')

    # ── Crest & kurtosis history ───────────────────────────────────────────────
    xc = np.arange(HISTORY_LEN)
    lc_mic,  = ax_cr.plot(xc, [0.0] * HISTORY_LEN, lw=1.0, color='cyan',    label='MIC crest')
    lc_imu,  = ax_cr.plot(xc, [0.0] * HISTORY_LEN, lw=1.0, color='#ff7f0e', label='IMU crest')
    lc_kurt, = ax_cr.plot(xc, [3.0] * HISTORY_LEN, lw=1.2, color='#aa44ff', label='MIC kurtosis/3')
    ax_cr.axhline(_rv.CREST_WARN,  color='yellow', ls='--', lw=0.8, alpha=0.8,
                  label=f'MIC Warn {_rv.CREST_WARN}')
    ax_cr.axhline(_rv.CREST_FAULT, color='red',    ls='--', lw=0.8, alpha=0.8,
                  label=f'MIC Fault {_rv.CREST_FAULT}')
    ax_cr.axhline(_rv.IMU_CREST_WARN, color='yellow', ls=':', lw=0.8, alpha=0.6,
                  label=f'IMU Warn {_rv.IMU_CREST_WARN}')
    ax_cr.set_ylim(0, 10)
    ax_cr.set_xlim(0, HISTORY_LEN - 1)
    ax_cr.set_ylabel('Factor', color='#aaaaaa', fontsize=7)
    ax_cr.set_xlabel(f'Last {HISTORY_LEN} frames', color='#aaaaaa', fontsize=7)
    ax_cr.set_title('Crest & Kurtosis History — impulsive fault indicators (kurtosis÷3 scaled)',
                    color='white', fontsize=8)
    ax_cr.legend(ncol=5, loc='upper right', fontsize=7,
                 facecolor='#1a1a1a', edgecolor='#444', labelcolor='white')

    title_t = fig.suptitle('EPM Live Monitor — waiting for satellite…',
                            color='white', fontsize=9)
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.show()

    last_id  = -1

    while plt.fignum_exists(fig.number):
        _rv._display.wait(timeout=0.3)
        frame, satname = _rv._display.get()
        if frame is None or frame['frame_id'] == last_id:
            plt.pause(0.05)
            continue
        last_id = frame['frame_id']

        _ensure_sat_history(satname)
        crest_mic = _sat_crest_mic[satname]
        crest_imu = _sat_crest_imu[satname]
        kurt_mic  = _sat_kurt_mic[satname]
        wf_buf    = _sat_wf_buf[satname]

        for line, data, ax in (
            (line_mic, frame['mic_fft'], ax_mic),
            (line_x,   frame['imu_x'],  ax_x),
            (line_y,   frame['imu_y'],  ax_y),
            (line_z,   frame['imu_z'],  ax_z),
        ):
            if len(data) == len(line.get_xdata()):
                line.set_ydata(data)
                lo = max(float(np.min(data)) - 5, -130)
                hi = min(float(np.max(data)) + 5,   10)
                ax.set_ylim(lo, hi)

        # Envelope panels (Phase 11b): fs comes from the frame itself
        # (mqtt_subscriber.decoded_to_frame_dict()'s imu_env_fs, read off the
        # wire's own SPECTRUM header -- never a hardcoded IMU_FS_HZ/8 guess).
        # tcp_legacy-sourced frames have no envelope sections at all (that
        # wire format structurally cannot carry channels 9-11), so
        # env_fs stays 0.0 and these panels simply stay at their
        # "awaiting first frame" placeholder for that path.
        env_panels = (
            (line_env_x, frame.get('imu_env_x', np.empty(0)), ax_env_x, 'X'),
            (line_env_y, frame.get('imu_env_y', np.empty(0)), ax_env_y, 'Y'),
            (line_env_z, frame.get('imu_env_z', np.empty(0)), ax_env_z, 'Z'),
        )
        env_fs = frame.get('imu_env_fs', 0.0)
        env_n_bins = next((len(d) for _, d, _, _ in env_panels if len(d) > 0), 0)
        if env_fs > 0 and env_n_bins > 0 and (
                env_fs != _env_state['fs'] or env_n_bins != _env_state['n_bins']):
            # First frame to report envelope data, or its fs/bin_count
            # changed -- (re)establish each panel's x-axis and bearing
            # markers against the real decoded fs.
            env_freqs = np.linspace(0, env_fs / 2, env_n_bins)
            for line, _data, ax, axis_label in env_panels:
                line.set_xdata(env_freqs)
                line.set_ydata(np.full(env_n_bins, -130.0))
                ax.set_xlim(0, env_fs / 2)
                ax.set_title(
                    f'IMU {axis_label} ENVELOPE spectrum  {env_n_bins * 2}-pt  '
                    f'env_fs={env_fs:.0f} Hz  (decimated from {imu_fs} Hz)',
                    color='white', fontsize=8)
            for artist in _env_state['artists']:
                artist.remove()
            _env_state['artists'] = []
            if bf is not None:
                bearing_freqs_env = bf.markers(env_fs)
                for _line, _data, ax, _axis_label in env_panels:
                    _env_state['artists'] += _draw_bearing_markers(ax, bearing_freqs_env, env_fs)
            _env_state['fs']      = env_fs
            _env_state['n_bins']  = env_n_bins

        for line, data, ax, _axis_label in env_panels:
            if len(data) > 0 and len(data) == len(line.get_xdata()):
                line.set_ydata(data)
                lo = max(float(np.min(data)) - 5, -130)
                hi = min(float(np.max(data)) + 5,   10)
                ax.set_ylim(lo, hi)

        # Waterfall: roll buffer down (row 0 = newest) and insert latest mic FFT
        mic_data = np.array(frame['mic_fft'], dtype=np.float32)
        if len(mic_data) == mic_bins:
            wf_buf[1:] = wf_buf[:-1]
            wf_buf[0]  = mic_data
            img_wf.set_data(wf_buf)
            # Auto-adjust colour range to live signal floor/peak
            sig_min = float(np.percentile(mic_data, 5))
            sig_max = float(np.percentile(mic_data, 99))
            img_wf.set_clim(vmin=max(sig_min - 5, -130), vmax=min(sig_max + 5, 0))

        crest_mic.append(float(frame['mic_crest']))
        crest_imu.append(float(frame['imu_crest']))
        kurt_mic.append(float(frame['mic_kurtosis']) / 3.0)
        lc_mic.set_ydata(list(crest_mic))
        lc_imu.set_ydata(list(crest_imu))
        lc_kurt.set_ydata(list(kurt_mic))

        n_conn = _sat_count()
        status = 'OK' if not frame['errors'] else 'WARN:' + ';'.join(frame['errors'])
        hb = frame.get('high_band_ratio', 0.0)
        title_t.set_text(
            f"EPM  [{satname}]  frame={frame['frame_id']}  "
            f"rms={frame['mic_rms']:.5f}  "
            f"K={frame['mic_kurtosis']:.2f}  "
            f"CF={frame['mic_crest']:.2f}  "
            f"HB={hb:.2f}  "
            f"{status}  (sat:{n_conn})"
        )

        fig.canvas.draw_idle()
        plt.pause(0.05)
