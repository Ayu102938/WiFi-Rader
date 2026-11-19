#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Modified by Ayu102938
# - 2026-10-24: Created csi_occupancy_meter.py for 3-level occupancy classification.
# - 2026-11-19: Moved the CSI front end (line parsing, subcarrier selection,
#   amplitude normalization, serial handling, read loop) to the shared
#   csi_common module; only the motion/activity statistics stay here.
# - 2026-11-19: Moved those statistics and the occupancy state machine to
#   csi_detectors.OccupancyMeter so the web UI can drive the same detector; this
#   script now only parses arguments and prints results.
#
# This file is derived from examples/get-started/tools/csi_motion_detector.py.
# The motion score is computed exactly as in that tool so the two stay
# comparable, while this script adds a second "activity" statistic and a
# three-level occupancy state machine.
#
"""Three-level occupancy classification from ESP-CSI serial output.

Reads ``CSI_DATA`` lines from a serial port and derives two independent
statistics from the same gain-normalized amplitude stream:

``motion_score``
    Variance of the normalized amplitude over ``--window`` packets.  This is
    exactly the score used by ``csi_motion_detector.py``: a person moving in the
    propagation path raises it far above the idle baseline.  It is EMA-smoothed
    with ``--ema-alpha`` before being compared against the thresholds.

``activity_score``
    How much the motion score itself fluctuates over the last
    ``--activity-window`` seconds.  A single person moving steadily produces a
    fairly constant motion score, while several people moving independently
    produce a constantly changing one.  Three metrics are available:

    ``cv``     coefficient of variation: std(motion) / mean(motion)
    ``diff``   standard deviation of the first differences of motion
    ``peaks``  local maxima of motion per second

The room must be empty during the first ``--calib`` seconds.  From that window
the script learns the idle motion baseline ``mu``/``sigma`` and the idle
activity baseline, then derives::

    motion_threshold_on  = mu + on_sigma  * sigma
    motion_threshold_off = mu + off_sigma * sigma
    activity_threshold   = max(activity_mu + 3 * activity_sigma, floor)

The activity baseline is only sampled once its buffer covers ~80% of
``--activity-window``, so the calibration statistic has the same span as the
run-time one (a short stub window biases ``peaks``/``diff`` low).
The activity floor is 0.1 for the dimensionless ``cv`` metric and for
``peaks`` (peaks/second).  ``diff`` has motion-score units, where 0.1 is far
above any realistic spread, so it only gets a tiny epsilon guard instead.
Use ``--activity-threshold`` to override the calibrated value.

Tuning notes:

* ``cv`` (default) is scale-free and works best when the empty room has some
  slow drift; a longer ``--activity-window`` (8-10 s) usually tightens the
  baseline, while the default 5 s reacts faster to people entering.
* ``diff`` is in motion-score units, so its absolute threshold depends on the
  link gain and antenna placement.  Expect to override it per setup.
* ``peaks`` only counts local maxima above ``mean + 1 sigma`` of the activity
  window; counting every local maximum would saturate at packet_rate / 3.

Classification:

* ``smoothed_motion < motion_threshold_off``            -> 無人
* otherwise, once ``smoothed_motion >= motion_threshold_on`` was seen -> occupied
* an occupied room is ``1人（小動き）`` while the activity score stays below
  ``activity_threshold`` and ``複数人（ざわざわ）`` while it stays above it.
  Both activity transitions require three consecutive packets on the new side,
  so a single noisy sample cannot flip the label.

Usage::

    # Default: /dev/ttyACM0 @ 921600, 15 s empty-room calibration
    python csi_occupancy_meter.py -p /dev/ttyACM0

    # Shorter windows, explicit activity metric and threshold
    python csi_occupancy_meter.py -p /dev/ttyACM0 -w 200 --activity-window 4 \
        --activity-metric diff --activity-threshold 0.2

    # Record raw CSI packets and per-packet classification metrics
    python csi_occupancy_meter.py -p /dev/ttyACM0 \
        --csv csi_capture.csv --metrics-csv occupancy.csv

Requires only ``pyserial`` and ``numpy`` (see requirements.txt).
"""

import argparse
import csv
import sys
import time

from csi_common import (PacketStats, Progress, SerialOpenError, csv_header,
                        iter_csi_packets, open_serial)
from csi_detectors import (ACTIVITY_METRICS, LEVEL_CALIBRATING,
                           LEVEL_EMPTY, LEVEL_MULTI, LEVEL_SINGLE,
                           OccupancyMeter)

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description='Three-level occupancy classification from ESP-CSI data',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('-p', '--port', default='/dev/ttyACM0',
                        help='serial port of the CSI receiver board')
    parser.add_argument('-b', '--baud', type=int, default=921600,
                        help='serial baud rate')
    parser.add_argument('-w', '--window', type=int, default=300,
                        help='number of packets in the motion sliding window')
    parser.add_argument('--activity-window', type=float, default=5.0,
                        help='seconds of motion scores used for the activity score')
    parser.add_argument('--activity-metric', choices=ACTIVITY_METRICS,
                        default='cv',
                        help='fluctuation metric used for the activity score')
    parser.add_argument('--activity-threshold', type=float, default=None,
                        help='fixed activity threshold; when given, the '
                             'auto-calibrated activity threshold is not used')
    parser.add_argument('--calib', type=float, default=15.0,
                        help='seconds of empty-room packets used to calibrate '
                             'the idle baselines')
    parser.add_argument('--calib-min-packets', type=int, default=20,
                        help='minimum packets required to finish calibration; '
                             'calibration continues past --calib if not reached')
    parser.add_argument('--on-sigma', type=float, default=3.0,
                        help='motion_threshold_on = mu + on_sigma * sigma')
    parser.add_argument('--off-sigma', type=float, default=1.5,
                        help='motion_threshold_off = mu + off_sigma * sigma')
    parser.add_argument('--ema-alpha', type=float, default=0.2,
                        help='EMA smoothing factor for the motion score '
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


def show_calibration(progress, detector, metrics=None):
    """Print the rate-limited calibration line (time, packets, both scores)."""
    elapsed, fraction, samples = detector.calibration_progress()
    extra = ''
    if metrics is not None:
        extra = (', motion=%s, activity=%s'
                 % ('-' if metrics['smoothed'] is None
                    else '%.4f' % metrics['smoothed'],
                    '-' if metrics['activity'] is None
                    else '%.2f' % metrics['activity']))
    progress.show('キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d packets%s)'
                  % (elapsed, detector.calibration.seconds,
                     int(fraction * 100), samples, extra))


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.window < 2:
        print('error: --window must be at least 2', file=sys.stderr)
        return 2
    if args.activity_window <= 0:
        print('error: --activity-window must be positive', file=sys.stderr)
        return 2
    if args.activity_threshold is not None and args.activity_threshold <= 0:
        print('error: --activity-threshold must be positive', file=sys.stderr)
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
    if args.on_sigma < args.off_sigma:
        print('error: --on-sigma must be >= --off-sigma to keep hysteresis',
              file=sys.stderr)
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
        metrics_writer.writerow(OccupancyMeter.columns)
        metrics_file.flush()

    detector = OccupancyMeter(
        window=args.window, calib=args.calib,
        calib_min_packets=args.calib_min_packets, ema_alpha=args.ema_alpha,
        on_sigma=args.on_sigma, off_sigma=args.off_sigma,
        activity_window=args.activity_window,
        activity_metric=args.activity_metric,
        activity_threshold=args.activity_threshold,
        subcarriers=args.subcarriers, edge_skip=args.edge_skip)
    stats = PacketStats()
    progress = Progress()

    print('Listening on %s @ %d baud (window=%d, activity-window=%.1fs, '
          'activity-metric=%s, ema-alpha=%.2f)'
          % (args.port, args.baud, args.window, args.activity_window,
             args.activity_metric, args.ema_alpha))
    print('Calibrating empty-room baselines for %.1f s -- keep the room empty.'
          % args.calib)
    print('Levels: %s / %s / %s. Press Ctrl+C to stop.'
          % (LEVEL_EMPTY, LEVEL_SINGLE, LEVEL_MULTI))

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
            if metrics['score'] is not None:
                stats.scored += 1

            if metrics['status'] == LEVEL_CALIBRATING:
                if metrics_writer is not None:
                    metrics_writer.writerow(
                        detector.csv_row(metrics, timestamp, stats.packets, rssi))
                    metrics_file.flush()
                show_calibration(progress, detector, metrics)
                if metrics['calibrated_now']:
                    print('\nキャリブレーション完了: motion mu=%.4f sigma=%.4f '
                          '-> on=%.4f off=%.4f / activity mu=%.4f sigma=%.4f '
                          '-> threshold=%.4f%s (%d motion, %d activity samples)'
                          % (metrics['mu'], metrics['sigma'],
                             metrics['threshold_on'], metrics['threshold_off'],
                             metrics['activity_mu'], metrics['activity_sigma'],
                             metrics['activity_threshold'],
                             ' [override]' if args.activity_threshold is not None
                             else '',
                             metrics['calib_samples'],
                             metrics['activity_samples']))
                    if (args.activity_threshold is None
                            and metrics['activity_samples'] < 20):
                        print('警告: アクティビティのベースラインが十分に取れませんでした '
                              '(%d samples)。--calib を長くするか、'
                              '--activity-threshold を指定してください。'
                              % metrics['activity_samples'], file=sys.stderr)
                continue

            if metrics['status'] is None:
                progress.show('収集中... (%d/%d packets)'
                              % (metrics['window_fill'], metrics['window_size']))
                continue

            if metrics_writer is not None:
                metrics_writer.writerow(
                    detector.csv_row(metrics, timestamp, stats.packets, rssi))
                metrics_file.flush()

            print('\r%s  motion=%.4f  activity=%.2f  rssi=%4d  packets=%6d'
                  % (metrics['status'], metrics['smoothed'],
                     float('nan') if metrics['activity'] is None
                     else metrics['activity'],
                     rssi, stats.packets),
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
