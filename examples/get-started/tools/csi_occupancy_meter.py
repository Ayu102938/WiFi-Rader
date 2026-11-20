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
from collections import deque

import numpy as np

from csi_common import (Ema, PacketStats, Progress, SerialOpenError,
                        SubcarrierSelector, csv_header, fmt_opt,
                        iter_csi_packets, normalize_amplitude, open_serial)

METRICS_COLUMNS = [
    'timestamp', 'packet_num', 'motion_score', 'smoothed_motion',
    'activity_score', 'activity_metric', 'occupancy_level', 'rssi',
    'baseline_mu', 'baseline_sigma', 'motion_threshold_on',
    'motion_threshold_off', 'activity_threshold',
]

LEVEL_EMPTY = '無人'
LEVEL_SINGLE = '1人（小動き）'
LEVEL_MULTI = '複数人（ざわざわ）'
LEVEL_CALIBRATING = 'CALIBRATING'

ACTIVITY_METRICS = ('cv', 'diff', 'peaks')

# Activity levels flip only after this many consecutive packets on one side.
ACTIVITY_HYSTERESIS_PACKETS = 3

# Floor for the auto-calibrated activity threshold: prevents a perfectly still
# empty room (activity_sigma == 0) from classifying every packet as 複数人.
# ``cv`` is dimensionless and ``peaks`` is in peaks/second, so 0.1 is a sensible
# floor for both.  ``diff`` has motion-score units, where 0.1 would be far above
# any real first-difference spread, so it only needs a tiny epsilon guard.
ACTIVITY_THRESHOLD_FLOORS = {
    'cv': 0.1,
    'diff': 1e-9,
    'peaks': 0.1,
}

# Baseline activity is only sampled once the activity buffer is nearly full,
# otherwise the statistic is measured over a shorter span than the one used at
# run time (this biases ``peaks`` and ``diff`` low, sometimes to zero).
MIN_ACTIVITY_SAMPLES = 5
BASELINE_ACTIVITY_WINDOW_FRACTION = 0.8


def compute_activity(samples, metric, window_seconds):
    """Compute the activity score from ``(monotonic_time, motion)`` samples.

    ``samples`` is an oldest-first sequence already trimmed to the activity
    window.  Returns ``None`` while there is not enough history for the chosen
    metric, so callers can hold the previous classification.
    """
    if len(samples) < MIN_ACTIVITY_SAMPLES:
        return None

    values = np.asarray([value for _, value in samples], dtype=np.float64)
    if not np.all(np.isfinite(values)):
        return None

    span = float(samples[-1][0] - samples[0][0])

    if metric == 'cv':
        mean = float(values.mean())
        if mean <= 1e-9:
            return None
        return float(values.std()) / mean

    if metric == 'diff':
        if values.size < 2:
            return None
        return float(np.diff(values).std())

    if metric == 'peaks':
        if values.size < 3:
            return None
        # Strict local maxima, but only "significant" ones: without the
        # prominence guard every 2-sample wiggle counts and the metric
        # saturates at sampling rate / 3 regardless of real activity.
        mean = float(values.mean())
        level = mean + float(values.std())
        middle = values[1:-1]
        local = (middle > values[:-2]) & (middle > values[2:]) & (middle >= level)
        peaks = int(np.count_nonzero(local))
        duration = span if span > 1e-6 else max(window_seconds, 1e-6)
        return peaks / duration

    return None


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


class Calibration:
    """Collects empty-room motion and activity scores and derives thresholds."""

    def __init__(self, seconds, min_packets):
        self.seconds = seconds
        self.min_packets = min_packets
        self.scores = []
        self.activity_scores = []
        self.start = None

    def start_now(self):
        if self.start is None:
            self.start = time.monotonic()

    def add(self, score):
        self.start_now()
        self.scores.append(score)

    def add_activity(self, score):
        self.activity_scores.append(score)

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

    def motion_result(self, on_sigma, off_sigma):
        """Return ``(mu, sigma, threshold_on, threshold_off)`` for motion.

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

    def activity_result(self, metric='cv'):
        """Return ``(mu, sigma, threshold)`` for the activity score.

        The floor depends on the metric: ``cv`` and ``peaks`` are floored at
        0.1, while ``diff`` lives in motion-score units and only gets a tiny
        epsilon guard so a degenerate baseline cannot force 複数人.
        """
        arr = np.asarray(self.activity_scores, dtype=np.float64)
        mu = float(arr.mean()) if arr.size else 0.0
        sigma = float(arr.std()) if arr.size else 0.0
        floor = ACTIVITY_THRESHOLD_FLOORS.get(metric, 0.1)
        threshold = max(mu + 3.0 * sigma, floor)
        return mu, sigma, threshold


def activity_window_ready(samples, window_seconds):
    """True once the activity buffer covers most of the activity window."""
    if len(samples) < MIN_ACTIVITY_SAMPLES:
        return False
    span = samples[-1][0] - samples[0][0]
    return span >= BASELINE_ACTIVITY_WINDOW_FRACTION * window_seconds


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
        metrics_writer.writerow(METRICS_COLUMNS)
        metrics_file.flush()

    window = deque(maxlen=args.window)
    # Timestamped motion scores; bounded so a long run cannot grow the buffer.
    activity = deque(maxlen=max(64, int(args.activity_window * 1000)))
    selector = SubcarrierSelector(args.subcarriers, args.edge_skip)
    stats = PacketStats()

    ema = Ema(args.ema_alpha)
    calibration = Calibration(args.calib, args.calib_min_packets)
    progress = Progress()

    mu = sigma = float('nan')
    threshold_on = threshold_off = float('nan')
    activity_mu = activity_sigma = float('nan')
    activity_threshold = args.activity_threshold
    calibrated = False

    occupied = False
    activity_high = False
    above_count = 0
    below_count = 0

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
                if not calibrated:
                    elapsed, fraction = calibration.progress()
                    progress.show(
                        'キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d packets)'
                        % (elapsed, calibration.seconds, int(fraction * 100),
                           len(calibration.scores)))
                continue

            row, amplitude, rssi = packet
            timestamp = time.time()
            now = time.monotonic()

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
                activity.clear()

            normalized = normalize_amplitude(amplitude, index)
            if normalized is None:
                stats.skipped += 1
                continue
            window.append(normalized)

            motion_score = None
            if len(window) >= 2:
                stack = np.asarray(window)
                variance = stack.var(axis=0)
                # Scale-free score: variance of the gain-normalized amplitude,
                # averaged over the selected subcarriers.
                motion_score = float(variance.mean())
                ema.update(motion_score)
                stats.scored += 1

            # The activity statistic is only meaningful on steady-state
            # windows; a partially filled window has inflated variance.
            steady = motion_score is not None and len(window) >= args.window
            if steady:
                activity.append((now, motion_score))
                while activity and now - activity[0][0] > args.activity_window:
                    activity.popleft()

            activity_score = None
            if steady and activity_window_ready(activity, args.activity_window):
                activity_score = compute_activity(
                    activity, args.activity_metric, args.activity_window)

            smoothed = ema.value

            if not calibrated:
                # The idle clock starts with the first usable score, so the
                # displayed progress matches wall-clock observation time.
                if smoothed is not None:
                    calibration.start_now()
                    # Only steady-state (full-window) scores feed the baseline;
                    # short warm-up windows have inflated variance and would
                    # bias the thresholds upward.
                    if steady:
                        calibration.add(smoothed)
                        if activity_score is not None:
                            calibration.add_activity(activity_score)
                elapsed, fraction = calibration.progress()

                if metrics_writer is not None:
                    metrics_writer.writerow([
                        '%.3f' % timestamp, stats.packets,
                        fmt_opt(motion_score), fmt_opt(smoothed),
                        fmt_opt(activity_score), args.activity_metric,
                        LEVEL_CALIBRATING, rssi,
                        '', '', '', '',
                        fmt_opt(args.activity_threshold),
                    ])
                    metrics_file.flush()

                progress.show(
                    'キャリブレーション中... %4.1f/%4.1fs (%3d%%, %d packets, '
                    'motion=%s, activity=%s)'
                    % (elapsed, calibration.seconds, int(fraction * 100),
                       len(calibration.scores),
                       '-' if smoothed is None else '%.4f' % smoothed,
                       '-' if activity_score is None else '%.2f' % activity_score))

                if calibration.done():
                    mu, sigma, threshold_on, threshold_off = \
                        calibration.motion_result(args.on_sigma, args.off_sigma)
                    activity_mu, activity_sigma, auto_activity = \
                        calibration.activity_result(args.activity_metric)
                    if args.activity_threshold is None:
                        activity_threshold = auto_activity
                        threshold_note = ''
                    else:
                        threshold_note = ' [override]'
                    calibrated = True
                    print('\nキャリブレーション完了: motion mu=%.4f sigma=%.4f '
                          '-> on=%.4f off=%.4f / activity mu=%.4f sigma=%.4f '
                          '-> threshold=%.4f%s (%d motion, %d activity samples)'
                          % (mu, sigma, threshold_on, threshold_off,
                             activity_mu, activity_sigma, activity_threshold,
                             threshold_note,
                             len(calibration.scores),
                             len(calibration.activity_scores)))
                    if (args.activity_threshold is None
                            and len(calibration.activity_scores) < 20):
                        print('警告: アクティビティのベースラインが十分に取れませんでした '
                              '(%d samples)。--calib を長くするか、'
                              '--activity-threshold を指定してください。'
                              % len(calibration.activity_scores),
                              file=sys.stderr)
                continue

            if smoothed is None:
                progress.show('収集中... (%d/%d packets)'
                              % (len(window), args.window))
                continue

            # Hysteresis: enter on the high threshold, leave on the low one.
            if occupied:
                if smoothed < threshold_off:
                    occupied = False
            else:
                if smoothed >= threshold_on:
                    occupied = True

            if not occupied:
                level = LEVEL_EMPTY
                activity_high = False
                above_count = below_count = 0
            else:
                # Activity level hysteresis: require several consecutive
                # packets on the new side before switching the label.
                if activity_score is None:
                    # Not enough activity history yet; keep the current level.
                    pass
                elif activity_score >= activity_threshold:
                    above_count += 1
                    below_count = 0
                    if above_count >= ACTIVITY_HYSTERESIS_PACKETS:
                        activity_high = True
                else:
                    below_count += 1
                    above_count = 0
                    if below_count >= ACTIVITY_HYSTERESIS_PACKETS:
                        activity_high = False
                level = LEVEL_MULTI if activity_high else LEVEL_SINGLE

            if metrics_writer is not None:
                metrics_writer.writerow([
                    '%.3f' % timestamp, stats.packets,
                    fmt_opt(motion_score), fmt_opt(smoothed),
                    fmt_opt(activity_score), args.activity_metric, level, rssi,
                    fmt_opt(mu), fmt_opt(sigma),
                    fmt_opt(threshold_on), fmt_opt(threshold_off),
                    fmt_opt(activity_threshold),
                ])
                metrics_file.flush()

            print('\r%s  motion=%.4f  activity=%.2f  rssi=%4d  packets=%6d'
                  % (level, smoothed,
                     float('nan') if activity_score is None else activity_score,
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
