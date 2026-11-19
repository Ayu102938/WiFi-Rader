#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Modified by Ayu102938
# - 2026-11-19: Created csi_detectors.py.  The per-packet state machines of
#   csi_motion_detector.py, csi_occupancy_meter.py and csi_proximity_meter.py
#   had been written inside their main() loops, so nothing but the terminal
#   tools could reuse them.  They now live here as classes, which is what lets
#   csi_web_server.py run the exact same detection logic.
#
"""Per-packet CSI state machines behind the serial tools and the web UI.

``csi_common`` turns one serial line into amplitudes.  This module is the layer
above it: it keeps the sliding window, the empty-room calibration and the
hysteresis for each detector, and reports one dict of metrics per packet.

Three detectors, one interface:

``MotionDetector``     有人 / 無人 from the variance of the normalized amplitude
``OccupancyMeter``     無人 / 1人 / 複数人, adding how often that variance moves
``ProximityMeter``     approaching / receding / still, from absolute energy

``update(amplitude, rssi, now)`` returns the metrics of that packet, or ``None``
when the packet cannot be used (for example an all-zero amplitude vector).
Callers count those themselves.  Metrics dict keys shared by all detectors:

``status``
    The classification label, ``None`` while the detector is still collecting
    enough packets after calibration, or the tool's own "CALIBRATING" label.
``score`` / ``smoothed``
    The per-packet statistic and its EMA, in that tool's units.
``calibrating`` / ``calibrated_now``
    Whether the baseline is still being learned, and whether this very packet
    finished it (the caller prints the summary line once).
``calib_elapsed`` / ``calib_fraction`` / ``calib_samples``
    Progress of the empty-room calibration.
``mu`` / ``sigma``
    The learned baseline the thresholds were derived from.

Tool-specific additions are documented on each class.  ``csv_row`` renders the
same per-packet columns the tools have always written; ``columns`` names them.
"""

import time
from collections import deque

import numpy as np

from csi_common import Ema, SubcarrierSelector, fmt_opt, normalize_amplitude

# Motion detector thresholds ------------------------------------------------

STATUS_EMPTY = '無人'
STATUS_OCCUPIED = '有人'
STATUS_CALIBRATING = 'CALIBRATING'

# Occupancy meter -----------------------------------------------------------

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

# Proximity meter -----------------------------------------------------------

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


class ScoreCalibration:
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

    def sample_count(self):
        """Return how many idle scores have been collected so far."""
        return len(self.scores)

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


class OccupancyCalibration(ScoreCalibration):
    """Adds the empty-room activity baseline to :class:`ScoreCalibration`."""

    def __init__(self, seconds, min_packets):
        super().__init__(seconds, min_packets)
        self.activity_scores = []

    def add_activity(self, score):
        self.activity_scores.append(score)

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


class EnergyCalibration:
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

    def sample_count(self):
        """Return how many idle energy samples have been collected so far."""
        return len(self.energies)

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


def activity_window_ready(samples, window_seconds):
    """True once the activity buffer covers most of the activity window."""
    if len(samples) < MIN_ACTIVITY_SAMPLES:
        return False
    span = samples[-1][0] - samples[0][0]
    return span >= BASELINE_ACTIVITY_WINDOW_FRACTION * window_seconds


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


class BaseDetector:
    """Subcarrier selection shared by every detector.

    The selection is cached per subcarrier count.  A changed count means the
    radio re-tuned, so the statistics derived from the previous frequency
    response are dropped through :meth:`reset`.
    """

    tool = ''
    columns = ()

    def __init__(self, subcarriers=None, edge_skip=10):
        self.selector = SubcarrierSelector(subcarriers, edge_skip)
        # Normalized amplitude of the last packet, for live displays.
        self.last_normalized = None

    def indexes(self, amplitude):
        """Return the subcarrier indexes, resetting on a re-tune."""
        index = self.selector.indexes(amplitude.shape[0])
        if self.selector.changed:
            self.reset()
        return index

    def reset(self):
        """Drop everything derived from the previous subcarrier set."""

    def calibration_progress(self):
        """Return ``(elapsed, fraction, samples)`` for progress displays."""
        elapsed, fraction = self.calibration.progress()
        return elapsed, fraction, self.calibration.sample_count()

    def update(self, amplitude, rssi=0, now=None):
        """Return the metrics of one packet, or ``None`` if it is unusable."""
        raise NotImplementedError

    def csv_row(self, metrics, timestamp, packet_num, rssi):
        """Render one row for the ``--metrics-csv`` file."""
        raise NotImplementedError


class MotionDetector(BaseDetector):
    """Presence detection from the variance of the normalized amplitude.

    ``score`` is the variance of the gain-normalized amplitude over
    ``window`` packets, averaged over the selected subcarriers.  Because every
    packet is divided by its own mean amplitude, transmitter/receiver gain
    changes and RSSI drift cannot inflate it.

    The first ``calib`` seconds of traffic are treated as an empty room; their
    score distribution gives ``mu``/``sigma``, and hysteresis around
    ``mu + on_sigma*sigma`` / ``mu + off_sigma*sigma`` decides between
    有人 and 無人.  Passing ``threshold`` skips calibration entirely.

    Extra metrics beyond the shared keys: none.
    """

    tool = 'motion'
    columns = ['timestamp', 'packet_num', 'raw_score', 'smoothed_score',
               'status', 'rssi', 'baseline_mu', 'baseline_sigma',
               'threshold_on', 'threshold_off']

    def __init__(self, window=300, calib=15.0, calib_min_packets=20,
                 ema_alpha=0.2, on_sigma=3.0, off_sigma=1.5, threshold=None,
                 subcarriers=None, edge_skip=10):
        super().__init__(subcarriers, edge_skip)
        self.window = window
        self.ema = Ema(ema_alpha)
        self.calibration = ScoreCalibration(calib, calib_min_packets)
        self.on_sigma = on_sigma
        self.off_sigma = off_sigma
        self.fixed_threshold = threshold
        self.samples = deque(maxlen=window)
        self.occupied = False
        self.calibrated = threshold is not None
        if self.calibrated:
            self.mu = self.sigma = float('nan')
            self.threshold_on = self.threshold_off = threshold
        else:
            self.mu = self.sigma = 0.0
            self.threshold_on = self.threshold_off = 0.0

    def reset(self):
        self.samples.clear()

    def update(self, amplitude, rssi=0, now=None):
        index = self.indexes(amplitude)
        normalized = normalize_amplitude(amplitude, index)
        if normalized is None:
            return None
        self.last_normalized = normalized
        self.samples.append(normalized)

        raw_score = None
        if len(self.samples) >= 2:
            variance = np.asarray(self.samples).var(axis=0)
            # Scale-free score: variance of the gain-normalized amplitude,
            # averaged over the selected subcarriers.
            raw_score = float(variance.mean())
            self.ema.update(raw_score)

        metrics = {
            'score': raw_score,
            'smoothed': self.ema.value,
            'threshold_on': self.threshold_on,
            'threshold_off': self.threshold_off,
            'mu': self.mu,
            'sigma': self.sigma,
            'status': None,
            'calibrating': not self.calibrated,
            'calibrated_now': False,
            'calib_elapsed': 0.0,
            'calib_fraction': 1.0,
            'calib_samples': len(self.calibration.scores),
            'window_fill': len(self.samples),
            'window_size': self.window,
        }

        if not self.calibrated:
            self._collect_baseline(raw_score, metrics)
            return metrics

        if raw_score is None:
            progress = min(1.0, len(self.samples) / float(self.window))
            metrics['calib_fraction'] = progress
            return metrics

        # Hysteresis: enter on the high threshold, leave on the low one.
        smoothed = self.ema.value
        if self.occupied:
            if smoothed <= self.threshold_off:
                self.occupied = False
        else:
            if smoothed >= self.threshold_on:
                self.occupied = True
        metrics['status'] = STATUS_OCCUPIED if self.occupied else STATUS_EMPTY
        return metrics

    def _collect_baseline(self, raw_score, metrics):
        metrics['status'] = STATUS_CALIBRATING
        # The idle clock starts with the first usable score, so the displayed
        # progress matches wall-clock observation time.
        if raw_score is not None:
            self.calibration.start_now()
            # Only steady-state (full-window) scores feed the baseline; short
            # warm-up windows have inflated variance and would bias the
            # thresholds upward.
            if len(self.samples) >= self.window:
                self.calibration.add(self.ema.value)
        elapsed, fraction = self.calibration.progress()
        metrics['calib_elapsed'] = elapsed
        metrics['calib_fraction'] = fraction
        metrics['calib_samples'] = len(self.calibration.scores)
        if self.calibration.done():
            (self.mu, self.sigma, self.threshold_on,
             self.threshold_off) = self.calibration.result(
                 self.on_sigma, self.off_sigma)
            self.calibrated = True
            metrics.update(mu=self.mu, sigma=self.sigma,
                           threshold_on=self.threshold_on,
                           threshold_off=self.threshold_off,
                           calibrated_now=True, calib_fraction=1.0)

    def csv_row(self, metrics, timestamp, packet_num, rssi):
        head = ['%.3f' % timestamp, packet_num,
                fmt_opt(metrics['score']), fmt_opt(metrics['smoothed'])]
        if metrics['status'] == STATUS_CALIBRATING:
            return head + [STATUS_CALIBRATING, rssi, '', '', '', '']
        return head + [metrics['status'], rssi, fmt_opt(metrics['mu']),
                       fmt_opt(metrics['sigma']),
                       '%.6f' % metrics['threshold_on'],
                       '%.6f' % metrics['threshold_off']]


class OccupancyMeter(BaseDetector):
    """Three-level occupancy from motion magnitude and motion frequency.

    ``score``/``smoothed`` are the motion score of :class:`MotionDetector`.
    On top of it, ``activity`` measures how much that score itself fluctuates
    over ``activity_window`` seconds; a steady single person keeps the score
    fairly constant, several people keep changing it.

    An occupied room is 1人（小動き） while the activity score stays below
    ``activity_threshold`` and 複数人（ざわざわ） while it stays above it.  Both
    activity transitions require ``ACTIVITY_HYSTERESIS_PACKETS`` consecutive
    packets on the new side, so one noisy sample cannot flip the label.

    Extra metrics beyond the shared keys: ``activity``, ``activity_metric``,
    ``activity_threshold``, ``activity_mu``, ``activity_sigma``.
    """

    tool = 'occupancy'
    columns = ['timestamp', 'packet_num', 'motion_score', 'smoothed_motion',
               'activity_score', 'activity_metric', 'occupancy_level', 'rssi',
               'baseline_mu', 'baseline_sigma', 'motion_threshold_on',
               'motion_threshold_off', 'activity_threshold']

    def __init__(self, window=300, calib=15.0, calib_min_packets=20,
                 ema_alpha=0.2, on_sigma=3.0, off_sigma=1.5,
                 activity_window=5.0, activity_metric='cv',
                 activity_threshold=None, subcarriers=None, edge_skip=10):
        super().__init__(subcarriers, edge_skip)
        self.window = window
        self.ema = Ema(ema_alpha)
        self.calibration = OccupancyCalibration(calib, calib_min_packets)
        self.on_sigma = on_sigma
        self.off_sigma = off_sigma
        self.activity_window = activity_window
        self.activity_metric = activity_metric
        self.samples = deque(maxlen=window)
        # Timestamped motion scores; bounded so a long run cannot grow it.
        self.activity_samples = deque(maxlen=max(64, int(activity_window * 1000)))
        self.mu = self.sigma = float('nan')
        self.threshold_on = self.threshold_off = float('nan')
        self.activity_mu = self.activity_sigma = float('nan')
        self.activity_threshold = activity_threshold
        self.calibrated = False
        self.occupied = False
        self.activity_high = False
        self.above_count = 0
        self.below_count = 0

    def reset(self):
        self.samples.clear()
        self.activity_samples.clear()

    def update(self, amplitude, rssi=0, now=None):
        if now is None:
            now = time.monotonic()
        index = self.indexes(amplitude)
        normalized = normalize_amplitude(amplitude, index)
        if normalized is None:
            return None
        self.last_normalized = normalized
        self.samples.append(normalized)

        motion_score = None
        if len(self.samples) >= 2:
            variance = np.asarray(self.samples).var(axis=0)
            # Scale-free score: variance of the gain-normalized amplitude,
            # averaged over the selected subcarriers.
            motion_score = float(variance.mean())
            self.ema.update(motion_score)

        # The activity statistic is only meaningful on steady-state windows; a
        # partially filled window has inflated variance.
        steady = motion_score is not None and len(self.samples) >= self.window
        if steady:
            self.activity_samples.append((now, motion_score))
            while (self.activity_samples
                   and now - self.activity_samples[0][0] > self.activity_window):
                self.activity_samples.popleft()

        activity_score = None
        if steady and activity_window_ready(self.activity_samples,
                                            self.activity_window):
            activity_score = compute_activity(
                self.activity_samples, self.activity_metric,
                self.activity_window)

        metrics = {
            'score': motion_score,
            'smoothed': self.ema.value,
            'activity': activity_score,
            'activity_metric': self.activity_metric,
            'activity_threshold': self.activity_threshold,
            'activity_mu': self.activity_mu,
            'activity_sigma': self.activity_sigma,
            'threshold_on': self.threshold_on,
            'threshold_off': self.threshold_off,
            'mu': self.mu,
            'sigma': self.sigma,
            'status': None,
            'calibrating': not self.calibrated,
            'calibrated_now': False,
            'calib_elapsed': 0.0,
            'calib_fraction': 1.0,
            'calib_samples': len(self.calibration.scores),
            'activity_samples': len(self.calibration.activity_scores),
            'window_fill': len(self.samples),
            'window_size': self.window,
        }

        if not self.calibrated:
            self._collect_baseline(motion_score, steady, activity_score, metrics)
            return metrics

        if self.ema.value is None:
            progress = min(1.0, len(self.samples) / float(self.window))
            metrics['calib_fraction'] = progress
            return metrics

        metrics['status'] = self._classify(activity_score)
        return metrics

    def _collect_baseline(self, motion_score, steady, activity_score, metrics):
        metrics['status'] = LEVEL_CALIBRATING
        smoothed = self.ema.value
        # The idle clock starts with the first usable score, so the displayed
        # progress matches wall-clock observation time.
        if smoothed is not None:
            self.calibration.start_now()
            # Only steady-state (full-window) scores feed the baseline; short
            # warm-up windows have inflated variance and would bias the
            # thresholds upward.
            if steady:
                self.calibration.add(smoothed)
                if activity_score is not None:
                    self.calibration.add_activity(activity_score)
        elapsed, fraction = self.calibration.progress()
        metrics['calib_elapsed'] = elapsed
        metrics['calib_fraction'] = fraction
        metrics['calib_samples'] = len(self.calibration.scores)
        metrics['activity_samples'] = len(self.calibration.activity_scores)
        if self.calibration.done():
            (self.mu, self.sigma, self.threshold_on,
             self.threshold_off) = self.calibration.result(
                 self.on_sigma, self.off_sigma)
            (self.activity_mu, self.activity_sigma,
             auto_activity) = self.calibration.activity_result(
                 self.activity_metric)
            self.activity_threshold_overridden = self.activity_threshold is not None
            if self.activity_threshold is None:
                self.activity_threshold = auto_activity
            self.calibrated = True
            metrics.update(mu=self.mu, sigma=self.sigma,
                           threshold_on=self.threshold_on,
                           threshold_off=self.threshold_off,
                           activity_mu=self.activity_mu,
                           activity_sigma=self.activity_sigma,
                           activity_threshold=self.activity_threshold,
                           calibrated_now=True, calib_fraction=1.0)

    def _classify(self, activity_score):
        smoothed = self.ema.value
        # Hysteresis: enter on the high threshold, leave on the low one.
        if self.occupied:
            if smoothed < self.threshold_off:
                self.occupied = False
        else:
            if smoothed >= self.threshold_on:
                self.occupied = True

        if not self.occupied:
            self.activity_high = False
            self.above_count = self.below_count = 0
            return LEVEL_EMPTY

        # Activity level hysteresis: require several consecutive packets on the
        # new side before switching the label.
        if activity_score is None:
            # Not enough activity history yet; keep the current level.
            pass
        elif activity_score >= self.activity_threshold:
            self.above_count += 1
            self.below_count = 0
            if self.above_count >= ACTIVITY_HYSTERESIS_PACKETS:
                self.activity_high = True
        else:
            self.below_count += 1
            self.above_count = 0
            if self.below_count >= ACTIVITY_HYSTERESIS_PACKETS:
                self.activity_high = False
        return LEVEL_MULTI if self.activity_high else LEVEL_SINGLE

    def csv_row(self, metrics, timestamp, packet_num, rssi):
        head = ['%.3f' % timestamp, packet_num,
                fmt_opt(metrics['score']), fmt_opt(metrics['smoothed']),
                fmt_opt(metrics['activity']), self.activity_metric]
        if metrics['status'] == LEVEL_CALIBRATING:
            return head + [LEVEL_CALIBRATING, rssi, '', '', '', '',
                           fmt_opt(metrics['activity_threshold'])]
        return head + [metrics['status'], rssi, fmt_opt(metrics['mu']),
                       fmt_opt(metrics['sigma']),
                       fmt_opt(metrics['threshold_on']),
                       fmt_opt(metrics['threshold_off']),
                       fmt_opt(metrics['activity_threshold'])]


class ProximityMeter(BaseDetector):
    """Coarse approach/departure detection from absolute received energy.

    ``score`` is the absolute CSI energy of the packet (``power`` by default),
    deliberately *not* divided by its mean: a person entering the Fresnel zone
    of the link changes the received power, and that absolute change is the
    signal.  ``trend`` is the smoothed energy minus the smoothed energy
    ``trend_window`` seconds ago; ``threshold`` comes from the empty-room trend
    spread (``3 * sigma``) unless it is given explicitly.

    Extra metrics beyond the shared keys: ``trend``, ``threshold``,
    ``energy_metric``.
    """

    tool = 'proximity'
    columns = ['timestamp', 'packet_num', 'energy', 'smoothed_energy', 'trend',
               'direction', 'rssi', 'energy_baseline_mu', 'energy_baseline_sigma',
               'trend_threshold']

    def __init__(self, calib=5.0, trend_window=2.0, ema_alpha=0.15,
                 energy_metric='power', threshold=None, subcarriers=None,
                 edge_skip=10):
        super().__init__(subcarriers, edge_skip)
        self.energy_metric = energy_metric
        self.trend_window = trend_window
        self.fixed_threshold = threshold
        self.threshold_fallback = False
        self.ema = Ema(ema_alpha)
        self.tracker = TrendTracker(trend_window)
        self.calibration = EnergyCalibration(
            calib, grace=max(2.0 * trend_window, 2.0))
        self.energy_mu = self.energy_sigma = float('nan')
        self.threshold = threshold
        self.calibrated = False

    def reset(self):
        # The energy scale changes with the subcarrier set, so restart the
        # smoothing and the trend history instead of reporting a fake jump.
        self.ema = Ema(self.ema.alpha)
        self.tracker.clear()

    def update(self, amplitude, rssi=0, now=None):
        if now is None:
            now = time.monotonic()
        index = self.indexes(amplitude)
        # Displayed as received: normalization would hide the very quantity
        # this tool measures.
        self.last_normalized = amplitude[index]
        energy = compute_energy(amplitude, index, self.energy_metric)
        if energy is None:
            return None

        smoothed = self.ema.update(energy)
        self.tracker.append(now, smoothed)
        trend = self.tracker.trend()

        metrics = {
            'score': energy,
            'smoothed': smoothed,
            'trend': trend,
            'threshold': self.threshold,
            'fixed_threshold': self.fixed_threshold,
            'energy_metric': self.energy_metric,
            'mu': self.energy_mu,
            'sigma': self.energy_sigma,
            'status': None,
            'calibrating': not self.calibrated,
            'calibrated_now': False,
            'calib_elapsed': 0.0,
            'calib_fraction': 1.0,
            'calib_samples': len(self.calibration.energies),
            'trend_samples': len(self.calibration.trends),
        }

        if not self.calibrated:
            self._collect_baseline(smoothed, trend, metrics)
            return metrics

        if trend is None:
            metrics['status'] = DIR_COLLECTING
        else:
            metrics['status'] = classify(trend, self.threshold)
        return metrics

    def _collect_baseline(self, smoothed, trend, metrics):
        metrics['status'] = DIR_CALIBRATING
        self.calibration.add_energy(smoothed)
        if trend is not None:
            self.calibration.add_trend(trend)
        elapsed, fraction = self.calibration.progress()
        metrics['calib_elapsed'] = elapsed
        metrics['calib_fraction'] = fraction
        metrics['calib_samples'] = len(self.calibration.energies)
        metrics['trend_samples'] = len(self.calibration.trends)
        # A fixed threshold is known up front, so report it while calibrating.
        metrics['threshold'] = self.fixed_threshold
        if self.calibration.done():
            self.energy_mu, self.energy_sigma = self.calibration.energy_result()
            if self.fixed_threshold is None:
                self.threshold, self.threshold_fallback = \
                    self.calibration.trend_threshold(self.energy_mu)
            else:
                self.threshold = self.fixed_threshold
                self.threshold_fallback = False
            self.calibrated = True
            metrics.update(mu=self.energy_mu, sigma=self.energy_sigma,
                           threshold=self.threshold, calibrated_now=True,
                           calib_fraction=1.0)

    def csv_row(self, metrics, timestamp, packet_num, rssi):
        head = ['%.3f' % timestamp, packet_num,
                fmt_opt(metrics['score']), fmt_opt(metrics['smoothed']),
                fmt_opt(metrics['trend'])]
        if metrics['status'] == DIR_CALIBRATING:
            return head + [DIR_CALIBRATING, rssi, '', '',
                           fmt_opt(self.fixed_threshold)]
        return head + [metrics['status'], rssi, fmt_opt(metrics['mu']),
                       fmt_opt(metrics['sigma']), fmt_opt(metrics['threshold'])]


def classify(trend, threshold):
    if trend > threshold:
        return DIR_APPROACHING
    if trend < -threshold:
        return DIR_AWAY
    return DIR_STATIONARY


DETECTORS = {
    MotionDetector.tool: MotionDetector,
    OccupancyMeter.tool: OccupancyMeter,
    ProximityMeter.tool: ProximityMeter,
}
