/* Web dashboard: fetches the JSON the local server exposes and draws it.
 *
 * No framework and no CDN. The page is served by the widget's own process on
 * localhost, which is often a laptop with no network at the time, so a build
 * step or a remote script tag would be a way for the dashboard to break at
 * exactly the moment you want to look at it.
 *
 * Chart conventions match the Qt dashboard deliberately -- same palette (from
 * the generated theme.css), same rounded data-ends, same 2px gaps, same
 * selective labelling, same hover layer. The two are meant to be the same
 * product seen through two windows.
 */

'use strict';

const state = {
  period: 7,
  scope: 'me',
  tab: 'overview',
  tasks: [],
  filtered: [],
  sort: { key: 'ended_at', dir: -1 },
  summary: null,
  team: null,
  tile: null,
  token: new URLSearchParams(location.search).get('token') || '',
};

const POLL_MS = 6000;

/* --------------------------------------------------------------- helpers */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  return node;
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

async function api(path, options = {}) {
  const url = new URL(path, location.origin);
  if (state.token) url.searchParams.set('token', state.token);
  const headers = Object.assign({}, options.headers);
  if (state.token) headers['X-Dashboard-Token'] = state.token;
  const response = await fetch(url, Object.assign({}, options, { headers }));
  if (!response.ok) throw new Error(`${path} -> ${response.status}`);
  return response.json();
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 3200);
}

/* A round number at or above `value`, so the top gridline is readable and the
 * tallest bar is not flush against the frame. */
function niceCeiling(value) {
  if (!(value > 0)) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
    if (value <= step * base) return step * base;
  }
  return 10 * base;
}

function fmtTokens(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return Math.round(n / 1e3) + 'K';
  return String(Math.round(n));
}

/* ------------------------------------------------------------- tooltip */

const tip = (() => {
  let node = null;
  return {
    show(event, html) {
      if (!node) {
        node = el('div', { class: 'tip' });
        document.body.append(node);
      }
      node.innerHTML = html;
      node.style.display = 'block';
      // Flip before the edge rather than overflowing it.
      const box = node.getBoundingClientRect();
      let x = event.clientX + 13;
      let y = event.clientY + 13;
      if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 13;
      if (y + box.height > window.innerHeight - 8) y = event.clientY - box.height - 13;
      node.style.left = `${Math.max(8, x)}px`;
      node.style.top = `${Math.max(8, y)}px`;
    },
    hide() { if (node) node.style.display = 'none'; },
  };
})();

/* ---------------------------------------------------------- stat tiles */

function statTile(value, caption, colour, sub, hint) {
  return el('div', { class: 'tile', title: hint || '' },
    el('div', { class: 'v', style: `color:${colour}`, text: value }),
    el('div', { class: 'c', text: caption }),
    sub ? el('div', { class: 'sub', text: sub }) : null,
  );
}

function renderStatTiles(data) {
  const host = $('#stat-tiles');
  host.replaceChildren(
    statTile(String(data.tasks), 'tasks finished', css('--status-done'), null,
      'Turns the agent completed -- one Stop hook each'),
    statTile(data.tokens_h, 'tokens used', css('--series-1'), null,
      'Read from the session transcript, once per turn'),
    statTile(data.cost_h, 'estimated cost', css('--series-2'), null,
      'Tokens priced with the table in stats.PRICING -- an estimate, not a bill'),
    statTile(data.task_median_h, 'median task', css('--ink'), null,
      'Prompt to finish, for turns that completed'),
    statTile(data.reply_h, data.reply_label, css('--health-cpu'),
      `slowest ${data.reply_max_h}`,
      'How long an agent sat blocked before a person answered'),
    statTile(String(data.tools_per_task), 'tool-calls / task', css('--ink-dim'), null,
      'A rough proxy for how much work a task took'),
  );
}

/* -------------------------------------------------------- column chart */

function renderColumns(hostId, points, valueKey, colour, labelKey) {
  const host = $(hostId);
  host.replaceChildren();
  if (!points || !points.length) {
    host.append(el('p', { class: 'chart-empty', text: 'No activity in this window yet' }));
    return;
  }

  const W = host.clientWidth || 760;
  const H = 168;
  const TOP = 20, AXIS = 20;
  const plotH = H - TOP - AXIS;
  const peak = Math.max(...points.map((p) => Number(p[valueKey]) || 0));
  const top = niceCeiling(peak);
  const slot = W / points.length;
  const barW = Math.max(2, slot - 2);          /* the 2px surface gap */
  const peakIndex = points.findIndex((p) => (Number(p[valueKey]) || 0) === peak);

  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, height: H,
                            preserveAspectRatio: 'none' });

  for (const fraction of [0, 0.5, 1]) {
    const y = TOP + plotH - fraction * plotH;
    root.append(svg('line', { class: 'grid-line', x1: 0, x2: W, y1: y, y2: y }));
  }
  const scaleLabel = svg('text', { class: 'axis-text', x: W - 2, y: TOP - 7,
                                   'text-anchor': 'end' });
  scaleLabel.textContent = fmtTokens(top);
  root.append(scaleLabel);

  points.forEach((point, index) => {
    const value = Number(point[valueKey]) || 0;
    const x = index * slot + (slot - barW) / 2;
    const baseline = TOP + plotH;

    if (value <= 0) {
      /* A quiet day is a baseline tick, not a gap: dropping it would make the
       * axis lie about the days it covers. */
      root.append(svg('line', { class: 'empty-tick', x1: x, x2: x + barW,
                                y1: baseline, y2: baseline }));
    } else {
      const h = Math.max(2, (value / top) * plotH);
      /* Rounded at the data end only, anchored to the baseline. */
      const r = Math.min(4, barW / 2, h);
      const path = `M${x},${baseline} L${x},${TOP + plotH - h + r}`
        + ` Q${x},${TOP + plotH - h} ${x + r},${TOP + plotH - h}`
        + ` L${x + barW - r},${TOP + plotH - h}`
        + ` Q${x + barW},${TOP + plotH - h} ${x + barW},${TOP + plotH - h + r}`
        + ` L${x + barW},${baseline} Z`;
      root.append(svg('path', { d: path, fill: colour, class: 'bar cool' }));

      if (index === peakIndex) {
        const label = svg('text', { class: 'value-text', x: x + barW / 2,
                                    y: TOP + plotH - h - 5,
                                    'text-anchor': 'middle' });
        label.textContent = fmtTokens(value);
        root.append(label);
      }
    }

    /* A hit target the full height of the plot, wider than the bar, so a 2px
     * bar is still hoverable. */
    const hit = svg('rect', { class: 'bar-hit', x: index * slot, y: TOP,
                              width: slot, height: plotH });
    hit.addEventListener('mousemove', (event) => tip.show(event,
      `<b>${escapeHtml(point[labelKey] ?? point.label)}</b><br>`
      + `<span>${fmtTokens(value)} tokens · ${point.tasks || 0} tasks</span>`));
    hit.addEventListener('mouseleave', () => tip.hide());
    root.append(hit);
  });

  /* First and last only, so labels never collide. */
  [0, points.length - 1].forEach((index) => {
    if (index < 0) return;
    const text = svg('text', { class: 'axis-text',
      x: Math.min(Math.max(index * slot + slot / 2, 14), W - 14),
      y: H - 5, 'text-anchor': 'middle' });
    text.textContent = points[index][labelKey] ?? points[index].label;
    root.append(text);
  });

  host.append(root);
}

/* ---------------------------------------------------------- donut */

function renderDonut(hostId, rows, centre, caption) {
  const host = $(hostId);
  host.replaceChildren();
  const series = (rows || []).filter((r) => Number(r.tokens) > 0);
  if (!series.length) {
    host.append(el('p', { class: 'chart-empty', text: 'No tokens in this window' }));
    return;
  }

  const S = 148, R = 60, THICK = 17;
  const total = series.reduce((sum, r) => sum + Number(r.tokens), 0) || 1;
  const root = svg('svg', { viewBox: `0 0 ${S} ${S}`, width: S, height: S });

  let angle = -Math.PI / 2;
  series.forEach((row) => {
    const fraction = Number(row.tokens) / total;
    /* 2px of surface between slices, expressed as an angular gap, so a thin
     * slice is not swallowed by its neighbour. */
    const gap = series.length > 1 ? 2 / R : 0;
    const sweep = fraction * Math.PI * 2;
    const a0 = angle + gap / 2;
    const a1 = angle + sweep - gap / 2;
    angle += sweep;
    if (a1 <= a0) return;

    const x0 = S / 2 + R * Math.cos(a0), y0 = S / 2 + R * Math.sin(a0);
    const x1 = S / 2 + R * Math.cos(a1), y1 = S / 2 + R * Math.sin(a1);
    const large = (a1 - a0) > Math.PI ? 1 : 0;

    const arc = svg('path', {
      d: `M${x0},${y0} A${R},${R} 0 ${large} 1 ${x1},${y1}`,
      fill: 'none', stroke: row.color, 'stroke-width': THICK,
    });
    arc.addEventListener('mousemove', (event) => tip.show(event,
      `<b>${escapeHtml(row.name)}</b><br><span>${row.tokens_h} tokens · `
      + `${(fraction * 100).toFixed(0)}%</span>`));
    arc.addEventListener('mouseleave', () => tip.hide());
    root.append(arc);
  });

  const value = svg('text', { x: S / 2, y: S / 2 + 1, 'text-anchor': 'middle',
                              class: 'value-text', style: 'font-size:15px' });
  value.textContent = centre;
  root.append(value);
  const sub = svg('text', { x: S / 2, y: S / 2 + 16, 'text-anchor': 'middle',
                            class: 'axis-text' });
  sub.textContent = caption;
  root.append(sub);

  host.append(root);
  host.append(el('div', { class: 'legend' }, series.map((row) =>
    el('div', { class: 'legend-row' },
      el('span', { class: 'legend-sw', style: `background:${row.color}` }),
      el('span', { class: 'legend-name', text: row.name }),
      el('span', { class: 'legend-val',
                   text: `${(Number(row.tokens) / total * 100).toFixed(0)}%` }),
    ))));
}

/* ----------------------------------------------------- ranked bars */

function renderRanked(hostId, rows, colourFor, limit = 8) {
  const host = $(hostId);
  host.replaceChildren();
  let items = (rows || []).slice().sort((a, b) => b.tokens - a.tokens);
  if (!items.length) {
    host.append(el('p', { class: 'chart-empty', text: 'Nothing to show yet' }));
    return;
  }
  if (items.length > limit) {
    /* Fold the tail rather than dropping it, so the bars still add up to the
     * total in the tiles above. */
    const tail = items.slice(limit - 1);
    items = items.slice(0, limit - 1).concat([{
      name: `other (${tail.length})`,
      tokens: tail.reduce((s, r) => s + Number(r.tokens || 0), 0),
      tokens_h: fmtTokens(tail.reduce((s, r) => s + Number(r.tokens || 0), 0)),
      tasks: tail.reduce((s, r) => s + Number(r.tasks || 0), 0),
      folded: true,
    }]);
  }

  const top = Math.max(...items.map((r) => Number(r.tokens) || 0)) || 1;
  const wrap = el('div', { class: 'ranked' });
  items.forEach((row, index) => {
    const pct = (Number(row.tokens) || 0) / top * 100;
    const colour = row.folded ? css('--ink-muted') : colourFor(row, index);
    const node = el('div', { class: 'ranked-row' },
      el('div', { class: 'ranked-name', text: row.name, title: row.name }),
      el('div', { class: 'ranked-track' },
        el('div', { class: 'ranked-fill',
                    style: `width:${Math.max(pct, 0.6)}%;background:${colour}` })),
      el('div', { class: 'ranked-val', text: row.tokens_h || fmtTokens(row.tokens) }),
    );
    node.addEventListener('mousemove', (event) => tip.show(event,
      `<b>${escapeHtml(row.name)}</b><br><span>`
      + [`${row.tokens_h || fmtTokens(row.tokens)} tokens`,
         row.tasks ? `${row.tasks} tasks` : null,
         row.cost_h || null,
         row.task_median_h ? `median ${row.task_median_h}` : null]
        .filter(Boolean).join(' · ')
      + '</span>'));
    node.addEventListener('mouseleave', () => tip.hide());
    wrap.append(node);
  });
  host.append(wrap);
}

function renderTableView(hostId, rows, nameHeading) {
  const host = $(hostId);
  if (!host) return;
  const table = el('table', {},
    el('thead', {}, el('tr', {},
      el('th', { text: nameHeading }),
      el('th', { class: 'num', text: 'Tasks' }),
      el('th', { class: 'num', text: 'Tokens' }),
      el('th', { class: 'num', text: 'Cost' }),
      el('th', { class: 'num', text: 'Median task' }),
      el('th', { class: 'num', text: 'Reply' }),
    )),
    el('tbody', {}, (rows || []).map((row) => el('tr', {},
      el('td', { text: row.name }),
      el('td', { class: 'num', text: row.tasks }),
      el('td', { class: 'num', text: row.tokens_h }),
      el('td', { class: 'num', text: row.cost_h }),
      el('td', { class: 'num', text: row.task_median_h }),
      el('td', { class: 'num', text: row.reply_h }),
    ))),
  );
  host.replaceChildren(table);
}

/* ------------------------------------------------------- hour strip */

function renderHours(buckets) {
  const host = $('#hour-chart');
  host.replaceChildren();
  if (!buckets || !buckets.length) return;
  const peak = Math.max(...buckets.map((b) => Number(b.tokens) || 0)) || 1;
  const ramp = Array.from({ length: 8 }, (_, i) => css(`--seq-${i}`));

  const strip = el('div', { class: 'hours' });
  buckets.forEach((bucket) => {
    const value = Number(bucket.tokens) || 0;
    const colour = value <= 0
      ? css('--grid')
      : ramp[Math.round((value / peak) * (ramp.length - 1))];
    const cell = el('div', { class: 'hour-cell', style: `background:${colour}` });
    cell.addEventListener('mousemove', (event) => tip.show(event,
      `<b>${String(bucket.hour).padStart(2, '0')}:00</b><br>`
      + `<span>${fmtTokens(value)} tokens · ${bucket.tasks || 0} tasks</span>`));
    cell.addEventListener('mouseleave', () => tip.hide());
    strip.append(cell);
  });
  host.append(strip);

  const axis = el('div', { class: 'hour-axis' });
  for (let hour = 0; hour < 24; hour += 1) {
    axis.append(el('span', { text: hour % 6 === 0 ? String(hour).padStart(2, '0') : '' }));
  }
  host.append(axis);
}

/* ------------------------------------------------------------- tasks */

function applyTaskFilter() {
  const needle = ($('#task-filter').value || '').trim().toLowerCase();
  let rows = state.tasks;
  if (needle) {
    rows = rows.filter((task) =>
      (task.project || '').toLowerCase().includes(needle)
      || (task.prompt || '').toLowerCase().includes(needle)
      || (task.member || '').toLowerCase().includes(needle)
      || (task.model || '').toLowerCase().includes(needle));
  }
  const { key, dir } = state.sort;
  rows = rows.slice().sort((a, b) => {
    const left = a[key], right = b[key];
    if (typeof left === 'number' || typeof right === 'number') {
      return ((Number(left) || 0) - (Number(right) || 0)) * dir;
    }
    return String(left ?? '').localeCompare(String(right ?? '')) * dir;
  });
  state.filtered = rows;

  const body = $('#task-table tbody');
  body.replaceChildren(...rows.map((task) => {
    const hasPrompt = Boolean(task.prompt);
    return el('tr', {},
      el('td', { class: 'mono', text: task.when }),
      el('td', { class: 'col-member', text: task.member }),
      el('td', { text: task.project }),
      el('td', {
        class: `prompt${hasPrompt ? '' : ' none'}`,
        title: task.prompt || '',
        text: hasPrompt ? task.prompt : '— no prompt recorded —',
      }),
      el('td', { class: 'num mono', text: task.duration_h }),
      el('td', { class: 'num mono', text: task.blocked_h }),
      el('td', { class: 'num mono', style: `color:${css('--series-1')}`,
                 text: task.tokens_h }),
      el('td', { class: 'num mono', style: `color:${css('--series-2')}`,
                 text: task.cost_h }),
      el('td', { class: 'num mono', text: task.tools }),
      el('td', { html: `<span class="model-pip" style="background:${task.model_color}"></span>`
                       + escapeHtml(task.model) }),
    );
  }));

  $('#task-count').textContent = rows.length === state.tasks.length
    ? `${rows.length} tasks`
    : `${rows.length} of ${state.tasks.length} tasks`;
  $$('#task-table th').forEach((th) => {
    th.classList.toggle('sorted-asc', th.dataset.sort === key && dir === 1);
    th.classList.toggle('sorted-desc', th.dataset.sort === key && dir === -1);
  });
  $$('.col-member').forEach((node) => {
    node.style.display = state.scope === 'team' ? '' : 'none';
  });
}

/* -------------------------------------------------------------- team */

function meter(label, colour, fraction, readout) {
  return el('div', { class: 'meter' },
    el('span', { class: 'm-label', text: label }),
    el('div', { class: 'm-track' },
      el('div', { class: 'm-fill',
                  style: `width:${Math.max(0, Math.min(100, fraction * 100))}%;`
                         + `background:${colour}` })),
    el('span', { class: 'm-read', text: readout }),
  );
}

function renderTeam(data) {
  const banner = $('#team-banner');
  const status = data.status || {};
  if (!status.enrolled) {
    banner.className = 'banner warn';
    banner.textContent = 'This laptop is not enrolled in a team. Set it up on '
      + 'the Deploy tab of the desktop dashboard — nothing is sent anywhere '
      + 'until you do.';
  } else if (data.error) {
    banner.className = 'banner warn';
    banner.textContent = `Enrolled as ${status.member}. ${data.error}`;
  } else {
    banner.className = 'banner good';
    banner.textContent = `Team ${status.team_id} · board fetched ${data.fetched_h}`
      + ` · reporting as ${status.member}`;
  }

  $('#team-tiles').replaceChildren(
    statTile(String((data.devices || []).length), 'laptops reporting', css('--ink')),
    statTile(String(data.online || 0), 'online now', css('--status-done')),
    statTile(String(data.working || 0), 'working now', css('--status-working')),
    statTile(String(data.waiting || 0), 'blocked on a person',
             css('--status-needs-input')),
  );

  const fleet = $('#fleet');
  fleet.replaceChildren();
  if (!(data.devices || []).length) {
    fleet.append(el('p', { class: 'banner',
      text: 'No laptops reporting yet. The desktop dashboard\'s Deploy tab '
            + 'builds the installer that enrolls one.' }));
    return;
  }

  data.devices.forEach((device) => {
    const colour = device.online ? css('--status-done') : css('--status-offline');
    const meters = el('div', { class: 'meters' });
    if (device.cpu_pct !== null && device.cpu_pct !== undefined) {
      meters.append(meter('CPU', css('--health-cpu'), device.cpu_pct / 100,
        `${Math.round(device.cpu_pct)}%`
        + (device.cpu_temp ? `  ${Math.round(device.cpu_temp)}°C` : '')));
    }
    if (device.ram_pct !== null && device.ram_pct !== undefined) {
      meters.append(meter('RAM', css('--health-ram'), device.ram_pct / 100,
        `${Math.round(device.ram_pct)}%`
        + (device.ram_used_gb
            ? `  ${device.ram_used_gb.toFixed(1)}/${Math.round(device.ram_total_gb)}G`
            : '')));
    }
    if (device.gpu_pct !== null && device.gpu_pct !== undefined) {
      meters.append(meter('GPU', css('--health-gpu'), device.gpu_pct / 100,
        `${Math.round(device.gpu_pct)}%`
        + (device.gpu_temp ? `  ${Math.round(device.gpu_temp)}°C` : '')));
    }

    const card = el('div', { class: 'device' },
      el('div', { class: 'device-head' },
        el('span', { class: 'dot', style: `background:${colour}` }),
        el('span', { class: 'device-name', text: device.member }),
        el('span', { class: 'device-host', text: `· ${device.host || ''}` }),
        /* The word, not just the colour. */
        el('span', { class: 'device-state', style: `color:${colour}`,
          text: device.online ? 'online' : `last seen ${device.last_seen_h}` }),
      ),
      el('div', { class: 'device-facts' },
        el('div', { class: 'fact' },
          el('div', { class: 'v', text: String(device.working + device.waiting) }),
          el('div', { class: 'c', text: 'live sessions' })),
        el('div', { class: 'fact' },
          el('div', { class: 'v', style: `color:${css('--status-working')}`,
                      text: String(device.working) }),
          el('div', { class: 'c', text: 'working' })),
        el('div', { class: 'fact' },
          el('div', { class: 'v', style: `color:${css('--status-needs-input')}`,
                      text: String(device.waiting) }),
          el('div', { class: 'c', text: 'blocked' })),
        meters.children.length ? meters : null,
      ),
      (device.sessions || []).length
        ? el('div', { class: 'device-sessions' }, device.sessions.map((session) =>
            el('div', { class: 'session' },
              el('span', { class: 'dot', style: `background:${session.state_color}` }),
              el('span', { class: 's-state', style: `color:${session.state_color}`,
                           text: session.state_label }),
              el('span', { class: 's-project', text: session.project || '' }),
              el('span', { class: 's-msg', text: session.message || '',
                           title: session.message || '' }),
              el('span', { class: 's-age', text: session.age_h || '' }),
            )))
        : null,
    );
    fleet.append(card);
  });
}

/* -------------------------------------------------------------- tile */

const TILE_CONTROLS = {
  appearance: [
    { key: 'scale', type: 'range', label: 'Size', min: 60, max: 250, step: 5,
      scale: 100, unit: '%' },
    { key: 'opacity', type: 'range', label: 'Opacity', min: 20, max: 100, step: 5,
      scale: 100, unit: '%' },
    { key: 'bg_alpha', type: 'select', label: 'Background',
      options: [['Transparent', 0], ['Tinted', 190], ['Solid', 252]] },
    { key: 'fan_mode', type: 'select', label: 'Fan readout',
      options: [['RPM', 'rpm'], ['Duty %', 'duty']] },
    { key: 'fan_max_rpm', type: 'number', label: 'Fan sparkline full scale',
      min: 1000, max: 20000, step: 500 },
  ],
  behaviour: [
    { key: 'interval_ms', type: 'select', label: 'Update rate',
      options: [['0.5 s', 500], ['1 s', 1000], ['2 s', 2000], ['5 s', 5000]] },
    { key: 'slow_every', type: 'number', label: 'Slow-sensor divisor',
      min: 1, max: 20, step: 1,
      hint: 'Poll nvidia-smi and the embedded controller every Nth tick. '
            + 'Higher is cheaper; GPU and fan update less often.' },
    { key: 'click_through', type: 'toggle', label: 'Click-through',
      hint: 'Let clicks pass to whatever is underneath the tile' },
    { key: 'hide_from_capture', type: 'toggle', label: 'Hide from screenshots',
      hint: 'Keep the tile out of Print Screen, Snipping Tool and screen shares' },
    { key: 'pop_on_alert', type: 'toggle', label: 'Pop to front on alert',
      hint: 'Raise and flash when a session finishes or needs you' },
    { key: 'show_stats', type: 'toggle', label: 'Show stats line on the tile' },
    { key: 'locked', type: 'toggle', label: 'Lock position' },
    { key: 'auto_trim', type: 'toggle', label: 'Trim memory automatically' },
    { key: 'auto_trim_pct', type: 'number', label: 'Auto-trim above (%)',
      min: 50, max: 99, step: 1 },
    { key: 'auto_trim_gb', type: 'number', label: '…or in use above (GB)',
      min: 1, max: 256, step: 0.5 },
  ],
};

function renderTile(data) {
  const banner = $('#tile-banner');
  if (data.live) {
    banner.className = 'banner good';
    banner.textContent = 'The tile is running. Changes here reach it immediately.';
  } else {
    banner.className = 'banner warn';
    banner.textContent = 'No tile is running, so these are written to '
      + 'config.json and take effect the next time it starts.';
  }

  for (const [group, controls] of Object.entries(TILE_CONTROLS)) {
    const host = $(`#tile-${group}`);
    host.replaceChildren(...controls.map((spec) => tileControl(spec, data.settings)));
  }
}

function tileControl(spec, settings) {
  const current = settings[spec.key];
  const send = (value) => applyTileSetting(spec.key, value);

  if (spec.type === 'toggle') {
    const input = el('input', { type: 'checkbox' });
    input.checked = Boolean(current);
    input.addEventListener('change', () => send(input.checked));
    return el('div', { class: 'control toggle' },
      el('label', { text: spec.label }),
      el('label', { class: 'switch' }, input,
         el('span', { text: input.checked ? 'on' : 'off' })),
      spec.hint ? el('p', { class: 'hint', text: spec.hint }) : null,
    );
  }

  if (spec.type === 'select') {
    const select = el('select', {});
    spec.options.forEach(([text, value]) => {
      const option = el('option', { value: String(value), text });
      if (String(value) === String(current)) option.selected = true;
      select.append(option);
    });
    select.addEventListener('change', () => {
      const raw = select.value;
      send(/^-?\d+$/.test(raw) ? Number(raw) : raw);
    });
    return el('div', { class: 'control' },
      el('label', { text: spec.label }), select, el('span', {}),
      spec.hint ? el('p', { class: 'hint', text: spec.hint }) : null);
  }

  if (spec.type === 'number') {
    const input = el('input', { type: 'number', min: spec.min, max: spec.max,
                                step: spec.step, value: current ?? spec.min });
    input.addEventListener('change', () => send(Number(input.value)));
    return el('div', { class: 'control' },
      el('label', { text: spec.label }), input, el('span', {}),
      spec.hint ? el('p', { class: 'hint', text: spec.hint }) : null);
  }

  /* range */
  const scale = spec.scale || 1;
  const input = el('input', { type: 'range', min: spec.min, max: spec.max,
                              step: spec.step,
                              value: Math.round((current ?? 1) * scale) });
  const read = el('span', { class: 'read',
                            text: `${input.value}${spec.unit || ''}` });
  input.addEventListener('input', () => {
    read.textContent = `${input.value}${spec.unit || ''}`;
  });
  /* Commit on release, not on every pixel of the drag: each change repaints
   * the tile and rewrites config.json. */
  input.addEventListener('change', () => send(Number(input.value) / scale));
  return el('div', { class: 'control' },
    el('label', { text: spec.label }), input, read,
    spec.hint ? el('p', { class: 'hint', text: spec.hint }) : null);
}

async function applyTileSetting(key, value) {
  try {
    const result = await api('/api/tile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, value }),
    });
    toast(result.ok ? `${key} → ${value}` : `Could not set ${key}: ${result.note}`);
  } catch (error) {
    toast(`Could not set ${key}: ${error.message}`);
  }
  /* Read back rather than trusting the write: the tile clamps some values. */
  setTimeout(loadTile, 350);
}

/* ------------------------------------------------------------- loading */

async function loadOverview() {
  const data = await api(`/api/summary?period=${state.period}&scope=${state.scope}`);
  state.summary = data;
  renderStatTiles(data);

  /* One bar is not a trend: for Today the day chart would be a single column,
   * so the hour buckets take its place. */
  if (state.period === 0) {
    $('#trend-title').textContent = 'Tokens per hour today';
    $('#trend-note').textContent = '';
    renderColumns('#trend-chart',
      data.hourly.map((b) => ({ ...b, label: String(b.hour).padStart(2, '0') })),
      'tokens', css('--series-1'), 'label');
  } else {
    $('#trend-title').textContent = 'Tokens per day';
    $('#trend-note').textContent = `${data.period} days · quiet days shown as ticks`;
    renderColumns('#trend-chart', data.daily, 'tokens', css('--series-1'), 'label');
  }

  renderDonut('#model-chart', data.models, data.tokens_h, 'tokens');
  renderRanked('#project-chart', data.projects_rows,
    (_row, index) => css(`--series-${Math.min(index + 1, 8)}`));
  renderTableView('#project-table', data.projects_rows, 'Project');
  renderHours(data.hourly);
  $('#footer').textContent = `${data.tasks} tasks · ${data.tokens_h} tokens · `
    + `${data.cost_h} estimated · ${data.projects} projects in window`;
}

async function loadTasks() {
  const data = await api(`/api/tasks?period=${state.period}&scope=${state.scope}`);
  state.tasks = data.tasks || [];
  applyTaskFilter();
}

async function loadTeam() {
  const data = await api('/api/team');
  state.team = data;
  renderTeam(data);
  if (state.summary) {
    renderRanked('#member-chart', state.summary.members_rows,
      (_row, index) => css(`--series-${Math.min(index + 1, 8)}`));
    renderTableView('#member-table', state.summary.members_rows, 'Member');
  }
}

async function loadTile() {
  const data = await api('/api/tile');
  state.tile = data;
  renderTile(data);
}

async function refresh() {
  try {
    /* Overview is always loaded: the Team tab reuses its member rows, and the
     * footer reads its totals whichever tab is open. */
    await loadOverview();
    if (state.tab === 'tasks') await loadTasks();
    if (state.tab === 'team') await loadTeam();
    if (state.tab === 'tile') await loadTile();
  } catch (error) {
    $('#footer').textContent = `Could not load: ${error.message}`;
  }
}

/* --------------------------------------------------------------- wiring */

function showTab(name) {
  state.tab = name;
  $$('.tabs button').forEach((button) => {
    button.setAttribute('aria-selected', String(button.dataset.tab === name));
  });
  $$('.tab-panel').forEach((panel) => {
    panel.hidden = panel.id !== `tab-${name}`;
  });
  refresh();
}

function init() {
  $$('.segmented [data-period]').forEach((button) => {
    button.addEventListener('click', () => {
      state.period = Number(button.dataset.period);
      $$('.segmented [data-period]').forEach((b) => b.classList.toggle('on', b === button));
      refresh();
    });
  });
  $$('.segmented [data-scope]').forEach((button) => {
    button.addEventListener('click', () => {
      state.scope = button.dataset.scope;
      $$('.segmented [data-scope]').forEach((b) => b.classList.toggle('on', b === button));
      refresh();
    });
  });
  $$('.tabs button').forEach((button) => {
    button.addEventListener('click', () => showTab(button.dataset.tab));
  });
  $$('[data-table-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const target = $(`#${button.dataset.tableToggle}`);
      target.hidden = !target.hidden;
      button.textContent = target.hidden ? 'Table' : 'Chart';
    });
  });
  $('#task-filter').addEventListener('input', applyTaskFilter);
  $$('#task-table th').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      state.sort = {
        key,
        dir: state.sort.key === key ? -state.sort.dir : -1,
      };
      applyTaskFilter();
    });
  });
  $('#sync-btn').addEventListener('click', async () => {
    const button = $('#sync-btn');
    button.disabled = true;
    button.textContent = 'Syncing…';
    try {
      const report = await api(`/api/sync?period=${state.period}`, { method: 'POST' });
      toast(report.ok
        ? `Synced — sent ${report.tasks || 0} tasks`
        : `Sync problem: ${report.error || 'unknown'}`);
    } catch (error) {
      toast(`Sync failed: ${error.message}`);
    }
    button.disabled = false;
    button.textContent = 'Sync now';
    refresh();
  });

  api('/api/meta').then((meta) => {
    $('#meta-note').textContent = `${meta.member} · ${meta.events} events logged`
      + (meta.enrolled ? '' : ' · not enrolled');
  }).catch(() => {});

  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  /* A resize changes the SVG viewBox width the columns were laid out against. */
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(refresh, 220);
  });
}

document.addEventListener('DOMContentLoaded', init);
