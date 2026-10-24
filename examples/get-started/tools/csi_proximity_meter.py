#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Modified by Ayu102938
# - 2026-10-24: Created csi_proximity_meter.py for approach/departure detection.
#
# This file is derived from examples/get-started/tools/csi_occupancy_meter.py and
# csi_motion_detector.py.  The CSI parser, subcarrier selection and CSV layout
# are copied verbatim so the three tools stay comparable, while this script
# looks at the *absolute* (unnormalized) energy trend instead of a gain-
# normalized motion score.
#
"""Coarse approach/departure detection from ESP-CSI serial output.

Reads ``CSI_DATA`` lines from a single receiver board and tracks the absolute
CSI energy with an exponential moving average (EMA).  The direction estimate is
the difference between the current smoothed energy and the smoothed energy
``--trend-window`` seconds ago::

    trend = smoothed(t) - smoothed(t - trend_window)

    trend >  +threshold  -> 近づいている   (approaching)
    trend <  -threshold  -> 遠ざかっている (moving away)
    |trend| <= threshold -> 静止           (stationary)

The energy is deliberately **not** gain-normalized: a person entering the
Fresnel zone of the link changes the received power, and that absolute change is
what this tool is trying to observe.  Two metrics are available via
``--energy-metric``:

``power`` (default)
    ``mean(I^2 + Q^2)`` over the selected subcarriers -- proportional to the
    received power on those subcarriers.
``amplitude``
    ``mean(sqrt(I^2 + Q^2))`` over the selected subcarriers.

Thresholds:

* ``--threshold`` uses a fixed trend threshold.
* Otherwise the room must be empty for ``--calib`` seconds (default 5 s) and the
  script sets ``threshold = max(3 * sigma_trend, 1e-9)`` from the trend observed
  in that window.  The trend is sampled for the baseline only after the history
  covers 80% of ``--trend-window``, so the calibration statistic has the same
  lag/span as the run-time one.

Limitations (important):

* This is a **coarse** proximity-direction estimator, not a range or velocity
  measurement.  Multipath reflections can make the received energy decrease
  briefly even while a person is approaching (and vice versa).  The tool
  therefore compares low-frequency trends over ``--trend-window`` seconds and
  intentionally ignores instantaneous changes.
* AGC / gain changes, channel switching, or movement of the boards themselves
  also change the absolute energy and will be reported as approach/departure.
* Absolute energy depends heavily on board position, antenna orientation and
  transmit power, so thresholds are per-setup; recalibrate after moving either
  board.

Usage::

    # Default: /dev/ttyACM0 @ 921600, 5 s empty-room calibration
    python csi_proximity_meter.py -p /dev/ttyACM0

    # Slower trend, amplitude metric and a fixed threshold
    python csi_proximity_meter.py -p /dev/ttyACM0 --trend-window 4 \
        --energy-metric amplitude --threshold 2.5

    # Record raw CSI packets and the per-packet direction metrics
    python csi_proximity_meter.py -p /dev/ttyACM0 \
        --csv csi_capture.csv --metrics-csv proximity.csv

Requires only ``pyserial`` and ``numpy`` (both in requirements.txt).
"""

import argparse
import csv
import json
import sys
import time
from collections import deque
from io import StringIO

import numpy as np
import serial

# Column layouts emitted by the ESP-CSI receiver firmwares.
DATA_COLUMNS_NAMES_C5C6 = [
    'type', 'id', 'mac', 'rssi', 'rate', 'noise_floor', 'fft_gain',
    'agc_gain', 'channel', 'local_timestamp', 'sig_len', 'rx_state',
    'len', 'first_word', 'data',
]
DATA_COLUMNS_NAMES = [
    'type', 'id', 'mac', 'rssi', 'rate', 'sig_mode', 'mcs', 'bandwidth',
    'smoothing', 'not_sounding', 'aggregation', 'stbc', 'fec_coding',
    'sgi', 'noise_floor', 'ampdu_cnt', 'channel', 'secondary_channel',
    'local_timestamp', 'ant', 'sig_len', 'rx_state', 'len', 'first_word',
    'data',
]

METRICS_COLUMNS = [
    'timestamp', 'packet_num', 'energy', 'smoothed_energy', 'trend',
    'direction', 'rssi', 'energy_baseline_mu', 'energy_baseline_sigma',
    'trend_threshold',
]

DIR_APPROACHING = '近づいている'
DIR_AWAY = '遠ざかっている'
DIR_STATIONARY = '静止'
DIR_CALIBRATING = 'キャリブレーション中'
DIR_COLLECTING = '収集中'

ENERGY_METRICS = ('power', 'amplitude')

# A trend estimate needs history covering this fraction of --trend-window.
TREND_MIN_COVERAGE = 0.8

# Fallback trend threshold when the calibration window produced no usable
# trend samples (for example a very slow packet rate): 1% of the mean energy.
TREND_FALLBACK_RELATIVE = 0.01

# Calibration needs a few trend samples on top of the elapsed time.
MIN_TREND_SAMPLES = 5
MIN_ENERGY_SAMPLES = 20


def parse_csi_line(line):
    """Parse one serial line into ``(row, amplitude, rssi)``.

    Returns ``None`` for anything that is not a complete, well-formed
    ``CSI_DATA`` record.  Malformed input is expected on a live serial
    link (boot messages, truncated reads), so callers simply skip it.
    """
    if 'CSI_DATA' not in line:
        return None

    try:
        row = next(csv.reader(StringIO(line)))
    except (csv.Error, StopIteration):
        return None

    if len(row) not in (len(DATA_COLUMNS_NAMES), len(DATA_COLUMNS_NAMES_C5C6)):
        return None

    try:
        csi_len = int(row[-3])
        raw = json.loads(row[-1])
    except (ValueError, IndexError, json.JSONDecodeError):
        return None

    if not isinstance(raw, list) or csi_len != len(raw):
        return None

    # The firmware interleaves imaginary then real parts: [Q, I, Q, I, ...]
    n_pairs = len(raw) // 2
    if n_pairs == 0:
        return None
    samples = np.asarray(raw[:n_pairs * 2], dtype=np.float64).reshape(n_pairs, 2)
    amplitude = np.hypot(samples[:, 0], samples[:, 1])

    try:
        rssi = int(row[3])
    except (ValueError, IndexError):
        rssi = 0

    return row, amplitude, rssi


def parse_subcarrier_spec(spec, count):
    """Turn ``"0-12,20,30-40"`` into an index array clipped to ``count``."""
    if not spec:
        return np.arange(count)

    indexes = set()
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            if '-' in part:
                start, end = part.split('-', 1)
                indexes.update(range(int(start), int(end) + 1))
            else:
                indexes.add(int(part))
        except ValueError:
            raise argparse.ArgumentTypeError(
                'invalid subcarrier specification: %r' % spec)

    valid = sorted(i for i in indexes if 0 <= i < count)
    if not valid:
        return np.arange(count)
    return np.asarray(valid, dtype=np.int64)


def select_subcarriers(count, spec, edge_skip):
    """Choose the subcarrier indexes used for the energy estimate.

    An explicit ``--subcarriers`` specification wins.  Otherwise the first and
    last ``edge_skip`` subcarriers are dropped, because guard, DC and null
    subcarriers carry little useful signal and mostly add noise.
    """
    if spec:
        return parse_subcarrier_spec(spec, count)

    skip = max(0, min(edge_skip, (count - 1) // 2))
    if skip == 0 or count - 2 * skip < 1:
        return np.arange(count)
    return np.arange(skip, count - skip)


def compute_energy(amplitude, index, metric):
    """Return the absolute per-packet energy over the selected subcarriers.

    ``power`` is ``mean(I^2 + Q^2)`` and ``amplitude`` is
    ``mean(sqrt(I^2 + Q^2))``.  The amplitude is *not* normalized by its own
    mean: the absolute received level is what makes an approach/departure
    visible at all.  Returns ``None`` when the result is not finite.
    """
    used = amplitude[index]
    if used.size == 0:
        return None

    if metric == 'amplitude':
        energy = float(np.mean(used))
    else:
        energy = float(np.mean(used * used))

    if not np.isfinite(energy):
        return None
    return energy


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description='Coarse approach/departure detection from ESP-CSI energy '
                    'trends',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-p', '--port', default='/dev/ttyACM0',
                        help='serial port of the CSI receiver board')
    parser.add_argument('-b', '--baud', type=int, default=921600,
                        help='serial baud rate')
    parser.add_argument('--energy-metric', choices=ENERGY_METRICS,
                        default='power',
                        help='energy metric computed from the raw CSI '
                             'amplitudes (power = mean(I^2+Q^2), '
                             'amplitude = mean(sqrt(I^2+Q^2)))')
    parser.add_argument('--trend-window', type=float, default=2.0,
                        help='seconds between the current smoothed energy and '
                             'the reference sample used for the trend')
    parser.add_argument('--threshold', type=float, default=None,
                        help='fixed trend threshold; when omitted, it is '
                             'calibrated as 3 * sigma_trend in an empty room')
    parser.add_argument('--calib', type=float, default=5.0,
                        help='seconds of empty-room packets used to calibrate '
                             'the trend threshold and energy baseline')
    parser.add_argument('--ema-alpha', type=float, default=0.15,
                        help='EMA smoothing factor for the energy '
                             '(0 < alpha <= 1)')
    parser.add_argument('--edge-skip', type=int, default=10,
                        help='subcarriers to skip at each edge (guard/DC/null); '
                             '0 uses all subcarriers')
    parser.add_argument('--subcarriers', default=None,
                        help='subcarrier indexes/ranges to use, e.g. "5-25,35-55" '
                             '(overrides --edge-skip; default: all minus edges)')
    parser.add_argument('--csv', default=None,
                        help='optional path to write raw CSI packets as CSV')
    parser.add_argument('--metrics-csv', default=None,
                        help='optional path to write per-packet metrics as CSV')
    return parser


class Ema:
    """Exponential moving average with warm-up on the first sample."""

    def __init__(self, alpha):
        self.alpha = alpha
        self.value = None

    def update(self, sample):
        if self.value is None:
            self.value = sample
        else:
            self.value = self.alpha * sample + (1.0 - self.alpha) * self.value
        return self.value


class TrendTracker:
    """Time-stamped smoothed-energy history for a lagged difference."""

    def __init__(self, window_seconds, min_coverage=TREND_MIN_COVERAGE):
        self.window = window_seconds
        self.min_coverage = min_coverage
        maxlen = max(512, int(window_seconds * 1000))
        self.samples = deque(maxlen=maxlen)

    def clear(self):
        self.samples.clear()

    def append(self, now, value):
        self.samples.append((now, value))
        # Keep a margin of 1.5 windows: enough to always contain the reference
        # sample without letting the buffer grow for the whole run.
        cutoff = now - 1.5 * self.window
        while len(self.samples) > 2 and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def coverage(self):
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1][0] - self.samples[0][0]
        return span / self.window if self.window > 0 else 1.0

    def ready(self):
        return self.coverage() >= self.min_coverage

    def trend(self):
        """Current smoothed energy minus the sample closest to now - window.

        Returns ``None`` until the history covers ``min_coverage`` of the
        window, so the trend never mixes a very short lag into the threshold
        calibration.
        """
        if not self.ready():
            return None
        now, current = self.samples[-1]
        target = now - self.window
        # The history is small (a couple of thousand entries at most), so a
        # linear scan is cheaper than maintaining a second index.
        best = min(self.samples, key=lambda item: abs(item[0] - target))
        return current - best[1]


class Calibration:
    """Collects empty-room energy and trend samples and derives thresholds."""

    def __init__(self, seconds, min_energy=MIN_ENERGY_SAMPLES,
                 min_trend=MIN_TREND_SAMPLES, grace=2.0):
        self.seconds = seconds
        self.min_energy = min_energy
        self.min_trend = min_trend
        self.grace = grace
        self.energies = []
        self.trends = []
        self.start = None

    def start_now(self):
        if self.start is None:
            self.start = time.monotonic()

    def add_energy(self, value):
        self.start_now()
        self.energies.append(value)

    def add_trend(self, value):
        self.start_now()
        self.trends.append(value)

    def elapsed(self):
        return 0.0 if self.start is None else time.monotonic() - self.start

    def progress(self):
        """Return ``(elapsed, fraction)`` for the calibration progress line."""
        elapsed = self.elapsed()
        fraction = 0.0
        if self.seconds > 0:
            fraction = min(1.0, elapsed / self.seconds)
        return elapsed, fraction

    def done(self):
        if self.start is None:
            return False
        elapsed = self.elapsed()
        if elapsed < self.seconds:
            return False
        if len(self.energies) < self.min_energy:
            return False
        if len(self.trends) >= self.min_trend:
            return True
        # Slow serial links may not fill the trend buffer in time; stop anyway
        # instead of calibrating forever and fall back to a relative threshold.
        return elapsed >= self.seconds + self.grace

    def energy_result(self):
        """Return ``(mu, sigma)`` of the smoothed energy during calibration."""
        arr = np.asarray(self.energies, dtype=np.float64)
        if not arr.size:
            return 0.0, 0.0
        mu = float(arr.mean())
        sigma = float(arr.std())
        return mu, sigma

    def trend_threshold(self, energy_mu):
        """Return the auto trend threshold and whether it was a fallback."""
        arr = np.asarray(self.trends, dtype=np.float64)
        if arr.size:
            sigma = float(arr.std())
            threshold = max(3.0 * sigma, 1e-9)
            if np.isfinite(threshold):
                return threshold, False
        return max(TREND_FALLBACK_RELATIVE * abs(energy_mu), 1e-9), True


class Progress:
    """Rate-limited ``\\r`` progress line so a piped log does not explode."""

    def __init__(self, interval=0.1):
        self.interval = interval
        self.last = 0.0

    def show(self, text):
        now = time.monotonic()
        if now - self.last < self.interval:
            return
        self.last = now
        print('\r' + text.ljust(88), end='', flush=True)

    def force(self, text):
        self.last = time.monotonic()
        print('\r' + text.ljust(88), end='', flush=True)


def fmt_opt(value):
    """Format a float for CSV, or an empty field when it is undefined."""
    if value is None or not np.isfinite(value):
        return ''
    return '%.6f' % value


def classify(trend, threshold):
    if trend > threshold:
        return DIR_APPROACHING
    if trend < -threshold:
        return DIR_AWAY
    return DIR_STATIONARY


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.trend_window <= 0:
        print('error: --trend-window must be positive', file=sys.stderr)
        return 2
    if args.threshold is not None and args.threshold <= 0:
        print('error: --threshold must be positive', file=sys.stderr)
        return 2
    if not 0.0 < args.ema_alpha <= 1.0:
        print('error: --ema-alpha must be in (0, 1]', file=sys.stderr)
        return 2
    if args.calib < 0:
        print('error: --calib must not be negative', file=sys.stderr)
        return 2
    if args.edge_skip < 0:
        print('error: --edge-skip must not be negative', file=sys.stderr)
        return 2

    try:
        ser = serial.Serial(port=args.port, baudrate=args.baud, bytesize=8,
                            parity='N', stopbits=1, timeout=1)
    except serial.SerialException as exc:
        print('error: cannot open %s: %s' % (args.port, exc), file=sys.stderr)
        return 1

    csv_file = None
    csv_writer = None
    if args.csv:
        csv_file = open(args.csv, 'w', newline='')
        csv_writer = csv.writer(csv_file)

    metrics_file = None
    metrics_writer = None
    if args.metrics_csv:
        metrics_file = open(args.metrics_csv, 'w', newline='')
        metrics_writer = csv.writer(metrics_file)
        metrics_writer.writerow(METRICS_COLUMNS)
        metrics_file.flush()

    index_cache = {}
    window_len = None
    packets = 0
    scored = 0
    skipped = 0
    other_lines = 0

    ema = Ema(args.ema_alpha)
    tracker = TrendTracker(args.trend_window)
    calibration = Calibration(
        args.calib, grace=max(2.0 * args.trend_window, 2.0))
    progress = Progress()

    energy_mu = energy_sigma = float('nan')
    trend_threshold = float('nan')
    calibrated = False
    direction = DIR_COLLECTING
    last_energy = float('nan')
    last_trend = None

    print('Listening on %s @ %d baud (energy-metric=%s, trend-window=%.1fs, '
          'ema-alpha=%.2f)'
          % (args.port, args.baud, args.energy_metric, args.trend_window,
             args.ema_alpha))
    print('Calibrating an empty room for %.1f s -- keep the room empty.'
          % args.calib)
    print('Directions: %s / %s / %s. Press Ctrl+C to stop.'
          % (DIR_APPROACHING, DIR_AWAY, DIR_STATIONARY))

    try:
        while True:
            line = ser.readline()
            if not line:
                if not calibrated:
                    elapsed, fraction = calibration.progress()
                    progress.show(
                        'キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d energy, '
                        '%d trend samples)'
                        % (elapsed, calibration.seconds, int(fraction * 100),
                           len(calibration.energies),
                           len(calibration.trends)))
                continue
            # A read interrupted by the timeout may return a partial line.
            if not line.endswith(b'\n'):
                skipped += 1
                continue

            text = line.decode('utf-8', errors='ignore').strip('\r\n')
            parsed = parse_csi_line(text)
            if parsed is None:
                # Firmware logs and boot banners are not CSI records.
                if 'CSI_DATA' in text:
                    skipped += 1
                else:
                    other_lines += 1
                continue

            row, amplitude, rssi = parsed
            packets += 1
            timestamp = time.time()
            now = time.monotonic()

            if csv_writer is not None:
                if packets == 1:
                    csv_writer.writerow(
                        DATA_COLUMNS_NAMES if len(row) == len(DATA_COLUMNS_NAMES)
                        else DATA_COLUMNS_NAMES_C5C6)
                csv_writer.writerow(row)
                csv_file.flush()

            # A changed subcarrier count means the radio re-tuned.  The energy
            # scale changes with it, so restart the smoothing and the trend
            # history instead of reporting a fake jump.
            if window_len is not None and amplitude.shape[0] != window_len:
                ema = Ema(args.ema_alpha)
                tracker.clear()
            window_len = amplitude.shape[0]

            if window_len not in index_cache:
                try:
                    index_cache[window_len] = select_subcarriers(
                        window_len, args.subcarriers, args.edge_skip)
                except argparse.ArgumentTypeError as exc:
                    print('\nerror: %s' % exc, file=sys.stderr)
                    return 2
            index = index_cache[window_len]

            energy = compute_energy(amplitude, index, args.energy_metric)
            if energy is None:
                skipped += 1
                continue

            smoothed = ema.update(energy)
            scored += 1
            last_energy = energy
            tracker.append(now, smoothed)
            trend = tracker.trend()
            last_trend = trend

            if not calibrated:
                calibration.add_energy(smoothed)
                if trend is not None:
                    calibration.add_trend(trend)

                if metrics_writer is not None:
                    fixed = fmt_opt(args.threshold) if args.threshold else ''
                    metrics_writer.writerow([
                        '%.3f' % timestamp, packets,
                        fmt_opt(energy), fmt_opt(smoothed), fmt_opt(trend),
                        DIR_CALIBRATING, rssi, '', '', fixed,
                    ])
                    metrics_file.flush()

                elapsed, fraction = calibration.progress()
                progress.show(
                    'キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d energy, '
                    '%d trend samples)'
                    % (elapsed, calibration.seconds, int(fraction * 100),
                       len(calibration.energies), len(calibration.trends)))

                if calibration.done():
                    energy_mu, energy_sigma = calibration.energy_result()
                    if args.threshold is None:
                        trend_threshold, fallback = \
                            calibration.trend_threshold(energy_mu)
                        note = ' [fallback]' if fallback else ''
                    else:
                        trend_threshold = args.threshold
                        note = ' [fixed]'
                    calibrated = True
                    print('\nキャリブレーション完了: energy mu=%.4f sigma=%.4f / '
                          'trend threshold=%.4f%s (%d energy, %d trend samples)'
                          % (energy_mu, energy_sigma, trend_threshold, note,
                             len(calibration.energies),
                             len(calibration.trends)))
                    if args.threshold is None and \
                            len(calibration.trends) < MIN_TREND_SAMPLES:
                        print('警告: trend のベースラインが十分に取れませんでした '
                              '(%d samples)。--calib を長くするか、'
                              '--threshold を指定してください。'
                              % len(calibration.trends), file=sys.stderr)
                continue

            if trend is None:
                direction = DIR_COLLECTING
            else:
                direction = classify(trend, trend_threshold)

            if metrics_writer is not None:
                metrics_writer.writerow([
                    '%.3f' % timestamp, packets,
                    fmt_opt(energy), fmt_opt(smoothed), fmt_opt(trend),
                    direction, rssi, fmt_opt(energy_mu),
                    fmt_opt(energy_sigma), fmt_opt(trend_threshold),
                ])
                metrics_file.flush()

            progress.show(
                '%s  energy=%.2f  trend=%s  rssi=%d  packets=%d'
                % (direction, smoothed,
                   '-' if trend is None else '%+.2f' % trend,
                   rssi, packets))

    except KeyboardInterrupt:
        print('\nstopped: %d packets parsed (%d scored), %d lines skipped, '
              '%d log lines ignored'
              % (packets, scored, skipped, other_lines))
    finally:
        ser.close()
        if csv_file is not None:
            csv_file.close()
        if metrics_file is not None:
            metrics_file.close()

    return 0


if __name__ == '__main__':
    sys.exit(main())
