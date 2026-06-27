#!/usr/bin/env python3
"""
bearing_math.py — Bearing fault frequency calculator for the EPM system.

Standard ISO formulas map shaft speed and bearing geometry to four
characteristic defect frequencies.  Peaks in the vibration FFT at or near
these frequencies are strong indicators of the corresponding fault type.

  BPFO = n/2 · f · (1 − d/D · cos α)   outer-race defect
  BPFI = n/2 · f · (1 + d/D · cos α)   inner-race defect
  BSF  = D/(2d) · f · (1 − (d/D·cos α)²)  ball-spin defect
  FTF  = 1/2   · f · (1 − d/D · cos α)    cage fundamental frequency

Variables:
  n = rolling-element count
  f = shaft rotation frequency (Hz)
  D = pitch circle diameter (mm)
  d = ball/roller diameter (mm)
  α = contact angle (degrees; 0° for standard deep-groove ball bearings)

Usage (command-line):
  python bearing_math.py 6205 1500          # bearing 6205 at 1500 RPM
  python bearing_math.py 9,38.5,10.3 3000   # custom geometry at 3000 RPM
  python bearing_math.py 8,33.5,9.5,0 900   # custom with contact angle

Import (used by recv_verify.py for live FFT annotation):
  from bearing_math import BearingFreqs, parse_bearing_arg, COMMON_BEARINGS
  bf = BearingFreqs.from_rpm(1500, COMMON_BEARINGS['6205'])
  markers = bf.markers(fs_hz=16000)   # {label: freq_hz} within Nyquist limit
"""

import math
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class BearingGeometry:
    """Physical dimensions of a rolling-element bearing."""
    name: str
    n_balls: int
    pitch_dia_mm: float
    ball_dia_mm: float
    contact_angle_deg: float = 0.0


# Geometry from SKF/NSK datasheets (dimensions approximate, standard metric series)
COMMON_BEARINGS: Dict[str, BearingGeometry] = {
    '6200': BearingGeometry('6200',  8,  19.5,  5.9,  0.0),
    '6201': BearingGeometry('6201',  8,  22.5,  6.75, 0.0),
    '6202': BearingGeometry('6202',  8,  26.0,  7.9,  0.0),
    '6203': BearingGeometry('6203',  8,  29.4,  8.7,  0.0),
    '6204': BearingGeometry('6204',  9,  33.5,  9.5,  0.0),
    '6205': BearingGeometry('6205',  9,  38.5, 10.3,  0.0),
    '6206': BearingGeometry('6206',  9,  44.0, 12.0,  0.0),
    '6207': BearingGeometry('6207',  9,  50.0, 14.0,  0.0),
    '6208': BearingGeometry('6208',  9,  56.0, 15.9,  0.0),
    '6209': BearingGeometry('6209',  9,  62.0, 17.5,  0.0),
    '6210': BearingGeometry('6210', 10,  68.0, 18.0,  0.0),
    '6304': BearingGeometry('6304',  8,  35.0, 12.7,  0.0),
    '6305': BearingGeometry('6305',  8,  41.0, 14.3,  0.0),
    '6306': BearingGeometry('6306',  8,  47.0, 16.0,  0.0),
    '6307': BearingGeometry('6307',  8,  52.5, 18.3,  0.0),
    '6308': BearingGeometry('6308',  8,  60.0, 20.6,  0.0),
    '6309': BearingGeometry('6309',  8,  67.5, 22.2,  0.0),
    '6310': BearingGeometry('6310',  8,  75.0, 25.4,  0.0),
}

# Color palette for bearing fault frequency markers in matplotlib
MARKER_COLORS: Dict[str, str] = {
    'shaft':  '#ffff44',   # yellow  — shaft 1×
    '2×sh':   '#aaff44',   # lime    — shaft 2× (misalignment indicator)
    'BPFO':   '#ff4444',   # red     — outer race
    '2×BPFO': '#ff8888',   # pink    — outer race 2nd harmonic
    'BPFI':   '#ff8800',   # orange  — inner race
    '2×BPFI': '#ffbb44',   # amber   — inner race 2nd harmonic
    'BSF':    '#cc44ff',   # purple  — ball spin
    'FTF':    '#44ccff',   # cyan    — cage frequency
}


@dataclass
class BearingFreqs:
    """Characteristic fault frequencies for one bearing at one shaft speed."""
    geom: BearingGeometry
    shaft_hz: float
    bpfo: float
    bpfi: float
    bsf: float
    ftf: float

    @classmethod
    def from_shaft_hz(cls, shaft_hz: float, geom: BearingGeometry) -> 'BearingFreqs':
        ca = math.cos(math.radians(geom.contact_angle_deg))
        r  = (geom.ball_dia_mm / geom.pitch_dia_mm) * ca
        n  = geom.n_balls
        return cls(
            geom      = geom,
            shaft_hz  = shaft_hz,
            bpfo      = n / 2 * shaft_hz * (1 - r),
            bpfi      = n / 2 * shaft_hz * (1 + r),
            bsf       = (geom.pitch_dia_mm / (2 * geom.ball_dia_mm)) * shaft_hz * (1 - r ** 2),
            ftf       = 0.5 * shaft_hz * (1 - r),
        )

    @classmethod
    def from_rpm(cls, rpm: float, geom: BearingGeometry) -> 'BearingFreqs':
        return cls.from_shaft_hz(rpm / 60.0, geom)

    def markers(self, fs_hz: float) -> Dict[str, float]:
        """
        Return {label: freq_hz} for all fault frequencies and 2nd harmonics
        that fall within the usable range [0, fs_hz/2].
        """
        nyq = fs_hz / 2
        candidates = {
            'shaft':  self.shaft_hz,
            '2×sh':   2 * self.shaft_hz,
            'BPFO':   self.bpfo,
            '2×BPFO': 2 * self.bpfo,
            'BPFI':   self.bpfi,
            '2×BPFI': 2 * self.bpfi,
            'BSF':    self.bsf,
            'FTF':    self.ftf,
        }
        return {k: v for k, v in candidates.items() if 0 < v < nyq}

    def print_table(self):
        rpm = self.shaft_hz * 60
        print(f'\nBearing {self.geom.name} @ {rpm:.0f} RPM  ({self.shaft_hz:.3f} Hz)')
        print(f'  n={self.geom.n_balls} balls   '
              f'D={self.geom.pitch_dia_mm} mm   '
              f'd={self.geom.ball_dia_mm} mm   '
              f'α={self.geom.contact_angle_deg}°')
        print(f'  {"Frequency":<12} {"Hz":>9}  {"CPM":>9}  {"Fault type"}')
        print(f'  {"-" * 52}')
        labels = {
            'shaft':  'Shaft rotation (imbalance / reference)',
            '2×sh':   'Shaft 2× (misalignment indicator)',
            'BPFO':   'Outer race defect',
            '2×BPFO': 'Outer race 2nd harmonic',
            'BPFI':   'Inner race defect',
            '2×BPFI': 'Inner race 2nd harmonic',
            'BSF':    'Ball spin defect',
            'FTF':    'Cage fundamental (FTF)',
        }
        for key, desc in labels.items():
            hz = self.markers(1_000_000).get(key)
            if hz:
                print(f'  {key:<12} {hz:>9.3f}  {hz*60:>9.0f}  {desc}')


def parse_bearing_arg(arg: str) -> Optional[BearingGeometry]:
    """
    Parse a --bearing argument.
    Accepts:
      '6205'             → look up in COMMON_BEARINGS
      'n,D,d'            → custom, no contact angle
      'n,D,d,alpha'      → custom with contact angle in degrees
    """
    if arg in COMMON_BEARINGS:
        return COMMON_BEARINGS[arg]
    try:
        parts = arg.split(',')
        if len(parts) == 3:
            n, D, d = int(parts[0]), float(parts[1]), float(parts[2])
            return BearingGeometry(f'custom({n},{D},{d})', n, D, d, 0.0)
        if len(parts) == 4:
            n, D, d, a = int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
            return BearingGeometry(f'custom({n},{D},{d},{a})', n, D, d, a)
    except (ValueError, TypeError):
        pass
    return None


if __name__ == '__main__':
    import argparse, sys

    ap = argparse.ArgumentParser(
        description='Compute bearing fault frequencies from geometry and shaft speed')
    ap.add_argument('bearing',
                    help='Bearing model (e.g. 6205) or n,D,d[,alpha]')
    ap.add_argument('rpm', type=float,
                    help='Shaft speed in RPM')
    ap.add_argument('--list', action='store_true',
                    help='List all built-in bearing geometries')
    args = ap.parse_args()

    if args.list:
        print('Built-in bearings:')
        for name, g in sorted(COMMON_BEARINGS.items()):
            print(f'  {name:<6}  n={g.n_balls}  D={g.pitch_dia_mm} mm  d={g.ball_dia_mm} mm')
        sys.exit(0)

    geom = parse_bearing_arg(args.bearing)
    if geom is None:
        known = ', '.join(sorted(COMMON_BEARINGS))
        print(f'Unknown bearing "{args.bearing}".  Built-in types: {known}')
        sys.exit(1)

    BearingFreqs.from_rpm(args.rpm, geom).print_table()
