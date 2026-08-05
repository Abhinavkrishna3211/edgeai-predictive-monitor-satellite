"""gateway/ingestion/tcp_legacy.py — TCP+AES satellite receiver, extracted from
recv_verify.py (Phase 8b3 task 1).

**Status: dev/test-only, not a live production ingestion path.** This module
decodes the old fixed-header TCP+AES wire format (`HEADER_FMT`/`EPM_MAGIC`)
that `tcp_task.c` used to send. That firmware transport was deleted outright
in Phase 7a in favor of MQTT (`gateway.ingestion.mqtt_subscriber`) — no real
satellite has spoken this format since. Its only remaining reason to exist is
that `mic_tools/satellite_sim.py` (the manual multi-satellite test double)
still speaks it, so this stays alive as *the thing satellite_sim.py connects
to* for interactive testing, not as a path any deployed satellite uses.
See docs/decisions/ADR-028-tcp-legacy-path-kept-for-dev-testing.md for the
keep-vs-retire decision and why. Do not treat this as parity with the MQTT
path — it is a relocated legacy receiver, named accordingly.

Every function here does a *lazy* `import recv_verify as _rv` inside its own
body (never at module level) to reach the shared, CLI-mutable gateway state
(`_decryptor`, `_ALERT_HISTORY`, `EPM_ALERT_OK`, `_process_satellite_frame`,
`_log_security_event`, `_BASE_DIR`, ...) — same reasoning as every other
gateway/* module extracted from recv_verify.py since Phase 8b1: a module-level
`import recv_verify` here would try to resolve `gateway.ingestion.tcp_legacy`
before recv_verify.py has finished its own top-level import of this package,
and a bare top-level reference would silently stop tracking recv_verify.
main()'s `global`-statement reassignments (now `gateway.main.main()`'s direct
attribute assignments — see gateway/main.py).

`_log_security_event` deliberately did NOT move here despite being grouped
with this path in the Phase 8b3 prompt: it is called both from here (GCM tag
failure) and from `_process_satellite_frame()` (replay detection) — the
latter is the shared, transport-agnostic pipeline step both the TCP and MQTT
paths call, and it references `_log_security_event` by a bare name. Moving
the definition here would make that shared pipeline step reach backward into
a path explicitly labeled legacy/dev-only for an audit-logging helper that
has nothing TCP-specific about it. It stays in recv_verify.py; satellite_
thread() below reaches it via the same lazy `_rv` import as everything else.

`EPM_MAGIC`/`HEADER_FMT`/`HELLO_FMT`/`EPM_PROTO_V2_MAGIC` and friends moved
here from recv_verify.py because they are pure TCP-wire-format constants with
zero other consumers inside gateway/ (confirmed via grep — the only other
files with copies are the fully-standalone `mic_tools/satellite_sim.py` and
`mic_tools/mic_char_analyze.py`, which define their own and import nothing
from here or recv_verify.py).
"""
import csv
import datetime
import os
import socket
import struct
import threading
import time

import numpy as np

# Optional: AES-128-GCM frame decryption (cryptography>=42.0.0)
_CRYPTO_AVAILABLE = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_AVAILABLE = True
except ImportError:
    AESGCM = None  # type: ignore[assignment,misc]

# ─── Protocol constants ───────────────────────────────────────────────────────

EPM_MAGIC   = 0xEA1DF00D
HELLO_MAGIC = 0xEA1D0000

HEADER_FMT  = '<IIIHHffffBfffBBB'   # 48 bytes — last B is overflow_count
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 48, f"Header size {HEADER_SIZE}"

HELLO_FMT   = '<I6sBB12s'
HELLO_SIZE  = struct.calcsize(HELLO_FMT)
assert HELLO_SIZE == 24, f"Hello size {HELLO_SIZE}"

EPM_PROTO_V2_MAGIC = 0xA2  # first byte of v2 reply — distinct from 0x00/0x01/0x02


# ─── AES-128-GCM frame decryption ─────────────────────────────────────────────

class FrameDecryptor:
    """Decrypts AES-128-GCM encrypted EPM frames produced by the satellite firmware.

    Wire format (after the uint32_t payload_bytes length prefix):
        iv[12]           AES-GCM nonce (TRNG-generated per frame on satellite)
        ciphertext[N]    encrypted epm_header_t + FFT arrays
        tag[16]          GCM authentication tag

    The `cryptography` library combines ciphertext and tag as `ciphertext||tag`
    for its decrypt() call — FrameDecryptor handles the split transparently.
    """
    def __init__(self, psk_bytes: bytes):
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography package not installed — run: pip install 'cryptography>=42.0.0'")
        if len(psk_bytes) != 16:
            raise ValueError(f"PSK must be exactly 16 bytes (AES-128), got {len(psk_bytes)}")
        self._aes = AESGCM(psk_bytes)

    def decrypt(self, blob: bytes) -> bytes:
        """Decrypt a payload blob = iv[12] + ciphertext[N] + tag[16].

        Returns the plaintext bytes on success.
        Raises an exception (InvalidTag) if authentication fails — caller logs SECURITY.
        """
        if len(blob) < 12 + 16:
            raise ValueError(f"encrypted blob too short ({len(blob)} bytes)")
        iv         = blob[:12]
        tag        = blob[-16:]
        ciphertext = blob[12:-16]
        return self._aes.decrypt(iv, ciphertext + tag, None)


# ─── TCP helpers ──────────────────────────────────────────────────────────────

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def parse_frame(raw, exp_mic_bins, exp_imu_bins):
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"frame too short ({len(raw)})")

    (magic, frame_id, ts_ms,
     mic_bins, imu_bins,
     mic_rms, mic_crest, mic_dc, mic_kurtosis, mic_clip,
     imu_rms, imu_crest, imu_dc, imu_clip,
     imu_axes, overflow_count) = struct.unpack_from(HEADER_FMT, raw, 0)

    errs = []
    if magic != EPM_MAGIC:
        errs.append(f"BAD MAGIC 0x{magic:08X}")
    if mic_bins != exp_mic_bins:
        errs.append(f"mic_bins={mic_bins} exp={exp_mic_bins}")
    if imu_bins != exp_imu_bins:
        errs.append(f"imu_bins={imu_bins} exp={exp_imu_bins}")
    if imu_axes != 3:
        errs.append(f"imu_axes={imu_axes} exp=3")
    if mic_clip:
        errs.append("MIC CLIP")
    if imu_clip:
        errs.append("IMU CLIP")

    exp_size = HEADER_SIZE + mic_bins * 4 + imu_bins * 4 * imu_axes
    if len(raw) != exp_size:
        # Raise immediately — np.frombuffer on a short buffer silently returns
        # fewer elements than requested, corrupting all FFT arrays downstream.
        raise ValueError(
            f"frame payload {len(raw)} B != expected {exp_size} B "
            f"(mic_bins={mic_bins} imu_bins={imu_bins} imu_axes={imu_axes}); "
            f"header errors: {errs or 'none'}"
        )

    off     = HEADER_SIZE
    mic_fft = np.frombuffer(raw, dtype='<f4', count=mic_bins, offset=off).copy()
    off    += mic_bins * 4
    imu_x   = np.frombuffer(raw, dtype='<f4', count=imu_bins, offset=off).copy()
    off    += imu_bins * 4
    imu_y   = np.frombuffer(raw, dtype='<f4', count=imu_bins, offset=off).copy()
    off    += imu_bins * 4
    imu_z   = np.frombuffer(raw, dtype='<f4', count=imu_bins, offset=off).copy()

    return dict(frame_id=frame_id, ts_ms=ts_ms,
                mic_bins=mic_bins, imu_bins=imu_bins, imu_axes=imu_axes,
                mic_rms=mic_rms, mic_crest=mic_crest, mic_kurtosis=mic_kurtosis,
                imu_rms=imu_rms, imu_crest=imu_crest,
                mic_fft=mic_fft, imu_x=imu_x, imu_y=imu_y, imu_z=imu_z,
                overflow_count=overflow_count,
                errors=errs)


# ─── Per-satellite connection thread ─────────────────────────────────────────

def satellite_thread(conn, addr, exp_mic_bins, exp_imu_bins):
    import recv_verify as _rv
    from gateway.pipeline.adaptive_control import _adaptive_overlap, _adaptive_avg_n

    mac_hex = None
    sat     = None
    csv_f   = None
    csv_w   = None
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(15.0)   # unblock recv_exact() if satellite goes silent

        # ── Parse hello packet ────────────────────────────────────────────────
        hello_raw = recv_exact(conn, HELLO_SIZE)
        magic, mac_bytes, fw_major, fw_minor, name_bytes = \
            struct.unpack(HELLO_FMT, hello_raw)

        if magic != HELLO_MAGIC:
            print(f"[{addr[0]}] Bad hello magic 0x{magic:08X} — dropping")
            return

        mac_hex = ':'.join(f'{b:02X}' for b in mac_bytes)
        name    = name_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
        sat     = _rv._sat_register(mac_hex, name, fw_major, fw_minor, addr)

        print(f"\n[+] Satellite connected: {name}  MAC={mac_hex}  "
              f"fw={fw_major}.{fw_minor}  from {addr[0]}:{addr[1]}")
        print(f"    Satellites active: {_rv._sat_count()}")
        _rv._print_sat_table()

        # ── CSV log: logs/csv/YYYY/MM/epm_{name}_{date}.csv, append on reconnect ──
        # Dated subdirectory layout allows background rotation to gzip by age.
        # Uses recv_verify.py's _BASE_DIR (mic_tools/, or wherever it physically
        # lives) rather than this file's own __file__ — this module now lives in
        # gateway/ingestion/, whose directory is not where logs/ belongs.
        log_dir  = os.path.join(_rv._BASE_DIR, 'logs')
        now_dt   = datetime.datetime.now()
        csv_dir  = os.path.join(log_dir, 'csv', now_dt.strftime('%Y'), now_dt.strftime('%m'))
        os.makedirs(csv_dir, exist_ok=True)
        date_str = now_dt.strftime('%Y%m%d')
        csv_path = os.path.join(csv_dir, f"epm_{name}_{date_str}.csv")
        is_new   = not os.path.exists(csv_path)
        csv_f    = open(csv_path, 'a', newline='')
        csv_w    = csv.writer(csv_f)
        if is_new:
            csv_w.writerow(['wall_time', 'frame_id', 'device_ms',
                            'mic_rms', 'mic_crest', 'mic_kurtosis',
                            'imu_rms', 'imu_crest',
                            'high_band_ratio', 'z_score', 'p_fault', 'alert',
                            'overflow_count'])
        print(f"    Logging to: {csv_path}  ({'new' if is_new else 'append'})")

        # Maximum valid payload: header + mic FFT + 3 × IMU FFT + 1 KB margin.
        # In encrypted mode, add 12 (IV) + 16 (tag) overhead.
        # Guards against a malicious/buggy satellite sending a huge length prefix
        # that would cause recv_exact to try to allocate gigabytes.
        _plaintext_len = HEADER_SIZE + exp_mic_bins * 4 + exp_imu_bins * 4 * 3
        max_payload = (_plaintext_len + 12 + 16 + 1024)

        # Per-connection streak counters — kept as local variables so mutations
        # never race with the dashboard HTTP reader (all sat writes go through lock).
        warn_streak    = 0
        ok_streak      = 0
        sent_alert     = _rv.EPM_ALERT_OK
        last_frame_id  = -1   # replay protection: must be monotonically increasing

        while True:
            (payload_bytes,) = struct.unpack('<I', recv_exact(conn, 4))
            # Accept either plaintext or encrypted payload sizes
            min_size = HEADER_SIZE if _rv._decryptor is None else (12 + HEADER_SIZE + 16)
            if payload_bytes < min_size or payload_bytes > max_payload:
                raise ValueError(
                    f"payload_bytes={payload_bytes} out of valid range "
                    f"[{min_size}..{max_payload}]")
            raw = recv_exact(conn, payload_bytes)

            # ── AES-128-GCM decryption (if gateway started with --psk-hex) ──────
            if _rv._decryptor is not None:
                try:
                    raw = _rv._decryptor.decrypt(raw)
                except Exception as exc:
                    _rv._log_security_event(
                        sat.name, mac_hex,
                        f"GCM tag verification failed on frame — possible key mismatch "
                        f"or injected data ({exc})")
                    continue   # keep connection; satellite can retry after reboot

            frame = parse_frame(raw, exp_mic_bins, exp_imu_bins)

            _result = _rv._process_satellite_frame(
                sat, frame, mac_hex, csv_w, csv_f,
                warn_streak, ok_streak, sent_alert, last_frame_id)
            if _result is None:
                continue   # replay — already logged inside _process_satellite_frame
            (alert, p_fault, now,
             warn_streak, ok_streak, sent_alert, last_frame_id) = _result

            # ── EPM v2 adaptive reply ──────────────────────────────────────────
            # Build and send the 8-byte v2 struct.  The AI posterior reshapes
            # the satellite's FFT pipeline: higher P(fault) → more overlap and
            # less averaging → faster temporal response to transient fault events.
            _ov  = _adaptive_overlap(p_fault)
            _avg = _adaptive_avg_n(p_fault)
            _v2  = struct.pack('<BBHBBBB',
                               EPM_PROTO_V2_MAGIC,        # proto_ver
                               alert,                     # alert_state
                               min(int(p_fault * 10000), 10000),  # fault_posterior
                               _ov,                       # fft_overlap_pct
                               _avg,                      # spec_avg_n
                               0, 0)                      # reserved
            try:
                conn.sendall(_v2)
            except OSError:
                break

            # Log ADAPT event when commanded parameters change
            if _ov != sat.adapt_overlap or _avg != sat.adapt_avg_n:
                _adapt_event = {
                    'time':       now,
                    'satellite':  sat.name,
                    'mac':        mac_hex,
                    'event_type': 'ADAPT',
                    'alert':      'INFO',
                    'prev':       'INFO',
                    'kurtosis':   0.0,
                    'crest':      0.0,
                    'z_score':    0.0,
                    'detail':     (f'Sensor adapt: overlap {sat.adapt_overlap}%→{_ov}%  '
                                   f'avg_n {sat.adapt_avg_n}→{_avg}  '
                                   f'p_fault={p_fault:.3f}'),
                }
                with _rv._ALERT_HISTORY_LOCK:
                    _rv._ALERT_HISTORY.appendleft(_adapt_event)
                sat.adapt_overlap = _ov
                sat.adapt_avg_n   = _avg

    except (ConnectionError, struct.error, OSError) as e:
        print(f"\n[-] {(sat.name if sat else mac_hex) or addr[0]} disconnected: {e}")
    except Exception as e:
        print(f"\n[-] {(sat.name if sat else mac_hex) or addr[0]} error: {e}")
    finally:
        if csv_f:
            try:
                csv_f.close()
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass
        if mac_hex:
            _rv._sat_disconnect(mac_hex)
        print(f"    Satellites remaining: {_rv._sat_count()}")
        _rv._print_sat_table()


# ─── Accept loop ─────────────────────────────────────────────────────────────

def accept_loop(host, port, fft_mic_n, fft_imu_n):
    exp_mic = fft_mic_n // 2
    exp_imu = fft_imu_n // 2

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    print(f"[server] Listening on {host}:{port}  "
          f"(mic={fft_mic_n}-pt  imu={fft_imu_n}-pt × 3 axes)  up to 16 satellites")

    while True:
        conn, addr = srv.accept()
        threading.Thread(
            target=satellite_thread,
            args=(conn, addr, exp_mic, exp_imu),
            daemon=True,
            name=f"sat-{addr[0]}",
        ).start()
