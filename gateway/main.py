#!/usr/bin/env python3
"""gateway/main.py — EPM gateway entry point (Phase 8b3 task 2).

Argument parsing + wiring only. All per-frame pipeline logic and shared
CLI-mutable state stay in recv_verify.py (see
docs/decisions/ADR-029-recv-verify-fate-and-main-py-split.md for why this
file exists as a thin wiring layer rather than absorbing recv_verify.py
wholesale, and why recv_verify.py wasn't simply retired). The live
matplotlib plot itself moved to gateway/api/live_plot.py (Phase 8c task 1);
this file just calls into it.

This module mutates recv_verify's module-level globals via plain attribute
assignment (`rv.NAME = value`) everywhere the old recv_verify.main() used a
`global NAME` statement — assigning `some_module.NAME` from outside is
exactly what a `global NAME; NAME = value` executed *inside* that module
would do, since both write the same entry in the module's `__dict__`. Every
other gateway/* module already depends on this (each does its own lazy
`import recv_verify as _rv` and reads `_rv.NAME`); this file is simply the
one place that also *writes* those names, replacing recv_verify.main()'s
old `global` statements one-for-one.

`import recv_verify as rv` is safe at *module level* here (unlike every
other gateway/* module, which must lazy-import it from inside function
bodies) because recv_verify.py does not import gateway.main anywhere in its
own top-level code — there is no cycle for this file to fall into. recv_verify.py's
own main() reaches this module lazily (`from gateway.main import main`) purely
so `python recv_verify.py` keeps working as a direct invocation; it is not
protecting against a circular import.
"""
import argparse
import logging
import os
import socket
import sys
import threading
import time

import numpy as np

# Repo root + mic_tools/ on sys.path so `import recv_verify` and
# `from gateway.X import Y` both resolve regardless of which directory this
# file is invoked from (matches the convention already used by
# tests/{registry,pipeline}/test_*.py — see e.g. tests/registry/test_satellite_state.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'mic_tools'))

import recv_verify as rv
from gateway.ingestion import tcp_legacy
from gateway.ingestion import mqtt_subscriber
from gateway.api.dashboard import start_dashboard
from gateway.api.live_plot import run_plot

log = logging.getLogger("gateway.main")


def main():
    # Without this, the `logging` module's default root level (WARNING) silently
    # drops every log.info()/log.debug() call -- mqtt_subscriber.py's connect/
    # subscribe messages included -- since nothing else in the gateway configures
    # a handler or level.
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    parser = argparse.ArgumentParser(
        description=rv.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port',      type=int,   default=5100)
    parser.add_argument('--listen-ip', type=str,   default='0.0.0.0')
    parser.add_argument('--fft-mic-n', type=int,   default=1024)
    parser.add_argument('--fft-imu-n', type=int,   default=2048)
    parser.add_argument('--shaft-hz',   type=float, default=None,
                        help='Shaft frequency Hz — marks harmonics on all FFT panels')
    parser.add_argument('--shaft-rpm',  type=float, default=None,
                        help='Shaft speed RPM — alternative to --shaft-hz')
    parser.add_argument('--bearing',    type=str,   default=None,
                        help='Bearing type for fault freq markers: e.g. 6205 or n,D,d[,alpha]. '
                             'Requires --shaft-hz or --shaft-rpm. '
                             'Run: python bearing_math.py --list')
    parser.add_argument('--model',      type=str,   default=None,
                        help='ML model prefix from ml_trainer.py (e.g. model/epm_model). '
                             'Enables ML-based alerting alongside threshold detection.')
    parser.add_argument('--crest-warn',  type=float, default=None,
                        help=f'Crest factor WARN threshold (default {rv.CREST_WARN})')
    parser.add_argument('--crest-fault', type=float, default=None,
                        help=f'Crest factor FAULT threshold (default {rv.CREST_FAULT})')
    parser.add_argument('--dashboard-port', type=int, default=8080,
                        help='HTTP port for the web dashboard (default 8080)')
    parser.add_argument('--no-plot', action='store_true',
                        help='Skip the live matplotlib plot — for SSH / headless / '
                             'Uno Q / server environments with no display')
    parser.add_argument('--auth', type=str, default=None, metavar='USER:PASS',
                        help='Protect dashboard with HTTP Basic Auth (e.g. admin:secret). '
                             'Required for production deployments.')
    parser.add_argument('--notify-webhook', type=str, default=None, metavar='URL',
                        help='Webhook URL for FAULT alerts — supports Discord, Slack, Teams, '
                             'or any generic JSON endpoint.')
    parser.add_argument('--notify-email', type=str, default=None,
                        metavar='FROM:TO:HOST[:PORT[:USER:PASS]]',
                        help='SMTP config for email FAULT alerts (colon-separated). '
                             'Example: alerts@co.com:ops@co.com:smtp.co.com:587:user:pass')
    parser.add_argument('--factory-name', type=str, default=None,
                        help='Site/factory name shown in the dashboard header '
                             '(default: "EPM Industrial Monitor")')
    parser.add_argument('--fault-prior', type=float, default=0.01,
                        help='Bayesian prior P(fault per frame) for multi-channel '
                             'fusion (default 0.01). Raise to 0.05 for noisier sites.')
    parser.add_argument('--evidence-midpoint', type=float, default=rv._EVIDENCE_Z_MID,
                        help='Z-score at which a channel contributes 50/50 fault '
                             'evidence (default %.1f — Phase 3 sweep optimum).' % rv._EVIDENCE_Z_MID)
    parser.add_argument('--threshold-sigma', type=float, default=4.0,
                        help='Adaptive z-sigma threshold for WARN from per-machine baseline '
                             '(default 4.0; FAULT is set to this value + 2.0). '
                             'Lower values increase sensitivity on quiet machines.')
    parser.add_argument('--psk-hex', type=str, default=None,
                        metavar='HEX32',
                        help='32-character hex AES-128 key for frame decryption '
                             '(e.g. deadbeefdeadbeefdeadbeefdeadbeef). '
                             'Must match EPM_PSK in wifi_creds.h or the NVS key on satellites. '
                             'Also readable from the EPM_PSK env-var. '
                             'Omit to run in plaintext mode (dev/debug only).')
    parser.add_argument('--autoencoder', type=str, default=None,
                        metavar='PATH',
                        help='Path to ONNX autoencoder model (e.g. model/autoencoder.onnx). '
                             'Adds reconstruction-error as a 4th Bayesian fusion channel. '
                             'Stats sidecar <model>_stats.npz must exist alongside the model. '
                             'Omit to run without neural autoencoder channel.')
    parser.add_argument('--mqtt-host', type=str, default=None,
                        help='Broker host for the MQTT section-list ingestion path '
                             '(Phase 8a) -- e.g. 192.168.1.8. Runs alongside the existing '
                             'TCP+AES receiver (does not replace it); omit to disable. '
                             'See gateway/ingestion/mqtt_subscriber.py and ADR-027 for the '
                             'satellite-identity and imu_rms/imu_crest derivation this path uses.')
    parser.add_argument('--mqtt-port', type=int, default=1883,
                        help='Broker port for --mqtt-host (default 1883)')
    args = parser.parse_args()

    # Port-in-use guard — prevents silent conflicts with orphaned gateway instances.
    # SO_REUSEADDR would let a new instance bind but the old process keeps accepting
    # connections first, causing partial/lost frames. Fail fast instead.
    _guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _guard.bind((args.listen_ip, args.port))
        _guard.close()
    except OSError as _e:
        print(f'[ERROR] Port {args.port} is already in use: {_e}')
        print(f'        Find & kill the old process:')
        print(f'          netstat -ano | findstr :{args.port}')
        print(f'          Stop-Process -Id <PID> -Force')
        sys.exit(1)

    if args.crest_warn is not None:
        rv.CREST_WARN = args.crest_warn
    if args.crest_fault is not None:
        rv.CREST_FAULT = args.crest_fault
    rv._FAULT_PRIOR    = args.fault_prior
    rv._EVIDENCE_Z_MID = args.evidence_midpoint
    rv.Z_WARN_SIGMA    = args.threshold_sigma
    rv.Z_FAULT_SIGMA   = args.threshold_sigma + 2.0
    if rv._FUSION_AVAILABLE:
        rv._bayesian_fusion = rv.BayesianFusion(
            prior=rv._FAULT_PRIOR, z_mid=rv._EVIDENCE_Z_MID, temperature=1.0)

    # ── Autoencoder ONNX model ─────────────────────────────────────────────────
    if args.autoencoder is not None:
        if not rv._AE_AVAILABLE:
            sys.exit('[EPM] --autoencoder requires onnxruntime. '
                     'Run: pip install onnxruntime>=1.17.0')
        stats_path = args.autoencoder.replace('.onnx', '_stats.npz')
        if not os.path.exists(stats_path):
            sys.exit(f'[EPM] Stats sidecar not found: {stats_path}\n'
                     '      Run train_autoencoder.py to generate it alongside the model.')
        rv._ae_engine = rv.InferenceEngine(args.autoencoder)
        rv._ae_stats  = np.load(stats_path)
        print(f'[EPM] Autoencoder: {args.autoencoder}')
        print(f'[EPM]   healthy mean_recon_err={float(rv._ae_stats["mean_recon_err"]):.6f}'
              f'  backend={rv._ae_engine.backend_label}')

    # ── Auth ──────────────────────────────────────────────────────────────────
    if args.auth:
        if ':' not in args.auth:
            sys.exit('--auth must be USER:PASS (e.g. admin:secret)')
        rv._AUTH_USER, rv._AUTH_PASS = args.auth.split(':', 1)

    # ── Notifications ─────────────────────────────────────────────────────────
    if args.notify_webhook:
        rv._NOTIFY_WEBHOOK = args.notify_webhook

    if args.notify_email:
        parts = args.notify_email.split(':')
        if len(parts) < 3:
            sys.exit('--notify-email must be FROM:TO:HOST[:PORT[:USER:PASS]]')
        rv._NOTIFY_EMAIL_CFG = {
            'from': parts[0],
            'to':   parts[1],
            'host': parts[2],
            'port': int(parts[3]) if len(parts) > 3 else 587,
            'user': parts[4] if len(parts) > 4 else None,
            'pass': parts[5] if len(parts) > 5 else None,
        }

    # ── Factory name ──────────────────────────────────────────────────────────
    if args.factory_name:
        rv._FACTORY_NAME = args.factory_name

    for n, name in ((args.fft_mic_n, 'fft-mic-n'), (args.fft_imu_n, 'fft-imu-n')):
        if n <= 0 or (n & (n - 1)):
            sys.exit(f"--{name} must be a power of 2 (got {n})")

    # Resolve shaft_hz from either --shaft-hz or --shaft-rpm
    shaft_hz = args.shaft_hz
    if args.shaft_rpm is not None and shaft_hz is None:
        shaft_hz = args.shaft_rpm / 60.0

    # Parse bearing geometry and compute fault frequencies for FFT annotation
    bearing_freqs_mic = None
    bearing_freqs_imu = None
    geom = None
    bf   = None   # kept (not just its .markers() dicts) so run_plot() can call
                  # bf.markers(envelope_fs) once the envelope panels' own
                  # decoded fs is known, rather than only at a hardcoded fs
    if args.bearing:
        if not rv._BEARING_AVAILABLE:
            print('WARNING: bearing_math.py not found in the same directory — ignoring --bearing')
        elif shaft_hz is None:
            print('WARNING: --bearing requires --shaft-hz or --shaft-rpm — ignoring')
        else:
            geom = rv.parse_bearing_arg(args.bearing)
            if geom is None:
                print(f'WARNING: unknown bearing "{args.bearing}" — ignoring. '
                      f'Run: python bearing_math.py --list')
            else:
                bf = rv.BearingFreqs.from_shaft_hz(shaft_hz, geom)
                bf.print_table()
                bearing_freqs_mic = bf.markers(rv.MIC_FS_HZ)
                bearing_freqs_imu = bf.markers(rv.IMU_FS_HZ)

    # Load ML model if requested
    if args.model:
        rv._load_ml_model(args.model)

    # ── SQLite storage ────────────────────────────────────────────────────────
    log_dir = os.path.join(rv._BASE_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    if rv._STORAGE_AVAILABLE:
        try:
            rv._storage = rv.Storage(os.path.join(log_dir, 'epm.db'))
            print(f'[storage] SQLite DB: {os.path.join(log_dir, "epm.db")}  (WAL mode)')
        except Exception as e:
            print(f'[storage] WARNING: could not open DB: {e} — using JSON fallback')

    # ── Load maintenance log (from SQLite or legacy JSON) ─────────────────────
    rv._load_maint_log(os.path.join(log_dir, 'maintenance_log.json'))

    # ── CSV rotation background thread — gzip files older than 90 days ────────
    if rv._STORAGE_AVAILABLE and rv.rotate_old_csvs is not None:
        def _csv_rotation_loop():
            csv_root = os.path.join(log_dir, 'csv')
            while True:
                time.sleep(3600)   # check hourly
                try:
                    n = rv.rotate_old_csvs(csv_root, max_age_days=90)
                    if n:
                        log.info("Gzipped %d CSV file(s) older than 90 days", n)
                except Exception as e:
                    log.error("CSV rotation failed: %s", e)
        threading.Thread(target=_csv_rotation_loop, daemon=True,
                         name='csv-rotation').start()

    # ── AES-128-GCM frame decryption ──────────────────────────────────────────
    psk_hex = args.psk_hex or os.environ.get('EPM_PSK')
    if psk_hex:
        if not tcp_legacy._CRYPTO_AVAILABLE:
            sys.exit('[crypto] ERROR: --psk-hex requires the cryptography package: '
                     'pip install "cryptography>=42.0.0"')
        psk_hex = psk_hex.strip()
        if len(psk_hex) != 32 or not all(c in '0123456789abcdefABCDEF' for c in psk_hex):
            sys.exit(f'[crypto] ERROR: --psk-hex must be exactly 32 hex characters (got {len(psk_hex)})')
        rv._decryptor = tcp_legacy.FrameDecryptor(bytes.fromhex(psk_hex))
        print(f'[crypto] AES-128-GCM decryption enabled — PSK: {psk_hex[:8]}...{psk_hex[-4:]} '
              f'(all {len(psk_hex)//2} bytes)')
    else:
        print('[crypto] Plaintext mode — pass --psk-hex or set EPM_PSK env-var to enable '
              'AES-128-GCM encryption (required for production)')

    # ── mDNS service advertisement ─────────────────────────────────────────────
    if rv._MDNS_AVAILABLE:
        try:
            my_ip = rv.get_local_ip()
            # Bind Zeroconf to the specific interface IP so it targets the correct
            # subnet (hotspot 192.168.137.x) instead of probing all interfaces.
            try:
                from zeroconf import InterfaceChoice
                zc = rv.Zeroconf(interfaces=[my_ip])
            except (ImportError, TypeError):
                zc = rv.Zeroconf()
            info  = rv.ServiceInfo(
                type_     = '_epm-gateway._tcp.local.',
                name      = 'EPM-Gateway._epm-gateway._tcp.local.',
                addresses = [socket.inet_aton(my_ip)],
                port      = args.port,
                properties= {'version': '2.0', 'factory': rv._FACTORY_NAME},
                server    = 'epm-gateway.local.',
            )
            zc.register_service(info)
            rv._zc_instance = zc
            print(f'[mDNS] Advertised: epm-gateway.local:{args.port} -> {my_ip}')
            print('[mDNS] Satellites will auto-discover the gateway (SERVER_IP becomes optional)')
        except Exception as e:
            print(f'[mDNS] WARNING: registration failed ({type(e).__name__}: {e}) — satellites must use static SERVER_IP')
    else:
        print('[mDNS] zeroconf not installed — satellites must use static SERVER_IP. '
              'Install: pip install "zeroconf>=0.131.0"')

    print("EPM gateway — multi-satellite predictive maintenance receiver")
    print(f"Factory: {rv._FACTORY_NAME}")
    print(f"Expecting: mic={args.fft_mic_n}-pt  imu={args.fft_imu_n}-pt × 3 axes")
    if shaft_hz:
        print(f"Shaft: {shaft_hz:.3f} Hz  ({shaft_hz*60:.0f} RPM)")
    if bearing_freqs_mic:
        print(f"Bearing: {geom.name}  BPFO={bearing_freqs_mic.get('BPFO', 0):.1f} Hz  "
              f"BPFI={bearing_freqs_mic.get('BPFI', 0):.1f} Hz")
    if rv._ML_MODEL:
        print(f"ML alerting: active")
    if rv._HST_AVAILABLE:
        print(f"[EPM] OnlineDetector: river HalfSpaceTrees, fully on-device.")
        print(f"[EPM]   - n_trees=10 height=15 window=250 features={rv.FEATURE_DIM}")
        print(f"[EPM]   - No network calls, no telemetry, no cloud dependencies.")
        print(f"[EPM]   - To verify: tcpdump -i any not port 22 and not port 5100 and not port 8080")
    else:
        print("[EPM] OnlineDetector: river not installed — HST scoring disabled.")
        print("[EPM]   Install: pip install 'river>=0.21.0'")
    if rv._FUSION_AVAILABLE:
        print(f"[EPM] BayesianFusion: multi-channel posterior P(fault|evidence).")
        print(f"[EPM]   prior={rv._FAULT_PRIOR:.3f}  z_mid={rv._EVIDENCE_Z_MID:.1f}  "
              f"WARN@p>{rv.P_FUSION_WARN:.0%}  FAULT@p>{rv.P_FUSION_FAULT:.0%}")
        _ae_ch = ' + z_ae (autoencoder)' if rv._ae_engine is not None else ''
        print(f"[EPM]   Channels: z_kurtosis, z_rms (+ z_hst when HST warmed up){_ae_ch}")
    else:
        print("[EPM] BayesianFusion: bayesian_fusion.py not found — multi-channel fusion disabled.")
    print("Firewall rule for TCP receiver (elevated PowerShell, run once):")
    print(f"  New-NetFirewallRule -DisplayName EPM-{args.port} -Direction Inbound "
          f"-Protocol TCP -LocalPort {args.port} -Action Allow")

    start_dashboard(args.dashboard_port)

    threading.Thread(
        target=tcp_legacy.accept_loop,
        args=(args.listen_ip, args.port, args.fft_mic_n, args.fft_imu_n),
        daemon=True,
    ).start()

    # ── MQTT section-list ingestion (Phase 8a) — additive, off by default ────
    if args.mqtt_host:
        mqtt_subscriber.start(rv, args.mqtt_host, args.mqtt_port)
        print(f"[mqtt] Ingesting {mqtt_subscriber.DATA_TOPIC_FILTER} from "
              f"{args.mqtt_host}:{args.mqtt_port}")

    if args.no_plot:
        print("[plot] --no-plot: running headless (TCP receiver + dashboard only)")
        print("[plot] Dashboard: http://localhost:{}/".format(args.dashboard_port))
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nExiting.")
    else:
        try:
            run_plot(args.fft_mic_n, args.fft_imu_n, shaft_hz=shaft_hz,
                     bearing_freqs_mic=bearing_freqs_mic,
                     bearing_freqs_imu=bearing_freqs_imu,
                     bf=bf)
        except KeyboardInterrupt:
            print("\nExiting.")


if __name__ == '__main__':
    main()
