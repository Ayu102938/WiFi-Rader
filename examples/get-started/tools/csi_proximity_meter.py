#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Modified by Ayu102938
# - 2026-10-24: Created csi_proximity_meter.py for approach/departure detection.
# - 2026-11-19: Moved the CSI front end (line parsing, subcarrier selection,
#   serial handling, read loop, CSV layout) to the shared csi_common module;
#   only the absolute-energy trend stays here.
# - 2026-11-19: Moved the energy/trend state machine to
#   csi_detectors.ProximityMeter so the web UI can drive the same detector; this
#   script now only parses arguments and prints results.
#
# This file is derived from examples/get-started/tools/csi_occupancy_meter.py and
# csi_motion_detector.py.  It shares the CSI front end through csi_common and
# keeps its own CSV layout so the three tools stay comparable, while it looks
# at the *absolute* (unnormalized) energy trend instead of a gain-normalized
# motion score.
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

Requires only ``pyserial`` and ``numpy`` (see requirements.txt).
"""

import argparse
import csv
import sys
import time

from csi_common import (PacketStats, Progress, SerialOpenError, csv_header,
                        iter_csi_packets, open_serial)
from csi_detectors import (DIR_APPROACHING, DIR_AWAY, DIR_CALIBRATING,
                           DIR_STATIONARY, ENERGY_METRICS, MIN_TREND_SAMPLES,
                           ProximityMeter)

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


def show_calibration(progress, detector):
    """Print the rate-limited calibration line (time, energy and trend samples)."""
    elapsed, fraction, samples = detector.calibration_progress()
    progress.show('キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d energy, '
                  '%d trend samples)'
                  % (elapsed, detector.calibration.seconds,
                     int(fraction * 100), samples,
                     len(detector.calibration.trends)))


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
        metrics_writer.writerow(ProximityMeter.columns)
        metrics_file.flush()

    detector = ProximityMeter(
        calib=args.calib, trend_window=args.trend_window,
        ema_alpha=args.ema_alpha, energy_metric=args.energy_metric,
        threshold=args.threshold, subcarriers=args.subcarriers,
        edge_skip=args.edge_skip)
    stats = PacketStats()
    progress = Progress(interval=0.1)

    print('Listening on %s @ %d baud (energy-metric=%s, trend-window=%.1fs, '
          'ema-alpha=%.2f)'
          % (args.port, args.baud, args.energy_metric, args.trend_window,
             args.ema_alpha))
    print('Calibrating an empty room for %.1f s -- keep the room empty.'
          % args.calib)
    print('Directions: %s / %s / %s. Press Ctrl+C to stop.'
          % (DIR_APPROACHING, DIR_AWAY, DIR_STATIONARY))

    try:
        for packet in iter_csi_packets(ser, stats):
            if packet is None:
                if not detector.calibrated:
                    show_calibration(progress, detector)
                continue

            row, amplitude, rssi = packet
            timestamp = time.time()

            if csv_writer is not None:
                if stats.packets == 1:
                    csv_writer.writerow(csv_header(row))
                csv_writer.writerow(row)
                csv_file.flush()

            try:
                metrics = detector.update(amplitude, rssi)
            except argparse.ArgumentTypeError as exc:
                print('\nerror: %s' % exc, file=sys.stderr)
                return 2
            if metrics is None:
                stats.skipped += 1
                continue
            stats.scored += 1

            if metrics['status'] == DIR_CALIBRATING:
                if metrics_writer is not None:
                    metrics_writer.writerow(
                        detector.csv_row(metrics, timestamp, stats.packets, rssi))
                    metrics_file.flush()
                show_calibration(progress, detector)
                if metrics['calibrated_now']:
                    if args.threshold is not None:
                        note = ' [fixed]'
                    elif detector.threshold_fallback:
                        note = ' [fallback]'
                    else:
                        note = ''
                    print('\nキャリブレーション完了: energy mu=%.4f sigma=%.4f / '
                          'trend threshold=%.4f%s (%d energy, %d trend samples)'
                          % (metrics['mu'], metrics['sigma'],
                             metrics['threshold'], note,
                             metrics['calib_samples'], metrics['trend_samples']))
                    if (args.threshold is None
                            and metrics['trend_samples'] < MIN_TREND_SAMPLES):
                        print('警告: trend のベースラインが十分に取れませんでした '
                              '(%d samples)。--calib を長くするか、'
                              '--threshold を指定してください。'
                              % metrics['trend_samples'], file=sys.stderr)
                continue

            if metrics_writer is not None:
                metrics_writer.writerow(
                    detector.csv_row(metrics, timestamp, stats.packets, rssi))
                metrics_file.flush()

            progress.show(
                '%s  energy=%.2f  trend=%s  rssi=%d  packets=%d'
                % (metrics['status'], metrics['smoothed'],
                   '-' if metrics['trend'] is None
                   else '%+.2f' % metrics['trend'],
                   rssi, stats.packets))

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
