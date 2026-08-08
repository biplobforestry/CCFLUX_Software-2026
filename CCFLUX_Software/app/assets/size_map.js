/* A size-resolved concentration drawn on the Noseboom flight track.
 *
 * Shared by the OPC and the Partector. Neither instrument records a position,
 * so the server pairs each sample with the nearest Noseboom fix in time; a
 * sample with no fix close enough is reported rather than placed by guesswork.
 * The page supplies the element ids and the endpoints, and this module owns
 * the map, the colour scale, the vertical colour bar and the PDF export.
 */
window.CCFLUX = window.CCFLUX || {};
window.CCFLUX.createSizeMap = function createSizeMap(options) {
  'use strict';
  const $ = id => document.getElementById(id);
  const ids = Object.assign({
    map: 'sizeMap', sensor: 'mapSensor', channel: 'mapChannel',
    palette: 'mapPalette', log: 'mapLog', legend: 'mapLegend',
    legendTitle: 'legendTitle', legendRamp: 'legendRamp',
    legendTicks: 'legendTicks', legendNote: 'legendNote',
    message: 'mapMessage', flightName: 'flightName'
  }, options.ids || {});
  const dataUrl = options.dataUrl;
  const exportUrl = options.exportUrl;
  const viewPath = options.viewPath;
  const unit = options.unit || '#/cm³';

  const PALETTES = {
    Viridis:['#440154','#414487','#2a788e','#22a884','#7ad151','#fde725'],
    Plasma:['#0d0887','#6a00a8','#b12a90','#e16462','#fca636','#f0f921'],
    Inferno:['#000004','#420a68','#932667','#dd513a','#fca50a','#fcffa4'],
    Cividis:['#00224e','#35456c','#666970','#948e77','#c8b866','#fee838'],
    YlOrRd:['#ffffcc','#fed976','#feb24c','#fd8d3c','#fc4e2a','#e31a1c','#b10026'],
    Turbo:['#30123b','#4145ab','#4675ed','#39a2fc','#1bcfd4','#62fc6b','#d1e935','#fe9b2d','#db3a07','#7a0403']
  };
  const format = value => Number(value).toPrecision(3).replace(/\.?0+$/, '');

  let data = null, map = null, layer = null, trackLine = null;
  let home = null, built = false, fitted = false;

  const sensor = () => (data && data.sensors) ? data.sensors[$(ids.sensor).value] : null;
  const selectedChannel = () => {
    const raw = $(ids.channel).value;
    return raw === '' ? null : Number(raw);
  };
  function channelName(index) {
    const current = sensor();
    const channel = current && current.channels ? current.channels[index] : null;
    return channel ? channel.label : `Channel ${index}`;
  }
  function valueOf(point) {
    const index = selectedChannel();
    if (index === null) return point.total;
    return point.values && point.values[index] !== undefined ? point.values[index] : null;
  }
  const paletteStops = () => PALETTES[$(ids.palette).value] || PALETTES.Viridis;
  function colourAt(fraction) {
    const stops = paletteStops();
    const position = Math.max(0, Math.min(1, fraction)) * (stops.length - 1);
    const left = Math.floor(position), right = Math.min(stops.length - 1, left + 1);
    const mix = position - left;
    const a = stops[left].match(/\w\w/g).map(v => parseInt(v, 16));
    const b = stops[right].match(/\w\w/g).map(v => parseInt(v, 16));
    return `rgb(${a.map((v, i) => Math.round(v * (1 - mix) + b[i] * mix)).join(',')})`;
  }
  // The range comes from the values actually present. A handful of spikes -
  // the Partector peaks 60x its own 99th percentile - would otherwise flatten
  // the whole track into the bottom colour.
  function bounds(values) {
    const finite = values.filter(Number.isFinite).sort((a, b) => a - b);
    if (!finite.length) return null;
    const positive = finite.filter(value => value > 0);
    const at = (list, fraction) =>
      list[Math.min(list.length - 1, Math.floor(list.length * fraction))];
    const max = finite[finite.length - 1];
    if ($(ids.log).checked && positive.length > 1) {
      const low = at(positive, .01), high = at(positive, .99);
      if (high > low) return {low, high, max, log: true};
    }
    const low = finite[0];
    const high = at(finite, .99);
    return {low, high: high > low ? high : low + 1e-12, max, log: false};
  }
  function fractionOf(value, range) {
    if (!Number.isFinite(value)) return null;
    if (range.log) {
      if (!(value > 0)) return null;
      const span = Math.log10(range.high) - Math.log10(range.low);
      if (!(span > 0)) return 0;
      const held = Math.min(Math.max(value, range.low), range.high);
      return (Math.log10(held) - Math.log10(range.low)) / span;
    }
    const span = range.high - range.low;
    if (!(span > 0)) return 0;
    return (Math.min(Math.max(value, range.low), range.high) - range.low) / span;
  }
  function fillPalettes() {
    const select = $(ids.palette);
    if (select.options.length) return;
    Object.keys(PALETTES).forEach(name => {
      const option = document.createElement('option');
      option.value = name; option.textContent = name;
      select.appendChild(option);
    });
  }
  function fillChannels() {
    const current = sensor(), select = $(ids.channel), previous = select.value;
    select.innerHTML = '';
    const all = document.createElement('option');
    all.value = ''; all.textContent = 'All sizes summed';
    select.appendChild(all);
    ((current && current.channels) || []).forEach(channel => {
      const option = document.createElement('option');
      option.value = String(channel.index);
      option.textContent = channel.label;
      select.appendChild(option);
    });
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
  }
  function renderLegend(range, title, note) {
    const legend = $(ids.legend);
    if (!range) { legend.hidden = true; return; }
    legend.hidden = false;
    $(ids.legendTitle).textContent = title;
    // The bar reads bottom to top, so the gradient runs upwards.
    $(ids.legendRamp).style.background = `linear-gradient(to top,${paletteStops().join(',')})`;
    const ticks = $(ids.legendTicks);
    ticks.innerHTML = '';
    for (let step = 0; step < 5; step += 1) {
      const fraction = step / 4;
      const value = range.log
        ? Math.pow(10, Math.log10(range.low) + fraction * (Math.log10(range.high) - Math.log10(range.low)))
        : range.low + fraction * (range.high - range.low);
      const tick = document.createElement('span');
      tick.className = 'tick';
      // Positioned from the top, like every other legend in the software:
      // `top` with translateY(-50%) centres the label on its value, while
      // `bottom` put the whole label above the line and over the title.
      tick.style.top = `${100 - fraction * 100}%`;
      tick.textContent = format(value);
      ticks.appendChild(tick);
    }
    $(ids.legendNote).textContent = note;
  }
  function showMessage(text) {
    const box = $(ids.message);
    box.hidden = !text;
    box.textContent = text || '';
  }
  function build() {
    if (built) return;
    map = L.map(ids.map, {preferCanvas: true, zoomControl: true});
    // crossOrigin is what makes the export possible: a tile fetched without
    // it taints the canvas, and toDataURL then refuses to read the map back.
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
       updateWhenZooming: false, crossOrigin: true}).addTo(map);
    map.setView([50.9, 6.9], 9);
    built = true;
  }
  function draw() {
    if (!built || !data) return;
    if (layer) { layer.remove(); layer = null; }
    if (trackLine) { trackLine.remove(); trackLine = null; }
    const current = sensor();
    if (!current || !current.points.length) {
      renderLegend(null);
      showMessage(`${current ? current.label : 'This sensor'} has no sample that could be placed on the flight track.`);
      return;
    }
    const track = (data.flight_track || []).filter(p => Number.isFinite(p.lat) && Number.isFinite(p.lon));
    if (track.length > 1) {
      trackLine = L.polyline(track.map(p => [p.lat, p.lon]),
        {color: '#8fa9bb', weight: 1.5, opacity: .55, interactive: false}).addTo(map);
    }
    const values = current.points.map(valueOf);
    const range = bounds(values);
    const index = selectedChannel();
    const label = index === null ? 'All sizes summed' : channelName(index);
    const drawn = [], positions = [];
    current.points.forEach((point, position) => {
      positions.push([point.lat, point.lon]);
      const fraction = range ? fractionOf(values[position], range) : null;
      if (fraction === null) return;
      drawn.push(L.circleMarker([point.lat, point.lon],
        {radius: 4, stroke: false, fillOpacity: .9, fillColor: colourAt(fraction)})
        .bindPopup(
          `<strong>${current.label}</strong><br>${point.time}<br>` +
          `${label}: ${format(values[position])} ${unit}<br>` +
          `${point.altitude_m === null || point.altitude_m === undefined ? '' : 'Altitude ' + format(point.altitude_m) + ' m<br>'}` +
          `Noseboom fix ${point.delta_s} s away`));
    });
    layer = L.layerGroup(drawn).addTo(map);
    if (positions.length) {
      home = L.latLngBounds(positions);
      if (!fitted) { map.fitBounds(home, {padding: [30, 30]}); fitted = true; }
    }
    const unplaced = current.unmatched_count + current.undated_count;
    const noReading = current.points.length - drawn.length;
    renderLegend(range, `${label} [${unit}]`,
      `${current.label} · ${range && range.log ? 'logarithmic' : 'linear'} · peak ${range ? format(range.max) : '—'}`);
    showMessage(
      `${current.matched_count.toLocaleString()} of ${current.sampled_from.toLocaleString()} samples placed` +
      (unplaced ? `; ${unplaced.toLocaleString()} had no Noseboom fix within ${data.maximum_time_delta_seconds} s` : '') +
      (noReading ? `; ${noReading.toLocaleString()} carried no reading for this size class` : ''));
  }
  async function show() {
    build();
    if (map) map.invalidateSize();
    if (data) { draw(); return; }
    try {
      const response = await fetch(dataUrl, {cache: 'no-store'});
      const body = await response.json();
      if (!response.ok || !body.ready) {
        renderLegend(null);
        showMessage(body.message || 'These samples could not be placed on a map.');
        return;
      }
      data = body.data;
      fillPalettes(); fillChannels(); applyPermalink(); draw();
    } catch (error) {
      renderLegend(null);
      showMessage(`Map data failed to load: ${error.message}`);
    }
  }
  function permalink() {
    const query = new URLSearchParams({
      sensor: $(ids.sensor).value, channel: $(ids.channel).value,
      palette: $(ids.palette).value, log: $(ids.log).checked ? '1' : '0'
    });
    return `${viewPath}?${query}`;
  }
  function applyPermalink() {
    const query = new URLSearchParams(location.search);
    if (!query.has('sensor')) return;
    const select = $(ids.sensor);
    if ([...select.options].some(option => option.value === query.get('sensor'))) {
      select.value = query.get('sensor');
    }
    fillChannels();
    if (query.has('channel')) $(ids.channel).value = query.get('channel') || '';
    if (query.has('palette')) $(ids.palette).value = query.get('palette') || 'Viridis';
    $(ids.log).checked = query.get('log') !== '0';
  }
  // Leaflet paints tiles and vectors into separate layers, so the export
  // redraws the visible extent onto one canvas: tiles, then the track, then
  // the coloured samples, then the legend.
  async function composeImage() {
    const container = $(ids.map);
    const width = Math.max(800, container.clientWidth);
    const height = Math.max(500, container.clientHeight);
    const canvas = document.createElement('canvas');
    canvas.width = width * 2; canvas.height = height * 2;
    const context = canvas.getContext('2d');
    context.scale(2, 2);
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, width, height);
    const base = container.getBoundingClientRect();
    for (const tile of container.querySelectorAll('img.leaflet-tile-loaded')) {
      const rect = tile.getBoundingClientRect();
      try {
        context.drawImage(tile, rect.left - base.left, rect.top - base.top, rect.width, rect.height);
      } catch (error) { /* a tile that would taint the canvas is skipped */ }
    }
    const current = sensor();
    if (current) {
      const values = current.points.map(valueOf), range = bounds(values);
      const track = data.flight_track || [];
      if (track.length > 1) {
        context.strokeStyle = '#8fa9bb'; context.lineWidth = 1.5; context.globalAlpha = .55;
        context.beginPath();
        track.forEach((point, index) => {
          const at = map.latLngToContainerPoint([point.lat, point.lon]);
          index ? context.lineTo(at.x, at.y) : context.moveTo(at.x, at.y);
        });
        context.stroke(); context.globalAlpha = 1;
      }
      current.points.forEach((point, position) => {
        const fraction = range ? fractionOf(values[position], range) : null;
        if (fraction === null) return;
        const at = map.latLngToContainerPoint([point.lat, point.lon]);
        if (at.x < 0 || at.y < 0 || at.x > width || at.y > height) return;
        context.fillStyle = colourAt(fraction);
        context.beginPath(); context.arc(at.x, at.y, 4, 0, Math.PI * 2); context.fill();
      });
      drawExportLegend(context, width, range);
    }
    drawMapFurniture(context, width, height);
    return canvas.toDataURL('image/png');
  }

  // Scale, orientation and coordinates, on every exported map. The canvas is
  // scaled 2x, so the drawing units here are the container's CSS pixels and the
  // furniture is sized against those; the physical width is seven inches either
  // way, which is what its point sizing is measured against.
  function drawMapFurniture(context, width, height) {
    if (typeof CCFLUXMapFurniture === 'undefined') return;
    const bounds = map.getBounds();
    CCFLUXMapFurniture.draw(context, {
      width, height,
      bounds: {north: bounds.getNorth(), south: bounds.getSouth(),
        east: bounds.getEast(), west: bounds.getWest()},
      project: (lat, lon) => map.latLngToContainerPoint([lat, lon]),
      metresPerPixel: CCFLUXMapFurniture.metresPerPixel(
        map.getCenter().lat, map.getZoom())
    });
  }
  // The screen legend carries no panel behind it, so the exported one carries
  // none either. Labels are read against whatever tiles fall behind them, so
  // each is stroked white before it is filled dark.
  function haloText(context, text, x, y) {
    context.lineJoin = 'round'; context.miterLimit = 2;
    context.strokeStyle = '#ffffff'; context.lineWidth = 3.4;
    context.strokeText(text, x, y);
    context.fillStyle = '#07182a'; context.fillText(text, x, y);
  }
  function drawExportLegend(context, width, range) {
    if (!range) return;
    const boxWidth = 160, boxHeight = 250, x = width - boxWidth - 18, y = 18;
    const index = selectedChannel();
    context.save();
    context.font = '700 13px Arial';
    haloText(context, `${index === null ? 'All sizes' : channelName(index)} [${unit}]`, x + 12, y + 22);
    const barX = x + 14, barY = y + 38, barWidth = 22, barHeight = boxHeight - 72;
    const gradient = context.createLinearGradient(0, barY + barHeight, 0, barY);
    paletteStops().forEach((shade, position, list) =>
      gradient.addColorStop(position / (list.length - 1), shade));
    context.fillStyle = gradient; context.fillRect(barX, barY, barWidth, barHeight);
    context.strokeStyle = '#07182a'; context.lineWidth = 1;
    context.strokeRect(barX, barY, barWidth, barHeight);
    context.font = '11px Arial';
    for (let step = 0; step < 5; step += 1) {
      const fraction = step / 4;
      const value = range.log
        ? Math.pow(10, Math.log10(range.low) + fraction * (Math.log10(range.high) - Math.log10(range.low)))
        : range.low + fraction * (range.high - range.low);
      haloText(context, format(value), barX + barWidth + 8, barY + barHeight - fraction * barHeight + 4);
    }
    context.font = '10px Arial';
    haloText(context, range.log ? 'logarithmic' : 'linear', x + 12, y + boxHeight - 12);
    context.restore();
  }
  async function exportPdf(button) {
    const original = button.textContent;
    button.disabled = true; button.textContent = 'Exporting…';
    try {
      const current = sensor();
      const index = selectedChannel();
      // Which layer, not a picture of it. The server draws the same
      // georeferenced values the page is drawing, fitted to the track: sending
      // the canvas exported whatever zoom the map happened to be left on, and
      // the colour bar with it, painted over the ground it described.
      const response = await fetch(exportUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          sensor: $(ids.sensor) ? $(ids.sensor).value : '',
          channel: index,
          log: Boolean($(ids.log) && $(ids.log).checked),
          flight_name: $(ids.flightName) ? $(ids.flightName).textContent : 'Flight'
        })
      });
      if (!response.ok) throw new Error(await response.text() || `Export failed (${response.status})`);
      const blob = await response.blob();
      const named = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '');
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = named ? named[1] : 'size_distribution_map.pdf';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 4000);
    } catch (error) {
      showMessage(`PDF export failed: ${error.message}`);
    } finally {
      button.disabled = false; button.textContent = original;
    }
  }
  function resetPosition() {
    if (map && home && home.isValid()) map.fitBounds(home, {padding: [30, 30]});
  }
  function invalidate() { if (map) map.invalidateSize(); }
  function onSensorChange() { fillChannels(); draw(); }

  return {show, draw, permalink, exportPdf, resetPosition, invalidate, onSensorChange};
};
