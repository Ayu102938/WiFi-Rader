#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Modified by Ayu102938
# - 2026-11-19: Created csi_web_server.py, a local dashboard that owns the
#   serial link and streams the metrics of csi_detectors to a browser.
#
"""Local web dashboard for the WiFi-Rader CSI detectors.

This is the browser counterpart of the three serial tools.  It owns the serial
port, feeds the packets to the very same detector classes the CLI tools use
(``csi_detectors``), and serves a small dashboard plus a JSON event stream:

``GET /``              the dashboard (static files from ``web/``)
``GET /api/stream``    server-sent events, one JSON tick per new packet burst
``POST /api/config``   switch detector or tune its parameters

Why a local server instead of the Web Serial API: the detection logic stays in
Python, where the CLI tools and the tests already use it, and the browser only
renders.  That also means it works in any browser, not only the ones that
implement Web Serial.

Usage::

    # Read the receiver on /dev/ttyACM0, dashboard on http://127.0.0.1:8000
    python csi_web_server.py -p /dev/ttyACM0

    # Same, but replay a captured CSI stream instead of the board
    python csi_web_server.py --replay capture.csv

    # Generate a synthetic stream (development aid, no hardware needed)
    python csi_web_server.py --synthetic

Requires only ``pyserial`` and ``numpy`` (see requirements.txt): the HTTP
server is the standard library's.
"""

import argparse
import json
import mimetypes
import os
import random
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import csi_common
import csi_detectors

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

# How often the dashboard is fed.  The board emits ~100 packets/s, which is
# more than a chart needs and more than a localhost DOM can paint.
TICK_INTERVAL = 0.1
HISTORY_POINTS = 600
WAVE_POINTS = 240
EVENT_LIMIT = 60
# Classification flips faster than this would flood the event log; the
# detector keeps flipping internally, the log just stays readable.
EVENT_MIN_INTERVAL = 0.4


class SerialSource:
    """The CSI receiver board."""

    kind = 'serial'

    def __init__(self, port, baud):
        self.ser = csi_common.open_serial(port, baud)
        self.description = '%s @ %d baud' % (port, baud)

    def readline(self):
        return self.ser.readline()

    def close(self):
        self.ser.close()


class ReplaySource:
    """Replays a captured CSI stream at a realistic packet rate.

    The file is the ``--csv`` output of any tool, or any text file of raw
    ``CSI_DATA`` lines: both are read back exactly as the board sent them.
    The file loops, so a demo dashboard keeps running.
    """

    kind = 'replay'

    def __init__(self, path, rate=100.0):
        with open(path, 'rb') as handle:
            self.lines = [line for line in handle if line.strip()]
        if not self.lines:
            raise ValueError('%s contains no CSI lines' % path)
        self.rate = rate
        self.index = 0
        self.next_due = None
        self.description = '%s (%d lines, %.0f packets/s, looping)' % (
            os.path.basename(path), len(self.lines), rate)

    def readline(self):
        line = self.lines[self.index]
        self.index = (self.index + 1) % len(self.lines)
        now = time.monotonic()
        if self.next_due is None:
            self.next_due = now
        self.next_due += 1.0 / self.rate
        delay = self.next_due - now
        if delay > 0:
            time.sleep(delay)
        return line

    def close(self):
        pass


class SyntheticSource:
    """Generates a CSI-like stream so the dashboard runs without hardware.

    The generated link is empty for ``idle_seconds``, then disturbed for
    ``busy_seconds`` with a small ramp.  The idle phase is deliberately longer
    than the dashboard's default calibration time, so a freshly started
    dashboard learns an empty-room baseline and then actually sees the busy
    phase: a shorter idle phase would fold motion into the baseline and leave
    the thresholds above anything the demo produces.
    """

    kind = 'synthetic'

    def __init__(self, rate=100.0, idle_seconds=20.0, busy_seconds=8.0):
        self.rate = rate
        self.idle_seconds = idle_seconds
        self.busy_seconds = busy_seconds
        self.text = ''
        self.start = time.monotonic()
        self.next_due = self.start
        self.rng = random.Random(1234)
        self.description = ('synthetic stream (%.0f packets/s, %.0fs idle / '
                            '%.0fs busy)' % (rate, idle_seconds, busy_seconds))

    def _busy(self, elapsed):
        cycle = self.idle_seconds + self.busy_seconds
        phase = elapsed % cycle
        if phase < self.idle_seconds:
            return 0.0
        # 0.5 s ramp in and out so the transition is not a step.
        ramp = min(1.0, (phase - self.idle_seconds) / 0.5,
                   (cycle - phase) / 0.5)
        return max(0.0, ramp)

    def readline(self):
        now = time.monotonic()
        self.next_due += 1.0 / self.rate
        delay = self.next_due - now
        if delay > 0:
            time.sleep(delay)

        strength = self._busy(now - self.start) * 400.0
        values = []
        for _ in range(64):
            real = 1000.0 + self.rng.gauss(0, 3) + self.rng.gauss(0, strength)
            imag = 1000.0 + self.rng.gauss(0, 3) + self.rng.gauss(0, strength)
            values.append(int(round(imag)))
            values.append(int(round(real)))
        row = ['CSI_DATA', '1', '"aa:bb:cc:dd:ee:ff"', '-50', '11', '1', '0',
               '1', '0', '1', '0', '0', '0', '0', '-95', '0', '11', '0',
               '12345', '0', '128', '0', str(len(values)), '0',
               '"%s"' % json.dumps(values)]
        return (','.join(row) + '\n').encode()

    def close(self):
        pass


def build_source(args):
    """Return the packet source selected on the command line."""
    if args.replay:
        return ReplaySource(args.replay, args.replay_rate)
    if args.synthetic:
        return SyntheticSource(args.replay_rate)
    return SerialSource(args.port, args.baud)


def default_config(tool='motion'):
    """Return the detector parameters the dashboard starts with."""
    return {
        'tool': tool,
        'calib': 15.0,
        'window': 300,
        'ema_alpha': 0.2,
        'on_sigma': 3.0,
        'off_sigma': 1.5,
        'activity_window': 5.0,
        'activity_metric': 'cv',
        'energy_metric': 'power',
        'trend_window': 2.0,
        'edge_skip': 10,
    }


# Parameters the dashboard may edit, with the limits it enforces.  ``--calib``
# and friends stay out of the browser's reach on purpose: the CLI keeps the
# full surface, the dashboard exposes what changes the display.
CONFIG_RULES = {
    'calib': (float, 1.0, 120.0),
    'window': (int, 20, 1000),
    'ema_alpha': (float, 0.02, 1.0),
    'on_sigma': (float, 0.5, 12.0),
    'off_sigma': (float, 0.2, 12.0),
    'activity_window': (float, 1.0, 20.0),
    'energy_metric': (str, None, None),
    'trend_window': (float, 0.2, 10.0),
}


def validate_config(current, updates):
    """Return ``(config, error)`` after applying ``updates`` to ``current``."""
    config = dict(current)
    for key, value in updates.items():
        if key == 'tool':
            if value not in csi_detectors.DETECTORS:
                return None, 'unknown detector: %r' % value
            config['tool'] = value
            continue
        if key == 'activity_metric':
            if value not in csi_detectors.ACTIVITY_METRICS:
                return None, 'unknown activity metric: %r' % value
            config['activity_metric'] = value
            continue
        if key not in CONFIG_RULES:
            return None, 'unknown parameter: %r' % key
        kind, low, high = CONFIG_RULES[key]
        if kind is str:
            allowed = (csi_detectors.ENERGY_METRICS if key == 'energy_metric'
                       else ())
            if value not in allowed:
                return None, 'invalid %s: %r' % (key, value)
            config[key] = value
            continue
        try:
            number = kind(value)
        except (TypeError, ValueError):
            return None, '%s must be a number' % key
        if not low <= number <= high:
            return None, '%s must be between %s and %s' % (key, low, high)
        config[key] = number
    if config['on_sigma'] < config['off_sigma']:
        return None, 'on_sigma must be >= off_sigma to keep hysteresis'
    return config, None


def make_detector(config):
    """Build the detector described by ``config``.

    The detectors share most parameters but not all: only the two score-based
    ones have a packet window, and each one has its own statistic knobs.
    """
    tool = config['tool']
    common = {'calib': config['calib'], 'ema_alpha': config['ema_alpha'],
              'edge_skip': config['edge_skip']}
    if tool == 'motion':
        return csi_detectors.MotionDetector(
            window=config['window'], on_sigma=config['on_sigma'],
            off_sigma=config['off_sigma'], **common)
    if tool == 'occupancy':
        return csi_detectors.OccupancyMeter(
            window=config['window'], on_sigma=config['on_sigma'],
            off_sigma=config['off_sigma'],
            activity_window=config['activity_window'],
            activity_metric=config['activity_metric'], **common)
    return csi_detectors.ProximityMeter(
        trend_window=config['trend_window'],
        energy_metric=config['energy_metric'], **common)


class Session:
    """Owns the detector and everything the dashboard displays."""

    def __init__(self, source, config):
        self.source = source
        self.lock = threading.Lock()
        self.config = dict(config)
        self.detector = make_detector(self.config)
        self.stats = csi_common.PacketStats()
        self.started = time.time()
        self.latest = {}
        self.wave = []
        self.history = deque(maxlen=HISTORY_POINTS)
        self.events = deque(maxlen=EVENT_LIMIT)
        self.event_id = 0
        self.last_status = None
        self.last_event_time = 0.0
        self.frames = 0
        self.error = None
        self.stopped = threading.Event()

    # -- reader thread ----------------------------------------------------

    def run(self):
        """Read packets until the source ends or the server stops."""
        try:
            for packet in csi_common.iter_csi_packets(self.source, self.stats):
                if self.stopped.is_set():
                    return
                if packet is None:
                    continue
                row, amplitude, rssi = packet
                detector = self.detector
                try:
                    metrics = detector.update(amplitude, rssi)
                except argparse.ArgumentTypeError as exc:
                    self._fail(str(exc))
                    return
                if metrics is None:
                    continue
                if metrics.get('score') is not None:
                    self.stats.scored += 1
                self._publish(metrics, rssi, detector)
        except (OSError, RuntimeError) as exc:  # serial disappeared, port closed
            self._fail(str(exc))
        finally:
            self.source.close()

    def _fail(self, message):
        """Record a fatal read error and wake the SSE loops.

        The stream only pushes a tick when something changed, so a reader that
        dies silently would leave every dashboard claiming to be connected
        while its numbers freeze.
        """
        with self.lock:
            self.error = message
            self.frames += 1

    def _publish(self, metrics, rssi, detector):
        wave = self.detector.last_normalized
        if wave is not None and len(wave) > 0:
            step = max(1, int(len(wave) // WAVE_POINTS))
            wave = [round(float(value), 4) for value in wave[::step]]
        else:
            wave = []

        now = time.time()
        with self.lock:
            if detector is not self.detector:
                # The dashboard switched detector while this packet was in
                # flight; its readings belong to the previous one.
                return
            self.latest = dict(metrics)
            self.latest['rssi'] = rssi
            self.latest['timestamp'] = now
            self.wave = wave
            self.frames += 1
            self.history.append({
                't': round(now - self.started, 3),
                'score': _number(metrics.get('score')),
                'smoothed': _number(metrics.get('smoothed')),
                'on': _number(metrics.get('threshold_on')
                              if metrics.get('threshold_on') is not None
                              else metrics.get('threshold')),
                'off': _number(metrics.get('threshold_off')),
                'status': metrics.get('status'),
                'trend': _number(metrics.get('trend')),
                'activity': _number(metrics.get('activity')),
            })
            self._record_event(metrics, now)

    def _record_event(self, metrics, now):
        """Remember a change of classification for the event log."""
        status = metrics.get('status')
        if status is None or metrics.get('calibrating'):
            return
        if status == self.last_status:
            return
        self.last_status = status
        if self.events and now - self.last_event_time < EVENT_MIN_INTERVAL:
            # Same burst of noise as the previous entry: replace it instead of
            # appending, but keep a fresh id so connected dashboards notice the
            # update.  A new dict, because snapshot() hands these out.
            self.event_id += 1
            self.events[0] = {
                'id': self.event_id,
                't': round(now - self.started, 2),
                'status': status,
                'score': _number(metrics.get('score')),
                'activity': _number(metrics.get('activity')),
                'trend': _number(metrics.get('trend')),
            }
            return
        self.last_event_time = now
        self.event_id += 1
        self.events.appendleft({
            'id': self.event_id,
            't': round(now - self.started, 2),
            'status': status,
            'score': _number(metrics.get('score')),
            'activity': _number(metrics.get('activity')),
            'trend': _number(metrics.get('trend')),
        })

    # -- dashboard --------------------------------------------------------

    def reconfigure(self, updates):
        """Apply ``updates``, rebuilding the detector.  Returns ``(config, error)``."""
        with self.lock:
            config, error = validate_config(self.config, updates)
            if error:
                return None, error
            self.config = config
            self.detector = make_detector(config)
            self.stats.reset()
            self.history.clear()
            self.events.clear()
            self.latest = {}
            self.wave = []
            self.last_status = None
            self.last_event_time = 0.0
            self.started = time.time()
            self.frames = 0
            self.error = None
        return config, None

    def tick(self):
        """Return the small payload of one dashboard update."""
        with self.lock:
            point = self.history[-1] if self.history else None
            return {
                'config': dict(self.config),
                'counters': self._counters(),
                'metrics': _jsonable(self.latest),
                'wave': list(self.wave),
                'point': point,
                'last_event': self.events[0] if self.events else None,
                'error': self.error,
                'uptime': round(time.time() - self.started, 1),
            }

    def _counters(self):
        return {
            'packets': self.stats.packets,
            'scored': self.stats.scored,
            'skipped': self.stats.skipped,
            'other_lines': self.stats.other_lines,
        }

    def snapshot(self):
        """Return the full state, sent when the dashboard connects."""
        with self.lock:
            return {
                'config': dict(self.config),
                'source': {'kind': self.source.kind,
                           'description': self.source.description},
                'counters': self._counters(),
                'metrics': _jsonable(self.latest),
                'wave': list(self.wave),
                'history': list(self.history),
                'events': list(self.events),
                'error': self.error,
                'uptime': round(time.time() - self.started, 1),
            }


def _number(value):
    """Return ``value`` as a JSON number, or ``None`` when undefined."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, 6)


def _jsonable(metrics):
    out = {}
    for key, value in metrics.items():
        if isinstance(value, bool) or value is None or isinstance(value, str):
            out[key] = value
        elif isinstance(value, int):
            out[key] = value
        elif isinstance(value, float):
            out[key] = _number(value)
        else:
            out[key] = value
    return out


class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the dashboard and its JSON/SSE endpoints."""

    server_version = 'WiFiRaderDashboard/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001 - answer instead of dropping
            self.log_error('GET failed: %s', exc)
            self._send_json({'error': 'internal error: %s' % exc}, status=500)

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:  # noqa: BLE001 - answer instead of dropping
            self.log_error('POST failed: %s', exc)
            self._send_json({'error': 'internal error: %s' % exc}, status=500)

    def _get(self):
        path = self.path.split('?', 1)[0]
        if path == '/api/stream':
            self._stream()
            return
        if path == '/api/state':
            self._send_json(self.server.session.snapshot())
            return
        self._serve_static(path)

    def _post(self):
        path = self.path.split('?', 1)[0]
        if path != '/api/config':
            self._send_json({'error': 'not found'}, status=404)
            return
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > 65536:
            self._send_json({'error': 'missing request body'}, status=400)
            return
        try:
            updates = json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({'error': 'invalid JSON: %s' % exc}, status=400)
            return
        if not isinstance(updates, dict):
            self._send_json({'error': 'expected a JSON object'}, status=400)
            return
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + (self.headers.get('Host') or ''):
            # Another page in the same browser can post here without a
            # preflight; the dashboard itself never sends an Origin header.
            self._send_json({'error': 'cross-origin request refused'},
                            status=403)
            return
        config, error = self.server.session.reconfigure(updates)
        if error:
            self._send_json({'error': error}, status=400)
            return
        self._send_json({'config': config})

    # -- helpers ----------------------------------------------------------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path):
        if path in ('/', ''):
            path = '/index.html'
        # Normalise separators first: os.path.join also treats "\\" as a
        # separator on Windows, where a bare ".." component check would not
        # catch "\\..\\..\\file".
        relative = path.lstrip('/').replace('\\', '/')
        if '..' in relative.split('/'):
            self._send_json({'error': 'not found'}, status=404)
            return
        root = os.path.realpath(WEB_ROOT)
        full = os.path.realpath(os.path.join(root, relative))
        if full != root and not full.startswith(root + os.sep):
            self._send_json({'error': 'not found'}, status=404)
            return
        if not os.path.isfile(full):
            self._send_json({'error': 'not found'}, status=404)
            return
        with open(full, 'rb') as handle:
            body = handle.read()
        content_type = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if content_type.startswith('text/') or content_type in (
                'application/javascript', 'application/json'):
            content_type += '; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        """Push the dashboard state as server-sent events.

        The first event is a full snapshot (config, history, event log) so a
        freshly opened tab can draw the chart immediately; the following ticks
        only carry the newest packet, which keeps the stream small at 10 Hz.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        session = self.server.session
        try:
            hello = json.dumps(session.snapshot(), ensure_ascii=False)
            self.wfile.write(('event: hello\ndata: %s\n\n' % hello).encode('utf-8'))
            self.wfile.flush()

            last_frames = -1
            while not self.server.stopping.is_set():
                with session.lock:
                    frames = session.frames
                if frames != last_frames:
                    last_frames = frames
                    payload = json.dumps(session.tick(), ensure_ascii=False)
                    self.wfile.write(('data: %s\n\n' % payload).encode('utf-8'))
                    self.wfile.flush()
                else:
                    # Keep the connection visibly alive during quiet periods.
                    self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
                time.sleep(TICK_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The browser tab went away; nothing to clean up.
            return


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Serve the WiFi-Rader CSI dashboard on a local port',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-p', '--port', default='/dev/ttyACM0',
                        help='serial port of the CSI receiver board')
    parser.add_argument('-b', '--baud', type=int, default=921600,
                        help='serial baud rate')
    parser.add_argument('--http-port', type=int, default=8000,
                        help='port to serve the dashboard on')
    parser.add_argument('--host', default='127.0.0.1',
                        help='address to bind (localhost only by default)')
    parser.add_argument('--tool', default='motion',
                        choices=sorted(csi_detectors.DETECTORS),
                        help='detector shown first')
    parser.add_argument('--replay', default=None,
                        help='replay a captured CSI stream (a --csv file) '
                             'instead of reading the board')
    parser.add_argument('--synthetic', action='store_true',
                        help='generate a synthetic stream instead of reading '
                             'the board (development aid)')
    parser.add_argument('--replay-rate', type=float, default=100.0,
                        help='packets per second for --replay/--synthetic')
    parser.add_argument('--verbose', action='store_true',
                        help='log every HTTP request')
    args = parser.parse_args(argv)

    if not os.path.isdir(WEB_ROOT):
        print('error: dashboard files not found in %s' % WEB_ROOT,
              file=sys.stderr)
        return 1

    try:
        source = build_source(args)
    except csi_common.SerialOpenError as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 1

    session = Session(source, default_config(args.tool))
    server = ThreadingHTTPServer((args.host, args.http_port), DashboardHandler)
    server.session = session
    server.stopping = session.stopped
    server.verbose = args.verbose

    reader = threading.Thread(target=session.run, name='csi-reader', daemon=True)
    reader.start()

    print('WiFi-Rader dashboard: http://%s:%d/  (%s)'
          % (args.host, args.http_port, source.description))
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        print('warning: --host %s exposes the dashboard and its config '
              'endpoint to every host that can reach this machine' % args.host,
              file=sys.stderr)
    print('Detector: %s. Press Ctrl+C to stop.' % args.tool)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print('\nstopped')
    finally:
        # serve_forever() has already returned, so shutdown() would be a no-op
        # from this thread; closing the socket is all that is left to do.
        session.stopped.set()
        server.server_close()
        # The reader exits within the serial timeout; closing the source first
        # would make it raise on the way out.
        reader.join(timeout=2.0)
        session.source.close()
    if session.error:
        print('warning: %s' % session.error, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
