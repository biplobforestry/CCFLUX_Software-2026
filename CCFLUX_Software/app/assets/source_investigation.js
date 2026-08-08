/* Source Investigation: the gases together, a region by eye, then the wind.
 *
 * The page holds three things a reader needs at once. The rows are where a
 * feature is spotted - several species on one panel, because a plume is
 * recognised by which of them move together. The region is chosen by dragging
 * across a row. The map and the wind rose answer where it was and where the air
 * came from, which is what turns an enhancement into a direction to look in.
 *
 * Two rules the page keeps to. Smoothing is display only: the shaded band
 * behind every line is the raw excursion of the samples that drawn point stands
 * for, so a spike between two plotted points is still visible, and every number
 * under the map is computed by the server from the raw record. And nothing is
 * offered that this flight does not carry - the channel list comes from the
 * data, not from a hard-coded list of ten.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    channels: [],        // what this flight can draw
    byKey: new Map(),
    rows: [],            // [{left:[entry], right:[entry]}]
    data: null,          // the last row payload
    region: null,        // {start, end}
    analysis: null,
    map: null,
    trackLayer: null,
    regionLayer: null,
    busy: false,
  };

  // Distinct at a glance and distinguishable in the common colour deficiencies.
  const PALETTE = ['#1756d1', '#d1471a', '#159447', '#8931ef', '#c79a00',
                   '#0e7773', '#b3401a', '#4d4d4d'];

  function show(kind, text) {
    const box = $('message');
    box.className = `message ${kind}`;
    box.textContent = text;
  }
  function clearMessage() { $('message').className = 'message'; }

  async function ask(path, body) {
    const options = body === undefined
      ? {cache: 'no-store'}
      : {method: 'POST', headers: {'Content-Type': 'application/json'},
         body: JSON.stringify(body)};
    const response = await fetch(path, options);
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch (error) { payload = null; }
    if (!response.ok) {
      throw new Error((payload && (payload.error || payload.message)) || text
        || `Request failed (${response.status})`);
    }
    return payload;
  }

  // -- time -----------------------------------------------------------------
  // The record is naive UTC throughout the project, so the inputs are read and
  // written as plain text rather than through Date, which would apply the
  // browser's own zone and shift every bound by an hour in summer.
  const forInput = (iso) => String(iso || '').slice(0, 19);
  const fromInput = (value) => (value ? String(value).slice(0, 19) : null);

  // -- the axis menu --------------------------------------------------------
  function closeMenu() { $('menu').style.display = 'none'; }
  document.addEventListener('click', closeMenu);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMenu();
  });

  function openMenu(event, rowIndex, side) {
    event.preventDefault();
    event.stopPropagation();
    const menu = $('menu');
    const plan = state.rows[rowIndex];
    const chosen = plan[side];
    menu.innerHTML = '';

    const heading = document.createElement('h3');
    heading.textContent = `${side === 'left' ? 'Left' : 'Right'} axis, row ${rowIndex + 1}`;
    menu.appendChild(heading);

    const groups = new Map();
    for (const channel of state.channels) {
      if (!groups.has(channel.group)) groups.set(channel.group, []);
      groups.get(channel.group).push(channel);
    }
    for (const [group, list] of groups) {
      const label = document.createElement('h3');
      label.textContent = group;
      menu.appendChild(label);
      for (const channel of list) {
        const picked = chosen.find((entry) => entry.key === channel.key);
        const button = document.createElement('button');
        button.type = 'button';
        button.className = picked ? 'picked' : '';
        button.textContent = `${picked ? '✓ ' : ''}${channel.label}` +
          (channel.unit ? ` (${channel.unit})` : '');
        button.onclick = (click) => {
          click.stopPropagation();
          toggle(rowIndex, side, channel.key);
          openMenu(event, rowIndex, side);
        };
        menu.appendChild(button);
      }
    }
    if (chosen.length) {
      menu.appendChild(document.createElement('hr'));
      const label = document.createElement('h3');
      label.textContent = 'Colour and line width';
      menu.appendChild(label);
      for (const entry of chosen) {
        const line = document.createElement('div');
        line.className = 'line';
        const name = document.createElement('span');
        name.textContent = labelOf(entry.key);
        const colour = document.createElement('input');
        colour.type = 'color';
        colour.value = entry.colour;
        colour.oninput = () => { entry.colour = colour.value; drawRows(); };
        const width = document.createElement('input');
        width.type = 'range';
        width.min = '0.5'; width.max = '4'; width.step = '0.25';
        width.value = String(entry.width);
        width.oninput = () => { entry.width = Number(width.value); drawRows(); };
        line.append(name, colour, width);
        line.onclick = (click) => click.stopPropagation();
        menu.appendChild(line);
      }
    }

    menu.style.display = 'block';
    // Kept on screen: a menu opened near the bottom right would otherwise run
    // off the page with its own contents unreachable.
    const box = menu.getBoundingClientRect();
    const x = Math.min(event.clientX, window.innerWidth - box.width - 8);
    const y = Math.min(event.clientY, window.innerHeight - box.height - 8);
    menu.style.left = `${Math.max(8, x)}px`;
    menu.style.top = `${Math.max(8, y)}px`;
  }

  function toggle(rowIndex, side, key) {
    const chosen = state.rows[rowIndex][side];
    const at = chosen.findIndex((entry) => entry.key === key);
    if (at >= 0) { chosen.splice(at, 1); }
    else {
      chosen.push({
        key,
        colour: PALETTE[(countSeries() + chosen.length) % PALETTE.length],
        width: 1.4,
      });
    }
    renderRows();
    drawRows();
  }

  const countSeries = () =>
    state.rows.reduce((total, plan) => total + plan.left.length + plan.right.length, 0);
  const labelOf = (key) => (state.byKey.get(key) || {}).label || key;
  const unitOf = (key) => (state.byKey.get(key) || {}).unit || '';

  // -- rows -----------------------------------------------------------------
  function defaultRows(count) {
    const gases = state.channels.filter((item) => item.group === 'Trace gas');
    const altitude = state.channels.find((item) => item.key === 'altitude');
    const rows = [];
    for (let index = 0; index < count; index += 1) {
      const previous = state.rows[index];
      if (previous) { rows.push(previous); continue; }
      const gas = gases[index % Math.max(1, gases.length)];
      rows.push({
        left: gas ? [{key: gas.key, colour: PALETTE[index % PALETTE.length], width: 1.4}] : [],
        // Altitude opposite the gas on every new row: the first question asked
        // of an enhancement is what height it was met at.
        right: altitude ? [{key: 'altitude', colour: '#6f6f6f', width: 1.0}] : [],
      });
    }
    return rows;
  }

  function renderRows() {
    const host = $('rows');
    host.innerHTML = '';
    state.rows.forEach((plan, index) => {
      const block = document.createElement('div');
      block.className = 'row-block';
      const head = document.createElement('div');
      head.className = 'row-head';
      const axes = document.createElement('div');
      axes.className = 'row-axes';
      for (const side of ['left', 'right']) {
        const chip = document.createElement('span');
        chip.className = 'axis-chip';
        const names = plan[side].map((entry) => labelOf(entry.key));
        chip.innerHTML = `<strong>${side === 'left' ? 'Left' : 'Right'}:</strong> ` +
          (names.length ? names.join(', ') : '<em>empty — right-click</em>');
        chip.oncontextmenu = (event) => openMenu(event, index, side);
        axes.appendChild(chip);
      }
      const note = document.createElement('span');
      note.textContent = `Row ${index + 1}`;
      head.append(axes, note);
      const plot = document.createElement('div');
      plot.className = 'row-plot';
      plot.id = `plot${index}`;
      block.append(head, plot);
      host.appendChild(block);
    });
  }

  function traceFor(entry, axis) {
    const values = state.data.series[entry.key];
    if (!values) return null;
    return {
      x: state.data.time, y: values, type: 'scatter', mode: 'lines',
      name: `${labelOf(entry.key)}${unitOf(entry.key) ? ` (${unitOf(entry.key)})` : ''}`,
      line: {color: entry.colour, width: entry.width},
      yaxis: axis, connectgaps: false,
      hovertemplate: `%{x}<br>%{y:.6g} ${unitOf(entry.key)}<extra>${labelOf(entry.key)}</extra>`,
    };
  }

  function bandFor(entry, axis) {
    // The excursion of the samples each drawn point stands for. Drawn as a
    // filled band behind the line so a spike that fell between two plotted
    // points is still visible - the whole reason this page exists.
    const band = state.data.envelope && state.data.envelope[entry.key];
    if (!band) return null;
    const times = state.data.time;
    return {
      x: times.concat(times.slice().reverse()),
      y: band.high.concat(band.low.slice().reverse()),
      type: 'scatter', mode: 'lines', fill: 'toself',
      fillcolor: withAlpha(entry.colour, 0.18),
      line: {width: 0}, yaxis: axis, hoverinfo: 'skip',
      showlegend: false,
    };
  }

  function withAlpha(hex, alpha) {
    const value = String(hex).replace('#', '');
    const number = parseInt(value.length === 3
      ? value.split('').map((c) => c + c).join('') : value, 16);
    return `rgba(${(number >> 16) & 255},${(number >> 8) & 255},${number & 255},${alpha})`;
  }

  function axisTitle(entries) {
    const names = entries.map((entry) => labelOf(entry.key));
    const units = [...new Set(entries.map((entry) => unitOf(entry.key)).filter(Boolean))];
    const unit = units.length === 1 ? ` (${units[0]})` : (units.length ? ` (${units.join(', ')})` : '');
    return names.join(', ') + unit;
  }

  function drawRows() {
    if (!state.data) return;
    state.rows.forEach((plan, index) => {
      const target = `plot${index}`;
      if (!document.getElementById(target)) return;
      const traces = [];
      for (const entry of plan.left) {
        const band = bandFor(entry, 'y'); if (band) traces.push(band);
      }
      for (const entry of plan.right) {
        const band = bandFor(entry, 'y2'); if (band) traces.push(band);
      }
      for (const entry of plan.left) {
        const trace = traceFor(entry, 'y'); if (trace) traces.push(trace);
      }
      for (const entry of plan.right) {
        const trace = traceFor(entry, 'y2'); if (trace) traces.push(trace);
      }
      const layout = {
        margin: {l: 66, r: plan.right.length ? 66 : 18, t: 8, b: 34},
        font: {family: 'Inter,Segoe UI,Arial', size: 12, color: '#0d2b30'},
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: '#fff',
        showlegend: true,
        legend: {orientation: 'h', y: 1.16, x: 0, font: {size: 11}},
        xaxis: {gridcolor: 'rgba(13,43,48,.12)', automargin: true},
        yaxis: {title: {text: axisTitle(plan.left), standoff: 8},
                gridcolor: 'rgba(13,43,48,.12)', automargin: true},
        dragmode: 'select', selectdirection: 'h', hovermode: 'x',
      };
      if (plan.right.length) {
        layout.yaxis2 = {title: {text: axisTitle(plan.right), standoff: 8},
                         overlaying: 'y', side: 'right', showgrid: false,
                         automargin: true};
      }
      if (state.region) {
        layout.shapes = [{
          type: 'rect', xref: 'x', yref: 'paper',
          x0: state.region.start, x1: state.region.end, y0: 0, y1: 1,
          fillcolor: 'rgba(14,119,115,.13)', line: {width: 1, color: '#0e7773'},
          layer: 'below',
        }];
      }
      Plotly.react(target, traces, layout,
                   {displaylogo: false, responsive: true}).then(() => {
        const element = document.getElementById(target);
        if (element.dataset.wired === '1') return;
        element.dataset.wired = '1';
        element.on('plotly_selected', (event) => {
          if (!event || !event.range || !event.range.x) return;
          setRegion(event.range.x[0], event.range.x[1]);
        });
        // Right-click inside the selection is how the location is asked for,
        // which is the gesture the operator asked for.
        element.oncontextmenu = (nativeEvent) => {
          if (!state.region) return;
          nativeEvent.preventDefault();
          loadRegion();
        };
      });
    });
  }

  function setRegion(start, end) {
    state.region = {start: forInput(start), end: forInput(end)};
    show('info', `Region ${state.region.start} to ${state.region.end}. ` +
      'Right-click inside it to see where it was.');
    drawRows();
  }

  // -- region, map and rose -------------------------------------------------
  async function loadRegion() {
    if (!state.region || state.busy) return;
    state.busy = true;
    show('info', 'Reading the region…');
    try {
      state.analysis = await ask('/api/miro-rack/source/region', {
        region_start: state.region.start, region_end: state.region.end,
      });
      drawMap();
      drawRose();
      drawStats();
      clearMessage();
    } catch (error) {
      show('error', error.message);
    } finally {
      state.busy = false;
    }
  }

  function ensureMap() {
    if (state.map) return state.map;
    state.map = L.map('regionMap', {preferCanvas: true});
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
    }).addTo(state.map);
    state.map.setView([50.9, 6.7], 9);
    return state.map;
  }

  function drawMap() {
    const track = state.analysis && state.analysis.track;
    const map = ensureMap();
    if (state.trackLayer) { map.removeLayer(state.trackLayer); state.trackLayer = null; }
    if (state.regionLayer) { map.removeLayer(state.regionLayer); state.regionLayer = null; }
    if (!track || !track.available) {
      show('info', (track && track.reason) || 'No navigation for this region.');
      return;
    }
    // The whole flight in grey, the region over it in colour: which leg the
    // feature was on, and whether the same ground was passed earlier without
    // seeing it, is most of what places a source.
    state.trackLayer = L.polyline(
      track.track.map((point) => [point.lat, point.lon]),
      {color: '#8a9a9c', weight: 2, opacity: .85}).addTo(map);
    state.regionLayer = L.polyline(
      track.region.map((point) => [point.lat, point.lon]),
      {color: '#b3401a', weight: 5, opacity: .95}).addTo(map);
    state.regionLayer.on('click', () => {
      $('windRose').scrollIntoView({behavior: 'smooth', block: 'nearest'});
    });
    map.fitBounds(state.trackLayer.getBounds(), {padding: [24, 24]});
    setTimeout(() => map.invalidateSize(), 60);
  }

  function drawRose() {
    const rose = state.analysis && state.analysis.windrose;
    if (!rose || !rose.samples) {
      Plotly.purge('windRose');
      $('windHint').textContent =
        'This region carries no wind record, so no rose can be drawn.';
      return;
    }
    $('windHint').textContent =
      `${rose.convention}, ${rose.samples.toLocaleString()} samples` +
      (rose.derived_direction
        ? ' · direction derived from the wind components' : '');
    const edges = rose.speed_edges;
    const traces = edges.map((low, index) => {
      const high = index + 1 < edges.length ? edges[index + 1] : null;
      return {
        type: 'barpolar',
        r: rose.petals.map((petal) =>
          petal.bands[index].count / rose.samples * 100),
        theta: rose.petals.map((petal) => petal.centre_deg),
        name: high === null ? `> ${low}` : `${low}–${high}`,
        marker: {color: `hsl(${170 - index * 26},62%,${62 - index * 6}%)`,
                 line: {color: '#fff', width: .6}},
        hovertemplate: '%{theta}°<br>%{r:.1f}% of samples<extra>%{fullData.name} m/s</extra>',
      };
    });
    Plotly.react('windRose', traces, {
      margin: {l: 30, r: 30, t: 20, b: 20},
      font: {family: 'Inter,Segoe UI,Arial', size: 12, color: '#0d2b30'},
      paper_bgcolor: 'rgba(0,0,0,0)',
      barmode: 'stack',
      legend: {title: {text: 'm s⁻¹'}, font: {size: 11}},
      // North at the top and clockwise: how a rose is read, and the opposite
      // of the plotting default.
      polar: {
        angularaxis: {direction: 'clockwise', rotation: 90,
                      tickmode: 'array', tickvals: [0, 45, 90, 135, 180, 225, 270, 315],
                      ticktext: ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']},
        radialaxis: {ticksuffix: '%', angle: 45, tickfont: {size: 10}},
      },
    }, {displaylogo: false, responsive: true});
  }

  function drawStats() {
    const host = $('regionStats');
    host.innerHTML = '';
    const analysis = state.analysis;
    if (!analysis) return;
    const rows = [];
    const stats = analysis.statistics || {};
    const add = (label, text) => rows.push([label, text]);
    if (stats.wind_direction && stats.wind_direction.available) {
      add('Wind from', `${stats.wind_direction.mean.toFixed(0)}° (${stats.wind_direction.label})`);
    }
    for (const [key, label, unit] of [
      ['wind_speed', 'Wind speed', 'm/s'],
      ['ground_speed', 'Ground speed', 'm/s'],
      ['altitude', 'Altitude', 'm'],
    ]) {
      const value = stats[key];
      if (value && value.available) {
        add(label, `${value.mean.toFixed(1)} ${unit} ` +
          `(${value.minimum.toFixed(1)}–${value.maximum.toFixed(1)})`);
      }
    }
    if (stats.track && stats.track.available) {
      add('Track', `${stats.track.mean.toFixed(0)}° (${stats.track.label})`);
    }
    const enhanced = Object.entries(analysis.enhancements || {})
      .filter(([, entry]) => entry.available && entry.enhancement !== null)
      .sort((a, b) => b[1].enhancement - a[1].enhancement)
      .slice(0, 6);
    for (const [, entry] of enhanced) {
      // Peak and background both shown: where the instrument's noise sits
      // below zero the enhancement exceeds the peak, which reads as an error
      // until the arithmetic is visible.
      add(entry.label, `peak ${entry.maximum.toPrecision(4)} − ` +
        `${entry.background.toPrecision(3)} = ` +
        `+${entry.enhancement.toPrecision(4)} ${entry.unit}`);
    }
    for (const [label, text] of rows) {
      const dt = document.createElement('dt'); dt.textContent = label;
      const dd = document.createElement('dd'); dd.textContent = text;
      host.append(dt, dd);
    }
    if (analysis.note) {
      const dt = document.createElement('dt'); dt.textContent = '';
      const dd = document.createElement('dd');
      dd.style.color = '#5b6f73'; dd.textContent = analysis.note;
      host.append(dt, dd);
    }
  }

  // -- driving --------------------------------------------------------------
  async function update() {
    if (state.busy) return;
    state.busy = true;
    $('update').disabled = true;
    show('info', 'Reading the record…');
    try {
      const count = Math.max(1, Math.min(8, Number($('rowCount').value) || 3));
      state.rows = defaultRows(count);
      state.data = await ask('/api/miro-rack/source/rows', {
        start: fromInput($('startTime').value),
        end: fromInput($('endTime').value),
        rows: count,
        smoothing: $('smoothing').value,
        smoothing_seconds: Number($('smoothSeconds').value) || 5,
        polynomial_order: Number($('polyOrder').value) || 2,
      });
      renderRows();
      drawRows();
      const smoothing = state.data.smoothing || {};
      $('drawNote').textContent =
        `${state.data.shown.toLocaleString()} of ${state.data.samples.toLocaleString()} ` +
        `samples drawn (every ${state.data.decimation}). ${smoothing.note || ''}`;
      clearMessage();
    } catch (error) {
      show('error', error.message);
    } finally {
      state.busy = false;
      $('update').disabled = false;
    }
  }

  async function exportFigures() {
    const button = $('exportFigures');
    button.disabled = true;
    show('info', 'Rendering figures…');
    try {
      const payload = await ask('/api/miro-rack/source/export', {
        start: fromInput($('startTime').value),
        end: fromInput($('endTime').value),
        rows: state.rows.length,
        smoothing: $('smoothing').value,
        smoothing_seconds: Number($('smoothSeconds').value) || 5,
        polynomial_order: Number($('polyOrder').value) || 2,
        region_start: state.region ? state.region.start : null,
        region_end: state.region ? state.region.end : null,
        layout: state.rows,
        formats: ['pdf', 'png'],
      });
      show('info', `Wrote ${payload.files.length} file(s) to ${payload.directory}`);
    } catch (error) {
      show('error', error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function start() {
    try {
      const catalogue = await ask('/api/miro-rack/source/channels');
      state.channels = catalogue.channels || [];
      state.byKey = new Map(state.channels.map((item) => [item.key, item]));
      if (!state.channels.length) {
        show('error', 'This flight carries no drawable MIRO channels.');
        return;
      }
      const defaults = catalogue.defaults || {};
      $('rowCount').value = defaults.rows || 3;
      $('rowCount').max = defaults.maximum_rows || 8;
      $('smoothing').value = defaults.smoothing || 'savgol';
      $('smoothSeconds').value = defaults.smoothing_seconds || 5;
      $('polyOrder').value = defaults.polynomial_order || 2;
      $('startTime').value = forInput(catalogue.start);
      $('endTime').value = forInput(catalogue.end);
      if (!catalogue.navigation) {
        show('info', 'No processed Noseboom navigation for this flight, so ' +
          'altitude, the map and the wind rose are unavailable. The gases ' +
          'are drawn as usual.');
      }
      await update();
    } catch (error) {
      show('error', error.message);
    }
  }

  $('update').onclick = update;
  $('exportFigures').onclick = exportFigures;
  $('resetRegion').onclick = () => {
    state.region = null; state.analysis = null;
    Plotly.purge('windRose');
    $('regionStats').innerHTML = '';
    $('windHint').textContent = 'Select a region to see the wind rose.';
    drawMap();
    drawRows();
    clearMessage();
  };
  start();
})();
