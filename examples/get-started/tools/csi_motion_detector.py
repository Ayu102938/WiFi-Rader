#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Modified by Ayu102938
#
# This file is derived from examples/get-started/tools/csi_data_read_parse.py.
# Changes:
# - 2026-10-24: Created a GUI-free real-time presence detector.
# - Added per-packet amplitude normalization, coefficient-of-variation style score,
#   auto-calibration, EMA smoothing, hysteresis, and per-packet metrics CSV output.
# - 2026-11-19: Moved line parsing, subcarrier selection, amplitude normalization,
#   serial handling and the read loop to the shared csi_common module.
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

Requires only ``pyserial`` and ``numpy`` (see requirements.txt).
"""

import argparse
import csv
import sys
import time
from collections import deque

import numpy as np

from csi_common import (Ema, PacketStats, Progress, SerialOpenError,
                        SubcarrierSelector, csv_header, fmt_opt,
                        iter_csi_packets, normalize_amplitude, open_serial)

METRICS_COLUMNS = [
    'timestamp', 'packet_num', 'raw_score', 'smoothed_score', 'status',
    'rssi', 'baseline_mu', 'baseline_sigma', 'threshold_on', 'threshold_off',
]

STATUS_EMPTY = '無人'
STATUS_OCCUPIED = '有人'
STATUS_CALIBRATING = 'CALIBRATING'


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
        ser = open_serial(args.port, args.baud)
    except SerialOpenError as exc:
        print('error: %s' % exc, file=sys.stderr)
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
    selector = SubcarrierSelector(args.subcarriers, args.edge_skip)
    stats = PacketStats()

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
        for packet in iter_csi_packets(ser, stats):
            if packet is None:
                if not calibrated:
                    elapsed, fraction = calibration.progress()
                    progress.show(
                        'キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d packets)'
                        % (elapsed, calibration.seconds, int(fraction * 100),
                           len(calibration.scores)))
                continue

            row, amplitude, rssi = packet
            timestamp = time.time()

            if csv_writer is not None:
                if stats.packets == 1:
                    csv_writer.writerow(csv_header(row))
                csv_writer.writerow(row)
                csv_file.flush()

            try:
                index = selector.indexes(amplitude.shape[0])
            except argparse.ArgumentTypeError as exc:
                print('\nerror: %s' % exc, file=sys.stderr)
                return 2
            # A changed subcarrier count means the radio re-tuned; restart.
            if selector.changed:
                window.clear()

            normalized = normalize_amplitude(amplitude, index)
            if normalized is None:
                stats.skipped += 1
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
                stats.scored += 1

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
                        '%.3f' % timestamp, stats.packets,
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
                    '%.3f' % timestamp, stats.packets,
                    '%.6f' % raw_score, '%.6f' % smoothed, status, rssi,
                    fmt_opt(mu), fmt_opt(sigma),
                    '%.6f' % threshold_on, '%.6f' % threshold_off,
                ])
                metrics_file.flush()

            print('\r%s  score=%8.4f  on=%7.4f  off=%7.4f  ema=%8.4f  '
                  'rssi=%4d  packets=%6d  skipped=%5d'
                  % (status, raw_score, threshold_on, threshold_off, smoothed,
                     rssi, stats.packets,
                     stats.skipped + stats.other_lines),
                  end='', flush=True)

    except KeyboardInterrupt:
        print('\nstopped: %s' % stats.summary())
    finally:
        ser.close()
        if csv_file is not None:
            csv_file.close()
        if metrics_file is not None:
            metrics_file.close()

    return 0


if __name__ == '__main__':
    sys.exit(main())
