#!/usr/bin/env python3
"""
satellite_sim.py — Simulates N EPM satellites for multi-satellite gateway testing.

Lets you test recv_verify.py with multiple satellites without buying extra hardware.
Each instance has a unique fake MAC, appears as a distinct satellite in the gateway,
and sends realistic frame data at ~2.2 fps.

Usage:
  python satellite_sim.py [host] [port] [n_satellites]
  python satellite_sim.py 127.0.0.1 5100 3

Fault injection (test alert logic and LED patterns):
  python satellite_sim.py --fault 2 --warn 3        # sat 2=FAULT, sat 3=WARN, rest=OK
  python satellite_sim.py 192.168.137.1 5100 5 --fault 1 --warn 2

Satellites automatically reconnect on gateway disconnect.
"""

import argparse
import math
import random
import socket
import struct
import sys
import threading
import time

# ─── Protocol constants (must match epm_protocol.h) ────────────────────────

HELLO_FMT  = '<I6sBB12s'          # 24 bytes
HEADER_FMT = '<IIIHHffffBfffBBx'  # 48 bytes
V2_FMT     = '<BBHBBBB'           # 8 bytes — epm_alert_v2_t

EPM_HELLO_MAGIC    = 0xEA1D0000
EPM_FRAME_MAGIC    = 0xEA1DF00D
EPM_PROTO_V2_MAGIC = 0xA2         # first byte of v2 reply

MIC_FS_HZ  = 16000
MIC_BINS   = 512   # FFT_MIC_N / 2
IMU_BINS   = 1024  # FFT_IMU_N / 2

ALERT_MAP = {0: 'OK', 1: 'WARN', 2: 'FAULT'}

# ─── Synthetic signal generators ────────────────────────────────────────────

def _gen_mic_fft(mode):
    """512-bin mic FFT in dBFS. Bearing noise injected in 2-4 kHz for fault/warn."""
    fft = []
    for k in range(MIC_BINS):
        # Pink noise baseline (−10 dB/decade roll-off)
        base = -72.0 - 8.0 * math.log10(max(k, 1) + 1)
        if mode == 'fault' and 128 <= k <= 256:   # 2-4 kHz bearing resonance
            base += random.gauss(18.0, 3.0)
        elif mode == 'warn' and 96 <= k <= 160:
            base += random.gauss(8.0, 2.0)
        fft.append(base + random.gauss(0, 1.5))
    fft[0] = -120.0  # DC bin zeroed
    return fft


def _gen_imu_fft(mode, axis='x'):
    """
    1024-bin IMU FFT in dBFS with axis-realistic signals.
    Matches the stub signals in imu_task.c:
      X radial A: 50 Hz imbalance tone
      Y radial B: 50 Hz + 150 Hz (3× harmonic)
      Z axial:    100 Hz (2× shaft — mild misalignment)
    12.5 Hz / bin at IMU_FS_HZ=25600, FFT_IMU_N=2048
    """
    BIN_HZ = 25600 / (IMU_BINS * 2)   # 12.5 Hz/bin
    fft = [-97.0 + random.gauss(0, 2.0) for _ in range(IMU_BINS)]
    fft[0] = -120.0   # DC bin zeroed

    b50  = round(50  / BIN_HZ)   # bin 4
    b100 = round(100 / BIN_HZ)   # bin 8
    b150 = round(150 / BIN_HZ)   # bin 12

    # Axis-specific base tones (match imu_task.c stub)
    if axis == 'x':
        fft[b50]  = -58.0 + random.gauss(0, 1.0)   # 50 Hz radial A
    elif axis == 'y':
        fft[b50]  = -59.0 + random.gauss(0, 1.0)   # 50 Hz radial B
        fft[b150] = -65.0 + random.gauss(0, 1.2)   # 150 Hz 3×
    elif axis == 'z':
        fft[b100] = -62.0 + random.gauss(0, 1.0)   # 100 Hz axial 2× shaft

    # Fault/warn excitations: elevated energy at bearing resonance frequencies
    if mode == 'fault':
        # Wideband impact at BPFI region (~280-400 Hz, bins 22-32)
        for b in range(22, 33):
            fft[b] = max(fft[b], -60.0 + random.gauss(0, 3.0))
        fft[b100] = max(fft[b100], -55.0)   # 100 Hz prominent on all axes
    elif mode == 'warn':
        for b in range(22, 30):
            fft[b] = max(fft[b], -72.0 + random.gauss(0, 2.0))

    return fft


def _make_frame_custom(frame_id, kurtosis, crest, mode):
    """Build a frame with explicit kurtosis and crest values (for ramp testing)."""
    ts_ms   = int(time.time() * 1000) & 0xFFFFFFFF
    mic_rms = 0.002 + kurtosis / 10000.0
    imu_rms = 0.005 + kurtosis / 5000.0
    hdr = struct.pack(HEADER_FMT,
                      EPM_FRAME_MAGIC, frame_id, ts_ms,
                      MIC_BINS, IMU_BINS,
                      mic_rms, crest, 0.0, kurtosis, 0,
                      imu_rms, crest * 0.9, 0.0, 0, 3)
    mic_bytes = struct.pack(f'<{MIC_BINS}f', *_gen_mic_fft(mode))
    imu_x     = struct.pack(f'<{IMU_BINS}f', *_gen_imu_fft(mode, 'x'))
    imu_y     = struct.pack(f'<{IMU_BINS}f', *_gen_imu_fft(mode, 'y'))
    imu_z     = struct.pack(f'<{IMU_BINS}f', *_gen_imu_fft(mode, 'z'))
    payload = hdr + mic_bytes + imu_x + imu_y + imu_z
    return struct.pack('<I', len(payload)) + payload


def _make_frame(frame_id, mode):
    """Build a complete length-prefixed frame packet for the given fault mode."""
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFF

    if mode == 'fault':
        mic_rms      = 0.018 + abs(random.gauss(0, 0.003))
        mic_crest    = 11.0  + random.gauss(0, 1.2)
        mic_kurtosis = 14.5  + random.gauss(0, 1.5)
        imu_rms      = 0.06
        imu_crest    = 9.0
    elif mode == 'warn':
        mic_rms      = 0.007 + abs(random.gauss(0, 0.001))
        mic_crest    = 6.5   + random.gauss(0, 0.6)
        mic_kurtosis = 7.2   + random.gauss(0, 0.6)
        imu_rms      = 0.018
        imu_crest    = 5.8
    else:  # OK / healthy
        mic_rms      = 0.002 + abs(random.gauss(0, 0.0003))
        mic_crest    = 3.0   + random.gauss(0, 0.35)
        mic_kurtosis = 2.85  + random.gauss(0, 0.25)
        imu_rms      = 0.005
        imu_crest    = 3.6

    hdr = struct.pack(HEADER_FMT,
                      EPM_FRAME_MAGIC, frame_id, ts_ms,
                      MIC_BINS, IMU_BINS,
                      mic_rms, mic_crest, 0.0, mic_kurtosis, 0,
                      imu_rms, imu_crest, 0.0, 0, 3)

    mic_bytes = struct.pack(f'<{MIC_BINS}f', *_gen_mic_fft(mode))
    imu_x     = struct.pack(f'<{IMU_BINS}f', *_gen_imu_fft(mode, 'x'))
    imu_y     = struct.pack(f'<{IMU_BINS}f', *_gen_imu_fft(mode, 'y'))
    imu_z     = struct.pack(f'<{IMU_BINS}f', *_gen_imu_fft(mode, 'z'))

    payload = hdr + mic_bytes + imu_x + imu_y + imu_z
    return struct.pack('<I', len(payload)) + payload


# ─── Per-satellite thread ────────────────────────────────────────────────────

def _recv_gateway_reply(sock, tag, frame_id):
    """Read and decode the gateway's v1 or v2 reply.  Returns (alert_name, extras_str)."""
    try:
        first = sock.recv(1)
        if not first:
            return None, None  # connection closed
        b0 = first[0]

        if b0 == EPM_PROTO_V2_MAGIC:
            # v2 reply — read remaining 7 bytes
            rest = b''
            while len(rest) < 7:
                chunk = sock.recv(7 - len(rest))
                if not chunk:
                    return None, None
                rest += chunk
            _, alert, posterior, overlap, avg_n, _, _ = struct.unpack(V2_FMT, first + rest)
            alert_name = ALERT_MAP.get(alert, f'0x{alert:02x}')
            p_pct = posterior / 100.0
            extras = f'p_fault={p_pct:.1f}%  OV={overlap}%  AVG={avg_n}'
            return alert_name, extras
        else:
            # v1 reply — single byte
            alert_name = ALERT_MAP.get(b0, f'0x{b0:02x}')
            return alert_name, '(v1)'
    except socket.timeout:
        return '(timeout)', ''


def _run_satellite(sat_id, host, port, mode, ramp=False):
    """Connect, send hello + frames, read v1/v2 reply. Reconnects automatically.

    When ramp=True the satellite slowly increases kurtosis from healthy to fault
    levels over 120 frames to exercise the adaptive-sensing closed loop.
    """
    mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0x00, sat_id & 0xFF])
    name_str   = f'SIM-{sat_id:02d}'
    name_bytes = name_str.encode('ascii').ljust(12, b'\x00')
    hello = struct.pack(HELLO_FMT, EPM_HELLO_MAGIC, mac, 1, 0, name_bytes)

    tag      = f'[{name_str}]'
    frame_id = 0

    mode_label = ' (RAMP)' if ramp else (f' ({mode.upper()})' if mode else '')
    print(f'{tag} Starting{mode_label}  target={host}:{port}')

    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8.0)
            sock.connect((host, port))
            sock.settimeout(2.5)
            sock.sendall(hello)
            print(f'{tag} Connected')

            ramp_frame = 0
            while True:
                # In ramp mode, linearly interpolate fault level over 120 frames
                # then hold at fault for another 60 before recovering
                if ramp:
                    if ramp_frame < 120:
                        t = ramp_frame / 120.0
                        # Interpolate kurtosis from healthy (3) to fault (15)
                        kurtosis = 3.0 + t * 12.0
                        crest    = 3.0 + t * 8.0
                        cur_mode = 'ok' if t < 0.3 else ('warn' if t < 0.6 else 'fault')
                    elif ramp_frame < 180:
                        kurtosis, crest, cur_mode = 15.0, 11.0, 'fault'
                    else:
                        ramp_frame = 0   # loop the ramp
                        kurtosis, crest, cur_mode = 3.0, 3.0, 'ok'
                    ramp_frame += 1
                    pkt = _make_frame_custom(frame_id, kurtosis, crest, cur_mode)
                else:
                    pkt = _make_frame(frame_id, mode)

                sock.sendall(pkt)

                alert_name, extras = _recv_gateway_reply(sock, tag, frame_id)
                if alert_name is None:
                    print(f'{tag} Gateway closed connection')
                    break
                print(f'{tag} frame={frame_id:5d}  alert={alert_name}  {extras}')

                frame_id += 1
                time.sleep(0.45)

        except ConnectionRefusedError:
            print(f'{tag} Connection refused — is recv_verify.py running?')
        except Exception as exc:
            print(f'{tag} Error: {exc}')
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

        time.sleep(2.0)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='EPM satellite simulator for multi-satellite gateway testing',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('host', nargs='?', default='127.0.0.1',
                    help='Gateway IP (default: 127.0.0.1)')
    ap.add_argument('port', nargs='?', type=int, default=5100,
                    help='Gateway port (default: 5100)')
    ap.add_argument('n', nargs='?', type=int, default=3,
                    help='Number of simulated satellites (default: 3)')
    ap.add_argument('--fault', type=int, action='append', default=[], metavar='ID',
                    help='Force satellite ID to send FAULT-level data (K≈15, CF≈11)')
    ap.add_argument('--warn',  type=int, action='append', default=[], metavar='ID',
                    help='Force satellite ID to send WARN-level data  (K≈7,  CF≈6.5)')
    ap.add_argument('--ramp',  type=int, action='append', default=[], metavar='ID',
                    help='Ramp satellite ID from healthy→fault over 120 frames to test '
                         'adaptive-sensing closed loop (verify overlap rises 0→50→75%%)')
    args = ap.parse_args()

    # Build per-satellite mode map
    modes = {}
    ramps = set(args.ramp)
    for sid in args.fault:
        modes[sid] = 'fault'
    for sid in args.warn:
        modes[sid] = 'warn'

    print(f'Starting {args.n} simulated satellite(s) → {args.host}:{args.port}')
    if modes:
        print(f'Fault injection: {modes}')
    if ramps:
        print(f'Ramp satellites (adaptive-sensing test): {sorted(ramps)}')

    threads = []
    for sid in range(1, args.n + 1):
        t = threading.Thread(
            target=_run_satellite,
            args=(sid, args.host, args.port, modes.get(sid), sid in ramps),
            daemon=True,
            name=f'sat-sim-{sid}')
        t.start()
        threads.append(t)
        time.sleep(0.35)  # stagger connects so gateway sees them arrive separately

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print('\nStopped.')
        sys.exit(0)


if __name__ == '__main__':
    main()
