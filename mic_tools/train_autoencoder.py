#!/usr/bin/env python3
"""
train_autoencoder.py -- MLP autoencoder trained on healthy EPM CSV logs.

Architecture: 7 → 32 → 16 → 8 → 16 → 32 → 7 (GELU activations, MSE loss).
Trained on frames where alert == 'OK'. Exported to ONNX for deployment via
inference.py / recv_verify.py.

Usage:
    python train_autoencoder.py --logs "logs/**/*.csv" --out model/autoencoder.onnx
    python train_autoencoder.py --logs "logs/**/*.csv" --out model/autoencoder.onnx --epochs 300
"""

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
except ImportError:
    sys.exit("PyTorch not installed. Run: pip install torch --index-url https://download.pytorch.org/whl/cpu")

try:
    import onnxruntime as ort
    _ORT_AVAILABLE = True
except ImportError:
    _ORT_AVAILABLE = False

# 7 features matching OnlineDetector FEATURE_DIM=7 and recv_verify.py feature order
FEATURE_COLUMNS = [
    'mic_rms', 'mic_crest', 'mic_kurtosis',
    'imu_rms', 'imu_crest',
    'high_band_ratio', 'z_score',
]

FEATURE_DIM = len(FEATURE_COLUMNS)   # 7


class _Autoencoder(nn.Module):
    def __init__(self, in_dim: int = FEATURE_DIM, bottleneck: int = 8):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(in_dim, 32), nn.GELU(),
            nn.Linear(32, 16),    nn.GELU(),
            nn.Linear(16, bottleneck),
        )
        self.decoder = nn.Sequential(
            nn.Linear(bottleneck, 16), nn.GELU(),
            nn.Linear(16, 32),         nn.GELU(),
            nn.Linear(32, in_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.decoder(self.encoder(x))


def _load_healthy_frames(pattern: str) -> np.ndarray:
    files = sorted(glob.glob(pattern, recursive=True))
    if not files:
        sys.exit(f"No CSV files matched: {pattern!r}")

    frames = []
    skipped = 0
    for path in files:
        try:
            df = pd.read_csv(path)
        except Exception as exc:
            print(f"[WARN] skip {path}: {exc}")
            skipped += 1
            continue
        missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
        if missing:
            print(f"[WARN] skip {path}: missing columns {missing}")
            skipped += 1
            continue
        if 'alert' not in df.columns:
            healthy = df[FEATURE_COLUMNS]
        else:
            healthy = df.loc[df['alert'] == 'OK', FEATURE_COLUMNS]
        frames.append(healthy.values.astype(np.float32))

    if not frames:
        sys.exit("No healthy frames found across all matched CSV files.")

    data = np.concatenate(frames, axis=0)
    print(f"[INFO] Loaded {len(data):,} healthy frames from {len(files) - skipped} files "
          f"({skipped} skipped).")
    return data


def _compute_stats(data: np.ndarray):
    mean = data.mean(axis=0)
    std  = data.std(axis=0)
    std  = np.where(std < 1e-9, 1.0, std)
    return mean.astype(np.float32), std.astype(np.float32)


def _train(data: np.ndarray, epochs: int, batch_size: int, lr: float,
           bottleneck: int) -> tuple:
    mean, std = _compute_stats(data)
    normed = (data - mean) / std

    tensor = torch.from_numpy(normed)
    dataset = TensorDataset(tensor)
    loader  = DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=False)

    model     = _Autoencoder(in_dim=FEATURE_DIM, bottleneck=bottleneck)
    optimiser = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, T_max=epochs)
    criterion = nn.MSELoss()

    model.train()
    for epoch in range(1, epochs + 1):
        epoch_loss = 0.0
        for (batch,) in loader:
            optimiser.zero_grad()
            loss = criterion(model(batch), batch)
            loss.backward()
            optimiser.step()
            epoch_loss += loss.item() * len(batch)
        scheduler.step()
        epoch_loss /= len(normed)
        if epoch % 50 == 0 or epoch == epochs:
            print(f"  epoch {epoch:>4d}/{epochs}  loss={epoch_loss:.6f}")

    return model, mean, std


def _export_onnx(model: nn.Module, mean: np.ndarray, std: np.ndarray,
                 mean_recon_err: float, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    model.eval()
    dummy = torch.zeros(1, FEATURE_DIM)
    torch.onnx.export(
        model, dummy, out_path,
        input_names=['input'],
        output_names=['output'],
        dynamic_axes={'input': {0: 'batch'}, 'output': {0: 'batch'}},
        opset_version=17,
        dynamo=False,
    )
    # Sidecar stats file — loaded by recv_verify.py to normalise features before inference
    stats_path = out_path.replace('.onnx', '_stats.npz')
    np.savez(stats_path, mean=mean, std=std,
             mean_recon_err=np.array([mean_recon_err], dtype=np.float32))
    print(f"[INFO] ONNX model: {out_path}")
    print(f"[INFO] Stats sidecar: {stats_path}")
    print(f"[INFO]   mean_recon_err (healthy baseline) = {mean_recon_err:.6f}")


def _verify_onnx(out_path: str) -> None:
    if not _ORT_AVAILABLE:
        print("[WARN] onnxruntime not available — skipping ONNX verification.")
        return
    session = ort.InferenceSession(out_path, providers=['CPUExecutionProvider'])
    dummy   = np.random.randn(1, FEATURE_DIM).astype(np.float32)
    out     = session.run(['output'], {'input': dummy})[0]
    err     = float(np.mean((dummy - out) ** 2))
    print(f"[INFO] ONNX verification: input shape={dummy.shape}, "
          f"output shape={out.shape}, recon_err={err:.6f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Train MLP autoencoder on healthy EPM CSV logs and export to ONNX.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--logs',       default='logs/**/*.csv',
                        help='Glob pattern for CSV log files (default: logs/**/*.csv)')
    parser.add_argument('--out',        default='model/autoencoder.onnx',
                        help='Output ONNX path (default: model/autoencoder.onnx)')
    parser.add_argument('--epochs',     type=int,   default=200,
                        help='Training epochs (default: 200)')
    parser.add_argument('--batch-size', type=int,   default=256,
                        help='Batch size (default: 256)')
    parser.add_argument('--lr',         type=float, default=1e-3,
                        help='Learning rate (default: 1e-3)')
    parser.add_argument('--bottleneck', type=int,   default=8,
                        help='Bottleneck dimension (default: 8)')
    args = parser.parse_args()

    print(f"[INFO] Feature columns: {FEATURE_COLUMNS}")
    print(f"[INFO] Bottleneck: {args.bottleneck}  Epochs: {args.epochs}  LR: {args.lr}")

    data  = _load_healthy_frames(args.logs)
    mean, std = _compute_stats(data)
    model, mean, std = _train(data, epochs=args.epochs, batch_size=args.batch_size,
                              lr=args.lr, bottleneck=args.bottleneck)

    # Compute mean reconstruction error on healthy training data for threshold baseline
    model.eval()
    normed = torch.from_numpy((data - mean) / std)
    with torch.no_grad():
        recon = model(normed).numpy()
    mean_recon_err = float(np.mean((normed.numpy() - recon) ** 2))

    _export_onnx(model, mean, std, mean_recon_err, args.out)
    _verify_onnx(args.out)
    print("[INFO] Done.")


if __name__ == '__main__':
    main()
