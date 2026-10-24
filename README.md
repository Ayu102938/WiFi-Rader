# WiFi-Rader

> **English** below, **日本語** はその後に記載しています。

> This repository is a fork of [espressif/esp-csi](https://github.com/espressif/esp-csi),
> which is licensed under the **Apache License, Version 2.0**.
>
> Modified by **Ayu102938**:
> - 2026-10-23: Disabled the `compensate_gain` log output in
>   `examples/get-started/csi_recv/main/app_main.c` to prevent the Python parser
>   from crashing on interleaved log lines.
> - 2026-10-24: Enabled `CONFIG_FORCE_GAIN` in
>   `examples/get-started/csi_recv/main/app_main.c` to stabilize AGC/FFT gains
>   for presence detection.
> - 2026-10-24: Created `examples/get-started/tools/csi_motion_detector.py`, a
>   real-time human-presence detector with per-packet normalization, automatic
>   calibration, EMA smoothing, hysteresis, and metrics CSV output.
> - 2026-10-24: Created `examples/get-started/tools/csi_occupancy_meter.py`, a
>   three-level occupancy classifier that uses both motion magnitude and
>   fluctuation frequency to distinguish empty / one person / multiple people.
> - 2026-10-24: Created `examples/get-started/tools/csi_proximity_meter.py`, a
>   coarse approach/departure detector based on absolute CSI energy trends.
> - 2026-10-24: Rewrote this `README.md` as a setup guide for the WiFi-Rader
>   project.
>
> 本リポジトリは [espressif/esp-csi](https://github.com/espressif/esp-csi) を
> **Apache License, Version 2.0** で Fork したものです。
>
> **Ayu102938** による改変:
> - 2026-10-23: Python パーサーがログ行にクラッシュしないよう、
>   `examples/get-started/csi_recv/main/app_main.c` の `compensate_gain` ログ出力を無効化。
> - 2026-10-24: 在否検出の安定性向上のため、
>   `examples/get-started/csi_recv/main/app_main.c` の `CONFIG_FORCE_GAIN` を有効化。
> - 2026-10-24: リアルタイム在否検出スクリプト
>   `examples/get-started/tools/csi_motion_detector.py` を新規作成。
>   振幅正規化、自動キャリブレーション、EMA 平滑化、ヒステリシス、metrics CSV 出力を実装。
> - 2026-10-24: 3 段階混雑度分類スクリプト
>   `examples/get-started/tools/csi_occupancy_meter.py` を新規作成。
>   動きの大きさと変動頻度を組み合わせて、無人 / 1人 / 複数人を判定。
> - 2026-10-24: 近接方向推定スクリプト
>   `examples/get-started/tools/csi_proximity_meter.py` を新規作成。
>   絶対エネルギーの時間変化から、近づいている / 遠ざかっている / 静止 を判定。
> - 2026-10-24: 本 `README.md` を WiFi-Rader プロジェクトのセットアップガイドとして全面改稿。

---

## Overview / 概要

**WiFi-Rader** is a minimal Wi-Fi CSI (Channel State Information) sensing setup
using two ESP32 devices: one sender (TX) and one receiver (RX). It detects human
presence in real time by measuring how a person's body disturbs the wireless
channel between the two devices.

**WiFi-Rader** は、ESP32 を 2 台（送信機 TX / 受信機 RX）使った最小構成の
Wi-Fi CSI（Channel State Information）センシング環境です。2 台の間を人が通ると
無線チャネルが乱されることを利用し、リアルタイムで在否を検出します。

### What this fork adds / この Fork の追加機能

- Real-time presence detector (`csi_motion_detector.py`) with automatic calibration.
- Gain-stabilized RX firmware for cleaner idle baseline.
- Bilingual setup guide (EN/JP).

- 自動キャリブレーション付きリアルタイム在否検出スクリプト (`csi_motion_detector.py`)
- クリーンなアイドル基線を得るためのゲイン固定 RX ファームウェア
- 日英バイリンガルのセットアップガイド

---

## Hardware Requirements / 必要な機材

| Item | Recommended / 推奨 | Role / 役割 |
|------|-------------------|-------------|
| ESP32-S3 | ESP32-S3-DevKitC or equivalent | RX (receiver) |
| ESP32 (original) | ESP32-WROOM-U or equivalent | TX (sender) |
| USB cables | 2 pcs | Power + data for RX, power for TX |
| PC | Linux (tested on CachyOS/Arch x86_64) | Build, flash, and run detector |

| 項目 | 推奨 | 役割 |
|------|------|------|
| ESP32-S3 | ESP32-S3-DevKitC など | RX（受信機） |
| ESP32（オリジナル） | ESP32-WROOM-U など | TX（送信機） |
| USB ケーブル | 2 本 | RX 用（電源＋データ）、TX 用（電源） |
| PC | Linux（CachyOS/Arch x86_64 で動作確認） | ビルド・書き込み・検出実行 |

### Placement tips / 配置のコツ

- Keep **TX and RX 2–3 meters apart**.
- RSSI should settle around **-45 to -60 dBm**.
- Avoid blocking the direct path completely; multipath helps sensitivity.
- An external IPEX antenna works better than the PCB antenna.

- **TX と RX は 2〜3 m 離す**。
- RSSI は **-45〜-60 dBm** 程度になる位置が目安。
- 直接波を完全に遮らない方がよい。反射波（マルチパス）があると感度が上がる。
- 外部 IPEX アンテナの方が PCB アンテナより性能が出やすい。

---

## Software Requirements / 必要なソフトウェア

- **ESP-IDF** v5.5.6 (installed via `eim`)
- **Python** 3.10–3.14
- `eim` (Espressif Installation Manager)
- `uv` for Python virtual environments
- `git`

Tested environment / 動作確認環境:
- OS: CachyOS (Arch-based) x86_64
- Shell: fish 4.9.3
- Python: 3.14.8
- mise, uv, docker

---

## Repository Setup / リポジトリの準備

```bash
cd ~/Projects
git clone https://github.com/Ayu102938/WiFi-Rader.git
cd WiFi-Rader
```

---

## Installing ESP-IDF / ESP-IDF のインストール

We use `eim` because the standard `export.sh` is a bash script and does not work
with fish shell.

`export.sh` は bash スクリプトなので fish では使えません。`eim` を使います。

```bash
# Install ESP-IDF v5.5.6 with Xtensa toolchains for esp32 and esp32s3
eim install -p ~/esp -i v5.5.6 -t esp32,esp32s3 -n true

# Verify / 確認
eim list
eim run "idf.py --version" v5.5.6
```

If `eim` fails with `libwebkit2gtk-4.1.so.0`, install it:

`eim` が `libwebkit2gtk-4.1.so.0` で失敗した場合:

```bash
sudo pacman -S --needed webkit2gtk-4.1
```

---

## Building and Flashing the Firmware / ファームウェアのビルドと書き込み

### RX — ESP32-S3 (receiver / 受信機)

```bash
cd examples/get-started/csi_recv
eim run "idf.py set-target esp32s3" v5.5.6
eim run "idf.py build" v5.5.6
eim run "idf.py -p /dev/ttyACM0 -b 921600 flash" v5.5.6
```

### TX — ESP32-WROOM-U (sender / 送信機)

```bash
cd examples/get-started/csi_send
eim run "idf.py set-target esp32" v5.5.6
eim run "idf.py build" v5.5.6

# Connect the WROOM-U board, find its port, then flash
# WROOM-U を接続し、ポートを確認してから書き込み
ls -la /dev/ttyUSB*
eim run "idf.py -p /dev/ttyUSB0 -b 921600 flash" v5.5.6
```

> Note: The TX console baud rate is the ESP-IDF default (115200), while RX uses
> 921600. If you monitor TX, use `-b 115200`.
>
> TX のコンソールボーレートは ESP-IDF デフォルトの 115200、RX は 921600 です。
> TX のモニタを見る場合は `-b 115200` を使ってください。

---

## Running the Presence Detector / 在否検出スクリプトの実行

### 1. Prepare Python environment / Python 環境の準備

```bash
cd examples/get-started/tools
uv venv
uv pip install numpy pyserial
```

For the original viewer with GUI, install all requirements instead:

GUI 付きの元のビューワーを使う場合はすべてインストール:

```bash
uv pip install -r requirements.txt
```

### 2. Run / 実行

```bash
uv run python csi_motion_detector.py -p /dev/ttyACM0 --metrics-csv metrics.csv
```

### 3. Calibrate / キャリブレーション

1. Keep the room empty for the first **15 seconds**.
2. The script prints the baseline `mu` and `sigma`.
3. After calibration, walk between TX and RX to test detection.

1. 最初の **15 秒間**、部屋に誰もいない状態にする。
2. 基準値 `mu` と `sigma` が表示される。
3. キャリブレーション完了後、TX と RX の間を歩いて検出を確認。

### 4. Stop / 停止

Press `Ctrl+C`.

---

## Calibration Tips / キャリブレーションのコツ

The detector uses the idle baseline to set thresholds automatically:

```text
有人 threshold = mu + 3 * sigma
無人 threshold = mu + 1.5 * sigma
```

If you get false positives / 誤検知が多い場合:

```bash
uv run python csi_motion_detector.py -p /dev/ttyACM0 --on-sigma 6 --off-sigma 3
```

If the detector is too slow or misses motion / 反応が鈍い場合:

```bash
uv run python csi_motion_detector.py -p /dev/ttyACM0 --on-sigma 2 --off-sigma 1
```

Other useful options / その他のオプション:

```bash
# Larger window (3 s) / 大きな窓（3 秒）
uv run python csi_motion_detector.py -p /dev/ttyACM0 -w 300

# Use all subcarriers / 全サブキャリアを使用
uv run python csi_motion_detector.py -p /dev/ttyACM0 --edge-skip 0

# Faster EMA response / より素早い EMA 反応
uv run python csi_motion_detector.py -p /dev/ttyACM0 --ema-alpha 0.4

# Shorter calibration / 短いキャリブレーション
uv run python csi_motion_detector.py -p /dev/ttyACM0 --calib 10
```

---

## CSV Output / CSV 出力

Two CSV files can be produced:

2 種類の CSV を出力できます:

### Raw CSI packets / 生 CSI パケット

```bash
uv run python csi_motion_detector.py -p /dev/ttyACM0 --csv csi_capture.csv
```

Contains the same columns as `csi_data_read_parse.py`.

`csi_data_read_parse.py` と同じ列を含みます。

### Detection metrics / 検出指標

```bash
uv run python csi_motion_detector.py -p /dev/ttyACM0 --metrics-csv metrics.csv
```

Columns / 列:

| Column | Description |
|--------|-------------|
| `timestamp` | Packet reception time |
| `packet_num` | Sequential packet number |
| `raw_score` | Unsmoothed motion score |
| `smoothed_score` | EMA-smoothed score used for decisions |
| `status` | `CALIBRATING`, `有人`, or `無人` |
| `rssi` | Received signal strength indicator |
| `baseline_mu` | Idle baseline mean after calibration |
| `baseline_sigma` | Idle baseline standard deviation |
| `threshold_on` | Threshold to enter occupied state |
| `threshold_off` | Threshold to leave occupied state |

| 列 | 説明 |
|------|------|
| `timestamp` | パケット受信時刻 |
| `packet_num` | 連番 |
| `raw_score` | 平滑化前の動きスコア |
| `smoothed_score` | 判定に使われる EMA 平滑化後スコア |
| `status` | `CALIBRATING` / `有人` / `無人` |
| `rssi` | 受信信号強度 |
| `baseline_mu` | キャリブレーション後のアイドル基線平均 |
| `baseline_sigma` | アイドル基線の標準偏差 |
| `threshold_on` | 有人に移行する閾値 |
| `threshold_off` | 無人に戻る閾値 |

---

## 3-Level Occupancy Meter / 3 段階混雑度メーター

For a richer estimate than binary presence/absence, use `csi_occupancy_meter.py`.
It combines the existing motion score with an **activity score** that measures
how frequently the motion score fluctuates. This gives a three-level estimate:

| Level | Meaning |
|-------|---------|
| 無人 | Empty room |
| 1人（小動き） | One person, steady movement |
| 複数人（ざわざわ） | Multiple people or bustling activity |

在否検出よりも細かい情報が欲しい場合は `csi_occupancy_meter.py` を使います。
既存の動きスコアに加えて、スコアの変動頻度を表す **アクティビティスコア** を
組み合わせることで、3 段階の混雑度を推定します。

| 段階 | 意味 |
|------|------|
| 無人 | 部屋に誰もいない |
| 1人（小動き） | 1 人が規則的に動いている |
| 複数人（ざわざわ） | 複数人または活発な動き |

### Run / 実行

```bash
uv run python csi_occupancy_meter.py -p /dev/ttyACM0 --metrics-csv occupancy.csv
```

### Activity metrics / アクティビティ指標

Choose the fluctuation metric with `--activity-metric`:

| Metric | Description |
|--------|-------------|
| `cv` (default) | Coefficient of variation: std / mean of motion score over the activity window. Scale-free. |
| `diff` | Standard deviation of first differences of motion score. |
| `peaks` | Local maxima of motion score per second above `mean + 1σ`. |

`--activity-metric` で変動の測り方を選べます:

| 指標 | 説明 |
|------|------|
| `cv`（デフォルト） | 変動係数（標準偏差 ÷ 平均）。スケールに依存しない。 |
| `diff` | 動きスコアの 1 階差分の標準偏差。 |
| `peaks` | 1 秒あたりの局所的最大値の数（`平均 + 1σ` 以上）。 |

### Example / 例

```bash
# Use the diff metric with an explicit threshold
# diff 指標を使い、閾値を明示的に指定
uv run python csi_occupancy_meter.py -p /dev/ttyACM0 \
  --activity-metric diff --activity-threshold 0.2 \
  --metrics-csv occupancy.csv
```

### Important limitation / 重要な制限

This is an **activity-level estimator**, not a people counter.
A single person moving vigorously can look like multiple people, and several
people standing still can look like an empty room.

これは **活動度の推定** であり、正確な人数計測ではありません。
1 人が激しく動けば複数人に見えるし、複数人が静止していれば無人に見えることがあります。

---

## Approach / Departure Detection / 近づく・遠ざかる検出

`csi_proximity_meter.py` tracks the **absolute received energy** (not the
normalized motion score). When a person approaches the link, the received power
generally rises; when they move away, it falls.

`csi_proximity_meter.py` は **正規化していない受信エネルギー** を追跡します。
人が TX-RX 間に近づくと受信電力が上がり、遠ざかると下がる傾向を利用して
方向を推定します。

### Run / 実行

```bash
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --metrics-csv proximity.csv
```

### Output / 出力

| Direction | Meaning |
|-----------|---------|
| `近づいている` | Energy is rising (approaching) |
| `遠ざかっている` | Energy is falling (moving away) |
| `静止` | Energy is stable |

| 方向 | 意味 |
|------|------|
| `近づいている` | エネルギーが上昇（近づいている） |
| `遠ざかっている` | エネルギーが下降（遠ざかっている） |
| `静止` | エネルギーが安定 |

### Options / オプション

```bash
# Use amplitude instead of power
# パワーではなく振幅でエネルギーを計算
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --energy-metric amplitude

# Slower, steadier trend
# よりゆっくり安定したトレンド
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --trend-window 3.0 --ema-alpha 0.1
```

### Important limitation / 重要な制限

This is a **coarse proximity-direction estimator**. Multipath reflections can
make energy decrease even when a person is approaching, so it looks at
low-frequency trends over seconds, not instant-by-instant changes.

これは **おおまかな近接方向推定** です。
マルチパス反射の影響で、近づいていても一時的にエネルギーが減ることがあるため、
瞬間的な変化ではなく秒単位の低周波トレンドを見ます。

---

## How the Detection Works / 検出の仕組み

1. **RX receives packets** from TX at 100 Hz.
2. Each packet contains CSI: amplitude and phase for many subcarriers.
3. **Per-packet normalization**: divide amplitudes by their mean, removing
distance/gain dependence.
4. **Score**: variance of the normalized amplitudes over a sliding window.
5. **Auto-calibration**: learns idle `mu` and `sigma`.
6. **Hysteresis**: stable transitions between 有人 and 無人.

1. RX が TX から 100 Hz でパケットを受信。
2. 各パケットには多数のサブキャリアの振幅・位相（CSI）が含まれる。
3. **パケットごとの正規化**: 振幅を平均で割り、距離・ゲイン依存を除去。
4. **スコア**: 正規化振幅の分散を滑らかな窓で計算。
5. **自動キャリブレーション**: アイドル時の `mu` と `sigma` を学習。
6. **ヒステリシス**: 有人 / 無人を安定して切り替え。

---

## Troubleshooting / トラブルシューティング

### `cannot open /dev/ttyACM0`

Another process is holding the port. Close `idf.py monitor` first (`Ctrl+]`).

他のプロセスがポートを占有しています。`idf.py monitor` を先に終了（`Ctrl+]`）。

### `packets` stays at 0 / `packets` が 0 のまま

TX is not sending. Reset or power-cycle TX.

TX が送信していません。TX の電源を抜き差しまたはリセット。

### Score always high, constant 有人 / スコアが常に高く有人判定

- TX/RX may be too close. Move them 2–3 m apart.
- Use auto-calibration and check `mu`/`sigma`.

- TX/RX が近すぎる。2〜3 m 離す。
- 自動キャリブレーションを使い、`mu`/`sigma` を確認。

### Score always low, constant 無人 / スコアが常に低く無人判定

- Check RSSI. If weaker than -80 dBm, move boards closer.
- Lower `--on-sigma`.

- RSSI を確認。-80 dBm より弱い場合は近づける。
- `--on-sigma` を下げる。

### Wrong chip error during flash / 書き込み時に chip mismatch

You ran `idf.py` from a build directory configured for the wrong target.
Run `idf.py set-target esp32s3` (RX) or `set-target esp32` (TX) again.

`idf.py` を間違ったターゲットで設定されたビルドディレクトリから実行しています。
RX は `set-target esp32s3`、TX は `set-target esp32` を再度実行。

---

## Original Documentation / 元のドキュメント

This fork retains the upstream documentation under `docs/` and
`examples/*/README.md`.

本 Fork では元のドキュメント `docs/` および `examples/*/README.md` を残しています。

- [Signal Processing Fundamentals](./docs/en/Signal-Processing-Fundamentals.md)
- [OFDM Introduction](./docs/en/OFDM-introduction.md)
- [Wireless Channel Fundamentals](./docs/en/Wireless-Channel-Fundamentals.md)

---

## License / ライセンス

This project is licensed under the **Apache License, Version 2.0**, the same as
the upstream [espressif/esp-csi](https://github.com/espressif/esp-csi) project.

See [LICENSE](./LICENSE) for the full text.

本プロジェクトは上流の
[espressif/esp-csi](https://github.com/espressif/esp-csi) と同じく
**Apache License, Version 2.0** でライセンスされています。

全文は [LICENSE](./LICENSE) を参照してください。

---

## Contributing / 貢献

If you modify existing files, please add a modification notice near the top of
the file to comply with Apache License 2.0 Section 4(b).

既存ファイルを変更する場合、Apache License 2.0 第 4 条 (b) に従い、
ファイル先頭に変更通知を追加してください。
