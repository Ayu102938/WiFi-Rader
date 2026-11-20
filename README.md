# WiFi-Rader

**English** · [日本語](README.ja.md)

Wi-Fi CSI sensing with two ESP32 boards: a transmitter and a receiver measure how
a person disturbs the wireless channel between them, and three detectors turn
that into presence, occupancy and approach/departure.

This repository is a fork of [espressif/esp-csi](https://github.com/espressif/esp-csi)
(Apache License 2.0). What this fork adds and changes is listed under
[Modifications](#modifications).

> **日本語版は [README.ja.md](README.ja.md) にあります。**

## What is here

| Piece | Role |
|-------|------|
| `examples/get-started/csi_send` | TX firmware: sends packets continuously |
| `examples/get-started/csi_recv` | RX firmware: prints one `CSI_DATA` line per packet |
| `csi_recv_router`, `esp-radar/*`, `esp-crab/*` | Upstream examples, kept as shipped |

`examples/get-started/tools/` holds the Python side:

| File | Role |
|------|------|
| `csi_common.py` | Shared front end: line parsing, subcarrier selection, amplitude normalisation, serial loop, progress, counters |
| `csi_detectors.py` | The three detectors as classes (`MotionDetector`, `OccupancyMeter`, `ProximityMeter`) |
| `csi_motion_detector.py` | CLI: occupied / empty |
| `csi_occupancy_meter.py` | CLI: empty / one person / several people |
| `csi_proximity_meter.py` | CLI: approaching / receding / still |
| `csi_web_server.py` + `web/` | Local dashboard that runs the same detectors in a browser |
| `csi_data_read_parse.py`, `csi_viewer.html` | Upstream GUI and browser CSI viewers (raw waveforms) |

The CLI tools only parse arguments and print; the statistics live in
`csi_detectors.py`, which is also what the web server imports. A fix to a
detector therefore lands in every front end at once.

## Hardware

| Item | Recommended | Role |
|------|-------------|------|
| ESP32-S3 | ESP32-S3-DevKitC or equivalent | RX (receiver) |
| ESP32 (original) | ESP32-WROOM-U or equivalent | TX (sender) |
| USB cables | 2 | Power and data for RX, power for TX |
| PC | Linux (tested on CachyOS/Arch x86_64) | Build, flash, run the tools |

Placement tips:

- Keep TX and RX **2–3 m apart**.
- RSSI should settle around **-45 to -60 dBm**.
- Do not block the direct path completely; multipath helps sensitivity.
- An external IPEX antenna works better than the PCB antenna.

## Software

- **ESP-IDF** v5.5.6, installed with `eim` (the standard `export.sh` is a bash
  script and does not work with fish)
- **Python** 3.10–3.14, with `uv` for the virtual environment
- `git`

Tested on CachyOS (Arch-based) x86_64, fish 4.9.3, Python 3.14.8.

## Setup

```bash
cd ~/Projects
git clone https://github.com/Ayu102938/WiFi-Rader.git
cd WiFi-Rader
```

### ESP-IDF

```bash
# Install v5.5.6 with the xtensa toolchains for esp32 and esp32s3
eim install -p ~/esp -i v5.5.6 -t esp32,esp32s3 -n true

# Verify
eim list
eim run "idf.py --version" v5.5.6
```

If `eim` fails on `libwebkit2gtk-4.1.so.0`, install it:
`sudo pacman -S --needed webkit2gtk-4.1`.

### Python environment

```bash
cd examples/get-started/tools
uv venv
uv pip install numpy pyserial          # the three CLIs and the dashboard
uv pip install -r requirements.txt     # only for the upstream GUI viewer
```

`requirements.txt` covers `csi_data_read_parse.py` (pandas, scipy, statsmodels,
PyQt5, pyqtgraph, matplotlib). The detectors and the dashboard need only
`numpy` and `pyserial`.

## Firmware

### RX — ESP32-S3

```bash
cd examples/get-started/csi_recv
eim run "idf.py set-target esp32s3" v5.5.6
eim run "idf.py build" v5.5.6
eim run "idf.py -p /dev/ttyACM0 -b 921600 flash" v5.5.6
```

### TX — ESP32-WROOM-U

```bash
cd examples/get-started/csi_send
eim run "idf.py set-target esp32" v5.5.6
eim run "idf.py build" v5.5.6
eim run "idf.py -p /dev/ttyUSB0 -b 921600 flash" v5.5.6
```

The RX console runs at 921600 baud (`sdkconfig.defaults`); the TX console keeps
the ESP-IDF default of 115200, so use `-b 115200` when you monitor TX.

## Presence detector

```bash
cd examples/get-started/tools
uv run python csi_motion_detector.py -p /dev/ttyACM0 --metrics-csv metrics.csv
```

1. Keep the room empty for the first **15 seconds**; the script prints the
   baseline `mu` and `sigma` when calibration ends.
2. Walk between TX and RX to trigger detection. `Ctrl+C` stops it.

The thresholds derive from that empty-room baseline:

```
threshold_on  = mu + on_sigma  * sigma     (default 3.0)
threshold_off = mu + off_sigma * sigma     (default 1.5)
```

Too many false positives: `--on-sigma 6 --off-sigma 3`. Too sluggish:
`--on-sigma 2 --off-sigma 1`. Other useful flags:

```bash
uv run python csi_motion_detector.py -p /dev/ttyACM0 -w 300            # ~3 s window
uv run python csi_motion_detector.py -p /dev/ttyACM0 --edge-skip 0     # all subcarriers
uv run python csi_motion_detector.py -p /dev/ttyACM0 --ema-alpha 0.4   # faster EMA
uv run python csi_motion_detector.py -p /dev/ttyACM0 --calib 10        # shorter calibration
uv run python csi_motion_detector.py -p /dev/ttyACM0 -t 0.05           # fixed threshold, no calibration
```

## Occupancy meter

```bash
uv run python csi_occupancy_meter.py -p /dev/ttyACM0 --metrics-csv occupancy.csv
```

Adds an **activity score** on top of the motion score — how much the motion
score itself fluctuates — and reports three levels: `無人` (empty),
`1人（小動き）` (one person moving steadily), `複数人（ざわざわ）` (several people
or vigorous activity). The activity label only flips after three consecutive
packets on the new side.

| `--activity-metric` | Meaning |
|---------------------|---------|
| `cv` (default) | Coefficient of variation of the motion score; scale-free |
| `diff` | Standard deviation of first differences of the motion score |
| `peaks` | Local maxima per second above `mean + 1σ` |

```bash
uv run python csi_occupancy_meter.py -p /dev/ttyACM0 \
  --activity-metric diff --activity-threshold 0.2 --metrics-csv occupancy.csv
```

This is an **activity-level estimator, not a people counter**: one person moving
vigorously looks like several, several people standing still look empty.

## Proximity meter

```bash
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --metrics-csv proximity.csv
```

Tracks the **absolute** received energy (not the normalised motion score) and
reports `近づいている` / `遠ざかっている` / `静止` from the trend over
`--trend-window` seconds. The default threshold is `3 * sigma` of the trend
measured in an empty room.

```bash
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --energy-metric amplitude
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --trend-window 3.0 --ema-alpha 0.1
```

Multipath can make energy drop while a person approaches, so this is a **coarse**
direction estimate: recalibrate after moving either board.

## Web dashboard

The same detectors, in a browser, served by a local process that owns the serial
port:

```bash
cd examples/get-started/tools
uv run python csi_web_server.py -p /dev/ttyACM0     # then open http://127.0.0.1:8000/
```

| Option | Meaning |
|--------|---------|
| `-p`, `--port` / `-b`, `--baud` | CSI receiver board (default `/dev/ttyACM0` @ 921600) |
| `--http-port` | dashboard port (default `8000`) |
| `--host` | bind address (default `127.0.0.1`) |
| `--tool` | detector shown first: `motion`, `occupancy` or `proximity` |
| `--replay FILE` | replay a capture instead of reading the board |
| `--synthetic` | generate a synthetic stream (no hardware needed) |
| `--replay-rate` | packets/s for `--replay` and `--synthetic` (default `100`) |
| `--verbose` | log every HTTP request |

The page switches between the three detectors, shows the score chart with its
thresholds (for proximity: the trend and the `±threshold` band), the
per-subcarrier amplitude of the latest packet, a log of state transitions and
the packet counters. Calibration time, window, EMA and the tool-specific
settings can be changed in the browser; applying restarts calibration.

- **Light and dark**: follows the OS preference. The header button pins the
  opposite scheme; pressing it again returns to following the system.
- **Keyboard and screen readers**: the detector switch is a radio group, status
  changes are announced through a live region, and the charts carry a text
  summary of their current values.
- **Local only**: the server binds to `127.0.0.1`. `--host 0.0.0.0` publishes an
  unauthenticated dashboard *and* its config endpoint to the network, so use it
  deliberately.

`--replay` accepts any text file of raw `CSI_DATA` lines — including the `--csv`
output of the CLI tools — and loops it. `--synthetic` alternates 20 s of idle
link with 8 s of disturbed link at 100 packets/s, which is enough to watch
calibration, thresholds and status transitions without any board.

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | the dashboard (`web/index.html`, `style.css`, `app.js`) |
| `GET /api/stream` | server-sent events: a full snapshot on connect, then one tick per new packet |
| `GET /api/state` | the same snapshot as JSON |
| `POST /api/config` | switch detector or retune parameters; invalid values answer `400` |

## CSV output

`--csv FILE` writes the raw packets with the same columns as
`csi_data_read_parse.py` (`type,id,mac,rssi,...,len,first_word,data`).
`--metrics-csv FILE` writes one row per packet. All three tools share
`timestamp`, `packet_num` and `rssi`; the rest differ:

| Tool | Metric columns |
|------|----------------|
| motion | `raw_score`, `smoothed_score`, `status`, `baseline_mu`, `baseline_sigma`, `threshold_on`, `threshold_off` |
| occupancy | `motion_score`, `smoothed_motion`, `activity_score`, `activity_metric`, `occupancy_level`, `baseline_mu`, `baseline_sigma`, `motion_threshold_on`, `motion_threshold_off`, `activity_threshold` |
| proximity | `energy`, `smoothed_energy`, `trend`, `direction`, `energy_baseline_mu`, `energy_baseline_sigma`, `trend_threshold` |

`status` is `CALIBRATING` while the baseline is being learned, then the Japanese
label the tool prints.

## How the detection works

1. RX receives packets from TX at ~100 Hz; each `CSI_DATA` line carries the CSI
   as interleaved `Q, I` pairs.
2. **Per-packet normalisation**: amplitudes are divided by their own mean, which
   removes gain and RSSI drift.
3. **Score**: variance of the normalised amplitudes over a sliding window,
   averaged over the subcarriers that carry signal (guard, DC and null
   subcarriers are dropped).
4. **Auto-calibration**: the empty-room scores give `mu`/`sigma`, and the
   thresholds follow from them.
5. **Hysteresis**: entering and leaving a state use different thresholds, so the
   label does not chatter.
6. **Subcarrier changes**: if the radio re-tunes and the subcarrier count
   changes, the window and the smoothed values restart instead of reporting a
   jump.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `cannot open /dev/ttyACM0` | Another process holds the port. Close `idf.py monitor` (`Ctrl+]`) first. |
| `packets` stays at 0 | TX is not sending. Reset or power-cycle the TX board. |
| Always `有人` | TX and RX too close: move them 2–3 m apart and recalibrate. |
| Always `無人` | RSSI below -80 dBm, or thresholds too high: move the boards closer, lower `--on-sigma`. |
| Wrong chip error while flashing | The build directory was configured for another target: run `set-target` again. |
| Dashboard connects but never updates | The board is sending nothing, or the port is wrong. The page shows the server-side error once the serial link dies. |
| `Address already in use` on `--http-port` | Another dashboard is running: pick another port or stop it. |

## Modifications

This fork started from `espressif/esp-csi` (Apache License 2.0). Changes by
**Ayu102938**:

- 2026-10-23 — `examples/get-started/csi_recv/main/app_main.c`: disabled the
  `compensate_gain` log line so interleaved log output cannot break the parser.
- 2026-10-24 — same file: enabled `CONFIG_FORCE_GAIN` to stabilise AGC/FFT gains.
- 2026-10-24 — added `csi_motion_detector.py`, `csi_occupancy_meter.py` and
  `csi_proximity_meter.py`.
- 2026-11-19 — extracted `csi_common.py` (CSI front end) and `csi_detectors.py`
  (detector state machines); the CLI tools now only parse arguments and print.
- 2026-11-19 — added `csi_web_server.py` and `web/`: a local dashboard driving
  the same detectors.
- 2026-11-19 — rewrote this README as a setup guide and split it into
  `README.md` (English) and `README.ja.md` (Japanese).

Each modified file also carries its change notice in the header.

## Original documentation

- [Signal Processing Fundamentals](./docs/en/Signal-Processing-Fundamentals.md)
- [OFDM Introduction](./docs/en/OFDM-introduction.md)
- [Wireless Channel Fundamentals](./docs/en/Wireless-Channel-Fundamentals.md)

The upstream examples keep their own `README.md` under `examples/*/`.

## License

Apache License 2.0, the same as [espressif/esp-csi](https://github.com/espressif/esp-csi).
See [LICENSE](./LICENSE).

## Contributing

When you modify an existing file, add a modification notice near its top, as
Apache License 2.0 section 4(b) requires.
