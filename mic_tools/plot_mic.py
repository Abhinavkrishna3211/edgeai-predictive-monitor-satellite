#!/usr/bin/env python3
"""
plot_mic.py — LEGACY SERIAL TOOL (development Phase 1 only).

This script reads a text-based serial protocol with STATS / RAW_START / FFT_START
markers that was used during the early firmware debug phase.  The CURRENT firmware
sends binary TCP frames — use recv_verify.py for live data from the running system.

Keep this file for historical reference or if you revert to serial-text debug output.
Do NOT use it with the production firmware — it will show nothing.

Legacy usage (Phase 1 firmware only):
    python plot_mic.py --port COM9
    python plot_mic.py --port COM9 --shaft-hz 50
    python plot_mic.py --port COM9 --mark-hz 156,312,468
    python plot_mic.py --port COM9 --db-min -100
"""

import argparse
import queue
import sys
import threading
from collections import namedtuple

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.lines import Line2D

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("pip install pyserial numpy matplotlib")
    sys.exit(1)

SAMPLE_RATE_HZ = 16000
FFT_N          = 1024
FFT_HALF       = FFT_N // 2
BAUD_RATE      = 921600
WATERFALL_ROWS = 80

# Crest factor thresholds for colour coding
CREST_WARN  = 3.0
CREST_FAULT = 6.0

Frame = namedtuple("Frame", ["rms", "dc", "min_s", "max_s", "clip", "crest", "raw", "fft"])


# ── Port selection ─────────────────────────────────────────────────────────────

def pick_port():
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        sys.exit(1)
    for p in ports:
        if "USB" in p.description:
            print(f"Auto-selected: {p.device}  ({p.description})")
            return p.device
    print("Ports:", [f"{p.device} ({p.description})" for p in ports])
    return ports[0].device


# ── Frame parser ───────────────────────────────────────────────────────────────

def read_frame(ser):
    stats   = None
    raw_buf = []
    fft_buf = []
    section = None

    while True:
        try:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
        except Exception:
            return None

        if line.startswith("STATS"):
            try:
                p = line.split()
                # rms dc min max clip crest
                stats = (float(p[1]), float(p[2]),
                         int(p[3]), int(p[4]), int(p[5]), float(p[6]))
            except (IndexError, ValueError):
                stats = None

        elif line == "RAW_START":
            raw_buf = []
            section = "raw"
        elif line == "RAW_END":
            section = None
        elif line == "FFT_START":
            fft_buf = []
            section = "fft"
        elif line == "FFT_END":
            section = None
            if stats and len(raw_buf) == FFT_N and len(fft_buf) == FFT_HALF:
                return Frame(
                    rms=stats[0], dc=stats[1],
                    min_s=stats[2], max_s=stats[3],
                    clip=stats[4],  crest=stats[5],
                    raw=np.array(raw_buf, dtype=np.float32),
                    fft=np.array(fft_buf, dtype=np.float32),
                )
            stats = None; raw_buf = []; fft_buf = []
        elif section == "raw":
            try:
                raw_buf.append(float(line))
            except ValueError:
                section = None
        elif section == "fft":
            try:
                fft_buf.append(float(line))
            except ValueError:
                section = None


# ── Background reader ─────────────────────────────────────────────────────────

def _reader(ser, q):
    while True:
        frame = read_frame(ser)
        if frame is None:
            continue
        if q.full():
            try:
                q.get_nowait()
            except queue.Empty:
                pass
        try:
            q.put_nowait(frame)
        except queue.Full:
            pass


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port",      default=None)
    parser.add_argument("--baud",      type=int,   default=BAUD_RATE)
    parser.add_argument("--db-min",    type=float, default=-120.0,
                        help="dBFS floor (default -120)")
    parser.add_argument("--db-max",    type=float, default=0.0,
                        help="dBFS ceiling (default 0)")
    parser.add_argument("--shaft-hz",  type=float, default=None,
                        help="Motor shaft frequency Hz (e.g. 50 = 3000 RPM). "
                             "Marks 1×–10× harmonics in yellow.")
    parser.add_argument("--mark-hz",   type=str,   default=None,
                        help="Comma-separated Hz to mark in magenta "
                             "(e.g. pre-calculated bearing fault freqs: '156,312,468').")
    args = parser.parse_args()

    port = args.port or pick_port()
    print(f"Opening {port} @ {args.baud} baud …")
    ser = serial.Serial(port, args.baud, timeout=3)

    frame_q = queue.Queue(maxsize=2)
    threading.Thread(target=_reader, args=(ser, frame_q), daemon=True).start()

    hz_per_bin = SAMPLE_RATE_HZ / FFT_N
    freqs = np.arange(FFT_HALF) * hz_per_bin
    t_ms  = np.arange(FFT_N) / SAMPLE_RATE_HZ * 1000.0

    # ── Figure ────────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(13, 9))
    fig.patch.set_facecolor("#0d0d0d")
    gs  = fig.add_gridspec(3, 1, height_ratios=[1, 1.1, 1.4], hspace=0.45)

    ax_wave = fig.add_subplot(gs[0])
    ax_spec = fig.add_subplot(gs[1])
    ax_fall = fig.add_subplot(gs[2])

    for ax in (ax_wave, ax_spec, ax_fall):
        ax.set_facecolor("#111111")
        ax.tick_params(colors="#aaaaaa", labelsize=7)
        for sp in ax.spines.values():
            sp.set_edgecolor("#333333")

    # ── Waveform ──────────────────────────────────────────────────────────────
    (line_wave,) = ax_wave.plot(t_ms, np.zeros(FFT_N), lw=0.6, color="#00ff88")
    ax_wave.set_xlim(0, t_ms[-1])
    ax_wave.set_ylim(-0.05, 0.05)
    ax_wave.set_ylabel("Amplitude", color="#aaaaaa", fontsize=8)
    ax_wave.set_title("Raw Waveform  (64 ms window, latest block)", color="white", fontsize=9)
    ax_wave.axhline(0, color="#2a2a2a", lw=0.5)

    stats_text = ax_wave.text(
        0.005, 0.97, "waiting …",
        transform=ax_wave.transAxes,
        color="#ffcc00", fontsize=7.5, va="top", family="monospace"
    )
    crest_text = ax_wave.text(
        0.995, 0.97, "",
        transform=ax_wave.transAxes,
        color="white", fontsize=8, va="top", ha="right",
        weight="bold", family="monospace"
    )

    # ── Spectrum ──────────────────────────────────────────────────────────────
    (line_spec,) = ax_spec.plot(
        freqs, np.full(FFT_HALF, args.db_min), lw=0.8, color="cyan", zorder=3
    )
    ax_spec.set_xlim(0, SAMPLE_RATE_HZ / 2)
    ax_spec.set_ylim(args.db_min, args.db_max)
    ax_spec.set_ylabel("dBFS", color="#aaaaaa", fontsize=8)
    ax_spec.set_title(
        f"FFT Spectrum  ({FFT_N}-pt, averaged ×4, {hz_per_bin:.1f} Hz/bin)",
        color="white", fontsize=9
    )
    ax_spec.grid(True, alpha=0.12, color="gray")

    # Static harmonic/fault-frequency markers (drawn once at setup)
    if args.shaft_hz and args.shaft_hz > 0:
        for h in range(1, 11):
            f = args.shaft_hz * h
            if f < SAMPLE_RATE_HZ / 2:
                ax_spec.axvline(f, color="#ffff00", alpha=0.35, lw=0.8, ls="--", zorder=2)
                ax_spec.text(f, args.db_max - 3, f"{h}×",
                             color="#ffff00", fontsize=6, ha="center", va="top")
        print(f"Shaft harmonics marked: {args.shaft_hz} Hz × 1–10")

    if args.mark_hz:
        for tok in args.mark_hz.split(","):
            try:
                f = float(tok.strip())
                ax_spec.axvline(f, color="#ff44ff", alpha=0.55, lw=1.0, ls=":", zorder=2)
                ax_spec.text(f, args.db_max - 10, f"{f:.0f}",
                             color="#ff44ff", fontsize=6, ha="center", va="top")
            except ValueError:
                pass
        print(f"Custom fault frequencies marked: {args.mark_hz}")

    peak_dot,  = ax_spec.plot([], [], "ro", markersize=4, zorder=5)
    peak_label = ax_spec.text(
        0.995, 0.97, "",
        transform=ax_spec.transAxes,
        color="#ff6666", fontsize=7.5, va="top", ha="right", family="monospace"
    )

    # Legend for markers
    legend_handles = [Line2D([0], [0], color="cyan", lw=1.5, label="averaged FFT")]
    if args.shaft_hz:
        legend_handles.append(Line2D([0], [0], color="#ffff00", ls="--", lw=0.8,
                                     label=f"shaft harmonics ({args.shaft_hz:.0f} Hz)"))
    if args.mark_hz:
        legend_handles.append(Line2D([0], [0], color="#ff44ff", ls=":", lw=1.0,
                                     label="fault freqs"))
    ax_spec.legend(handles=legend_handles, loc="upper right", fontsize=6,
                   facecolor="#1a1a1a", edgecolor="#444444", labelcolor="white")

    # ── Waterfall ─────────────────────────────────────────────────────────────
    wf = np.full((WATERFALL_ROWS, FFT_HALF), args.db_min, dtype=np.float32)
    img = ax_fall.imshow(
        wf, aspect="auto", origin="upper",
        extent=[0, SAMPLE_RATE_HZ / 2,
                WATERFALL_ROWS * FFT_N * 4 / SAMPLE_RATE_HZ, 0],  # ×4 for avg window
        vmin=args.db_min, vmax=args.db_max,
        cmap="inferno", interpolation="nearest",
    )
    cbar = plt.colorbar(img, ax=ax_fall, fraction=0.02, pad=0.01)
    cbar.set_label("dBFS", color="#aaaaaa", fontsize=7)
    cbar.ax.tick_params(colors="#aaaaaa", labelsize=7)
    ax_fall.set_xlabel("Frequency (Hz)", color="#aaaaaa", fontsize=8)
    ax_fall.set_ylabel("Time — newest at top", color="#aaaaaa", fontsize=8)
    total_s = WATERFALL_ROWS * FFT_N * 4 / SAMPLE_RATE_HZ
    ax_fall.set_title(
        f"Waterfall  ({WATERFALL_ROWS} averaged frames = {total_s:.0f} s history)",
        color="white", fontsize=9
    )

    if args.shaft_hz:
        for h in range(1, 11):
            f = args.shaft_hz * h
            if f < SAMPLE_RATE_HZ / 2:
                ax_fall.axvline(f, color="#ffff00", alpha=0.25, lw=0.6, ls="--")

    fig.suptitle(
        f"EdgeAI Predictive Monitor  |  {FFT_N}-pt FFT ×4 avg  |  "
        f"{hz_per_bin:.1f} Hz/bin  |  Fs={SAMPLE_RATE_HZ} Hz",
        color="white", fontsize=9
    )

    # ── Animation ─────────────────────────────────────────────────────────────

    def crest_colour(c):
        if c >= CREST_FAULT: return "#ff3333"
        if c >= CREST_WARN:  return "#ffaa00"
        return "#00ff88"

    def update(_):
        try:
            frame = frame_q.get_nowait()
        except queue.Empty:
            return

        # Waveform — auto-scale to block peak
        pa = max(abs(float(frame.raw.max())), abs(float(frame.raw.min())), 1e-4)
        ax_wave.set_ylim(-pa * 1.15, pa * 1.15)
        line_wave.set_ydata(frame.raw)

        # Stats text
        clk_str = "OK" if frame.clip == 0 else f"CLIP={frame.clip}!"
        stats_text.set_text(
            f"rms={frame.rms:.5f}  dc={frame.dc:+.5f}  "
            f"peak±{max(abs(frame.min_s), abs(frame.max_s))}  {clk_str}"
        )
        stats_text.set_color("#ffcc00" if frame.clip == 0 else "#ff4444")

        # Crest factor (prominent — primary fault indicator)
        cc = crest_colour(frame.crest)
        crest_text.set_text(f"crest={frame.crest:.1f}")
        crest_text.set_color(cc)

        # Spectrum
        line_spec.set_ydata(frame.fft)
        pk = int(np.argmax(frame.fft[1:])) + 1
        peak_dot.set_data([freqs[pk]], [frame.fft[pk]])
        peak_label.set_text(f"{freqs[pk]:.0f} Hz  {frame.fft[pk]:.1f} dBFS")

        # Waterfall
        wf[1:] = wf[:-1]
        wf[0]  = frame.fft
        img.set_data(wf)

        fig.canvas.draw_idle()

    animation.FuncAnimation(fig, update, interval=50, blit=False)
    plt.show()


if __name__ == "__main__":
    main()
