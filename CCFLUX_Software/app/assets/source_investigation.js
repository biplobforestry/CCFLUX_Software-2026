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
        width: 1.8,
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
        // of an enhancement is what height it was met at. Drawn at the same
        // weight as the gas - a one-pixel light grey line is easy to take for
        // grid work and be missed entirely.
        right: altitude ? [{key: 'altitude', colour: '#5b6f73', width: 1.4}] : [],
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
      plot.style.height = `${rowHeight(plan)}px`;
      block.append(head, plot);
      host.appendChild(block);
    });
  }

  // Every series gets its own scale, its own ticks and its own colour on them.
  // Sharing one axis between CO at 20 000 ppb and N2O at 300 flattens the
  // second into the axis line: the reader sees a straight trace and concludes
  // nothing happened, when what happened was that the scale belonged to the
  // other species.
  const AXIS_WIDTH = 0.052;   // paper fraction one stacked axis occupies
  const ROW_BASE_HEIGHT = 330;
  const ROW_HEIGHT_PER_AXIS = 46;

  function rowHeight(plan) {
    // Adding axes takes width from the plot, and stacking series takes room in
    // the legend; without growing, a row with six series is a squeezed strip.
    const axes = plan.left.length + plan.right.length;
    return ROW_BASE_HEIGHT + ROW_HEIGHT_PER_AXIS * Math.max(0, axes - 2);
  }

  function axisFor(entry, index, side, order, counts) {
    const name = index === 0 ? 'yaxis' : `yaxis${index + 1}`;
    // The first series chosen sits nearest the plot and later ones stack
    // outwards, so adding a series does not move the ones already read.
    const outward = (side === 'left' ? counts.left : counts.right) - 1 - order;
    const axis = {
      title: {
        text: `${labelOf(entry.key)}${unitOf(entry.key) ? ` (${unitOf(entry.key)})` : ''}`,
        standoff: 8, font: {color: entry.colour, size: 14},
      },
      tickfont: {color: entry.colour, size: 14},
      // Heavy enough to read as an axis rather than as a hairline, and to tell
      // which of four scales a trace belongs to at a glance.
      linecolor: entry.colour, linewidth: 2.4, showline: true,
      ticks: 'outside', ticklen: 6, tickwidth: 2.0, tickcolor: entry.colour,
      zeroline: false, automargin: false,
      // Only the first axis draws a grid; one grid per series would be a mesh.
      showgrid: index === 0,
      gridcolor: 'rgba(13,43,48,.12)',
      side,
      anchor: 'free',
      position: side === 'left'
        ? Math.max(0, outward * AXIS_WIDTH)
        : Math.min(1, 1 - outward * AXIS_WIDTH),
    };
    if (index > 0) axis.overlaying = 'y';
    const range = rangeFor(entry);
    if (range) axis.range = range;
    return {name, axis, ref: index === 0 ? 'y' : `y${index + 1}`};
  }

  // The scale follows the drawn line, not the envelope. Autoscaled to include
  // every raw excursion, one bad sample sets the range and the trace collapses
  // onto the axis: a flight whose CO sits near 200 ppb was being drawn on a
  // 0-20 000 axis because of a handful of spikes, and read as a flat line.
  // The band still draws, and where it runs past the top of the axis it is
  // clipped there, which is the visible sign that something exceeded the view.
  function rangeFor(entry) {
    const values = (state.data.series[entry.key] || [])
      .filter((value) => value !== null && Number.isFinite(value));
    if (values.length < 2) return null;
    const sorted = values.slice().sort((a, b) => a - b);
    const at = (fraction) =>
      sorted[Math.min(sorted.length - 1,
                      Math.max(0, Math.round((sorted.length - 1) * fraction)))];
    // The body of the trace, not its two worst samples. CO sitting near
    // 120 ppb with a pair of spikes at 7 000 was drawn on a 0-7 000 axis and
    // read as a flat line along the bottom. A spike beyond this runs off the
    // top of the panel, which is visible, and the region readout still gives
    // its true value because that comes from the raw record.
    let low = at(0.005);
    let high = at(0.995);
    if (!(high > low)) { low = sorted[0]; high = sorted[sorted.length - 1]; }
    if (!(high > low)) {
      const pad = Math.abs(high) * 0.05 || 1;
      return [low - pad, high + pad];
    }
    const pad = (high - low) * 0.08;
    // A concentration does not go below zero, and an axis that starts at -457
    // says it might.
    const floor = sorted[0] >= 0 ? Math.max(0, low - pad) : low - pad;
    return [floor, high + pad];
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
    // The excursion of the samples each drawn point stands for, off by default:
    // behind several series at once it reads as scribble over the traces, and
    // the line is what the page is for. Turned on, it is how a spike that fell
    // between two plotted points stays visible.
    if (!$('showBand') || !$('showBand').checked) return null;
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
      const entries = [
        ...plan.left.map((entry, order) => ({entry, side: 'left', order})),
        ...plan.right.map((entry, order) => ({entry, side: 'right', order})),
      ];
      const counts = {left: plan.left.length, right: plan.right.length};
      const layout = {
        // Room outside the outermost axis for its own title and ticks, which
        // sit at a fixed paper position and are not automargined.
        margin: {t: 10, b: 52, l: 74, r: counts.right ? 74 : 20},
        font: {family: 'Inter,Segoe UI,Arial', size: 14, color: '#0d2b30'},
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: '#fff',
        showlegend: true,
        legend: {orientation: 'h', y: 1.10, x: 0, font: {size: 13}},
        // The plot gives up the strip each stacked axis needs, so the axes sit
        // beside the data rather than on top of it.
        xaxis: {
          gridcolor: 'rgba(13,43,48,.12)',
          linecolor: '#0d2b30', linewidth: 2.0, showline: true,
          ticks: 'outside', ticklen: 6, tickwidth: 2.0,
          tickfont: {size: 14},
          domain: [plan.left.length * AXIS_WIDTH,
                   1 - plan.right.length * AXIS_WIDTH],
        },
        dragmode: 'select', selectdirection: 'h', hovermode: 'x',
      };
      const traces = [];
      entries.forEach(({entry, side, order}, index) => {
        const {name, axis, ref} = axisFor(entry, index, side, order, counts);
        layout[name] = axis;
        const band = bandFor(entry, ref);
        if (band) traces.push(band);
      });
      entries.forEach(({entry, side, order}, index) => {
        const {ref} = axisFor(entry, index, side, order, counts);
        const trace = traceFor(entry, ref);
        if (trace) traces.push(trace);
      });
      if (!entries.length) {
        layout.yaxis = {title: {text: 'Right-click an axis to choose a series'}};
      }
      if (state.region) {
        layout.shapes = [{
          type: 'rect', xref: 'x', yref: 'paper',
          x0: state.region.start, x1: state.region.end, y0: 0, y1: 1,
          fillcolor: 'rgba(14,119,115,.13)', line: {width: 1, color: '#0e7773'},
          layer: 'below',
        }];
      }
      const element = document.getElementById(target);
      // Re-measured on every draw: adding an axis through the menu has to make
      // the row taller there and then, not only on the next Update.
      element.style.height = `${rowHeight(plan)}px`;
      Plotly.react(target, traces, layout,
                   {displaylogo: false, responsive: true}).then(() => {
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
        // And a plain click on the curve, which is the faster gesture when a
        // feature is narrow: it takes a window around the point clicked rather
        // than asking for a drag that would be a few pixels wide.
        element.on('plotly_click', (event) => {
          const point = event && event.points && event.points[0];
          if (!point) return;
          if (state.region && withinRegion(point.x)) { loadRegion(); return; }
          setRegionAround(point.x);
          loadRegion();
        });
      });
    });
  }

  // A click asks about the moment clicked, so the region is a window around it.
  // Two minutes is about what a Zeppelin covers while flying through a plume,
  // and it is wide enough that the wind rose has samples to count.
  const CLICK_WINDOW_SECONDS = 120;

  function withinRegion(value) {
    if (!state.region) return false;
    const at = String(forInput(value));
    return at >= state.region.start && at <= state.region.end;
  }

  function setRegionAround(value) {
    // Built through Date only to add and subtract seconds, then written back
    // as plain text: the record is naive UTC and must not pick up a zone here.
    const middle = new Date(`${forInput(value)}Z`).getTime();
    if (!Number.isFinite(middle)) return;
    const half = CLICK_WINDOW_SECONDS * 500;
    const asText = (millis) =>
      new Date(millis).toISOString().slice(0, 19);
    setRegion(asText(middle - half), asText(middle + half));
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
    show('info', 'Reading the region and computing the wind rose…');
    // Said in the panel the answer will appear in, not only in the bar at the
    // top of the page, which is off screen once the rows are scrolled past.
    $('windHint').textContent = 'Computing the wind rose over the selection…';
    $('regionStats').innerHTML = '';
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
      margin: {l: 40, r: 40, t: 28, b: 28},
      font: {family: 'Inter,Segoe UI,Arial', size: 14, color: '#0d2b30'},
      paper_bgcolor: 'rgba(0,0,0,0)',
      barmode: 'stack',
      legend: {title: {text: 'm s⁻¹'}, font: {size: 13}},
      // North at the top and clockwise: how a rose is read, and the opposite
      // of the plotting default.
      polar: {
        angularaxis: {direction: 'clockwise', rotation: 90,
                      tickmode: 'array', tickvals: [0, 45, 90, 135, 180, 225, 270, 315],
                      ticktext: ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'],
                      tickfont: {size: 14}, linewidth: 2.0},
        radialaxis: {ticksuffix: '%', angle: 45, tickfont: {size: 12},
                     linewidth: 1.6},
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
      const ambient = state.data.ambient || {};
      $('drawNote').textContent =
        `${state.data.shown.toLocaleString()} of ${state.data.samples.toLocaleString()} ` +
        `samples drawn (every ${state.data.decimation}). ` +
        `${ambient.note ? ambient.note + ' ' : ''}${smoothing.note || ''}`;
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
  // Redrawn rather than refetched: the band is already in the payload, so
  // turning it on must not cost a round trip.
  $('showBand').onchange = drawRows;
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
