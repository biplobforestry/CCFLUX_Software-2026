(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const number = value => Number(value ?? 0).toLocaleString();
  async function api(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(`Request failed (${r.status})`);return r.json();}

  const plotConfig = {responsive:true,displaylogo:false,doubleClick:'reset',
    toImageButtonOptions:{format:'png',scale:2}};
  // SVG traces, not scattergl. A flight is at most a few thousand captures,
  // which SVG draws comfortably, and nine WebGL panels on one page is a page
  // that shows "WebGL is not supported by your browser" instead of the figures
  // wherever WebGL is missing - a VM, a remote desktop, an old driver - or once
  // the browser's context limit is reached.
  // Matches the FLIR and SIF workspaces so a reader moving between them is not
  // relearning the axes each time.
  function layout(title, xTitle, yTitle, extra = {}) {
    const base = {
      title:{text:title,x:.5,y:.97,yanchor:'top',font:{size:17}},
      paper_bgcolor:'#f7fafc',plot_bgcolor:'#fff',
      font:{family:'Arial, sans-serif',size:13,color:'#172431'},
      // The legend sits above the plot, so the title needs room above it or the
      // two print on top of each other - which they did on every panel.
      margin:{l:78,r:58,t:86,b:64},
      xaxis:{title:xTitle,gridcolor:'#dce5ea',automargin:true},
      yaxis:{title:yTitle,gridcolor:'#dce5ea',automargin:true,separatethousands:true,exponentformat:'none'},
      legend:{orientation:'h',y:1.06,x:0,yanchor:'bottom'},hovermode:'closest'
    };
    return {...base,...extra,
      xaxis:{...base.xaxis,...(extra.xaxis||{})},
      yaxis:{...base.yaxis,...(extra.yaxis||{})}};
  }
  const COLOURS = {
    a:'#0072b2', b:'#d55e00', c:'#009e73', d:'#cc79a7', e:'#5b4b8a', warn:'#a3762b'
  };
  // A capture whose field was never recorded is a gap, not a zero: Plotly is
  // given null so the line breaks instead of diving to the axis.
  const finite = value => (Number.isFinite(Number(value)) ? Number(value) : null);
  const series = (rows, field) => rows.map(row => finite(row[field]));
  const hasAny = values => values.some(value => value !== null);

  function noData(id, message) {
    const target = $(id);
    if (target) target.innerHTML = `<p class="muted" style="padding:18px">${message}</p>`;
  }

  function card(name, value, note) {
    return `<div class="summary"><span>${name}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;
  }
  function renderSummary(summary) {
    const evaluated = summary.image_count ?? 0;
    const delivered = summary.delivered_image_count;
    const interval = summary.median_trigger_interval_seconds;
    $('summaryGrid').innerHTML = [
      card('Images evaluated', number(evaluated),
        delivered && delivered !== evaluated ? `${number(delivered)} delivered` : 'Inside the Time Filter'),
      card('Captures', number(summary.capture_count),
        `${number(summary.complete_capture_count)} complete · ${number(summary.incomplete_capture_count)} incomplete`),
      card('Bands', (summary.bands || []).join(', ') || '—', 'Present in the delivery'),
      card('Trigger interval',
        Number.isFinite(Number(interval)) ? `${Number(interval).toFixed(2)} s` : '—', 'Median between captures'),
      card('GPS present', number(summary.gps_present_count), 'Of the evaluated images'),
      card('Exposure present', number(summary.exposure_present_count), 'Of the evaluated images')
    ].join('');
  }
  // A file the camera wrote badly is reported, not hidden, and never stops the
  // rest of the delivery being shown.
  function renderQuality(summary, warnings) {
    const items = [];
    const corrupt = (summary.corrupt_files || []).length;
    const small = (summary.unusually_small_files || []).length;
    if (corrupt) items.push([`${number(corrupt)} file(s) could not be read and were skipped`, true]);
    if (small) items.push([`${number(small)} file(s) are unusually small`, true]);
    (warnings || []).forEach(text => items.push([String(text), true]));
    (summary.excluded_operations || []).forEach(name =>
      items.push([`Not performed here: ${name}`, false]));
    if (!items.length) items.push(['No acquisition problem was found.', false]);
    $('qcList').innerHTML = items
      .map(([text, warn]) => `<li class="${warn ? 'warn' : ''}">${text}</li>`).join('');
  }
  function renderThumbnails(thumbnails) {
    const shown = thumbnails || [];
    $('thumbs').innerHTML = shown.length
      ? shown.map(item =>
          `<figure><img loading="lazy" src="${item.url}" alt="${item.name}"
             onerror="this.closest('figure').remove()"><figcaption>${item.name}</figcaption></figure>`).join('')
      : '<p class="muted">No thumbnail was written for this flight.</p>';
  }
  function renderExports(exports) {
    $('exportList').innerHTML = (exports || []).length
      ? exports.map(path => `<li>${path}</li>`).join('')
      : '<li>No export was written.</li>';
  }

  function renderTraceability(summary) {
    const identity = summary.camera_identity || {};
    const entries = [
      ['Rig', identity.rig_name],
      ['DLS serial', identity.dls_serial],
      ['DLS firmware', identity.dls_software],
      ['Flight ID', identity.flight_id]
    ].filter(([, value]) => value);
    $('traceability').innerHTML = entries.length
      ? entries.map(([name, value]) =>
          `<li><span>${name}</span><strong>${value}</strong></li>`).join('')
      : '<li><span>Camera identity</span><strong>Not recorded in the delivery</strong></li>';
  }

  // Captures are ordered by trigger time upstream. Anything with no usable
  // trigger time - a camera powered up without a clock write - cannot be placed
  // on a time axis and is left out of the series rather than plotted at 1970.
  function timedCaptures(captures) {
    return (captures || [])
      .filter(row => row.trigger_time && !String(row.trigger_time).startsWith('19'));
  }

  function renderCadence(captures) {
    const timed = timedCaptures(captures);
    if (timed.length < 2) return noData('cadencePlot', 'Fewer than two timed captures; there is no interval to show.');
    const times = timed.map(row => row.trigger_time);
    const intervals = timed.slice(1).map((row, index) =>
      (new Date(row.trigger_time) - new Date(timed[index].trigger_time)) / 1000);
    const sorted = [...intervals].sort((a, b) => a - b);
    const nominal = sorted[Math.floor(sorted.length / 2)];
    Plotly.react('cadencePlot', [
      {type:'scatter',mode:'markers',x:times.slice(1),y:intervals,name:'Trigger interval',
       marker:{color:COLOURS.a,size:4},
       hovertemplate:'%{x}<br>%{y:.3f} s<extra></extra>'},
      {type:'scatter',mode:'lines',x:[times[1],times[times.length-1]],y:[nominal,nominal],
       name:`Median ${nominal.toFixed(2)} s`,line:{color:COLOURS.b,width:1.6,dash:'dash'}}
    ], layout('Time between camera triggers','Capture time (UTC)','Interval [s]'), plotConfig);
  }

  function renderTrack(captures) {
    const rows = (captures || []).filter(row =>
      finite(row.gps_latitude) !== null && finite(row.gps_longitude) !== null
      && Number(row.gps_latitude) !== 0 && Number(row.gps_longitude) !== 0);
    if (!rows.length) return noData('trackPlot', 'No capture carries a usable GPS position.');
    const altitudes = series(rows, 'gps_altitude');
    Plotly.react('trackPlot', [{
      type:'scatter',mode:'markers',
      x:series(rows,'gps_longitude'),y:series(rows,'gps_latitude'),
      marker:{color:hasAny(altitudes)?altitudes:COLOURS.a,size:5,colorscale:'Viridis',
        showscale:hasAny(altitudes),colorbar:{title:{text:'Alt [m]',side:'right'}}},
      text:rows.map(row => row.trigger_time || row.capture_id || ''),
      hovertemplate:'%{text}<br>%{y:.5f}, %{x:.5f}<extra></extra>',
      name:'Capture position'
    }], layout('Capture positions from image GPS','Longitude [deg]','Latitude [deg]',
      {yaxis:{scaleanchor:'x',scaleratio:1}}), plotConfig);
  }

  function renderAltitude(captures) {
    const rows = timedCaptures(captures);
    const gps = series(rows,'gps_altitude'), barometric = series(rows,'pressure_alt');
    if (!hasAny(gps) && !hasAny(barometric)) return noData('altitudePlot', 'No altitude was recorded.');
    const times = rows.map(row => row.trigger_time);
    const traces = [];
    if (hasAny(gps)) traces.push({type:'scatter',mode:'lines',x:times,y:gps,
      name:'GPS altitude',line:{color:COLOURS.a,width:1.3}});
    if (hasAny(barometric)) traces.push({type:'scatter',mode:'lines',x:times,y:barometric,
      name:'Barometric altitude',line:{color:COLOURS.b,width:1.3}});
    Plotly.react('altitudePlot', traces,
      layout('Altitude per capture','Capture time (UTC)','Altitude [m]'), plotConfig);
  }

  function renderIrradiance(captures) {
    const rows = timedCaptures(captures);
    const times = rows.map(row => row.trigger_time);
    const wanted = [
      ['dls_irradiance','Irradiance',COLOURS.a],
      ['dls_horizontal_irradiance','Horizontal',COLOURS.b],
      ['dls_direct_irradiance','Direct',COLOURS.c],
      ['dls_scattered_irradiance','Scattered',COLOURS.d]
    ];
    const traces = wanted
      .map(([field,name,colour]) => [series(rows,field),name,colour])
      .filter(([values]) => hasAny(values))
      .map(([values,name,colour]) => ({type:'scatter',mode:'lines',x:times,y:values,
        name,line:{color:colour,width:1.3}}));
    if (!traces.length) return noData('irradiancePlot', 'The light sensor recorded no irradiance for this flight.');
    Plotly.react('irradiancePlot', traces,
      layout('Downwelling light sensor irradiance','Capture time (UTC)','Irradiance [W/m²/nm]'),
      plotConfig);
  }

  function renderExposure(captures) {
    const rows = timedCaptures(captures);
    const times = rows.map(row => row.trigger_time);
    const exposure = series(rows,'exposure_time'), iso = series(rows,'iso_speed');
    if (!hasAny(exposure) && !hasAny(iso)) return noData('exposurePlot', 'No exposure metadata was recorded.');
    const traces = [];
    if (hasAny(exposure)) traces.push({type:'scatter',mode:'lines',x:times,y:exposure,
      name:'Exposure time',line:{color:COLOURS.a,width:1.3}});
    if (hasAny(iso)) traces.push({type:'scatter',mode:'lines',x:times,y:iso,
      name:'ISO',yaxis:'y2',line:{color:COLOURS.b,width:1.3}});
    Plotly.react('exposurePlot', traces,
      layout('Exposure and gain per capture','Capture time (UTC)','Exposure time [s]',
        {yaxis2:{title:'ISO',overlaying:'y',side:'right',gridcolor:'transparent',automargin:true}}),
      plotConfig);
  }

  function renderOrientation(captures) {
    const rows = timedCaptures(captures);
    const times = rows.map(row => row.trigger_time);
    const wanted = [
      ['dls_yaw','Yaw',COLOURS.a],
      ['dls_pitch','Pitch',COLOURS.b],
      ['dls_roll','Roll',COLOURS.c]
    ];
    const traces = wanted
      .map(([field,name,colour]) => [series(rows,field),name,colour])
      .filter(([values]) => hasAny(values))
      .map(([values,name,colour]) => ({type:'scatter',mode:'lines',x:times,y:values,
        name,line:{color:colour,width:1.3}}));
    if (!traces.length) return noData('orientationPlot', 'The light sensor recorded no orientation.');
    Plotly.react('orientationPlot', traces,
      layout('Light sensor attitude','Capture time (UTC)','Angle [deg]'), plotConfig);
  }

  function renderSolar(captures) {
    const rows = timedCaptures(captures);
    const times = rows.map(row => row.trigger_time);
    const elevation = series(rows,'solar_elevation');
    const azimuth = series(rows,'solar_azimuth');
    if (!hasAny(elevation) && !hasAny(azimuth)) return noData('solarPlot', 'No solar geometry was recorded.');
    const traces = [];
    if (hasAny(elevation)) traces.push({type:'scatter',mode:'lines',x:times,y:elevation,
      name:'Solar elevation',line:{color:COLOURS.b,width:1.4}});
    if (hasAny(azimuth)) traces.push({type:'scatter',mode:'lines',x:times,y:azimuth,
      name:'Solar azimuth',yaxis:'y2',line:{color:COLOURS.e,width:1.4}});
    Plotly.react('solarPlot', traces,
      layout('Solar geometry as recorded by the light sensor','Capture time (UTC)','Elevation [rad, as recorded]',
        {yaxis2:{title:'Azimuth [rad]',overlaying:'y',side:'right',gridcolor:'transparent',automargin:true}}),
      plotConfig);
  }

  function renderTemperature(captures) {
    const rows = timedCaptures(captures);
    const values = series(rows,'imager_temperature_c');
    if (!hasAny(values)) return noData('temperaturePlot', 'The camera recorded no imager temperature.');
    Plotly.react('temperaturePlot', [{type:'scatter',mode:'lines',
      x:rows.map(row => row.trigger_time),y:values,
      name:'Imager temperature',line:{color:COLOURS.b,width:1.3}}],
      layout('Imager body temperature','Capture time (UTC)','Temperature [°C]'), plotConfig);
  }

  function renderCompleteness(captures) {
    const rows = captures || [];
    if (!rows.length) return noData('completenessPlot', 'No capture was grouped for this flight.');
    const counts = new Map();
    rows.forEach(row => {
      const bands = (row.found_bands || []).length;
      counts.set(bands, (counts.get(bands) || 0) + 1);
    });
    const keys = [...counts.keys()].sort((a, b) => a - b);
    Plotly.react('completenessPlot', [{
      type:'bar',x:keys.map(String),y:keys.map(key => counts.get(key)),
      marker:{color:keys.map(key => (key === 6 ? COLOURS.c : COLOURS.warn))},
      hovertemplate:'%{x} band(s): %{y} capture(s)<extra></extra>',name:'Captures'
    }], layout('Bands delivered per capture','Bands in the capture','Captures',
      // Band counts are categories, not a continuous scale. Left numeric,
      // Plotly auto-ranged a single "6" across 5.6-6.4 and drew one bar the
      // full width of the panel.
      {xaxis:{type:'category'},bargap:.6}), plotConfig);
  }

  function renderPlots(data) {
    const captures = data.captures || [];
    renderTraceability(data.summary || {});
    renderCadence(captures);
    renderTrack(captures);
    renderAltitude(captures);
    renderIrradiance(captures);
    renderExposure(captures);
    renderOrientation(captures);
    renderSolar(captures);
    renderTemperature(captures);
    renderCompleteness(captures);
  }
  async function load() {
    $('busy').classList.add('show');
    try {
      const response = await api('/api/micasense');
      $('flightName').textContent = response.flight_id || 'No project';
      if (!response.ready) {
        $('statusText').textContent = response.message || 'MicaSense products are not ready';
        document.querySelector('main').innerHTML =
          `<div class="empty"><div><strong>MicaSense processing is required</strong>${response.message || ''}</div></div>`;
        return;
      }
      const data = response.data || {};
      $('statusDot').classList.add('ready');
      $('statusText').textContent = 'Processed MicaSense data loaded from the active Flight Project';
      renderSummary(data.summary || {});
      renderQuality(data.summary || {}, data.warnings);
      renderPlots(data);
      renderThumbnails(data.thumbnails);
      renderExports(data.exports);
    } catch (error) {
      $('statusText').textContent = `MicaSense view failed: ${error.message}`;
      $('statusText').style.color = 'var(--danger)';
    } finally {
      $('busy').classList.remove('show');
    }
  }
  $('refreshBtn').onclick = load;
  load();
})();
