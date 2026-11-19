#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Modified by Ayu102938
# - 2026-11-19: Created csi_common.py and moved the CSI front end that
#   csi_motion_detector.py, csi_occupancy_meter.py and csi_proximity_meter.py
#   had been copying verbatim (line parsing, subcarrier selection, amplitude
#   normalization, serial handling, counters) out of those scripts.
#
"""Shared front end for the WiFi-Rader serial CSI tools.

Every tool in this directory reads the same ``CSI_DATA`` lines from the CSI
receiver board and applies the same front end to them:

``parse_csi_line``
    One serial line -> per-subcarrier amplitudes plus RSSI.  Anything that is
    not a complete record (boot banners, firmware logs, truncated reads) is
    rejected so callers can simply skip it.
``select_subcarriers``
    Which subcarriers carry usable signal.  Guard, DC and null subcarriers
    only add noise, so they are dropped by default.
``normalize_amplitude``
    Divide by the mean amplitude, removing the link gain so a gain change
    alone cannot be mistaken for motion.

The tools differ only in the statistic they compute on top of that front end
(motion variance, activity frequency, absolute energy trend), so the front end
and the serial loop live here.  Keep this module dependency-light: ``numpy``
and ``pyserial`` only.
"""

import argparse
import csv
import json
import time
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


def csv_header(row):
    """Return the column-name layout of a ``parse_csi_line`` row."""
    return (DATA_COLUMNS_NAMES if len(row) == len(DATA_COLUMNS_NAMES)
            else DATA_COLUMNS_NAMES_C5C6)


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


class SubcarrierSelector:
    """Selects subcarrier indexes and caches them per subcarrier count.

    The index set only depends on the subcarrier count, which changes when the
    radio re-tunes.  ``changed`` reports that, because a new count means a new
    frequency response and the caller must restart the statistics it derived
    from the previous one.
    """

    def __init__(self, spec=None, edge_skip=10):
        self.spec = spec
        self.edge_skip = edge_skip
        self.changed = False
        self._count = None
        self._cache = {}

    def indexes(self, count):
        """Return the selected indexes for ``count`` subcarriers.

        Raises ``argparse.ArgumentTypeError`` for an invalid ``--subcarriers``
        specification.
        """
        self.changed = count != self._count
        self._count = count
        if count not in self._cache:
            self._cache[count] = select_subcarriers(
                count, self.spec, self.edge_skip)
        return self._cache[count]


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
    """Rate-limited carriage-return progress line.

    Rewriting one terminal line keeps a piped log from exploding.  Output is
    padded to ``width`` so a shorter message fully overwrites the longer one
    it replaces.
    """

    def __init__(self, interval=0.2, width=88):
        self.interval = interval
        self.width = width
        self.last = 0.0

    def show(self, text):
        if time.monotonic() - self.last < self.interval:
            return
        self.force(text)

    def force(self, text):
        self.last = time.monotonic()
        print('\r' + text.ljust(self.width), end='', flush=True)


def fmt_opt(value):
    """Format a float for CSV, or an empty field when it is undefined."""
    if value is None or not np.isfinite(value):
        return ''
    return '%.6f' % value


class SerialOpenError(Exception):
    """Raised when the receiver's serial port cannot be opened."""


def open_serial(port, baud):
    """Open the CSI receiver's serial port with the tools' fixed framing."""
    try:
        return serial.Serial(port=port, baudrate=baud, bytesize=8, parity='N',
                             stopbits=1, timeout=1)
    except serial.SerialException as exc:
        raise SerialOpenError('cannot open %s: %s' % (port, exc))


class PacketStats:
    """Line and packet counters shared by every tool."""

    def __init__(self):
        self.packets = 0
        self.scored = 0
        self.skipped = 0
        self.other_lines = 0

    def reset(self):
        """Forget all counts (used when the dashboard switches detector)."""
        self.packets = 0
        self.scored = 0
        self.skipped = 0
        self.other_lines = 0

    def summary(self):
        """Return the one-line end-of-run summary printed on Ctrl+C."""
        return ('%d packets parsed (%d scored), %d lines skipped, '
                '%d log lines ignored'
                % (self.packets, self.scored, self.skipped,
                   self.other_lines))


def iter_csi_packets(ser, stats):
    """Yield the usable CSI packets read from ``ser``.

    Yields ``(row, amplitude, rssi)`` for each complete record and ``None``
    when a read times out, so callers can refresh their progress display.
    Unusable input is counted in ``stats`` instead of interrupting the loop:
    a live link always carries firmware logs and truncated lines.
    """
    while True:
        line = ser.readline()
        if not line:
            yield None
            continue
        # A read interrupted by the timeout may return a partial line.
        if not line.endswith(b'\n'):
            stats.skipped += 1
            continue

        text = line.decode('utf-8', errors='ignore').strip('\r\n')
        parsed = parse_csi_line(text)
        if parsed is None:
            # Firmware logs and boot banners are not CSI records.
            if 'CSI_DATA' in text:
                stats.skipped += 1
            else:
                stats.other_lines += 1
            continue

        stats.packets += 1
        yield parsed
