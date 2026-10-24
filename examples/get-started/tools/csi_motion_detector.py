#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
"""Real-time human presence detection from ESP-CSI serial output.

Reads ``CSI_DATA`` lines from a serial port, converts the raw interleaved
I/Q array into per-subcarrier amplitudes, normalizes each packet by its mean
amplitude, and measures how much those normalized amplitudes fluctuate over a
sliding window of packets.  A person moving in the propagation path disturbs
the channel, so the score rises well above the idle baseline.

The score is scale-free (coefficient-of-variation style): because every packet
is divided by its own mean amplitude, transmitter/receiver gain changes and
RSSI drift no longer inflate it.  The detector self-calibrates on the first
``--calib`` seconds of traffic, then applies hysteresis around
``mu + 3*sigma`` / ``mu + 1.5*sigma`` to decide between 有人 and 無人.

Usage::

    # Default port and baud rate; auto-calibrates for 15 seconds
    python csi_motion_detector.py -p /dev/ttyACM0

    # Shorter window and calibration, keep all subcarriers
    python csi_motion_detector.py -p /dev/ttyACM0 -w 300 --calib 10 --edge-skip 0

    # Only use a subset of subcarriers (indexes are per-packet, 0-based)
    python csi_motion_detector.py -p /dev/ttyACM0 --subcarriers 5-25,35-55

    # Record raw CSI packets and per-packet detection metrics
    python csi_motion_detector.py -p /dev/ttyACM0 \
        --csv csi_capture.csv --metrics-csv metrics.csv

Keep the room empty during calibration: the collected mean/variance is the
idle baseline used to derive the detection thresholds.  Walk between TX and RX
afterwards to trigger detection.

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
    'timestamp', 'packet_num', 'raw_score', 'smoothed_score', 'status',
    'rssi', 'baseline_mu', 'baseline_sigma', 'threshold_on', 'threshold_off',
]

STATUS_EMPTY = '無人'
STATUS_OCCUPIED = '有人'
STATUS_CALIBRATING = 'CALIBRATING'


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
    """Choose the subcarrier indexes used for the score.

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


def normalize_amplitude(amplitude, index):
    """Divide the used subcarriers by their mean amplitude.

    This makes the metric independent of the link gain, so a gain or RSSI
    change alone cannot be mistaken for motion.  Returns ``None`` when the
    used subcarriers have zero (or non-finite) average magnitude.
    """
    used = amplitude[index]
    mean = float(np.mean(used))
    if not np.isfinite(mean) or mean <= 0.0:
        return None
    normalized = used / mean
    if not np.all(np.isfinite(normalized)):
        return None
    return normalized


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description='Real-time human presence detection from ESP-CSI data',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-p', '--port', default='/dev/ttyACM0',
                        help='serial port of the CSI receiver board')
    parser.add_argument('-b', '--baud', type=int, default=921600,
                        help='serial baud rate')
    parser.add_argument('-w', '--window', type=int, default=300,
                        help='number of packets in the sliding window')
    parser.add_argument('-t', '--threshold', type=float, default=None,
                        help='fixed score threshold; when given, auto-calibration '
                             'is skipped and this value is used for both '
                             'threshold_on and threshold_off')
    parser.add_argument('--calib', type=float, default=15.0,
                        help='seconds of packets used to calibrate the idle baseline')
    parser.add_argument('--calib-min-packets', type=int, default=20,
                        help='minimum packets required to finish calibration; '
                             'calibration continues past --calib if not reached')
    parser.add_argument('--ema-alpha', type=float, default=0.2,
                        help='EMA smoothing factor for the raw score (0 < alpha <= 1)')
    parser.add_argument('--on-sigma', type=float, default=3.0,
                        help='threshold_on = mu + on_sigma * sigma')
    parser.add_argument('--off-sigma', type=float, default=1.5,
                        help='threshold_off = mu + off_sigma * sigma')
    parser.add_argument('--edge-skip', type=int, default=10,
                        help='subcarriers to skip at each edge (guard/DC/null); '
                             '0 uses all subcarriers')
    parser.add_argument('--subcarriers', default=None,
                        help='subcarrier indexes/ranges to use, e.g. "5-25,35-55" '
                             '(overrides --edge-skip; default: all minus edges)')
    parser.add_argument('--csv', default=None,
                        help='optional path to write raw CSI packets as CSV')
    parser.add_argument('--metrics-csv', default=None,
                        help='optional path to write per-packet detection metrics as CSV')
    return parser


class Calibration:
    """Collects idle scores and derives hysteresis thresholds from them."""

    def __init__(self, seconds, min_packets):
        self.seconds = seconds
        self.min_packets = min_packets
        self.scores = []
        self.start = None

    def start_now(self):
        if self.start is None:
            self.start = time.monotonic()

    def add(self, score):
        self.start_now()
        self.scores.append(score)

    def progress(self):
        """Return ``(elapsed, fraction)`` for the calibration progress line."""
        elapsed = 0.0 if self.start is None else time.monotonic() - self.start
        fraction = 0.0
        if self.seconds > 0:
            fraction = min(1.0, elapsed / self.seconds)
        return elapsed, fraction

    def done(self):
        if self.start is None:
            return False
        elapsed = time.monotonic() - self.start
        return elapsed >= self.seconds and len(self.scores) >= self.min_packets

    def result(self, on_sigma, off_sigma):
        """Return ``(mu, sigma, threshold_on, threshold_off)``.

        The smoothed score is used as the statistic, so sigma reflects the
        noise of the value that is actually compared against the thresholds.
        A floor keeps thresholds above the baseline in degenerate cases
        (a perfectly still window would otherwise give sigma == 0).
        """
        arr = np.asarray(self.scores, dtype=np.float64)
        mu = float(arr.mean()) if arr.size else 0.0
        sigma = float(arr.std()) if arr.size else 0.0
        # Guard against a zero/near-zero sigma: assume at least 2% relative jitter.
        sigma = max(sigma, abs(mu) * 0.02, 1e-6)
        return mu, sigma, mu + on_sigma * sigma, mu + off_sigma * sigma


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


class Progress:
    """Rate-limited ``\r`` progress line so a piped log does not explode."""

    def __init__(self, interval=0.2):
        self.interval = interval
        self.last = 0.0

    def show(self, text):
        now = time.monotonic()
        if now - self.last < self.interval:
            return
        self.last = now
        print('\r' + text, end='', flush=True)


def fmt_opt(value):
    """Format a float for CSV, or an empty field when it is undefined."""
    if value is None or not np.isfinite(value):
        return ''
    return '%.6f' % value


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.window < 2:
        print('error: --window must be at least 2', file=sys.stderr)
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
    if args.calib_min_packets < 1:
        print('error: --calib-min-packets must be at least 1', file=sys.stderr)
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

    window = deque(maxlen=args.window)
    window_len = None
    index_cache = {}
    packets = 0
    scored = 0
    skipped = 0
    other_lines = 0

    ema = Ema(args.ema_alpha)
    calibration = Calibration(args.calib, args.calib_min_packets)
    progress = Progress()
    calibrated = args.threshold is not None
    if calibrated:
        mu = sigma = float('nan')
        threshold_on = threshold_off = args.threshold
    else:
        mu = sigma = 0.0
        threshold_on = threshold_off = 0.0

    occupied = False

    print('Listening on %s @ %d baud (window=%d, edge-skip=%d, ema-alpha=%.2f)'
          % (args.port, args.baud, args.window, args.edge_skip, args.ema_alpha))
    if calibrated:
        print('Fixed threshold: %.3f (auto-calibration disabled)' % args.threshold)
    else:
        print('Calibrating idle baseline for %.1f s -- keep the room empty.'
              % args.calib)
    print('Walk between TX and RX to trigger detection. Press Ctrl+C to stop.')

    try:
        while True:
            line = ser.readline()
            if not line:
                if not calibrated:
                    elapsed, fraction = calibration.progress()
                    progress.show(
                        'キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d packets)'
                        % (elapsed, calibration.seconds, int(fraction * 100),
                           len(calibration.scores)))
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

            if csv_writer is not None:
                if packets == 1:
                    csv_writer.writerow(
                        DATA_COLUMNS_NAMES if len(row) == len(DATA_COLUMNS_NAMES)
                        else DATA_COLUMNS_NAMES_C5C6)
                csv_writer.writerow(row)
                csv_file.flush()

            # A changed subcarrier count means the radio re-tuned; restart.
            if window_len is not None and amplitude.shape[0] != window_len:
                window.clear()
            window_len = amplitude.shape[0]

            if window_len not in index_cache:
                try:
                    index_cache[window_len] = select_subcarriers(
                        window_len, args.subcarriers, args.edge_skip)
                except argparse.ArgumentTypeError as exc:
                    print('\nerror: %s' % exc, file=sys.stderr)
                    return 2
            index = index_cache[window_len]

            normalized = normalize_amplitude(amplitude, index)
            if normalized is None:
                skipped += 1
                continue
            window.append(normalized)

            raw_score = None
            if len(window) >= 2:
                stack = np.asarray(window)
                variance = stack.var(axis=0)
                # Scale-free score: variance of the gain-normalized amplitude,
                # averaged over the selected subcarriers.
                raw_score = float(variance.mean())
                ema.update(raw_score)
                scored += 1

            if not calibrated:
                # The idle clock starts with the first usable score, so the
                # displayed progress matches wall-clock observation time.
                if raw_score is not None:
                    calibration.start_now()
                    # Only steady-state (full-window) scores feed the baseline;
                    # short warm-up windows have inflated variance and would
                    # bias the thresholds upward.
                    if len(window) >= args.window:
                        calibration.add(ema.value)
                elapsed, fraction = calibration.progress()
                if metrics_writer is not None:
                    metrics_writer.writerow([
                        '%.3f' % timestamp, packets,
                        '' if raw_score is None else '%.6f' % raw_score,
                        '' if ema.value is None else '%.6f' % ema.value,
                        STATUS_CALIBRATING, rssi,
                        '', '', '', '',
                    ])
                    metrics_file.flush()
                progress.show(
                    'キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d packets, '
                    'score=%s)'
                    % (elapsed, calibration.seconds, int(fraction * 100),
                       len(calibration.scores),
                       '-' if ema.value is None else '%.4f' % ema.value))
                if calibration.done():
                    mu, sigma, threshold_on, threshold_off = calibration.result(
                        args.on_sigma, args.off_sigma)
                    calibrated = True
                    print('\nキャリブレーション完了: baseline mu=%.4f sigma=%.4f '
                          '-> on=%.4f off=%.4f (%d packets)'
                          % (mu, sigma, threshold_on, threshold_off,
                             len(calibration.scores)))
                continue

            if raw_score is None:
                progress.show('収集中... (%d/%d packets)'
                              % (len(window), args.window))
                continue

            smoothed = ema.value

            # Hysteresis: enter on the high threshold, leave on the low one.
            if occupied:
                if smoothed <= threshold_off:
                    occupied = False
            else:
                if smoothed >= threshold_on:
                    occupied = True
            status = STATUS_OCCUPIED if occupied else STATUS_EMPTY

            if metrics_writer is not None:
                metrics_writer.writerow([
                    '%.3f' % timestamp, packets,
                    '%.6f' % raw_score, '%.6f' % smoothed, status, rssi,
                    fmt_opt(mu), fmt_opt(sigma),
                    '%.6f' % threshold_on, '%.6f' % threshold_off,
                ])
                metrics_file.flush()

            print('\r%s  score=%8.4f  on=%7.4f  off=%7.4f  ema=%8.4f  '
                  'rssi=%4d  packets=%6d  skipped=%5d'
                  % (status, raw_score, threshold_on, threshold_off, smoothed,
                     rssi, packets, skipped + other_lines),
                  end='', flush=True)

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
