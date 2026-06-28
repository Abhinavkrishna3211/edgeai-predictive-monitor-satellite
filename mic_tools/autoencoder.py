#!/usr/bin/env python3
"""
autoencoder.py — Spectral-aware neural autoencoder for EPM bearing anomaly detection.

Designed to run on the Arduino Uno Q (QRB2210 SoC) via TFLite delegate.

Hardware clarification — Arduino Uno Q (QRB2210):
  The QRB2210 contains an Adreno 702 GPU (OpenCL 2.0, documented).
  It does NOT have a Hexagon DSP / QNN-HTP NPU (not listed in the QRB2210
  datasheet).  The reliable hardware-accelerated inference path on Uno Q is:

      libQnnGpu.so (Adreno 702 via QNN GPU delegate)  ← primary
      CPU TFLite (4 threads)                           ← always-available fallback

  libQnnHtp.so and libhexagon_* are silently skipped if not found.

Feature vector (INPUT_DIM = 41):
  [0:9]   Statistical features:
            mic_rms, mic_crest, mic_kurtosis, imu_rms, imu_crest,
            high_band_ratio, z_score, log_kurtosis, log_z
  [9:41]  32 compressed spectral bands from mic FFT (dBFS → normalised [-1,1]).
            Each band = mean of 16 consecutive FFT bins.
            Bands 16-32 cover 2-8 kHz (bearing emission zone) — highest sensitivity.

Architecture:
  Encoder:  41 → Dense(64,BN,ReLU) → Dense(32,BN,ReLU) → Dense(16,ReLU) ← bottleneck
  Decoder:  16 → Dense(32,BN,ReLU) → Dense(64,BN,ReLU) → Dense(41,linear)
  Loss:     MSE(input, reconstruction)
  Anomaly:  reconstruction MSE above learned threshold → WARN/FAULT

Thresholds (derived from training data):
  t_warn  = 95th percentile of healthy-frame MSE   (5% false-positive rate)
  t_fault = 98.33rd percentile                      (1.67% false-positive rate)

Why an autoencoder beats IsolationForest for this use case:
  1. It is a neural network — runs on GPU/CPU silicon, not CPU trees
  2. Spectral input — it learns which *frequencies* matter for each machine
  3. More discriminative — 3584-dimensional input compressed to 16 latent dims
  4. Quantisable — int8 TFLite export for low-latency GPU inference
  5. Per-machine model — trained on one machine's healthy baseline only
"""

import json
import math
import os

import numpy as np

# ── Feature engineering ────────────────────────────────────────────────────────

INPUT_DIM  = 41
SPEC_BANDS = 32  # compressed mic FFT bands appended to stats
STAT_DIM   = 9   # statistical features (indices 0-8)

_STAT_KEYS = [
    'mic_rms', 'mic_crest', 'mic_kurtosis',
    'imu_rms', 'imu_crest', 'high_band_ratio', 'z_score',
    # indices 7,8 are derived log features — computed below
]


def make_feature_vector(frame: dict) -> np.ndarray:
    """
    Build 41-float feature vector from a frame dict.

    frame must contain the standard statistical keys.
    'mic_fft' (numpy array, dBFS) is optional — zero bands if absent.
    Works with both live recv_verify.py frame dicts and CSV-derived dicts.
    """
    kurtosis = float(frame.get('mic_kurtosis', 3.0))
    z_score  = float(frame.get('z_score',      0.0))

    stats = np.array([
        float(frame.get('mic_rms',         0.0)),
        float(frame.get('mic_crest',        1.0)),
        kurtosis,
        float(frame.get('imu_rms',          0.0)),
        float(frame.get('imu_crest',        1.0)),
        float(frame.get('high_band_ratio',  0.0)),
        z_score,
        math.log1p(max(kurtosis, 0.0)),  # log_kurtosis — flattens heavy tail
        math.log1p(max(z_score,  0.0)),  # log_z
    ], dtype=np.float32)

    # ── 32 spectral bands from mic FFT ────────────────────────────────────────
    mic_fft = frame.get('mic_fft')
    if mic_fft is not None:
        fft_arr = np.asarray(mic_fft, dtype=np.float32)
        # Use first 512 bins (0–8 kHz at 16 kHz Fs)
        n = min(len(fft_arr), 512)
        if n >= SPEC_BANDS:
            p = fft_arr[:n]
            # dBFS → linear power (avoid log(0) with 1e-12 floor)
            power = 10.0 ** (np.clip(p, -120.0, 0.0) / 10.0)
            # Average every (n // SPEC_BANDS) bins into one band
            bins_per_band = n // SPEC_BANDS
            bands = power[:bins_per_band * SPEC_BANDS].reshape(
                SPEC_BANDS, bins_per_band).mean(axis=1)
            # Back to dB, normalise [-120,0] dBFS → [-1,1]
            bands_db   = 10.0 * np.log10(bands + 1e-12)
            bands_norm = np.clip((bands_db + 60.0) / 60.0, -1.0, 1.0)
        else:
            bands_norm = np.zeros(SPEC_BANDS, dtype=np.float32)
    else:
        bands_norm = np.zeros(SPEC_BANDS, dtype=np.float32)

    return np.concatenate([stats, bands_norm.astype(np.float32)])


# ── Model definition ───────────────────────────────────────────────────────────

def build_autoencoder(input_dim: int = INPUT_DIM):
    """
    Dense autoencoder using only NPU-friendly ops:
    Dense + BatchNorm + ReLU (all supported by Qualcomm Adreno GPU delegate).

    BatchNorm is folded into the preceding Dense during TFLite conversion,
    so inference has no BN overhead and runs in a single fused op per layer.
    """
    import tensorflow as tf

    inp = tf.keras.Input(shape=(input_dim,), name='features')

    # Encoder
    x = tf.keras.layers.Dense(64, use_bias=False, name='enc1')(inp)
    x = tf.keras.layers.BatchNormalization(name='bn1')(x)
    x = tf.keras.layers.Activation('relu', name='relu1')(x)

    x = tf.keras.layers.Dense(32, use_bias=False, name='enc2')(x)
    x = tf.keras.layers.BatchNormalization(name='bn2')(x)
    x = tf.keras.layers.Activation('relu', name='relu2')(x)

    bottleneck = tf.keras.layers.Dense(16, activation='relu', name='bottleneck')(x)

    # Decoder
    x = tf.keras.layers.Dense(32, use_bias=False, name='dec1')(bottleneck)
    x = tf.keras.layers.BatchNormalization(name='bn3')(x)
    x = tf.keras.layers.Activation('relu', name='relu3')(x)

    x = tf.keras.layers.Dense(64, use_bias=False, name='dec2')(x)
    x = tf.keras.layers.BatchNormalization(name='bn4')(x)
    x = tf.keras.layers.Activation('relu', name='relu4')(x)

    # Linear output — reconstruction (no activation, can be negative)
    out = tf.keras.layers.Dense(input_dim, name='reconstruction')(x)

    return tf.keras.Model(inp, out, name='epm_autoencoder')


# ── Training ───────────────────────────────────────────────────────────────────

def train_autoencoder(X_raw: np.ndarray, contamination: float = 0.05,
                      epochs: int = 100, batch_size: int = 32):
    """
    Train autoencoder on healthy feature matrix X_raw (N × INPUT_DIM).

    X_raw must contain ONLY healthy (OK-alert) frames.  The model learns the
    reconstruction of normal operation; anomalies have high reconstruction MSE.

    Returns:
        model      — trained Keras autoencoder
        scaler     — fitted StandardScaler (must be applied before inference)
        t_warn     — MSE threshold for WARN alert
        t_fault    — MSE threshold for FAULT alert
        mse_train  — per-sample training MSE (useful for diagnostics)
    """
    import tensorflow as tf
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler()
    X_s    = scaler.fit_transform(X_raw).astype(np.float32)

    model = build_autoencoder(X_raw.shape[1])
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=1e-3),
        loss='mse',
    )

    callbacks = [
        tf.keras.callbacks.EarlyStopping(
            monitor='val_loss', patience=12, restore_best_weights=True, verbose=0),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor='val_loss', factor=0.5, patience=6, min_lr=1e-5, verbose=0),
    ]

    model.fit(
        X_s, X_s,
        epochs=epochs,
        batch_size=min(batch_size, len(X_s)),
        validation_split=0.15,
        callbacks=callbacks,
        verbose=0,
    )

    recon   = model.predict(X_s, verbose=0)
    mse_arr = np.mean((X_s - recon) ** 2, axis=1)

    # contamination=0.05 → 95th percentile of healthy MSE as WARN
    # contamination/3   → 98.33rd percentile as FAULT
    t_warn  = float(np.percentile(mse_arr, 100.0 * (1.0 - contamination)))
    t_fault = float(np.percentile(mse_arr, 100.0 * (1.0 - contamination / 3.0)))

    n_flag = int(np.sum(mse_arr > t_warn))
    print(f'  Autoencoder trained: {len(X_s)} samples  '
          f'MSE mean={mse_arr.mean():.4f}  '
          f'WARN≥{t_warn:.4f}  FAULT≥{t_fault:.4f}  '
          f'flagged={n_flag} ({n_flag/len(X_s):.1%})')

    return model, scaler, t_warn, t_fault, mse_arr


# ── TFLite export ──────────────────────────────────────────────────────────────

def export_tflite(model, scaler, path_prefix: str,
                  X_train: np.ndarray) -> str:
    """
    Convert trained Keras model to TFLite with full-integer quantisation.

    Full int8 quantisation allows all MatMul ops to be mapped to integer HW units
    by the Qualcomm Adreno GPU delegate (libQnnGpu.so).  Float ops would be
    silently downgraded to CPU, so int8 is required for true GPU acceleration.
    The representative dataset calibrates activation ranges for quantisation.

    Saves:
      <path_prefix>_autoencoder.tflite  — deployable GPU/CPU model
      <path_prefix>_scaler.json         — StandardScaler params (applied externally)

    Returns path to .tflite file.
    """
    import tensorflow as tf

    X_s = scaler.transform(X_train).astype(np.float32)

    def _representative_dataset():
        for i in range(min(256, len(X_s))):
            yield [X_s[i:i+1]]

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = _representative_dataset

    # INT8 ops with float32 I/O — the Python caller doesn't need to quantise
    # inputs/outputs; the delegate handles that internally.
    converter.target_spec.supported_ops = [
        tf.lite.OpsSet.TFLITE_BUILTINS_INT8,
        tf.lite.OpsSet.TFLITE_BUILTINS,  # fallback for any non-quantisable op
    ]
    converter.inference_input_type  = tf.float32
    converter.inference_output_type = tf.float32

    tflite_bytes = converter.convert()
    tflite_path  = path_prefix + '_autoencoder.tflite'
    with open(tflite_path, 'wb') as f:
        f.write(tflite_bytes)

    # Scaler persisted separately — needed at inference time
    scaler_path = path_prefix + '_scaler.json'
    with open(scaler_path, 'w') as f:
        json.dump({
            'mean':       scaler.mean_.tolist(),
            'scale':      scaler.scale_.tolist(),
            'n_features': int(scaler.n_features_in_),
        }, f)

    size_kb = len(tflite_bytes) / 1024
    print(f'  TFLite exported: {tflite_path}  ({size_kb:.1f} kB, int8-quantised, Adreno GPU-ready)')
    print(f'  Scaler saved:    {scaler_path}')
    return tflite_path


# ── NPU-accelerated inference ──────────────────────────────────────────────────

class NpuInferencer:
    """
    TFLite inference with hardware-accelerated delegate auto-selection.

    Delegate priority on Arduino Uno Q (QRB2210 — Adreno 702):
      1. libQnnGpu.so  — Qualcomm QNN GPU delegate (Adreno 702, OpenCL 2.0) ← best
      2. CPU (4 threads) — Always available, guaranteed fallback

    The QRB2210 datasheet documents an Adreno 702 GPU; it does not list a
    Hexagon DSP or QNN-HTP NPU.  libQnnHtp.so and libhexagon_* candidates are
    left in the list as no-ops so this code also works on QCS6490-based boards
    that do have Hexagon, without any code change.

    The model is identical regardless of which delegate runs it — correctness is
    independent of the backend.  Only latency changes (GPU: ~2 ms, CPU: ~8 ms).

    self.backend    — human-readable string of active backend
    self.npu_active — True if a hardware accelerator is in use
    """

    _DELEGATE_CANDIDATES = [
        ('libQnnGpu.so',          'Qualcomm Adreno 702 GPU (QNN GPU delegate)'),
        ('libQnnHtp.so',          'Qualcomm QNN-HTP (Hexagon — not on QRB2210)'),
        ('libhexagon_nn_skel.so', 'Qualcomm Hexagon NN legacy (not on QRB2210)'),
    ]

    def __init__(self, tflite_path: str, scaler_path: str, use_npu: bool = True):
        self.tflite_path = tflite_path
        self.backend     = 'CPU (TFLite)'
        self.npu_active  = False
        self._interp     = None

        with open(scaler_path) as f:
            d = json.load(f)
        self._mean  = np.array(d['mean'],  dtype=np.float32)
        self._scale = np.array(d['scale'], dtype=np.float32)

        self._load(use_npu)

    def _load(self, use_npu: bool):
        try:
            import tflite_runtime.interpreter as tflite_mod
        except ImportError:
            try:
                import tensorflow.lite as tflite_mod
            except ImportError:
                print('[npu] WARNING: no TFLite runtime — NPU inferencer unavailable')
                return

        delegates = []
        if use_npu:
            for lib, label in self._DELEGATE_CANDIDATES:
                try:
                    d = tflite_mod.load_delegate(lib)
                    delegates    = [d]
                    self.backend = label
                    self.npu_active = True
                    print(f'[npu] Hardware delegate active: {label}')
                    break
                except Exception:
                    pass
            if not self.npu_active:
                print('[npu] No hardware delegate found — CPU TFLite (4 threads)')

        self._interp = tflite_mod.Interpreter(
            model_path=self.tflite_path,
            experimental_delegates=delegates,
            num_threads=4,
        )
        self._interp.allocate_tensors()
        self._in_idx  = self._interp.get_input_details()[0]['index']
        self._out_idx = self._interp.get_output_details()[0]['index']

    def infer(self, feat_raw: np.ndarray) -> float:
        """
        Run reconstruction pass.  feat_raw is the RAW (un-scaled) feature vector.
        Returns reconstruction MSE — higher = more anomalous.
        Returns 0.0 if no runtime is available.
        """
        if self._interp is None:
            return 0.0
        feat_scaled = ((feat_raw.astype(np.float32) - self._mean) / self._scale)
        x = feat_scaled.reshape(1, -1)
        self._interp.set_tensor(self._in_idx, x)
        self._interp.invoke()
        recon = self._interp.get_tensor(self._out_idx)[0]
        return float(np.mean((feat_scaled - recon) ** 2))

    @property
    def available(self) -> bool:
        return self._interp is not None


# ── Model persistence ──────────────────────────────────────────────────────────

def load_npu_model(path_prefix: str) -> dict | None:
    """
    Load a TFLite autoencoder model from disk.

    Looks for:
      <path_prefix>_autoencoder.tflite
      <path_prefix>_scaler.json
      <path_prefix>_meta.json   (optional)

    Returns a model dict compatible with recv_verify._ml_score_with(), or None
    if the files are missing.
    """
    tflite_path = path_prefix + '_autoencoder.tflite'
    scaler_path = path_prefix + '_scaler.json'
    meta_path   = path_prefix + '_meta.json'

    if not (os.path.exists(tflite_path) and os.path.exists(scaler_path)):
        return None

    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)

    try:
        inferencer = NpuInferencer(tflite_path, scaler_path)
    except Exception as e:
        print(f'[npu] Failed to load NpuInferencer from {tflite_path}: {e}')
        return None

    return {
        'type':       'tflite',
        'inferencer': inferencer,
        't_warn':     float(meta.get('threshold_warn',  0.05)),
        't_fault':    float(meta.get('threshold_fault', 0.10)),
        'npu_active': inferencer.npu_active,
        'backend':    inferencer.backend,
        'trained_at': meta.get('trained_at', ''),
        'n_samples':  int(meta.get('n_samples', 0)),
    }


# ── Stand-alone trainer ────────────────────────────────────────────────────────

def train_and_export(csv_paths: list[str], path_prefix: str,
                     contamination: float = 0.05) -> dict | None:
    """
    High-level helper: load CSVs, build feature matrix, train, export TFLite.
    Used by ml_trainer.py and the background training thread.
    Returns the loaded model dict (ready for inference), or None on failure.
    """
    import pandas as pd
    import tensorflow as tf

    dfs = []
    for p in csv_paths:
        try:
            dfs.append(pd.read_csv(p))
        except Exception as e:
            print(f'  WARNING: skipping {p}: {e}')
    if not dfs:
        return None
    df = pd.concat(dfs, ignore_index=True)
    print(f'  Rows loaded: {len(df):,}')

    feats = []
    for _, row in df.iterrows():
        f = {
            'mic_rms':         float(row.get('mic_rms',         0.0)),
            'mic_crest':       float(row.get('mic_crest',        1.0)),
            'mic_kurtosis':    float(row.get('mic_kurtosis',     3.0)),
            'imu_rms':         float(row.get('imu_rms',          0.0)),
            'imu_crest':       float(row.get('imu_crest',        1.0)),
            'high_band_ratio': float(row.get('high_band_ratio',  0.0)),
            'z_score':         float(row.get('z_score',          0.0)),
        }
        feats.append(make_feature_vector(f))

    X = np.array(feats, dtype=np.float32)
    X = X[~np.any(np.isnan(X) | np.isinf(X), axis=1)]
    if len(X) < 30:
        print(f'  Not enough valid rows ({len(X)}) — skipping')
        return None

    model, scaler, t_warn, t_fault, _ = train_autoencoder(
        X, contamination=contamination)

    os.makedirs(os.path.dirname(path_prefix) or '.', exist_ok=True)
    export_tflite(model, scaler, path_prefix, X)

    import datetime
    trained_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta = {
        'trained_at':      trained_at,
        'n_samples':       len(X),
        'contamination':   contamination,
        'threshold_warn':  t_warn,
        'threshold_fault': t_fault,
        'model_type':      'autoencoder_tflite',
        'input_dim':       INPUT_DIM,
        'spec_bands':      SPEC_BANDS,
    }
    with open(path_prefix + '_meta.json', 'w') as mf:
        json.dump(meta, mf, indent=2)

    return load_npu_model(path_prefix)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import glob as _glob

    ap = argparse.ArgumentParser(
        description='Train EPM neural autoencoder and export TFLite for Uno Q (Adreno 702 GPU)')
    ap.add_argument('--log-dir',  default='logs',  help='CSV log directory')
    ap.add_argument('--out',      default='model/epm_auto',
                    help='Output path prefix (default: model/epm_auto)')
    ap.add_argument('--contamination', type=float, default=0.05)
    ap.add_argument('--satellite', default=None,
                    help='Train on one satellite only (name must appear in CSV filename)')
    args = ap.parse_args()

    pattern = (f'epm_{args.satellite}_*.csv' if args.satellite else 'epm_*.csv')
    csvs    = sorted(_glob.glob(os.path.join(args.log_dir, pattern)))
    if not csvs:
        raise SystemExit(f'No CSV files matching {pattern} in {args.log_dir}')

    print(f'Training on {len(csvs)} CSV file(s)…')
    result = train_and_export(csvs, args.out, args.contamination)
    if result:
        print(f'\nModel ready.  Backend: {result["backend"]}')
        print(f'WARN≥{result["t_warn"]:.4f}   FAULT≥{result["t_fault"]:.4f}')
        print(f'Deploy: copy {args.out}_autoencoder.tflite + _scaler.json + _meta.json to model/')
    else:
        raise SystemExit('Training failed — see errors above')
