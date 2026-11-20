# WiFi-Rader

[English](README.md) · **日本語**

ESP32 を 2 台（送信 TX / 受信 RX）使った Wi-Fi CSI センシングのセットアップです。
2 台の間を人が通ると無線チャネルが乱れるので、その変化から **在否・混雑度・接近方向**
を推定します。

本リポジトリは [espressif/esp-csi](https://github.com/espressif/esp-csi)（Apache License 2.0）を
Fork したものです。この Fork での追加・変更は[改変内容](#改変内容)にまとめています。

> **English version: [README.md](README.md)**

## 構成

| 場所 | 役割 |
|------|------|
| `examples/get-started/csi_send` | TX ファームウェア（パケットを送り続ける） |
| `examples/get-started/csi_recv` | RX ファームウェア（1 パケットごとに `CSI_DATA` 行を出力） |
| `examples/get-started/csi_recv_router` | ルーターを受信元にする上流の派生版 |
| `examples/esp-radar/*`, `examples/esp-crab/*` | 上流のサンプル（そのまま収録） |

`examples/get-started/tools/` の Python 側:

| ファイル | 役割 |
|----------|------|
| `csi_common.py` | 共通の前処理（行パース、サブキャリア選択、振幅正規化、シリアル読み取り、進捗表示、カウンタ） |
| `csi_detectors.py` | 3 つの検出器をクラス化（`MotionDetector` / `OccupancyMeter` / `ProximityMeter`） |
| `csi_motion_detector.py` | CLI: 有人 / 無人 |
| `csi_occupancy_meter.py` | CLI: 無人 / 1人 / 複数人 |
| `csi_proximity_meter.py` | CLI: 近づいている / 遠ざかっている / 静止 |
| `csi_web_server.py` + `web/` | 同じ検出器をブラウザで見るローカルダッシュボード |
| `csi_data_read_parse.py`, `csi_viewer.html` | 上流の GUI / Web Serial ビューア（生波形） |

CLI ツールは引数解析と表示だけを担当し、統計処理は `csi_detectors.py` にあります。
Web サーバも同じモジュールを import するので、検出ロジックの修正は全フロントエンドに
一度で反映されます。

## 必要な機材

| 項目 | 推奨 | 役割 |
|------|------|------|
| ESP32-S3 | ESP32-S3-DevKitC など | RX（受信機） |
| ESP32（オリジナル） | ESP32-WROOM-U など | TX（送信機） |
| USB ケーブル | 2 本 | RX 用（電源＋データ）、TX 用（電源） |
| PC | Linux（CachyOS/Arch x86_64 で動作確認） | ビルド・書き込み・実行 |

配置のコツ:

- **TX と RX は 2〜3 m 離す**
- RSSI が **-45〜-60 dBm** 程度になる位置が目安
- 直接波を完全に遮らない方がよい（反射波があると感度が上がる）
- 外部 IPEX アンテナの方が PCB アンテナより性能が出やすい

## 必要なソフトウェア

- **ESP-IDF** v5.5.6（`eim` で導入。標準の `export.sh` は bash スクリプトで fish では動きません）
- **Python** 3.10〜3.14（仮想環境は `uv`）
- `git`

動作確認環境: CachyOS（Arch 系）x86_64, fish 4.9.3, Python 3.14.8。

## セットアップ

```bash
cd ~/Projects
git clone https://github.com/Ayu102938/WiFi-Rader.git
cd WiFi-Rader
```

### ESP-IDF

```bash
# esp32 / esp32s3 用の xtensa ツールチェーン込みで v5.5.6 を導入
eim install -p ~/esp -i v5.5.6 -t esp32,esp32s3 -n true

# 確認
eim list
eim run "idf.py --version" v5.5.6
```

`eim` が `libwebkit2gtk-4.1.so.0` で失敗する場合: `sudo pacman -S --needed webkit2gtk-4.1`。

### Python 環境

```bash
cd examples/get-started/tools
uv venv
uv pip install numpy pyserial          # 3 つの CLI とダッシュボード
uv pip install -r requirements.txt     # 上流の GUI ビューアを使う場合のみ
```

`requirements.txt` は `csi_data_read_parse.py`（pandas, scipy, statsmodels, PyQt5,
pyqtgraph, matplotlib）向けです。検出器とダッシュボードは `numpy` と `pyserial`
だけで動きます。

## ファームウェアのビルドと書き込み

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

RX のコンソールは 921600 baud（`sdkconfig.defaults`）、TX は ESP-IDF 既定の
115200 です。TX をモニタするときは `-b 115200` を指定してください。

## 在否検出

```bash
cd examples/get-started/tools
uv run python csi_motion_detector.py -p /dev/ttyACM0 --metrics-csv metrics.csv
```

1. 最初の **15 秒間**、部屋に誰もいない状態にする（較正が終わると基準値 `mu` と
   `sigma` が表示されます）
2. TX と RX の間を歩いて検出を確認する。`Ctrl+C` で停止

閾値は空室時の基準値から決まります:

```
threshold_on  = mu + on_sigma  * sigma     (既定 3.0)
threshold_off = mu + off_sigma * sigma     (既定 1.5)
```

誤検知が多いときは `--on-sigma 6 --off-sigma 3`、反応が鈍いときは
`--on-sigma 2 --off-sigma 1`。その他のよく使うオプション:

```bash
uv run python csi_motion_detector.py -p /dev/ttyACM0 -w 300            # 窓を約 3 秒に
uv run python csi_motion_detector.py -p /dev/ttyACM0 --edge-skip 0     # 全サブキャリアを使う
uv run python csi_motion_detector.py -p /dev/ttyACM0 --ema-alpha 0.4   # EMA を速く
uv run python csi_motion_detector.py -p /dev/ttyACM0 --calib 10        # 較正を短く
uv run python csi_motion_detector.py -p /dev/ttyACM0 -t 0.05           # 固定閾値（較正なし）
```

## 混雑度メーター

```bash
uv run python csi_occupancy_meter.py -p /dev/ttyACM0 --metrics-csv occupancy.csv
```

動きの大きさ（motion score）に加えて、その**変動の頻度**（activity score）を使い、
`無人` / `1人（小動き）` / `複数人（ざわざわ）` の 3 段階で判定します。活動度の
ラベルは、新しい側に 3 パケット連続で振れたときだけ切り替わります。

| `--activity-metric` | 意味 |
|---------------------|------|
| `cv`（既定） | motion score の変動係数。スケールに依存しない |
| `diff` | motion score の 1 階差分の標準偏差 |
| `peaks` | `平均 + 1σ` を超える局所的最大値の毎秒の個数 |

```bash
uv run python csi_occupancy_meter.py -p /dev/ttyACM0 \
  --activity-metric diff --activity-threshold 0.2 --metrics-csv occupancy.csv
```

これは **活動度の推定であり人数計測ではありません**。1 人が激しく動けば複数人に、
複数人が静止していれば無人に見えることがあります。

## 近接方向メーター

```bash
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --metrics-csv proximity.csv
```

**絶対値**の受信エネルギー（正規化しない）を追跡し、`--trend-window` 秒前との差から
`近づいている` / `遠ざかっている` / `静止` を判定します。閾値の既定は、空室時に
測った trend の `3 * sigma` です。

```bash
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --energy-metric amplitude
uv run python csi_proximity_meter.py -p /dev/ttyACM0 --trend-window 3.0 --ema-alpha 0.1
```

マルチパス反射のため、近づいていてもエネルギーが下がることがあります。**おおまかな**
方向推定であり、基板を動かしたら再キャリブレーションしてください。

## Web ダッシュボード

同じ検出器をブラウザで表示します。ローカルのプロセスがシリアルポートを所有します:

```bash
cd examples/get-started/tools
uv run python csi_web_server.py -p /dev/ttyACM0     # → http://127.0.0.1:8000/
```

| オプション | 意味 |
|-----------|------|
| `-p`, `--port` / `-b`, `--baud` | CSI 受信機（既定 `/dev/ttyACM0` @ 921600） |
| `--http-port` | ダッシュボードのポート（既定 `8000`） |
| `--host` | バインド先（既定 `127.0.0.1`） |
| `--tool` | 最初に表示する検出器（`motion` / `occupancy` / `proximity`） |
| `--replay FILE` | 基板の代わりにキャプチャを再生 |
| `--synthetic` | 合成ストリームを生成（実機不要） |
| `--replay-rate` | `--replay` / `--synthetic` の毎秒パケット数（既定 `100`） |
| `--verbose` | HTTP リクエストをログ出力 |

画面では 3 つの検出器を切り替えられ、スコアと閾値のグラフ（近接は trend と
`±閾値` の帯）、直近パケットのサブキャリア振幅、状態遷移のログ、パケット数が
表示されます。較正時間・窓幅・EMA・各ツール固有の設定はブラウザから変更でき、
適用すると新しい設定でキャリブレーションをやり直します。

- **ライト／ダーク**: OS の設定に追従します。ヘッダーのボタンで逆側に固定でき、
  もう一度押すと OS 追従に戻ります
- **キーボード／スクリーンリーダー**: 検出器の切り替えはラジオボタン、状態の変化は
  live region で通知、グラフには現在値のテキスト要約が付きます
- **ローカル限定**: 既定では `127.0.0.1` にのみバインドします。`--host 0.0.0.0` は
  認証のないダッシュボードと設定エンドポイントをネットワークに公開するので、
  意図して使ってください

`--replay` は生の `CSI_DATA` 行を含むテキストファイル（各 CLI の `--csv` 出力を含む）
をループ再生します。`--synthetic` は 100 packets/s で「20 秒の空室」と「8 秒の動き
あり」を交互に生成するので、実機なしでキャリブレーション・閾値・状態遷移まで確認
できます。

### エンドポイント

| エンドポイント | 用途 |
|----------------|------|
| `GET /` | ダッシュボード（`web/index.html`, `style.css`, `app.js`） |
| `GET /api/stream` | SSE: 接続時にスナップショット、以降は新パケットごとに 1 tick |
| `GET /api/state` | 同じ内容を JSON で取得 |
| `POST /api/config` | 検出器の切替・パラメータ変更（不正値は `400`） |

## CSV 出力

`--csv FILE` は生の CSI パケットを `csi_data_read_parse.py` と同じ列
（`type,id,mac,rssi,...,len,first_word,data`）で書き出します。
`--metrics-csv FILE` は 1 パケット 1 行の指標を書き出します。3 ツール共通の列は
`timestamp`, `packet_num`, `rssi` で、残りは次のとおりです:

| ツール | 指標の列 |
|--------|----------|
| motion | `raw_score`, `smoothed_score`, `status`, `baseline_mu`, `baseline_sigma`, `threshold_on`, `threshold_off` |
| occupancy | `motion_score`, `smoothed_motion`, `activity_score`, `activity_metric`, `occupancy_level`, `baseline_mu`, `baseline_sigma`, `motion_threshold_on`, `motion_threshold_off`, `activity_threshold` |
| proximity | `energy`, `smoothed_energy`, `trend`, `direction`, `energy_baseline_mu`, `energy_baseline_sigma`, `trend_threshold` |

`status` は較正中は `CALIBRATING`、以降は画面に出す日本語ラベルです。

## 検出の仕組み

1. RX が TX から約 100 Hz でパケットを受信する（`CSI_DATA` 行は `Q, I` の交互配列）
2. **パケットごとの正規化**: 振幅をそのパケットの平均で割り、ゲインや RSSI の変動を除去
3. **スコア**: 正規化振幅の分散をスライディング窓で計算し、信号のあるサブキャリア
   （ガード・DC・ヌルを除く）で平均
4. **自動キャリブレーション**: 空室時のスコアから `mu` / `sigma` を学習し閾値を決定
5. **ヒステリシス**: 入る側と出る側で閾値を変え、ラベルが暴れないようにする
6. **サブキャリア数の変化**: 無線が再チューニングされて本数が変わったら、窓と平滑値を
   リセットして偽のジャンプを報告しない

## トラブルシューティング

| 症状 | 原因と対処 |
|------|-----------|
| `cannot open /dev/ttyACM0` | 他プロセスがポートを占有。`idf.py monitor`（`Ctrl+]`）を先に終了する |
| `packets` が 0 のまま | TX が送っていない。TX をリセット／電源を入れ直す |
| 常に `有人` | TX と RX が近すぎる。2〜3 m 離して再キャリブレーション |
| 常に `無人` | RSSI が -80 dBm より弱い、または閾値が高い。近づける／`--on-sigma` を下げる |
| 書き込み時に chip mismatch | ビルドディレクトリが別ターゲット。`set-target` をやり直す |
| ダッシュボードは繋がるが更新されない | 基板が送っていない／ポートが違う。シリアルが切れると画面にエラーが出る |
| `--http-port` で `Address already in use` | 既に別のダッシュボードが動作中。ポートを変えるか停止する |

## 改変内容

この Fork は `espressif/esp-csi`（Apache License 2.0）を出発点にしています。
**Ayu102938** による変更:

- 2026-10-23 — `examples/get-started/csi_recv/main/app_main.c`: ログ行の混在で
  パーサーが落ちないよう `compensate_gain` のログ出力を無効化
- 2026-10-24 — 同ファイル: AGC/FFT ゲインを安定させるため `CONFIG_FORCE_GAIN` を有効化
- 2026-10-24 — `csi_motion_detector.py`, `csi_occupancy_meter.py`,
  `csi_proximity_meter.py` を追加
- 2026-11-19 — `csi_common.py`（CSI 前処理）と `csi_detectors.py`（検出器の状態機械）を
  抽出。CLI ツールは引数解析と表示のみに
- 2026-11-19 — `csi_web_server.py` と `web/` を追加（同じ検出器を動かすローカル
  ダッシュボード）
- 2026-11-19 — 本 README をセットアップガイドとして書き直し、`README.md`（英語）と
  `README.ja.md`（日本語）に分割

変更したファイルには、それぞれ先頭に変更通知を入れています。

## 元のドキュメント

- [Signal Processing Fundamentals](./docs/en/Signal-Processing-Fundamentals.md)
- [OFDM Introduction](./docs/en/OFDM-introduction.md)
- [Wireless Channel Fundamentals](./docs/en/Wireless-Channel-Fundamentals.md)

上流のサンプルは `examples/*/README.md` にそれぞれの説明を残しています。

## ライセンス

上流の [espressif/esp-csi](https://github.com/espressif/esp-csi) と同じ
**Apache License 2.0** です。全文は [LICENSE](./LICENSE) を参照してください。

## 貢献

既存ファイルを変更する場合は、Apache License 2.0 第 4 条 (b) に従い、ファイル先頭に
変更通知を追加してください。
