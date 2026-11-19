'use strict';

// Dashboard front end.  It only renders what the local server sends: the
// detection logic lives in Python (csi_detectors.py), so this file never
// computes a score or a threshold.

const MAX_POINTS = 600;      // ~60 s at the server's 10 Hz tick rate
const MAX_EVENTS = 40;
const CHART_SECONDS = 60;

// Status labels come from the Python detectors; colours and hints match them.
const STATUS_STYLES = {
  '有人': { color: 'var(--danger)', hint: 'TX と RX の間で動きを検出しています。' },
  '無人': { color: 'var(--ok)', hint: '動きは検出されていません。' },
  '1人（小動き）': { color: 'var(--ok)', hint: '1 人が継続的に動いています。' },
  '複数人（ざわざわ）': { color: 'var(--warn)', hint: '変動が多く、複数人または活発な動きと推定。' },
  '近づいている': { color: 'var(--warn)', hint: '受信エネルギーが上昇しています。' },
  '遠ざかっている': { color: 'var(--danger)', hint: '受信エネルギーが下降しています。' },
  '静止': { color: 'var(--ok)', hint: '受信エネルギーは安定しています。' },
  'CALIBRATING': { color: 'var(--accent)', hint: '空室のベースラインを学習中です。' },
  'キャリブレーション中': { color: 'var(--accent)', hint: '空室のベースラインを学習中です。' },
  '収集中': { color: 'var(--muted)', hint: '判定に必要なパケットを収集中です。' },
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
};

const el = {
  badge: document.getElementById('status-badge'),
  hint: document.getElementById('status-hint'),
  readouts: document.getElementById('readouts'),
  tabs: Array.from(document.querySelectorAll('#tool-tabs .tab')),
  calib: document.getElementById('cfg-calib'),
  window: document.getElementById('cfg-window'),
  ema: document.getElementById('cfg-ema'),
  activityMetric: document.getElementById('cfg-activity-metric'),
  energyMetric: document.getElementById('cfg-energy-metric'),
  fieldActivity: document.getElementById('field-activity'),
  fieldEnergy: document.getElementById('field-energy'),
  apply: document.getElementById('apply'),
  message: document.getElementById('message'),
  chart: document.getElementById('chart'),
  chartMetric: document.getElementById('chart-metric'),
  chartRange: document.getElementById('chart-range'),
  wave: document.getElementById('wave'),
  waveMetric: document.getElementById('wave-metric'),
  waveCount: document.getElementById('wave-count'),
  events: document.getElementById('events'),
  eventCount: document.getElementById('event-count'),
  sourceBadge: document.getElementById('source-badge'),
  sourceDetail: document.getElementById('source-detail'),
  uptime: document.getElementById('uptime'),
  cPackets: document.getElementById('c-packets'),
  cScored: document.getElementById('c-scored'),
  cSkipped: document.getElementById('c-skipped'),
  cOther: document.getElementById('c-other'),
  linkState: document.getElementById('link-state'),
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
  return STATUS_STYLES[status] || { color: 'var(--muted)', hint: '' };
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
    applyConfig(state.config);
    render();
  });

  stream.onmessage = (event) => {
    const data = JSON.parse(event.data);
    onTick(data);
  };

  stream.onopen = () => setConnected(true);
  stream.onerror = () => setConnected(false);
}

function onTick(data) {
  if (data.config && state.config && data.config.tool !== state.config.tool) {
    // The detector was switched from another tab; restart the local view.
    state.points = [];
    state.events = [];
    state.lastEventId = 0;
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
    state.config = data.config;
    applyConfig(data.config);
    hideMessage();
    render();
  } catch (error) {
    showMessage('サーバに接続できません: ' + error.message);
  }
}

function showMessage(text) {
  el.message.textContent = text;
  el.message.hidden = false;
}

function hideMessage() {
  el.message.hidden = true;
}

function setConnected(connected) {
  state.connected = connected;
  el.linkState.textContent = connected ? '接続中' : '切断 — 再接続しています';
  el.linkState.style.color = connected ? 'var(--ok)' : 'var(--warn)';
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
  el.tabs.forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.tool === config.tool);
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

el.tabs.forEach((tab) => {
  tab.addEventListener('click', () => postConfig({ tool: tab.dataset.tool }));
});

el.apply.addEventListener('click', () => {
  const updates = {
    calib: Number(el.calib.value),
    window: Number(el.window.value),
    ema_alpha: Number(el.ema.value),
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
  drawChart();
  drawWave();
  renderEvents();
}

function renderStatus() {
  const metrics = state.metrics;
  let status = metrics.status;
  let hint;

  if (!status && metrics.calibrating) {
    status = 'CALIBRATING';
    const percent = Math.round((metrics.calib_fraction || 0) * 100);
    hint = '空室のベースラインを学習中です（' + percent + '%）。部屋を空けてください。';
  } else if (!status) {
    status = '収集中';
  }

  const style = statusStyle(status);
  el.badge.textContent = status;
  el.badge.style.color = style.color;
  el.badge.style.borderColor = style.color;
  el.badge.style.background = 'rgba(255, 255, 255, 0.04)';
  el.hint.textContent = (hint || style.hint) + ' · ' + sourceLabel()
    + (state.source ? ' — ' + state.source.description : '');
  el.uptime.textContent = fmtSeconds(state.uptime);
  if (state.source) el.sourceDetail.textContent = state.source.description;
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
}

function renderEvents() {
  el.eventCount.textContent = String(state.events.length);
  if (!state.events.length) {
    el.events.innerHTML = '<li class="muted">まだ変化はありません。</li>';
    return;
  }
  el.events.innerHTML = state.events.map((event) => {
    const style = statusStyle(event.status);
    const detail = event.activity !== null && event.activity !== undefined
      ? 'activity ' + fmt(event.activity, 2)
      : (event.trend !== null && event.trend !== undefined
        ? 'trend ' + fmt(event.trend, 0)
        : 'score ' + fmt(event.score, 4));
    return '<li><span class="t">' + fmtSeconds(event.t) + '</span>'
      + '<span class="s" style="color:' + style.color + '">' + event.status + '</span>'
      + '<span class="v">' + detail + '</span></li>';
  }).join('');
}

// -- charts ----------------------------------------------------------------

function prepareCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = Number(canvas.getAttribute('height'));
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
    };
  }
  return {
    main: 'smoothed',
    faint: 'score',
    mode: 'upper',
    legend: ['smoothed', 'on 閾値', 'off 閾値'],
  };
}

function drawChart() {
  const { ctx, width, height } = prepareCanvas(el.chart);
  const points = state.points;
  const tool = state.config ? state.config.tool : 'motion';
  const spec = chartSpec(tool);
  el.chartRange.textContent = '直近 ' + CHART_SECONDS + ' 秒';

  if (!points.length) {
    drawPlaceholder(ctx, width, height, 'データを待っています…');
    return;
  }

  const last = points[points.length - 1];
  const tMax = last.t;
  const tMin = Math.max(0, tMax - CHART_SECONDS);
  const visible = points.filter((point) => point.t >= tMin);
  const padLeft = 56;
  const padBottom = 22;
  const plotTop = 22;
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
    values.push(threshold, -threshold, 0);
    yMax = Math.max.apply(null, values);
    yMin = Math.min.apply(null, values);
  } else {
    values.push(threshold);
    yMax = Math.max.apply(null, values);
    yMin = 0;
  }
  if (!Number.isFinite(yMax) || !Number.isFinite(yMin) || yMax - yMin < 1e-9) {
    yMax = (yMax || 0) + 1;
    yMin = spec.mode === 'symmetric' ? -1 : 0;
  }
  const headroom = (yMax - yMin) * 0.12;
  yMax += headroom;
  if (spec.mode === 'symmetric') yMin -= headroom;

  const x = (t) => padLeft + (plotWidth * (t - tMin)) / Math.max(tMax - tMin, 0.001);
  const y = (value) => plotTop + plotHeight * (yMax - value) / (yMax - yMin);

  // occupied spans, so a glance shows when the room was busy
  ctx.fillStyle = 'rgba(255, 107, 129, 0.10)';
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

  // grid + y labels
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  ctx.font = '11px system-ui, sans-serif';
  for (let step = 0; step <= 4; step += 1) {
    const value = yMin + ((yMax - yMin) * step) / 4;
    const py = y(value);
    ctx.beginPath();
    ctx.moveTo(padLeft, py);
    ctx.lineTo(padLeft + plotWidth, py);
    ctx.stroke();
    ctx.fillStyle = 'rgba(142,163,194,0.9)';
    ctx.fillText(terseNumber(value), 6, py + 4);
  }

  if (spec.mode === 'symmetric') {
    ctx.strokeStyle = 'rgba(255,255,255,0.18)';
    ctx.beginPath();
    ctx.moveTo(padLeft, y(0));
    ctx.lineTo(padLeft + plotWidth, y(0));
    ctx.stroke();
    ctx.fillStyle = 'rgba(142,163,194,0.9)';
    ctx.fillText('0', 6, y(0) + 4);
    drawDashed(ctx, threshold, 'rgba(255, 107, 129, 0.85)', x, y, padLeft, plotWidth);
    drawDashed(ctx, -threshold, 'rgba(255, 190, 92, 0.85)', x, y, padLeft, plotWidth);
  } else {
    const onValue = typeof last.on === 'number' ? last.on : 0;
    const offValue = typeof last.off === 'number' ? last.off : 0;
    if (onValue > 0) drawDashed(ctx, onValue, 'rgba(255, 107, 129, 0.85)', x, y, padLeft, plotWidth);
    if (offValue > 0) drawDashed(ctx, offValue, 'rgba(255, 190, 92, 0.85)', x, y, padLeft, plotWidth);
  }

  if (spec.faint) drawPolyline(ctx, visible, spec.faint, 'rgba(90, 200, 250, 0.35)', 1.2, x, y);
  drawPolyline(ctx, visible, spec.main, 'rgba(90, 200, 250, 1)', 2.2, x, y);

  // legend
  const colors = ['rgba(90, 200, 250, 1)', 'rgba(255, 107, 129, 0.9)', 'rgba(255, 190, 92, 0.9)'];
  let legendX = padLeft + 4;
  spec.legend.forEach((label, index) => {
    ctx.fillStyle = colors[index];
    ctx.fillText(label, legendX, 16);
    legendX += ctx.measureText(label).width + 18;
  });

  // time axis
  ctx.fillStyle = 'rgba(142,163,194,0.9)';
  ctx.fillText(tMin.toFixed(0) + ' s', padLeft, height - 6);
  const right = tMax.toFixed(0) + ' s';
  ctx.fillText(right, padLeft + plotWidth - ctx.measureText(right).width, height - 6);
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
  const wave = state.wave || [];
  el.waveCount.textContent = wave.length ? wave.length + ' subcarriers' : '—';
  if (!wave.length) {
    drawPlaceholder(ctx, width, height, 'パケットを待っています…');
    return;
  }

  let low = Math.min(...wave);
  let high = Math.max(...wave);
  if (!Number.isFinite(low) || !Number.isFinite(high)) {
    drawPlaceholder(ctx, width, height, 'データを待っています…');
    return;
  }
  if (high - low < 1e-6) { low -= 0.5; high += 0.5; }
  const pad = (high - low) * 0.15;
  low -= pad;
  high += pad;

  const plotTop = 10;
  const plotBottom = height - 10;
  const barWidth = width / wave.length;
  const baseline = plotBottom - ((0 - low) / (high - low)) * (plotBottom - plotTop);

  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.beginPath();
  ctx.moveTo(0, Math.min(Math.max(baseline, plotTop), plotBottom));
  ctx.lineTo(width, Math.min(Math.max(baseline, plotTop), plotBottom));
  ctx.stroke();

  wave.forEach((value, index) => {
    const clamped = Math.min(Math.max(value, low), high);
    const py = plotBottom - ((clamped - low) / (high - low)) * (plotBottom - plotTop);
    const top = Math.min(py, plotBottom);
    ctx.fillStyle = index % 2 ? 'rgba(61, 220, 151, 0.75)' : 'rgba(90, 200, 250, 0.75)';
    ctx.fillRect(index * barWidth, top, Math.max(barWidth - 0.6, 0.6), plotBottom - top);
  });
}

function drawPlaceholder(ctx, width, height, text) {
  ctx.fillStyle = 'rgba(142,163,194,0.8)';
  ctx.font = '13px system-ui, sans-serif';
  ctx.fillText(text, 16, height / 2);
}

window.addEventListener('resize', () => { drawChart(); drawWave(); });

connect();
