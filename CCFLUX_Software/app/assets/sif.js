(() => {
  'use strict';

  const variableDescriptions = {
    'temp1 [C]': 'AirFloX mainboard temperature',
    'h1 [%]': 'AirFloX relative humidity',
    'Incoming at 750nm Full [W m-2nm-1sr-1]': 'FULL incoming radiance near 750 nm',
    'Reflected 750nm full [W m-2nm-1sr-1]': 'FULL reflected radiance near 750 nm',
    'Incoming at 750nm FLUO [W m-2nm-1sr-1]': 'FLUO incoming radiance near 750 nm',
    'Reflected 750nm FLUO [W m-2nm-1sr-1]': 'FLUO reflected radiance near 750 nm',
    'SIF_A_ifld [mW m-2nm-1sr-1]': 'Solar-induced fluorescence retrieved in the O₂-A band',
    'SIF_B_ifld [mW m-2nm-1sr-1]': 'Solar-induced fluorescence retrieved in the O₂-B band',
    'PAR inc [W m-2]': 'Incoming photosynthetically active radiation',
    'PAR ref [W m-2]': 'Reflected photosynthetically active radiation',
    'APAR [umol m-2 s-1]': 'Absorbed photosynthetically active radiation',
    'E_stability full [%]': 'FULL WR1/WR2 irradiance stability',
    'E_stability FLUO [%]': 'FLUO WR1/WR2 irradiance stability',
    'Dynamic range E full [%]': 'FULL upward-channel dynamic range',
    'Dynamic range L full [%]': 'FULL downward-channel dynamic range',
    'Dynamic range E FLUO [%]': 'FLUO upward-channel dynamic range',
    'Dynamic range L FLUO [%]': 'FLUO downward-channel dynamic range',
    NDVI: 'Normalized Difference Vegetation Index',
    PRI: 'Photochemical Reflectance Index',
    MTCI: 'MERIS Terrestrial Chlorophyll Index',
    SR: 'Simple Ratio',
    EVI: 'Enhanced Vegetation Index',
    REP: 'Red Edge Position',
    TCARI: 'Transformed Chlorophyll Absorption Reflectance Index',
    REDCl: 'Red Edge Chlorophyll Index',
    MCRI: 'Modified Carotenoid Reflectance Index'
  };
  const vegetationIndices = [
    ['NDVI', 'Normalized Difference Vegetation Index', '800; 670', '(R800 − R670) / (R800 + R670)'],
    ['PRI', 'Photochemical Reflectance Index', '531; 570', '(R531 − R570) / (R531 + R570)'],
    ['MTCI', 'MERIS Terrestrial Chlorophyll Index', '754; 709; 681', '(R754 − R709) / (R709 + R681)'],
    ['SR', 'Simple Ratio', '795; 810', 'R795 / R810'],
    ['EVI', 'Enhanced Vegetation Index', '800; 670; 480', '2.5 × (a − b) / (a + 6b − 7.5c + 1)'],
    ['REP', 'Red Edge Position', '670; 800; 700; 740', '700 + 40 × ((a − b/2) − c) / (d − c)'],
    ['TCARI', 'Transformed Chlorophyll Absorption Reflectance Index', '700; 670; 550; 670', '3 × (a − b − 0.2 × (a − c) × a/d)'],
    ['REDCl', 'Red Edge Chlorophyll Index', '785; 725', 'R785 / R725 − 1'],
    ['MCRI', 'Modified Carotenoid Reflectance Index', '510; 725; 785', 'R785 / (R510 − R725)'],
    ['L800', 'Radiance at 800 nm', '800', 'L800'],
    ['SIF A/B', 'Solar-induced fluorescence by improved FLD', 'O₂-A / O₂-B', 'Validated AirFloX iFLD retrieval']
  ];
  const manual = `SIF / FLOX WORKFLOW

1. Select and scan the Flight Folder in the Main GUI. The flight should contain FULL and FLUO/FLOX raw CSV files.
2. SIF UAV/Airship mode uses an existing SIF telemetry log when available. Otherwise it creates one by matching Gremsy Gimbal timestamps to Noseboom latitude, longitude, and altitude.
3. Open Configure SIF in the processing queue. Choose FULL, FLUO, spectral shift, nonlinearity, row handling, altitude filtering, and the maximum navigation time gap.
4. Raw files smaller than the configured threshold are ignored. The validated default is 100 KB.
5. The global Main GUI Time Filter is applied in UTC after telemetry matching.
6. Start Processing. This page follows telemetry preparation, FULL/FLUO calibration, vegetation indices, SIF iFLD, GIS, browser payload, and completion.
7. Inspect the overview, its distribution and frequency curve, the time series, and the georeferenced vegetation-index map.

DEFAULT SCIENCE POLICY
• FULL spectral shift correction: off
• Nonlinearity correction: off
• Drop unmatched telemetry rows: on
• Drop invalid spectral rows: off
• Maximum Gimbal/Noseboom position gap: 0.2 s
• Ignore raw files smaller than: 100 KB
• Raw source files are never modified.

BUNDLED ESSENTIALS
The main application uses the validated FULL calibration, FLUO calibration, and Indices_ICOS definition shipped under instruments/sif/essentials.

OUTPUTS
Each immutable SIF run writes incoming radiance, reflected radiance, reflectance, the combined index/SIF table, GIS shapefiles, and the browser payload under the active Flight Project.`;

  const api = async path => {
    const response = await fetch(path, {cache: 'no-store'});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    return body;
  };
  const layout = (xTitle, yTitle) => ({
    paper_bgcolor: '#f7fafc', plot_bgcolor: '#f7fafc',
    margin: {l: 86, r: 28, t: 35, b: 72},
    font: {family: 'Inter, Segoe UI, Arial', color: '#17324a', size: 14},
    xaxis: {title: xTitle, gridcolor: '#d9e3ea', automargin: true},
    yaxis: {title: yTitle, gridcolor: '#d9e3ea', automargin: true},
    legend: {orientation: 'h', y: 1.08}, hovermode: 'closest'
  });
  const config = {responsive: true, displaylogo: false, scrollZoom: true};
  let payload = null;
  let map = null;
  let pointLayer = null;
  let initialBounds = null;
  let legend = null;
  let retryTimer = null;

  function finitePairs(x, y, extra = null) {
    const result = {x: [], y: [], extra: []};
    for (let index = 0; index < Math.min(x.length, y.length); index += 1) {
      if (x[index] == null || y[index] == null || !Number.isFinite(Number(y[index]))) continue;
      result.x.push(x[index]);
      result.y.push(Number(y[index]));
      result.extra.push(extra ? extra[index] : null);
    }
    return result;
  }
  function selectedMode() {
    return payload?.modes?.[document.getElementById('modeSelect').value] || null;
  }
  function selectedVariable() {
    return document.getElementById('variableSelect').value;
  }
  function setModes() {
    const select = document.getElementById('modeSelect');
    const modes = Object.keys(payload.modes || {});
    select.innerHTML = modes.map(mode => `<option value="${mode}">${mode === 'FULL' ? 'FULL / FLOX' : mode}</option>`).join('');
    if (!modes.length) return;
    const requestedMode = new URLSearchParams(location.search).get('mode');
    const preferred = modes.includes(requestedMode) ? requestedMode
      : location.pathname.toLowerCase().includes('fluo') ? 'FLUO' : modes[0];
    select.value = modes.includes(preferred) ? preferred : modes[0];
    setVariables();
  }
  function setVariables() {
    const mode = selectedMode();
    const select = document.getElementById('variableSelect');
    const names = mode?.variable_names || [];
    const preferred = names.find(name => /SIF_A/i.test(name))
      || names.find(name => name === 'NDVI') || names[0];
    select.innerHTML = names.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
    if (preferred) select.value = preferred;
  }
  function renderSummary() {
    const modeName = document.getElementById('modeSelect').value;
    const mode = selectedMode();
    const summary = payload.summary || {};
    const option = summary.options || {};
    const cards = [
      ['Product', modeName, `${mode?.row_count || 0} evaluated spectra`],
      ['UTC coverage', shortTime(mode?.time?.find(Boolean)), shortTime([...(mode?.time || [])].reverse().find(Boolean))],
      ['Position source', summary.position_mode === 'tower' ? 'Tower/static' : 'Gimbal + Noseboom', option.drop_unmatched_telemetry === false ? 'Unmatched rows retained' : 'Unmatched rows dropped'],
      ['Corrections', option.spectral_shift_correction ? 'Spectral shift on' : 'Spectral shift off', option.apply_nonlinearity_correction ? 'Nonlinearity on' : 'Nonlinearity off'],
      ['Raw-file filter', `${summary.raw_file_filter_kb ?? option.raw_min_kb ?? 100} KB`, `${(summary.skipped_raw_files || []).length} small file(s) skipped`]
    ];
    document.getElementById('summaryGrid').innerHTML = cards.map(([label, value, note]) =>
      `<div class="summary"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || '—')}</strong><small>${escapeHtml(note || '')}</small></div>`
    ).join('');
  }
  function renderPlots() {
    const mode = selectedMode();
    const variable = selectedVariable();
    if (!mode || !variable) return;
    const values = mode.variables[variable] || [];
    const time = finitePairs(mode.time || [], values);
    Plotly.react('overviewPlot', [{x: time.x, y: time.y, type: 'scattergl', mode: 'lines+markers', marker: {size: 4, color: '#008ec4'}, line: {width: 1.4, color: '#008ec4'}, name: variable}], layout('Capture time [UTC]', variable), config);
    Plotly.react('timePlot', [{x: time.x, y: time.y, type: 'scattergl', mode: 'lines', line: {width: 1.6, color: '#0b8f88'}, name: variable}], layout('Capture time [UTC]', variable), config);
    renderDistribution(values, variable);
    document.getElementById('selectionNote').textContent = `${mode.row_count.toLocaleString()} ${document.getElementById('modeSelect').value} spectra · ${variable}`;
    renderMap(mode, mapVariable(mode));
    renderSummary();
  }
  // The frequency curve is a frequency polygon: the same counts as the bars,
  // joined at the bin centres. It needs no bandwidth chosen for it, so it
  // states the distribution the histogram already shows rather than a smoothed
  // reinterpretation of it.
  function renderDistribution(values, variable) {
    const finite = values.filter(Number.isFinite);
    const bins = 35;
    let traces = [];
    if (finite.length) {
      const low = Math.min(...finite), high = Math.max(...finite);
      const width = (high - low) / bins || 1;
      const counts = new Array(bins).fill(0);
      finite.forEach(value => {
        const index = Math.min(bins - 1, Math.floor((value - low) / width));
        counts[index] += 1;
      });
      const centres = counts.map((_, index) => low + width * (index + 0.5));
      traces = [
        {x: finite, type: 'histogram', name: 'Spectra per bin', marker: {color: '#0b8f88'},
         xbins: {start: low, end: high, size: width}, opacity: .82},
        {x: centres, y: counts, type: 'scatter', mode: 'lines+markers',
         name: 'Frequency curve', line: {color: '#e6a11a', width: 2.4, shape: 'spline', smoothing: 0.6},
         marker: {size: 5, color: '#e6a11a'}}
      ];
    }
    Plotly.react('histogramPlot', traces, layout(variable, 'Number of spectra'), config);
  }

  // The map answers a vegetation question, so it offers vegetation indices
  // rather than every column the retrieval produced.
  const INDEX_NAMES = vegetationIndices
    .map(row => row[0])
    .filter(name => !/\s/.test(name));

  function indexVariables(mode) {
    const available = Object.keys(mode?.variables || {});
    // The payload names the indices its own run produced, read from the index
    // definition file that run used, so an operator's own list works too. The
    // built-in names are the fallback for a project written before that.
    const declared = Array.isArray(payload?.index_names) && payload.index_names.length
      ? payload.index_names
      : INDEX_NAMES;
    const indices = available.filter(name => declared.includes(name));
    // Nothing matched at all: showing everything is a worse view than an index
    // list, but a far better one than an empty map with no explanation.
    return indices.length ? indices : available;
  }

  function mapVariable(mode) {
    const select = document.getElementById('mapVariableSelect');
    const names = indexVariables(mode);
    const current = select.value;
    if (select.dataset.names !== names.join(' ')) {
      select.dataset.names = names.join(' ');
      select.innerHTML = names.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
      // ?index= is honoured once, when the list is first built: a tab opened
      // for one index must arrive showing it, and must not then snap back to
      // that index every time the operator picks another in the same tab.
      const requested = new URLSearchParams(location.search).get('index');
      select.value = names.includes(requested) ? requested
        : names.includes(current) ? current
          : names.includes('NDVI') ? 'NDVI' : names[0] || '';
    }
    return select.value || names[0];
  }

  function renderMap(mode, variable) {
    if (!map) {
      map = L.map('sifMap', {preferCanvas: true});
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '© OpenStreetMap contributors'}).addTo(map);
      L.control.scale({imperial: false}).addTo(map);
      pointLayer = L.layerGroup().addTo(map);
      legend = L.control({position: 'bottomright'});
      legend.onAdd = () => {
        const div = L.DomUtil.create('div', 'map-legend');
        div.innerHTML = '<strong id="mapLegendName">Variable</strong><div class="gradient"></div><span id="mapLegendMin">—</span><span id="mapLegendMax" style="float:right">—</span>';
        return div;
      };
      legend.addTo(map);
    }
    pointLayer.clearLayers();
    const points = [];
    const values = mode.variables[variable] || [];
    const finite = values.filter(value => Number.isFinite(value));
    const low = finite.length ? Math.min(...finite) : 0;
    const high = finite.length ? Math.max(...finite) : 1;
    for (let i = 0; i < values.length; i += 1) {
      const lat = Number(mode.latitude[i]), lon = Number(mode.longitude[i]), value = Number(values[i]);
      if (![lat, lon, value].every(Number.isFinite)) continue;
      const ratio = high > low ? (value - low) / (high - low) : 0.5;
      const color = viridis(ratio);
      L.circleMarker([lat, lon], {radius: 4.5, color: '#7a1020', weight: 1, fillColor: color, fillOpacity: .9})
        .bindPopup(`<strong>${escapeHtml(variable)}</strong>: ${value.toPrecision(6)}<br>Lat: ${lat.toFixed(6)}<br>Lon: ${lon.toFixed(6)}<br>Altitude: ${formatNumber(mode.altitude_m[i])} m<br>UTC: ${escapeHtml(mode.time[i] || '—')}`)
        .addTo(pointLayer);
      points.push([lat, lon]);
    }
    if (points.length) {
      initialBounds = L.latLngBounds(points).pad(.08);
      map.fitBounds(initialBounds);
    }
    const note = document.getElementById('selectionNote');
    if (!points.length && note) {
      note.textContent = `${variable} has no value with a position in this product — nothing to map.`;
    }
    document.getElementById('mapLegendName').textContent = variable;
    document.getElementById('mapLegendMin').textContent = formatNumber(low);
    document.getElementById('mapLegendMax').textContent = formatNumber(high);
    setTimeout(() => map.invalidateSize(), 40);
  }
  function viridis(value) {
    const stops = [[68,1,84],[49,104,142],[53,183,121],[253,231,37]];
    const scaled = Math.max(0, Math.min(.999, value)) * (stops.length - 1);
    const index = Math.floor(scaled), amount = scaled - index;
    const rgb = stops[index].map((part, channel) => Math.round(part + (stops[index + 1][channel] - part) * amount));
    return `rgb(${rgb.join(',')})`;
  }
  const VIEWS = ['overview', 'timeseries', 'map'];
  function routeView() {
    const requested = location.pathname.split('/').filter(Boolean)[1] || 'overview';
    // A view that no longer exists - /sif/spectra, from a bookmark or the
    // browser's history - would match no card and leave the page blank.
    const view = VIEWS.includes(requested) ? requested : 'overview';
    // One control at a time in the toolbar: Variable drives the overview and
    // the time series, Index drives the map. Showing both invites changing the
    // one that has no effect on what is currently on screen.
    document.querySelectorAll('[data-toolbar-for]').forEach(node => {
      node.hidden = node.dataset.toolbarFor !== (view === 'map' ? 'map' : 'plots');
    });
    document.querySelectorAll('[data-view]').forEach(link => link.classList.toggle('active', link.dataset.view === view));
    document.querySelectorAll('[data-section]').forEach(card => card.hidden = card.dataset.section !== view && !(view === 'overview' && card.dataset.section === 'overview'));
    if (view === 'map' && map) setTimeout(() => map.invalidateSize(), 50);
  }
  function showReference(title, content) {
    document.getElementById('referenceTitle').textContent = title;
    document.getElementById('referenceBody').innerHTML = content;
    document.getElementById('reference').classList.add('show');
  }
  function table(headers, rows) {
    return `<table><thead><tr>${headers.map(value => `<th>${escapeHtml(value)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${escapeHtml(value)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  }
  async function load() {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    document.getElementById('busy').classList.add('show');
    let keepBusy = false;
    try {
      const response = await api('/api/sif');
      document.getElementById('flightName').textContent = response.flight_id || 'No project';
      if (!response.ready) {
        const state = response.status?.sif || {};
        const progress = Math.max(0, Math.min(100, Number(state.processing_progress) || 0));
        const active = ['queued', 'processing'].includes(state.processing_status);
        const publishing = state.processing_status === 'complete';
        document.getElementById('statusText').textContent = active || publishing
          ? `${state.processing_step || 'Preparing SIF products'} · ${progress.toFixed(0)}%`
          : response.message || 'Process SIF / FLOX from the Main GUI first.';
        document.getElementById('summaryGrid').innerHTML = `<div class="summary"><span>Status</span><strong>Not ready</strong><small>${escapeHtml(response.message || '')}</small></div>`;
        document.querySelector('.chart-grid').style.display = 'none';
        // This page shows the products; it does not run the job and must not
        // look as though it does. A blocking spinner here sat at 0% for as long
        // as the run took, and stayed there for good if the run never started.
        // Progress belongs in the main window, which owns the processing.
        document.getElementById('busy').classList.remove('show');
        document.getElementById('summaryGrid').innerHTML = active || publishing
          ? `<div class="summary"><span>Status</span><strong>${
              publishing ? 'Publishing' : 'Processing in the main window'
            }</strong><small>${escapeHtml(state.processing_step || '')}${
              progress ? ` · ${progress.toFixed(0)}%` : ''
            }<br>This page will show the products when it finishes.</small></div>`
          : `<div class="summary"><span>Status</span><strong>Not processed yet</strong><small>${
              escapeHtml(response.message || '')
            }<br>Select SIF in the main window and start processing.</small></div>`;
        if (active || publishing) {
          retryTimer = setTimeout(load, 2000);
        }
        return;
      }
      payload = response.data;
      document.querySelector('.chart-grid').style.display = '';
      document.getElementById('statusDot').classList.add('ready');
      document.getElementById('statusText').textContent = `Processed FULL/FLUO data loaded · ${payload.time_basis}`;
      document.querySelector('#sifProgress span').style.width = '100%';
      document.getElementById('sifProgressValue').textContent = '100% complete';
      setModes();
      renderPlots();
      routeView();
    } catch (error) {
      document.getElementById('statusText').textContent = error.message;
    } finally {
      if (!keepBusy) document.getElementById('busy').classList.remove('show');
    }
  }
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  }
  function shortTime(value) {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toISOString().replace('T', ' ').replace('.000Z', ' UTC');
  }
  function formatNumber(value) {
    return Number.isFinite(Number(value)) ? Number(value).toLocaleString(undefined, {maximumFractionDigits: 4}) : '—';
  }

  document.getElementById('refreshBtn').onclick = load;
  document.getElementById('applyBtn').onclick = renderPlots;
  document.getElementById('modeSelect').onchange = () => { setVariables(); renderPlots(); };
  document.getElementById('variableSelect').onchange = renderPlots;
  // Redraws only the map: the index shown there is chosen independently of the
  // variable the overview and time series are following.
  document.getElementById('mapVariableSelect').onchange = () => {
    const mode = selectedMode();
    if (mode) renderMap(mode, document.getElementById('mapVariableSelect').value);
  };
  document.getElementById('resetMapBtn').onclick = () => { if (map && initialBounds) map.fitBounds(initialBounds); };
  // One index on its own, in its own tab: the product and the index travel in
  // the URL so the new tab opens on exactly what was being looked at, and can
  // be kept open beside another index for comparison.
  document.getElementById('mapNewTabBtn').onclick = () => {
    const parameters = new URLSearchParams({
      mode: document.getElementById('modeSelect').value,
      index: document.getElementById('mapVariableSelect').value
    });
    window.open(`/sif/map?${parameters}`, '_blank', 'noopener');
  };
  document.getElementById('mapFullscreenBtn').onclick = () => {
    const shell = document.querySelector('[data-section="map"]');
    if (!document.fullscreenElement) {
      shell?.requestFullscreen?.().then(() => setTimeout(() => map?.invalidateSize(), 120));
    } else {
      document.exitFullscreen();
    }
  };
  // Leaving full screen changes the container size just as entering it does.
  addEventListener('fullscreenchange', () => setTimeout(() => map?.invalidateSize(), 120));
  document.getElementById('variablesBtn').onclick = () => showReference('Variables', table(['Variable', 'Description'], Object.entries(variableDescriptions)));
  document.getElementById('indicesBtn').onclick = () => showReference('Vegetation Index', table(['Index', 'Description', 'Wavelength [nm]', 'Expression'], vegetationIndices));
  document.getElementById('manualBtn').onclick = () => showReference('SIF / FLOX User Manual', `<div class="manual-copy">${escapeHtml(manual)}</div>`);
  document.getElementById('closeReference').onclick = () => document.getElementById('reference').classList.remove('show');
  document.querySelectorAll('[data-fullscreen]').forEach(button => button.onclick = () => document.getElementById(button.dataset.fullscreen)?.requestFullscreen());
  document.querySelectorAll('[data-view]').forEach(link => link.onclick = event => { event.preventDefault(); history.pushState({}, '', link.href); routeView(); });
  addEventListener('popstate', routeView);
  load();

  // Stamps the running build into the footer. A version number alone cannot
  // answer "am I running the code I just pulled?" - it only changes at a
  // release, so a fetch that was never merged looks identical to an update.
  fetch('/api/build', {cache: 'no-store'})
    .then(response => response.json())
    .then(info => {
      document.querySelectorAll('.app-version').forEach(node => {
        node.textContent = `Version ${info.version} · build ${info.build}`;
      });
    })
    .catch(() => {});
})();
