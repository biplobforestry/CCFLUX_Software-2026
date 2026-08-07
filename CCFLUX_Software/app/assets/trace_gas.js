/* Trace Gas Investigation.
 *
 * Attribute a species' variation to instrument state, altitude and time, so a
 * disagreement with the reference analyser can be read as a drift rather than
 * argued about. Flight_CC0806 is the case this was built for: MIRO CO2 sat at
 * R2 = 0.05 against the Picarro while H2O, through the same readers and clock,
 * reached 0.996, and the difference tracked the MIRO cell temperature at
 * 6.2 ppm per degree.
 */
(() => {
  const PLOT_FONT = {family: 'Segoe UI, Inter, Arial, sans-serif', size: 12, color: '#eaf7ff'};
  const LAYOUT = {
    paper_bgcolor: '#081a28', plot_bgcolor: '#06131f', font: PLOT_FONT,
    margin: {l: 62, r: 62, t: 12, b: 46},
    xaxis: {gridcolor: '#17384b', zerolinecolor: '#24475a'},
    yaxis: {gridcolor: '#17384b', zerolinecolor: '#24475a'},
    legend: {orientation: 'h', y: -0.22, bgcolor: 'rgba(0,0,0,0)'},
    showlegend: true,
  };
  const CONFIG = {displaylogo: false, responsive: true,
                  modeBarButtonsToRemove: ['lasso2d', 'select2d']};

  let state = null;        // the last payload from the server
  let selected = null;     // {species, driver}

  const $ = id => document.getElementById(id);
  const message = $('message');
  const status = $('status');

  function fail(text) {
    message.textContent = text;
    message.style.display = 'block';
  }
  function clearFailure() {
    message.style.display = 'none';
    message.textContent = '';
  }

  function busy(title, text) {
    $('busyTitle').textContent = title;
    $('busyMessage').textContent = text;
    if (!$('busyDialog').open) $('busyDialog').showModal();
  }
  function idle() {
    if ($('busyDialog').open) $('busyDialog').close();
  }

  /* A statistic that could not be computed is blank, never a silent zero. */
  function fixed(value, digits) {
    return (value === null || value === undefined || !isFinite(value))
      ? '—' : Number(value).toFixed(digits);
  }
  /* Concentrations here span nanomole and micromole fractions in one table. */
  function significant(value, digits = 4) {
    if (value === null || value === undefined || !isFinite(value)) return '—';
    const size = Math.abs(value);
    if (size !== 0 && (size < 1e-3 || size >= 1e5)) return value.toExponential(2);
    return Number(value).toPrecision(digits).replace(/\.?0+$/, '');
  }
  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, ch => (
      {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]
    ));
  }
  /* R2 is the reason anyone opens this page; colour it so it reads at a glance. */
  function gradeR2(value) {
    if (value === null || value === undefined || !isFinite(value)) return 'dim';
    if (value >= 0.7) return 'bad';      // a driver explaining the species IS the problem
    if (value >= 0.3) return 'warn';
    return 'good';
  }
  function gradeAgreement(value) {
    if (value === null || value === undefined || !isFinite(value)) return 'dim';
    if (value >= 0.9) return 'good';
    if (value >= 0.5) return 'warn';
    return 'bad';
  }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({cache: 'no-store'}, options || {}));
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `Request failed: ${path}`);
    return body;
  }

  function filters() {
    return {
      resolution_seconds: Number($('resolution').value),
      start: $('start').value.trim(),
      end: $('end').value.trim(),
      altitude_min: $('altitudeMin').value.trim(),
      altitude_max: $('altitudeMax').value.trim(),
      stable_ambient_only: $('stableAmbient').checked,
    };
  }

  async function load() {
    clearFailure();
    busy('Trace Gas Investigation', 'Reading MIRO, Picarro and navigation, and fitting the drivers…');
    try {
      state = await api('/api/miro-rack/trace-gas/data', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(filters()),
      });
      renderDrivers();
      renderMatrix();
      renderReference();
      renderNotes();
      draw();
      const w = state.window;
      status.textContent =
        `${w.samples} samples at ${w.resolution_seconds} s · ${w.start.replace('T', ' ')}`
        + ` to ${w.end.replace('T', ' ')} UTC`
        + (w.altitude_min !== null && w.altitude_max !== null
            ? ` · altitude ${fixed(w.altitude_min, 0)}–${fixed(w.altitude_max, 0)} m` : '')
        + (w.stable_ambient_only ? ' · stable ambient only' : ' · all valve states');
    } catch (error) {
      status.textContent = 'No investigation is loaded.';
      fail(error.message);
    } finally {
      idle();
    }
  }

  function renderDrivers() {
    const select = $('driver');
    const previous = selected ? selected.driver : select.value;
    select.innerHTML = state.drivers
      .map(d => `<option value="${escapeHtml(d.name)}">${escapeHtml(d.label)}`
                + `${d.unit ? ` [${escapeHtml(d.unit)}]` : ''}</option>`).join('');
    if (previous && state.drivers.some(d => d.name === previous)) select.value = previous;
    select.onchange = () => {
      if (selected) selected.driver = select.value;
      renderMatrix();
      draw();
    };
  }

  function currentDriver() {
    return $('driver').value || (state.drivers[0] && state.drivers[0].name);
  }

  function renderMatrix() {
    const driver = currentDriver();
    const table = $('matrix');
    const unitOf = name => {
      const found = state.drivers.find(d => d.name === name);
      return found && found.unit ? found.unit : 'unit';
    };
    table.querySelector('thead').innerHTML = `
      <tr>
        <th>Species</th><th>Unit</th><th>n</th><th>Mean</th><th>SD</th>
        <th>Slope /${escapeHtml(unitOf(driver))}</th>
        <th>%/${escapeHtml(unitOf(driver))}</th>
        <th>R&sup2;</th>
        <th>Partial /${escapeHtml(unitOf(driver))}</th>
        <th>Joint R&sup2;</th>
        <th></th>
      </tr>`;
    table.querySelector('tbody').innerHTML = state.species.map(entry => {
      const d = entry.drivers[driver] || {};
      // Collinearity first: where the drivers are nearly the same line the
      // partial column carries no information, and calling something
      // "confounded" on that basis would be a claim the fit cannot support.
      const flag = entry.partial_reliable === false
        ? '<span class="pill dim" title="The recorded drivers are too nearly the'
          + ' same line over this window to be told apart">collinear</span>'
        : (d.confounded ? '<span class="pill warn">confounded</span>' : '');
      const chosen = selected && selected.species === entry.name;
      return `<tr data-species="${escapeHtml(entry.name)}" aria-selected="${chosen}">
        <td><strong>${escapeHtml(entry.label)}</strong></td>
        <td class="dim">${escapeHtml(entry.unit)}</td>
        <td>${entry.samples}</td>
        <td>${significant(entry.mean)}</td>
        <td>${significant(entry.sd, 3)}</td>
        <td>${significant(d.slope, 3)}</td>
        <td>${fixed(d.percent_per_unit, 3)}</td>
        <td class="${gradeR2(d.r_squared)}">${fixed(d.r_squared, 3)}</td>
        <td class="${entry.partial_reliable === false ? 'dim' : ''}">${significant(d.partial_slope, 3)}</td>
        <td class="dim">${fixed(entry.joint_r_squared, 3)}</td>
        <td>${flag}</td>
      </tr>`;
    }).join('');
    table.querySelectorAll('tbody tr').forEach(row => {
      row.onclick = () => {
        selected = {species: row.dataset.species, driver: currentDriver()};
        renderMatrix();
        draw();
      };
    });
  }

  function renderReference() {
    const card = $('referenceCard');
    const referenced = state.species.filter(entry => entry.reference);
    if (!referenced.length) {
      card.style.display = 'none';
      return;
    }
    card.style.display = '';
    const table = $('reference');
    table.querySelector('thead').innerHTML = `
      <tr>
        <th>Species</th><th>Reference</th><th>n</th>
        <th>R&sup2;</th><th>Slope</th><th>Bias</th><th>RMSE</th>
        <th>Drift driver</th><th>Drift /unit</th><th>Drift R&sup2;</th>
        <th>R&sup2; after removal</th><th>Slope after</th>
      </tr>`;
    table.querySelector('tbody').innerHTML = referenced.map(entry => {
      const r = entry.reference;
      const d = entry.reference_detrended || {};
      return `<tr data-species="${escapeHtml(entry.name)}">
        <td><strong>${escapeHtml(entry.label)}</strong> <span class="dim">[${escapeHtml(entry.unit)}]</span></td>
        <td class="dim">${escapeHtml(entry.reference_label || '')}</td>
        <td>${r.samples}</td>
        <td class="${gradeAgreement(r.r_squared)}">${fixed(r.r_squared, 4)}</td>
        <td>${fixed(r.slope, 3)}</td>
        <td>${significant(r.bias, 3)}</td>
        <td>${significant(r.rmse, 3)}</td>
        <td class="dim">${escapeHtml(d.driver_label || '—')}</td>
        <td>${significant(d.slope_per_unit, 3)}</td>
        <td class="${gradeR2(d.driver_r_squared)}">${fixed(d.driver_r_squared, 3)}</td>
        <td class="${gradeAgreement(d.r_squared)}">${fixed(d.r_squared, 4)}</td>
        <td>${fixed(d.slope, 3)}</td>
      </tr>`;
    }).join('');
    table.querySelectorAll('tbody tr').forEach(row => {
      row.onclick = () => {
        selected = {species: row.dataset.species, driver: currentDriver()};
        renderMatrix();
        draw();
      };
    });
  }

  function renderNotes() {
    $('notes').innerHTML = (state.notes || [])
      .map(note => `<li>${escapeHtml(note)}</li>`).join('')
      || '<li>No caveats apply to this window.</li>';
  }

  function chosenSpecies() {
    if (!state.species.length) return null;
    if (selected) {
      const found = state.species.find(entry => entry.name === selected.species);
      if (found) return found;
    }
    // Default to the species a driver explains best, which is the one worth
    // looking at first.
    const driver = currentDriver();
    return state.species.slice().sort((a, b) =>
      ((b.drivers[driver] || {}).r_squared || 0) - ((a.drivers[driver] || {}).r_squared || 0)
    )[0];
  }

  function draw() {
    const entry = chosenSpecies();
    if (!entry) return;
    const driver = currentDriver();
    const info = state.drivers.find(d => d.name === driver) || {label: driver, unit: ''};
    const series = state.series;
    const time = series.time;
    const values = series[entry.name];
    const drive = series[driver];
    const unit = entry.unit ? ` [${entry.unit}]` : '';
    const driverUnit = info.unit ? ` [${info.unit}]` : '';

    $('seriesTitle').textContent = `${entry.label} and ${info.label.toLowerCase()} over time`;
    Plotly.react('seriesPlot', [
      {x: time, y: values, name: `${entry.label}${unit}`, mode: 'lines',
       line: {color: '#35d5ff', width: 1.4}},
      {x: time, y: drive, name: `${info.label}${driverUnit}`, mode: 'lines',
       yaxis: 'y2', line: {color: '#ffb454', width: 1.4}},
    ], Object.assign({}, LAYOUT, {
      xaxis: Object.assign({}, LAYOUT.xaxis, {title: {text: 'Time (UTC)'}}),
      yaxis: Object.assign({}, LAYOUT.yaxis, {title: {text: `${entry.label}${unit}`}}),
      yaxis2: {overlaying: 'y', side: 'right', title: {text: `${info.label}${driverUnit}`},
               gridcolor: 'rgba(0,0,0,0)', color: '#ffb454'},
    }), CONFIG);

    const fit = entry.drivers[driver] || {};
    $('scatterTitle').textContent = `${entry.label} against ${info.label.toLowerCase()}`;
    const traces = [{
      x: drive, y: values, mode: 'markers', name: 'samples',
      marker: {size: 4, color: '#35d5ff', opacity: 0.55},
    }];
    const finite = drive.filter(v => v !== null && isFinite(v));
    if (finite.length && isFinite(fit.slope)) {
      const lo = Math.min(...finite), hi = Math.max(...finite);
      traces.push({
        x: [lo, hi], y: [fit.slope * lo + fit.intercept, fit.slope * hi + fit.intercept],
        mode: 'lines', name: `fit: ${significant(fit.slope, 3)} ${entry.unit}/${info.unit || 'unit'}`
          + `, R² ${fixed(fit.r_squared, 3)}`,
        line: {color: '#42d99b', width: 2},
      });
    }
    Plotly.react('scatterPlot', traces, Object.assign({}, LAYOUT, {
      xaxis: Object.assign({}, LAYOUT.xaxis, {title: {text: `${info.label}${driverUnit}`}}),
      yaxis: Object.assign({}, LAYOUT.yaxis, {title: {text: `${entry.label}${unit}`}}),
    }), CONFIG);

    drawReferencePlot(entry);
  }

  function drawReferencePlot(entry) {
    const holder = $('referencePlot');
    const reference = state.series[`ref::${entry.name}`];
    if (!entry.reference || !reference) {
      Plotly.purge(holder);
      holder.style.display = 'none';
      return;
    }
    holder.style.display = '';
    const values = state.series[entry.name];
    const unit = entry.unit ? ` [${entry.unit}]` : '';
    const detrended = entry.reference_detrended;
    const traces = [{
      x: reference, y: values, mode: 'markers', name: 'as measured',
      marker: {size: 4, color: '#ff7b7b', opacity: 0.5},
    }];
    if (detrended && isFinite(detrended.slope_per_unit) && isFinite(detrended.intercept)) {
      const drive = state.series[detrended.driver];
      // Exactly the correction the server scored: the fit is on the difference
      // between the analysers, so what is subtracted is the drift and not the
      // species' own signal, and the intercept carries the constant offset -
      // without it the corrected cloud sits at the old bias instead of on 1:1.
      const corrected = values.map((value, index) => {
        const d = drive ? drive[index] : null;
        if (value === null || d === null || d === undefined) return null;
        return value - (detrended.slope_per_unit * d + detrended.intercept);
      });
      traces.push({
        x: reference, y: corrected,
        mode: 'markers',
        name: `after removing ${detrended.driver_label.toLowerCase()}`
              + ` (R² ${fixed(detrended.r_squared, 3)})`,
        marker: {size: 4, color: '#42d99b', opacity: 0.55},
      });
    }
    const finite = reference.filter(v => v !== null && isFinite(v));
    if (finite.length) {
      const lo = Math.min(...finite), hi = Math.max(...finite);
      traces.push({x: [lo, hi], y: [lo, hi], mode: 'lines', name: '1:1',
                   line: {color: '#a9c8d5', width: 1.4, dash: 'dot'}});
    }
    Plotly.react(holder, traces, Object.assign({}, LAYOUT, {
      xaxis: Object.assign({}, LAYOUT.xaxis,
        {title: {text: `${entry.reference_label}${unit}`}}),
      yaxis: Object.assign({}, LAYOUT.yaxis, {title: {text: `MIRO ${entry.label}${unit}`}}),
    }), CONFIG);
  }

  /* Export ---------------------------------------------------------------- */

  function openExport() {
    $('exportMessage').textContent = '';
    $('exportDialog').showModal();
  }

  async function runExport() {
    const formats = [...document.querySelectorAll('.exportFormat:checked')].map(box => box.value);
    if (!formats.length) {
      $('exportMessage').textContent = 'Choose at least one format.';
      return;
    }
    const entry = chosenSpecies();
    $('exportDialog').close();
    busy('Export figure', 'Rendering at seven inches wide, nothing below nine point…');
    try {
      const result = await api('/api/miro-rack/trace-gas/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(Object.assign(filters(), {
          view: $('exportView').value,
          formats,
          dpi: Number($('exportDpi').value) || 600,
          species: entry ? entry.name : null,
          driver: currentDriver(),
        })),
      });
      idle();
      status.textContent = `Exported ${result.files.length} file(s) to ${result.directory}`;
      clearFailure();
    } catch (error) {
      idle();
      fail(error.message);
    }
  }

  /* Wiring ---------------------------------------------------------------- */

  $('apply').onclick = load;
  $('reset').onclick = () => {
    $('start').value = '';
    $('end').value = '';
    $('altitudeMin').value = '';
    $('altitudeMax').value = '';
    load();
  };
  $('exportButton').onclick = openExport;
  $('exportRun').onclick = runExport;
  $('exportCancel').onclick = () => $('exportDialog').close();
  window.addEventListener('resize', () => {
    ['seriesPlot', 'scatterPlot', 'referencePlot'].forEach(id => {
      const node = $(id);
      if (node && node.data) Plotly.Plots.resize(node);
    });
  });

  load();
})();
