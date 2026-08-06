#!/usr/bin/env python3
"""
mic_char_analyze.py — INMP441 frequency-response characterization tool (WP-08).

Listens on TCP port 5100, accepts the satellite's binary EPM frames, and
reports per-frame FFT peak analysis for empirical tone testing.  Run this
instead of recv_verify.py during a characterization session.

Usage:
    # Baseline or ambient check at the current 16 kHz build:
    python mic_tools/mic_char_analyze.py --sample-rate 16000

    # Tone test at 32 kHz build — play a 10 kHz tone near the mic:
    python mic_tools/mic_char_analyze.py --sample-rate 32000 --tone 10000

    # Same but firmware uses AES-GCM encryption:
    python mic_tools/mic_char_analyze.py --sample-rate 32000 --tone 10000 --psk-hex 0123456789abcdef0123456789abcdef

Per-frame output columns:
    frame  ovf  clip  peak_bin  peak_hz  peak_dBFS  Δref_dB  acc?  alias?  rms  kurtosis

    ovf      — overflow_count delta sent by firmware (0 = DMA keeping up)
    clip     — 1 if any sample hit full-scale this frame
    peak_hz  — frequency of the highest FFT bin (excluding DC)
    Δref_dB  — amplitude relative to the 1 kHz reference (NaN until 1 kHz seen)
    acc?     — ✓ if peak is within ±2 bins of --tone, ✗(exp N) if off-bin
    alias?   — amplitude at the mirror alias bin (2*Nyquist - tone); shown when
               --tone is above Nyquist; a strong value here means the INMP441's
               internal filter is not suppressing out-of-band content at this rate

Logging (on by default):
    Every frame is written to a per-session CSV under --log-dir
    (default mic_tools/char_logs/), named e.g. 32000hz_fft1024_tone10000_20260704_141502.csv,
    with raw numeric columns (not the formatted display strings) for later analysis
    in Python/Excel. At the end of each session, a summary row (frame count,
    overflow/clip totals, tone-accuracy pass rate, alias detected y/n, peak_dBFS
    min/max/mean) is appended to a shared mic_tools/char_logs/_index.csv, so results
    across multiple sample-rate/tone runs can be compared side by side.
    Use --tag to label a session (e.g. --tag phone-10cm) and --no-log to disable.
"""

import argparse
import csv
import os
import socket
import struct
import sys
import time
from datetime import datetime

# ── Protocol constants — must match firmware epm_common.h + recv_verify.py ──────
EPM_MAGIC          = 0xEA1DF00D
HELLO_MAGIC        = 0xEA1D0000
HELLO_FMT          = '<I6sBB12s'
HELLO_SIZE         = struct.calcsize(HELLO_FMT)   # 24 bytes
HEADER_FMT         = '<IIIHHffffBfffBBB'
HEADER_SIZE        = struct.calcsize(HEADER_FMT)  # 48 bytes
EPM_PROTO_V2_MAGIC = 0xA2
DEFAULT_PORT       = 5100
DEFAULT_IMU_FFT_N  = 2048  # must match FFT_IMU_N; determines frame size

assert HELLO_SIZE  == 24, f"HELLO_SIZE={HELLO_SIZE}"
assert HEADER_SIZE == 48, f"HEADER_SIZE={HEADER_SIZE}"


# ── Network helpers ──────────────────────────────────────────────────────────────

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def _send_v2_ok(conn: socket.socket, spec_avg_n: int) -> None:
    """Send an 8-byte v2 reply: alert=OK, posterior=0, no adaptive changes."""
    reply = struct.pack('<BBHBBBB',
                       EPM_PROTO_V2_MAGIC,  # proto_ver
                       0x00,                # alert=OK
                       0,                   # fault_posterior (×10000) = 0
                       0,                   # fft_overlap_pct = 0
                       spec_avg_n,          # spec_avg_n (pass-through)
                       0, 0)               # reserved
    try:
        conn.sendall(reply)
    except OSError:
        pass


# ── Frame parsing ────────────────────────────────────────────────────────────────

def _parse_header(raw: bytes) -> dict:
    (magic, frame_id, ts_ms,
     mic_bins, imu_bins,
     mic_rms, mic_crest, mic_dc, mic_kurtosis, mic_clip,
     imu_rms, imu_crest, imu_dc, imu_clip,
     imu_axes, overflow_count) = struct.unpack_from(HEADER_FMT, raw, 0)
    return dict(magic=magic, frame_id=frame_id, ts_ms=ts_ms,
                mic_bins=mic_bins, imu_bins=imu_bins,
                mic_rms=mic_rms, mic_crest=mic_crest, mic_dc=mic_dc,
                mic_kurtosis=mic_kurtosis, mic_clip=int(mic_clip),
                imu_axes=imu_axes, overflow_count=int(overflow_count))


# ── Logging helpers ──────────────────────────────────────────────────────────────

def _open_csv_log(log_dir: str, fs: int, fft_n: int, tone, tag) -> tuple:
    """Creates log_dir if needed and opens a per-session CSV log for writing.
    Returns (csv_path, file_handle, csv.writer)."""
    os.makedirs(log_dir, exist_ok=True)
    ts_str   = datetime.now().strftime('%Y%m%d_%H%M%S')
    tone_str = f"tone{tone}" if tone else "ambient"
    tag_str  = f"_{tag}" if tag else ""
    fname    = f"{fs}hz_fft{fft_n}_{tone_str}{tag_str}_{ts_str}.csv"
    path     = os.path.join(log_dir, fname)
    f = open(path, 'w', newline='')
    w = csv.writer(f)
    w.writerow([
        'frame_id', 'ts_ms', 'wall_time', 'overflow_count', 'mic_clip',
        'peak_bin', 'peak_hz', 'peak_dBFS', 'delta_ref_dB',
        'tone_hz', 'exp_bin', 'acc_pass', 'alias_bin', 'alias_peak_dBFS', 'alias_flag',
        'mic_rms', 'mic_crest', 'mic_kurtosis',
    ])
    return path, f, w


def _append_index_row(log_dir: str, row: dict) -> str:
    """Appends one summary row to a master index CSV shared across all sessions
    in log_dir, creating the header if the file doesn't exist yet."""
    idx_path   = os.path.join(log_dir, '_index.csv')
    fieldnames = ['timestamp', 'sample_rate_hz', 'fft_n', 'tone_hz', 'frames',
                  'overflow_total', 'clip_total', 'acc_pass_rate', 'alias_detected',
                  'peak_dBFS_min', 'peak_dBFS_max', 'peak_dBFS_mean', 'ref_1k_dBFS',
                  'log_file']
    write_header = not os.path.exists(idx_path)
    with open(idx_path, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        w.writerow(row)
    return idx_path


# ── Main analysis loop ───────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    fs          = args.sample_rate
    fft_n       = args.fft_n
    mic_bins    = fft_n // 2
    imu_bins    = args.imu_fft_n // 2
    nyquist     = fs // 2
    hz_per_bin  = fs / fft_n

    # Expected frame payload size (plaintext).
    # send_frame() in wifi_task.c: header + mic_fft + imu_x + imu_y + imu_z
    exp_plain   = HEADER_SIZE + mic_bins * 4 + imu_bins * 4 * 3
    max_payload = exp_plain + 12 + 16 + 1024  # encrypted headroom

    # ── Optional AES-128-GCM decryptor ──────────────────────────────────────
    decryptor = None
    if args.psk_hex:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            print("ERROR: --psk-hex requires: pip install 'cryptography>=42.0.0'")
            sys.exit(1)
        psk = bytes.fromhex(args.psk_hex.strip())
        if len(psk) != 16:
            print(f"ERROR: PSK must be exactly 16 bytes (32 hex chars), got {len(psk)}")
            sys.exit(1)
        decryptor = AESGCM(psk)

    def _decrypt(blob: bytes) -> bytes:
        if len(blob) < 28:  # 12 IV + 0 plain + 16 tag minimum
            raise ValueError(f"encrypted blob too short ({len(blob)} B)")
        iv  = blob[:12]
        tag = blob[-16:]
        return decryptor.decrypt(iv, blob[12:-16] + tag, None)

    # ── Pre-flight summary ───────────────────────────────────────────────────
    print()
    print("── INMP441 Characterization Tool ───────────────────────────────────────")
    print(f"  Sample rate : {fs:,} Hz       Nyquist : {nyquist:,} Hz")
    print(f"  FFT size    : {fft_n} pts       Hz/bin  : {hz_per_bin:.3f}")
    print(f"  mic_bins    : {mic_bins}          imu_bins: {imu_bins}")
    print(f"  Encryption  : {'AES-128-GCM (--psk-hex supplied)' if decryptor else 'none'}")
    if args.tone:
        exp_bin   = round(args.tone * fft_n / fs)
        alias_hz  = 2 * nyquist - args.tone
        alias_bin = round(alias_hz * fft_n / fs) if 0 < alias_hz < nyquist else -1
        print(f"  Tone check  : {args.tone} Hz → expected bin {exp_bin} ({exp_bin * hz_per_bin:.1f} Hz)")
        if alias_bin >= 0:
            print(f"  Alias check : mirror at {alias_hz:.0f} Hz → bin {alias_bin}"
                  f" (strong peak here = filter leak)")
        else:
            print(f"  Alias check : tone below Nyquist — checking accuracy only")
    print(f"  Listening on 0.0.0.0:{args.port} ...")

    # ── Per-session CSV log ──────────────────────────────────────────────────
    log_path, log_f, log_w = (None, None, None)
    if not args.no_log:
        log_path, log_f, log_w = _open_csv_log(
            args.log_dir, fs, fft_n, args.tone, args.tag)
        print(f"  Logging to  : {log_path}")
    else:
        print(f"  Logging     : disabled (--no-log)")
    print()

    # ── TCP server ───────────────────────────────────────────────────────────
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('', args.port))
    srv.listen(1)

    try:
        conn, addr = srv.accept()
    except KeyboardInterrupt:
        srv.close()
        print("\nAborted before connection.")
        return

    conn.settimeout(15.0)
    print(f"  Connected: {addr[0]}:{addr[1]}")

    # ── Hello handshake ──────────────────────────────────────────────────────
    try:
        hello_raw = _recv_exact(conn, HELLO_SIZE)
    except (ConnectionError, OSError) as e:
        print(f"  ERROR reading hello: {e}")
        conn.close(); srv.close(); return

    magic, mac_bytes, fw_major, fw_minor, name_bytes = struct.unpack(HELLO_FMT, hello_raw)
    if magic != HELLO_MAGIC:
        print(f"  ERROR: bad hello magic 0x{magic:08X} (expected 0x{HELLO_MAGIC:08X})")
        conn.close(); srv.close(); return

    name = name_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
    mac  = ':'.join(f'{b:02X}' for b in mac_bytes)
    print(f"  Satellite : {name}  MAC={mac}  fw={fw_major}.{fw_minor}")
    print()

    # ── Column header ────────────────────────────────────────────────────────
    hdr_line = (f"{'frame':>6}  {'ovf':>3}  {'clip':>4}  "
                f"{'peak_bin':>8}  {'peak_hz':>8}  {'peak_dBFS':>10}  "
                f"{'Δref_dB':>8}  {'acc?':>10}  {'alias?':>14}  "
                f"{'rms':>8}  {'kurtosis':>8}")
    print(hdr_line)
    print("─" * len(hdr_line))

    ref_db_1k = None   # 1 kHz reference amplitude for Δref tracking

    # ── Session accumulators (feed the summary + master index at the end) ────
    n_frames        = 0
    overflow_total  = 0
    clip_total      = 0
    acc_pass_count  = 0
    acc_checked     = 0
    alias_detected  = False
    peak_db_values: list[float] = []

    try:
        while True:
            # ── Read length prefix + payload ─────────────────────────────────
            try:
                (payload_bytes,) = struct.unpack('<I', _recv_exact(conn, 4))
            except (ConnectionError, OSError, struct.error) as e:
                print(f"\n  Connection lost ({e})")
                break

            if payload_bytes < HEADER_SIZE or payload_bytes > max_payload:
                print(f"\n  ERROR: payload_bytes={payload_bytes} out of valid range "
                      f"[{HEADER_SIZE}..{max_payload}] — disconnecting")
                break

            try:
                raw = _recv_exact(conn, payload_bytes)
            except (ConnectionError, OSError) as e:
                print(f"\n  Read error: {e}")
                break

            # ── Decrypt if needed ─────────────────────────────────────────────
            if decryptor is not None:
                try:
                    raw = _decrypt(raw)
                except Exception as e:
                    print(f"\n  DECRYPT ERROR (key mismatch?): {e} — skipping frame")
                    _send_v2_ok(conn, args.spec_avg_n)
                    continue

            # ── Validate size ─────────────────────────────────────────────────
            if len(raw) != exp_plain:
                print(f"\n  WARN: frame {len(raw)} B != expected {exp_plain} B — skipping")
                _send_v2_ok(conn, args.spec_avg_n)
                continue

            # ── Parse header + mic FFT ────────────────────────────────────────
            hdr = _parse_header(raw)
            if hdr['magic'] != EPM_MAGIC:
                print(f"\n  ERROR: bad frame magic 0x{hdr['magic']:08X} — disconnecting")
                break

            mic_fft: list[float] = list(
                struct.unpack_from(f'<{mic_bins}f', raw, HEADER_SIZE))

            # ── Peak detection (skip DC bin 0) ────────────────────────────────
            peak_bin = max(range(1, mic_bins), key=lambda i: mic_fft[i])
            peak_db  = mic_fft[peak_bin]
            peak_hz  = peak_bin * hz_per_bin

            # Track 1 kHz reference for relative amplitude comparison
            if 800 <= peak_hz <= 1200 and peak_db > -70.0:
                ref_db_1k = peak_db
            delta_ref = (peak_db - ref_db_1k) if ref_db_1k is not None else float('nan')

            # ── Tone accuracy check ───────────────────────────────────────────
            acc_str = ""
            exp_bin = None
            acc_pass = None
            if args.tone:
                exp_bin  = round(args.tone * fft_n / fs)
                acc_pass = abs(peak_bin - exp_bin) <= 2
                acc_str  = "✓" if acc_pass else f"✗(exp {exp_bin})"
                acc_checked += 1
                if acc_pass:
                    acc_pass_count += 1

            # ── Alias check: mirror of above-Nyquist tone ─────────────────────
            alias_str  = ""
            alias_bin  = None
            alias_peak = None
            alias_flag = False
            if args.tone and args.tone > nyquist:
                alias_hz  = 2 * nyquist - args.tone
                alias_bin = round(alias_hz * fft_n / fs)
                if 0 < alias_bin < mic_bins:
                    window     = mic_fft[max(0, alias_bin - 3):alias_bin + 4]
                    alias_peak = max(window) if window else -120.0
                    alias_flag = alias_peak > -50.0
                    if alias_flag:
                        alias_str = f"ALIAS {alias_peak:+.1f}dB"
                        alias_detected = True
                    else:
                        alias_str = f"ok({alias_peak:.0f})"

            print(f"{hdr['frame_id']:>6}  "
                  f"{hdr['overflow_count']:>3}  "
                  f"{hdr['mic_clip']:>4}  "
                  f"{peak_bin:>8}  "
                  f"{peak_hz:>8.1f}  "
                  f"{peak_db:>10.1f}  "
                  f"{delta_ref:>8.2f}  "
                  f"{acc_str:>10}  "
                  f"{alias_str:>14}  "
                  f"{hdr['mic_rms']:>8.5f}  "
                  f"{hdr['mic_kurtosis']:>8.2f}")

            # ── Accumulate + persist this frame's data ────────────────────────
            n_frames       += 1
            overflow_total += hdr['overflow_count']
            clip_total     += hdr['mic_clip']
            peak_db_values.append(peak_db)

            if log_w is not None:
                log_w.writerow([
                    hdr['frame_id'], hdr['ts_ms'], f"{time.time():.3f}",
                    hdr['overflow_count'], hdr['mic_clip'],
                    peak_bin, f"{peak_hz:.2f}", f"{peak_db:.2f}",
                    f"{delta_ref:.2f}" if delta_ref == delta_ref else '',  # NaN check
                    args.tone or '', exp_bin if exp_bin is not None else '',
                    acc_pass if acc_pass is not None else '',
                    alias_bin if alias_bin is not None else '',
                    f"{alias_peak:.2f}" if alias_peak is not None else '',
                    alias_flag,
                    f"{hdr['mic_rms']:.6f}", f"{hdr['mic_crest']:.4f}",
                    f"{hdr['mic_kurtosis']:.4f}",
                ])
                if n_frames % 20 == 0:
                    log_f.flush()

            _send_v2_ok(conn, args.spec_avg_n)

    except KeyboardInterrupt:
        print("\n\n── Stopped by user ──")
    finally:
        conn.close()
        srv.close()
        if log_f is not None:
            log_f.flush()
            log_f.close()

    # ── Session summary ──────────────────────────────────────────────────────
    print()
    print("── Session config ──────────────────────────────────────────────────────")
    print(f"  Sample rate : {fs:,} Hz  |  Nyquist : {nyquist:,} Hz  |  Hz/bin : {hz_per_bin:.3f}")
    if args.tone:
        exp_bin = round(args.tone * fft_n / fs)
        print(f"  Tone tested : {args.tone} Hz → bin {exp_bin} ({exp_bin * hz_per_bin:.1f} Hz)")
    if ref_db_1k is not None:
        print(f"  1 kHz ref   : {ref_db_1k:.1f} dBFS (used as Δref_dB baseline)")
    else:
        print(f"  1 kHz ref   : not observed (play 1 kHz first to calibrate Δref_dB)")

    print()
    print("── Session results ──────────────────────────────────────────────────────")
    print(f"  Frames      : {n_frames}")
    print(f"  Overflows   : {overflow_total} total"
          + ("  *** DMA could not keep up — data has gaps ***" if overflow_total else "  (clean)"))
    clip_note = "" if clip_total == 0 else "  *** reduce source level or mic distance ***"
    print(f"  Clips       : {clip_total} frames hit full-scale{clip_note}")
    if acc_checked:
        acc_rate = acc_pass_count / acc_checked
        print(f"  Tone accuracy: {acc_pass_count}/{acc_checked} frames within ±2 bins "
              f"({acc_rate*100:.1f}%)")
    else:
        acc_rate = None
    if args.tone and args.tone > nyquist:
        print(f"  Alias       : {'DETECTED — filter leak at this rate' if alias_detected else 'not detected (clean)'}")
    if peak_db_values:
        pmin, pmax = min(peak_db_values), max(peak_db_values)
        pmean = sum(peak_db_values) / len(peak_db_values)
        print(f"  peak_dBFS   : min {pmin:.1f}  max {pmax:.1f}  mean {pmean:.1f}")
    else:
        pmin = pmax = pmean = None

    if log_path is not None:
        print(f"  CSV log     : {log_path}")
        idx_path = _append_index_row(args.log_dir, {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'sample_rate_hz': fs,
            'fft_n': fft_n,
            'tone_hz': args.tone or '',
            'frames': n_frames,
            'overflow_total': overflow_total,
            'clip_total': clip_total,
            'acc_pass_rate': f"{acc_rate:.3f}" if acc_rate is not None else '',
            'alias_detected': alias_detected if (args.tone and args.tone > nyquist) else '',
            'peak_dBFS_min': f"{pmin:.1f}" if pmin is not None else '',
            'peak_dBFS_max': f"{pmax:.1f}" if pmax is not None else '',
            'peak_dBFS_mean': f"{pmean:.1f}" if pmean is not None else '',
            'ref_1k_dBFS': f"{ref_db_1k:.1f}" if ref_db_1k is not None else '',
            'log_file': os.path.basename(log_path),
        })
        print(f"  Index row   : appended to {idx_path}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="INMP441 mic characterization: tone-test FFT peak analyzer via TCP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)

    ap.add_argument('--sample-rate', type=int, required=True,
                    help='MIC_SAMPLE_RATE_HZ compiled into the test firmware (Hz). '
                         'Must match the mic_char_* env used to flash the device.')
    ap.add_argument('--fft-n', type=int, default=1024,
                    help='FFT_MIC_N compiled into firmware (default 1024). '
                         'Determines mic_bins = fft_n/2 and Hz/bin = sample_rate/fft_n.')
    ap.add_argument('--imu-fft-n', type=int, default=DEFAULT_IMU_FFT_N,
                    help=f'FFT_IMU_N in firmware (default {DEFAULT_IMU_FFT_N}). '
                         f'Must match to compute correct frame size (imu_bins = imu_fft_n/2).')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT,
                    help=f'TCP listen port (default {DEFAULT_PORT})')
    ap.add_argument('--psk-hex', default=None, metavar='HEX32',
                    help='AES-128-GCM key as 32 hex chars. '
                         'Omit for unencrypted builds (no -DEPM_ENCRYPT_FRAMES in build_flags).')
    ap.add_argument('--tone', type=int, default=None, metavar='HZ',
                    help='Expected tone frequency (Hz) currently playing near the mic. '
                         'Used for accuracy check (acc? column) and alias detection '
                         'when tone > Nyquist.')
    ap.add_argument('--spec-avg-n', type=int, default=4,
                    help='spec_avg_n to echo in v2 reply (default 4 = normal averaging).')
    ap.add_argument('--log-dir', default='mic_tools/char_logs',
                    help='Directory for per-session CSV logs + master _index.csv '
                         '(default: mic_tools/char_logs)')
    ap.add_argument('--tag', default=None,
                    help='Optional label appended to the log filename '
                         '(e.g. "phone-10cm") to help tell sessions apart.')
    ap.add_argument('--no-log', action='store_true',
                    help='Disable CSV logging (console output only).')

    args = ap.parse_args()

    if args.fft_n < 2 or (args.fft_n & (args.fft_n - 1)) != 0:
        ap.error('--fft-n must be a power of 2 (e.g. 512, 1024, 2048)')
    if args.imu_fft_n < 2 or (args.imu_fft_n & (args.imu_fft_n - 1)) != 0:
        ap.error('--imu-fft-n must be a power of 2')
    if args.tone is not None and args.tone <= 0:
        ap.error('--tone must be a positive frequency in Hz')

    run(args)


if __name__ == '__main__':
    main()
