'use strict';

// Dashboard front end.  It renders what the local server sends; the detection
// logic lives in Python (csi_detectors.py), so nothing here computes a score
// or a threshold.

const MAX_POINTS = 600;      // ~60 s at the server's 10 Hz tick rate
const MAX_EVENTS = 40;
const CHART_SECONDS = 60;
const THEME_KEY = 'color-scheme';

// Labels, tones and shapes come from the Python detectors.  The shape matters:
// state must not be conveyed by colour alone (WCAG 1.4.1).
const STATUS_STYLES = {
  '有人': { tone: 'danger', shape: 'triangle', hint: 'TX と RX の間で動きを検出しています。' },
  '無人': { tone: 'ok', shape: 'circle', hint: '動きは検出されていません。' },
  '1人（小動き）': { tone: 'ok', shape: 'circle', hint: '1 人が継続的に動いています。' },
  '複数人（ざわざわ）': { tone: 'warn', shape: 'diamond', hint: '変動が多く、複数人または活発な動きと推定。' },
  '近づいている': { tone: 'warn', shape: 'triangle', hint: '受信エネルギーが上昇しています。' },
  '遠ざかっている': { tone: 'danger', shape: 'diamond', hint: '受信エネルギーが下降しています。' },
  '静止': { tone: 'ok', shape: 'circle', hint: '受信エネルギーは安定しています。' },
  'CALIBRATING': { tone: 'accent', shape: 'circle', hint: '空室のベースラインを学習中です。' },
  'キャリブレーション中': { tone: 'accent', shape: 'circle', hint: '空室のベースラインを学習中です。' },
  '収集中': { tone: 'muted', shape: 'circle', hint: '判定に必要なパケットを収集中です。' },
};

const TOOLS = {
  motion: {
    chart: 'score / smoothed',
    wave: 'normalized amplitude',
    readouts: [
      ['score', 'score (raw)', 4],
      ['smoothed', 'smoothed', 4],
      ['threshold_on', 'on 閾値', 4],
      ['threshold_off', 'off 閾値', 4],
      ['mu', 'baseline μ', 4],
      ['sigma', 'baseline σ', 4],
      ['rssi', 'RSSI', 0],
    ],
  },
  occupancy: {
    chart: 'motion / smoothed',
    wave: 'normalized amplitude',
    readouts: [
      ['smoothed', 'motion', 4],
      ['activity', 'activity', 2],
      ['activity_threshold', 'activity 閾値', 2],
      ['threshold_on', 'on 閾値', 4],
      ['threshold_off', 'off 閾値', 4],
      ['rssi', 'RSSI', 0],
    ],
  },
  proximity: {
    chart: 'trend / 閾値',
    wave: 'raw amplitude',
    readouts: [
      ['score', 'energy', 0],
      ['smoothed', 'smoothed energy', 0],
      ['trend', 'trend', 0],
      ['threshold', 'trend 閾値', 0],
      ['rssi', 'RSSI', 0],
    ],
  },
};

const OCCUPIED_LABELS = new Set(['有人', '1人（小動き）', '複数人（ざわざわ）']);

const state = {
  config: null,
  source: null,
  metrics: {},
  counters: {},
  wave: [],
  points: [],
  events: [],
  lastEventId: 0,
  uptime: 0,
  connected: false,
  announcedStatus: null,
};

const el = {
  statusMark: document.getElementById('status-mark'),
  statusLabel: document.getElementById('status-label'),
  statusHint: document.getElementById('status-hint'),
  readouts: document.getElementById('readouts'),
  radios: Array.from(document.querySelectorAll('input[name="detector"]')),
  form: document.getElementById('params'),
  calib: document.getElementById('cfg-calib'),
  window: document.getElementById('cfg-window'),
  ema: document.getElementById('cfg-ema'),
  activityMetric: document.getElementById('cfg-activity-metric'),
  energyMetric: document.getElementById('cfg-energy-metric'),
  fieldActivity: document.getElementById('field-activity'),
  fieldEnergy: document.getElementById('field-energy'),
  formMessage: document.getElementById('form-message'),
  chart: document.getElementById('chart'),
  chartMetric: document.getElementById('chart-metric'),
  chartRange: document.getElementById('chart-range'),
  chartDescription: document.getElementById('chart-description'),
  wave: document.getElementById('wave'),
  waveMetric: document.getElementById('wave-metric'),
  waveCount: document.getElementById('wave-count'),
  waveDescription: document.getElementById('wave-description'),
  events: document.getElementById('events'),
  eventCount: document.getElementById('event-count'),
  sourceBadge: document.getElementById('source-badge'),
  sourceDetail: document.getElementById('source-detail'),
  cPackets: document.getElementById('c-packets'),
  cScored: document.getElementById('c-scored'),
  cSkipped: document.getElementById('c-skipped'),
  cOther: document.getElementById('c-other'),
  cUptime: document.getElementById('c-uptime'),
  linkState: document.getElementById('link-state'),
  announcer: document.getElementById('announcer'),
  themeToggle: document.getElementById('theme-toggle'),
  palette: document.getElementById('palette'),
};

// -- formatting ------------------------------------------------------------

function fmt(value, digits) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const abs = Math.abs(value);
  if (abs !== 0 && (abs < 0.001 || abs >= 100000)) return value.toExponential(2);
  return value.toFixed(digits === undefined ? 3 : digits);
}

function fmtSeconds(seconds) {
  if (!seconds) return '0 s';
  if (seconds < 90) return seconds.toFixed(1) + ' s';
  const minutes = Math.floor(seconds / 60);
  return minutes + ' min ' + Math.round(seconds % 60) + ' s';
}

function statusStyle(status) {
  return STATUS_STYLES[status]
    || { tone: 'muted', shape: 'circle', hint: '' };
}

// -- theme -----------------------------------------------------------------

const colorSchemeMeta = document.querySelector('meta[name="color-scheme"]');
const prefersDark = window.matchMedia('(prefers-color-scheme: dark)');

function systemTheme() {
  return prefersDark.matches ? 'dark' : 'light';
}

function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch (error) {
    return null;
  }
}

function applyTheme(value) {
  // ``light dark`` hands control back to the system preference; the CSS maps
  // this meta element onto :root { color-scheme }, so light-dark() tokens and
  // native controls follow along.
  colorSchemeMeta.content = value || 'light dark';
  document.dispatchEvent(new CustomEvent('themechange'));
}

el.themeToggle.addEventListener('click', () => {
  const current = storedTheme() || systemTheme();
  const target = current === 'dark' ? 'light' : 'dark';
  // Only pin the scheme when it differs from the system preference; otherwise
  // the toggle's real intent is "go back to following the OS".
  const next = target === systemTheme() ? null : target;
  try {
    if (next) localStorage.setItem(THEME_KEY, next);
    else localStorage.removeItem(THEME_KEY);
  } catch (error) {
    // Storage blocked: the change still applies for this page view.
  }
  applyTheme(next);
});

prefersDark.addEventListener('change', () => {
  document.dispatchEvent(new CustomEvent('themechange'));
});

window.addEventListener('storage', (event) => {
  if (event.key === THEME_KEY) applyTheme(storedTheme());
});

function readPalette() {
  const palette = {};
  el.palette.querySelectorAll('[data-token]').forEach((probe) => {
    palette[probe.dataset.token] = getComputedStyle(probe).color;
  });
  return palette;
}

// -- networking ------------------------------------------------------------

function connect() {
  const stream = new EventSource('api/stream');

  stream.addEventListener('hello', (event) => {
    const data = JSON.parse(event.data);
    state.config = data.config;
    state.source = data.source;
    state.metrics = data.metrics || {};
    state.counters = data.counters || {};
    state.wave = data.wave || [];
    state.points = data.history || [];
    state.events = data.events || [];
    state.uptime = data.uptime || 0;
    state.lastEventId = state.events.length ? state.events[0].id : 0;
    state.announcedStatus = null;
    applyConfig(state.config);
    render();
  });

  stream.onmessage = (event) => onTick(JSON.parse(event.data));
  stream.onopen = () => setConnected(true);
  stream.onerror = () => setConnected(false);
}

function onTick(data) {
  if (data.config && state.config && data.config.tool !== state.config.tool) {
    // Another tab switched the detector; restart the local view.
    state.points = [];
    state.events = [];
    state.lastEventId = 0;
    state.announcedStatus = null;
  }
  if (data.config) state.config = data.config;
  state.metrics = data.metrics || {};
  state.counters = data.counters || {};
  state.wave = data.wave || [];
  state.uptime = data.uptime || state.uptime;
  if (data.point) {
    state.points.push(data.point);
    if (state.points.length > MAX_POINTS) state.points.shift();
  }
  if (data.last_event && data.last_event.id > state.lastEventId) {
    state.lastEventId = data.last_event.id;
    state.events.unshift(data.last_event);
    if (state.events.length > MAX_EVENTS) state.events.pop();
  }
  if (data.error) showMessage('サーバ側エラー: ' + data.error);
  setConnected(true);
  render();
}

async function postConfig(updates) {
  try {
    const response = await fetch('api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    const data = await response.json();
    if (!response.ok) {
      showMessage(data.error || '設定を変更できませんでした。');
      return;
    }
    state.points = [];
    state.events = [];
    state.lastEventId = 0;
    state.announcedStatus = null;
    state.config = data.config;
    applyConfig(data.config);
    hideMessage();
    render();
  } catch (error) {
    showMessage('サーバに接続できません: ' + error.message);
  }
}

function showMessage(text) {
  el.formMessage.textContent = text;
  el.formMessage.hidden = false;
}

function hideMessage() {
  el.formMessage.hidden = true;
}

function setConnected(connected) {
  state.connected = connected;
  el.linkState.textContent = connected ? '接続中' : '切断 — 再接続しています';
  el.sourceBadge.textContent = connected ? sourceLabel() : '切断';
}

function sourceLabel() {
  if (!state.source) return '接続待ち';
  return { serial: 'シリアル', replay: 'リプレイ', synthetic: '合成データ' }[state.source.kind]
    || state.source.kind;
}

// -- controls --------------------------------------------------------------

function applyConfig(config) {
  if (!config) return;
  el.radios.forEach((radio) => {
    radio.checked = radio.value === config.tool;
  });
  el.fieldActivity.hidden = config.tool !== 'occupancy';
  el.fieldEnergy.hidden = config.tool !== 'proximity';
  setValue(el.calib, config.calib);
  setValue(el.window, config.window);
  setValue(el.ema, config.ema_alpha);
  el.activityMetric.value = config.activity_metric;
  el.energyMetric.value = config.energy_metric;
  const info = TOOLS[config.tool] || TOOLS.motion;
  el.chartMetric.textContent = info.chart;
  el.waveMetric.textContent = info.wave;
}

function setValue(input, value) {
  if (value === null || value === undefined) return;
  if (document.activeElement !== input) input.value = value;
}

el.radios.forEach((radio) => {
  radio.addEventListener('change', () => {
    if (radio.checked) postConfig({ tool: radio.value });
  });
});

el.form.addEventListener('submit', (event) => {
  event.preventDefault();
  const updates = {
    calib: el.calib.valueAsNumber,
    window: el.window.valueAsNumber,
    ema_alpha: el.ema.valueAsNumber,
  };
  if (state.config && state.config.tool === 'occupancy') {
    updates.activity_metric = el.activityMetric.value;
  }
  if (state.config && state.config.tool === 'proximity') {
    updates.energy_metric = el.energyMetric.value;
  }
  postConfig(updates);
});

// -- rendering -------------------------------------------------------------

function render() {
  renderStatus();
  renderReadouts();
  renderCounters();
  scheduleDraw();
  renderEvents();
}

function currentStatus() {
  const metrics = state.metrics;
  if (!metrics.status && metrics.calibrating) {
    return { label: 'CALIBRATING', style: statusStyle('CALIBRATING') };
  }
  if (!metrics.status) return { label: '収集中', style: statusStyle('収集中') };
  return { label: metrics.status, style: statusStyle(metrics.status) };
}

function renderStatus() {
  const { label, style } = currentStatus();
  const metrics = state.metrics;

  el.statusLabel.textContent = label;
  el.statusMark.dataset.shape = style.shape;
  el.statusMark.style.background = 'var(--' + style.tone + ')';

  let hint = style.hint;
  if (label === 'CALIBRATING') {
    hint = '空室のベースラインを学習中です（'
      + Math.round((metrics.calib_fraction || 0) * 100) + '%）。部屋を空けてください。';
  }
  el.statusHint.textContent = hint + (state.source ? ' · ' + state.source.description : '');

  // Announce transitions only: this changes a few times per minute, not at the
  // 10 Hz tick rate.
  if (label !== state.announcedStatus && label !== 'CALIBRATING' && label !== '収集中') {
    state.announcedStatus = label;
    const detail = metrics.activity !== null && metrics.activity !== undefined
      ? 'activity ' + fmt(metrics.activity, 2)
      : (metrics.trend !== null && metrics.trend !== undefined
        ? 'trend ' + fmt(metrics.trend, 0)
        : 'score ' + fmt(metrics.score, 4));
    el.announcer.textContent = state.connected
      ? label + '（' + detail + '）'
      : '';
  }
}

function renderReadouts() {
  const tool = state.config ? state.config.tool : 'motion';
  const info = TOOLS[tool] || TOOLS.motion;
  el.readouts.innerHTML = info.readouts.map(([key, label, digits]) => {
    const value = state.metrics[key];
    return '<div><dt>' + label + '</dt><dd>' + fmt(value, digits) + '</dd></div>';
  }).join('');
}

function renderCounters() {
  const counters = state.counters || {};
  el.cPackets.textContent = counters.packets || 0;
  el.cScored.textContent = counters.scored || 0;
  el.cSkipped.textContent = counters.skipped || 0;
  el.cOther.textContent = counters.other_lines || 0;
  el.cUptime.textContent = fmtSeconds(state.uptime);
  if (state.source) el.sourceDetail.textContent = state.source.description;
}

function renderEvents() {
  el.eventCount.textContent = String(state.events.length);
  if (!state.events.length) {
    el.events.innerHTML = '<li class="muted">まだ変化はありません。</li>';
    return;
  }
  el.events.innerHTML = state.events.map((event) => {
    const detail = event.activity !== null && event.activity !== undefined
      ? 'activity ' + fmt(event.activity, 2)
      : (event.trend !== null && event.trend !== undefined
        ? 'trend ' + fmt(event.trend, 0)
        : 'score ' + fmt(event.score, 4));
    return '<li><span class="when">' + fmtSeconds(event.t) + '</span>'
      + '<span class="what">' + escapeHtml(event.status) + '</span>'
      + '<span class="value">' + detail + '</span></li>';
  }).join('');
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}

// -- charts ----------------------------------------------------------------

let drawHandle = null;

function scheduleDraw() {
  if (drawHandle !== null) return;
  drawHandle = requestAnimationFrame(() => {
    drawHandle = null;
    drawChart();
    drawWave();
  });
}

function prepareCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(rect.width, 1);
  const height = Math.max(rect.height, 1);
  if (canvas.width !== Math.round(width * ratio)
      || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx: ctx, width: width, height: height };
}

function chartSpec(tool) {
  if (tool === 'proximity') {
    // The trend oscillates around zero and the thresholds are symmetric, so
    // the chart shows the trend and the band the classifier compares against.
    return {
      main: 'trend',
      faint: null,
      mode: 'symmetric',
      legend: ['trend', '+閾値', '−閾値'],
      description: 'trend の推移。赤は上側、黄は下側のしきい値です。',
    };
  }
  return {
    main: 'smoothed',
    faint: 'score',
    mode: 'upper',
    legend: ['smoothed', 'on 閾値', 'off 閾値'],
    description: 'スコアの推移。赤は有人しきい値、黄は無人しきい値です。',
  };
}

function drawChart() {
  const { ctx, width, height } = prepareCanvas(el.chart);
  const palette = readPalette();
  const points = state.points;
  const tool = state.config ? state.config.tool : 'motion';
  const spec = chartSpec(tool);
  el.chartRange.textContent = '直近 ' + CHART_SECONDS + ' 秒';
  el.chartDescription.textContent = spec.description;

  if (!points.length) {
    drawPlaceholder(ctx, width, height, 'データを待っています…', palette);
    el.chart.setAttribute('aria-label', 'スコア推移（データ待ち）');
    return;
  }

  const last = points[points.length - 1];
  const tMax = last.t;
  const tMin = Math.max(0, tMax - CHART_SECONDS);
  const visible = points.filter((point) => point.t >= tMin);
  const padLeft = 62;
  const padBottom = 20;
  const plotTop = 20;
  const plotWidth = width - padLeft - 12;
  const plotHeight = height - padBottom - plotTop;

  const threshold = Math.abs(typeof last.on === 'number' ? last.on : 0);
  const values = [];
  visible.forEach((point) => {
    if (typeof point[spec.main] === 'number') values.push(point[spec.main]);
    if (spec.faint && typeof point[spec.faint] === 'number') values.push(point[spec.faint]);
  });

  let yMax;
  let yMin;
  if (spec.mode === 'symmetric') {
    // Symmetric around zero, so 0 is one of the gridlines and the ±threshold
    // labels stay readable.
    const reach = Math.max(
      Math.abs(threshold),
      values.reduce((top, value) => Math.max(top, Math.abs(value)), 0),
      1e-9,
    );
    yMax = reach * 1.12;
    yMin = -yMax;
  } else {
    values.push(threshold);
    yMax = Math.max.apply(null, values);
    yMin = 0;
    if (!Number.isFinite(yMax) || yMax < 1e-9) yMax = 1;
    yMax += (yMax - yMin) * 0.12;
  }

  const x = (t) => padLeft + (plotWidth * (t - tMin)) / Math.max(tMax - tMin, 0.001);
  const y = (value) => plotTop + plotHeight * (yMax - value) / (yMax - yMin);

  // Busy spans: a light tint behind the curve.
  ctx.save();
  ctx.globalAlpha = 0.1;
  ctx.fillStyle = palette.danger;
  let spanStart = null;
  visible.forEach((point, index) => {
    const occupied = OCCUPIED_LABELS.has(point.status);
    if (occupied && spanStart === null) spanStart = point.t;
    if (!occupied && spanStart !== null) {
      ctx.fillRect(x(spanStart), plotTop, Math.max(x(point.t) - x(spanStart), 1), plotHeight);
      spanStart = null;
    }
    if (occupied && index === visible.length - 1) {
      ctx.fillRect(x(spanStart), plotTop, Math.max(x(point.t) - x(spanStart), 1), plotHeight);
    }
  });
  ctx.restore();

  // Grid and axis labels.
  ctx.save();
  ctx.globalAlpha = 0.22;
  ctx.strokeStyle = palette.muted;
  ctx.lineWidth = 1;
  ctx.font = '11px system-ui, sans-serif';
  for (let step = 0; step <= 4; step += 1) {
    const value = yMin + ((yMax - yMin) * step) / 4;
    const py = y(value);
    ctx.beginPath();
    ctx.moveTo(padLeft, py);
    ctx.lineTo(padLeft + plotWidth, py);
    ctx.stroke();
  }
  ctx.restore();

  ctx.fillStyle = palette.muted;
  ctx.font = '11px system-ui, sans-serif';
  for (let step = 0; step <= 4; step += 1) {
    const value = yMin + ((yMax - yMin) * step) / 4;
    ctx.fillText(terseNumber(value), 6, y(value) + 4);
  }

  if (spec.mode === 'symmetric') {
    ctx.save();
    ctx.globalAlpha = 0.35;
    ctx.strokeStyle = palette.text;
    ctx.beginPath();
    ctx.moveTo(padLeft, y(0));
    ctx.lineTo(padLeft + plotWidth, y(0));
    ctx.stroke();
    ctx.restore();
    if (threshold > 0) {
      drawDashed(ctx, threshold, palette.danger, x, y, padLeft, plotWidth);
      drawDashed(ctx, -threshold, palette.warn, x, y, padLeft, plotWidth);
    }
  } else {
    const onValue = typeof last.on === 'number' ? last.on : 0;
    const offValue = typeof last.off === 'number' ? last.off : 0;
    if (onValue > 0) drawDashed(ctx, onValue, palette.danger, x, y, padLeft, plotWidth);
    if (offValue > 0) drawDashed(ctx, offValue, palette.warn, x, y, padLeft, plotWidth);
  }

  if (spec.faint) {
    ctx.save();
    ctx.globalAlpha = 0.35;
    drawPolyline(ctx, visible, spec.faint, palette.accent, 1.2, x, y);
    ctx.restore();
  }
  drawPolyline(ctx, visible, spec.main, palette.accent, 2.2, x, y);

  // Legend, drawn from the same tokens as the lines.
  const legendColors = [palette.accent, palette.danger, palette.warn];
  let legendX = padLeft + 4;
  spec.legend.forEach((label, index) => {
    ctx.fillStyle = legendColors[index];
    ctx.fillText(label, legendX, 14);
    legendX += ctx.measureText(label).width + 18;
  });

  ctx.fillStyle = palette.muted;
  ctx.fillText(tMin.toFixed(0) + ' s', padLeft, height - 5);
  const right = tMax.toFixed(0) + ' s';
  ctx.fillText(right, padLeft + plotWidth - ctx.measureText(right).width, height - 5);

  // Text alternative for the canvas: the reading, not the pixels.
  el.chart.setAttribute('aria-label',
    'スコア推移。現在 ' + fmt(last[spec.main], 4)
    + '、しきい値 ' + fmt(threshold, 4) + '、直近 ' + CHART_SECONDS + ' 秒。');
}

function terseNumber(value) {
  if (value === 0) return '0';
  if (Math.abs(value) >= 1000 || Math.abs(value) < 0.01) return value.toExponential(1);
  return value.toFixed(value < 1 ? 3 : 1);
}

function drawDashed(ctx, value, color, x, y, padLeft, plotWidth) {
  ctx.save();
  ctx.setLineDash([6, 4]);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(padLeft, y(value));
  ctx.lineTo(padLeft + plotWidth, y(value));
  ctx.stroke();
  ctx.restore();
}

function drawPolyline(ctx, points, key, color, lineWidth, x, y) {
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.beginPath();
  let started = false;
  points.forEach((point) => {
    const value = point[key];
    if (typeof value !== 'number') { started = false; return; }
    if (!started) { ctx.moveTo(x(point.t), y(value)); started = true; }
    else ctx.lineTo(x(point.t), y(value));
  });
  ctx.stroke();
}

function drawWave() {
  const { ctx, width, height } = prepareCanvas(el.wave);
  const palette = readPalette();
  const wave = state.wave || [];
  el.waveCount.textContent = wave.length ? wave.length + ' subcarriers' : '—';

  if (!wave.length) {
    drawPlaceholder(ctx, width, height, 'パケットを待っています…', palette);
    el.wave.setAttribute('aria-label', 'サブキャリア振幅（データ待ち）');
    el.waveDescription.textContent = '';
    return;
  }

  let low = Math.min.apply(null, wave);
  let high = Math.max.apply(null, wave);
  if (!Number.isFinite(low) || !Number.isFinite(high)) {
    drawPlaceholder(ctx, width, height, 'データを待っています…', palette);
    return;
  }
  if (high - low < 1e-6) { low -= 0.5; high += 0.5; }
  const pad = (high - low) * 0.15;
  low -= pad;
  high += pad;

  const plotTop = 8;
  const plotBottom = height - 8;
  const barWidth = width / wave.length;
  const scale = (value) => plotBottom
    - ((Math.min(Math.max(value, low), high) - low) / (high - low)) * (plotBottom - plotTop);

  ctx.fillStyle = palette.accent;
  ctx.globalAlpha = 0.85;
  wave.forEach((value, index) => {
    const py = scale(value);
    ctx.fillRect(index * barWidth, py, Math.max(barWidth - 0.6, 0.6), plotBottom - py);
  });
  ctx.globalAlpha = 1;

  const tool = state.config ? state.config.tool : 'motion';
  el.waveDescription.textContent = tool === 'proximity'
    ? '直近パケットの受信振幅（' + wave.length + ' サブキャリア、'
      + fmt(low, 0) + '〜' + fmt(high, 0) + '）。'
    : '直近パケットの正規化振幅（' + wave.length + ' サブキャリア、平均 1 基準）。';
  el.wave.setAttribute('aria-label',
    'サブキャリア振幅 ' + wave.length + ' 本。最小 ' + fmt(low, 3)
    + '、最大 ' + fmt(high, 3) + '。');
}

function drawPlaceholder(ctx, width, height, text, palette) {
  ctx.fillStyle = palette ? palette.muted : 'currentColor';
  ctx.font = '13px system-ui, sans-serif';
  ctx.fillText(text, 16, height / 2);
}

// Redraw when the containers change size (window resizes are only one cause)
// and when the colour scheme changes, since the canvas caches pixels.
const resizeObserver = new ResizeObserver(() => scheduleDraw());
resizeObserver.observe(el.chart);
resizeObserver.observe(el.wave);
document.addEventListener('themechange', () => scheduleDraw());

connect();
