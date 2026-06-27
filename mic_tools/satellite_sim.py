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

HELLO_FMT  = '<I6sBB12s'       # 24 bytes
HEADER_FMT = '<IIIHHffffBfffBBx'  # 48 bytes

EPM_HELLO_MAGIC = 0xEA1D0000
EPM_FRAME_MAGIC = 0xEA1DF00D

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


def _gen_imu_fft(mode):
    """1024-bin IMU FFT in dBFS with a 50 Hz shaft tone."""
    fft = [-97.0 + random.gauss(0, 2.0) for _ in range(IMU_BINS)]
    fft[0] = -120.0
    fft[4] = -58.0 + random.gauss(0, 1.0)   # 50 Hz shaft (bin 4 at 12.5 Hz/bin)
    if mode == 'fault':
        fft[8]  = -62.0  # 100 Hz 2× harmonic
        fft[12] = -65.0  # 150 Hz 3× harmonic
    return fft


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
    imu_bytes = struct.pack(f'<{IMU_BINS}f', *_gen_imu_fft(mode))

    payload = hdr + mic_bytes + imu_bytes * 3  # 3 identical axes (X, Y, Z)
    return struct.pack('<I', len(payload)) + payload


# ─── Per-satellite thread ────────────────────────────────────────────────────

def _run_satellite(sat_id, host, port, mode):
    """Connect, send hello + frames, read alert bytes. Reconnects automatically."""
    # Build a deterministic but unique fake MAC from sat_id
    mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0x00, sat_id & 0xFF])
    name_str   = f'SIM-{sat_id:02d}'
    name_bytes = name_str.encode('ascii').ljust(12, b'\x00')
    hello = struct.pack(HELLO_FMT, EPM_HELLO_MAGIC, mac, 1, 0, name_bytes)

    tag      = f'[{name_str}]'
    frame_id = 0

    mode_label = f' ({mode.upper()})' if mode else ''
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

            while True:
                pkt = _make_frame(frame_id, mode)
                sock.sendall(pkt)

                try:
                    raw = sock.recv(1)
                    if not raw:
                        print(f'{tag} Gateway closed connection')
                        break
                    alert_name = ALERT_MAP.get(raw[0], f'0x{raw[0]:02x}')
                    print(f'{tag} frame={frame_id:5d}  alert={alert_name}')
                except socket.timeout:
                    print(f'{tag} frame={frame_id:5d}  (no alert byte — gateway timeout)')

                frame_id += 1
                time.sleep(0.45)  # ~2.2 fps — matches real satellite rate

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

        time.sleep(2.0)  # wait before reconnect


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
    args = ap.parse_args()

    # Build per-satellite mode map
    modes = {}
    for sid in args.fault:
        modes[sid] = 'fault'
    for sid in args.warn:
        modes[sid] = 'warn'

    print(f'Starting {args.n} simulated satellite(s) → {args.host}:{args.port}')
    if modes:
        print(f'Fault injection: {modes}')

    threads = []
    for sid in range(1, args.n + 1):
        t = threading.Thread(
            target=_run_satellite,
            args=(sid, args.host, args.port, modes.get(sid)),
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
