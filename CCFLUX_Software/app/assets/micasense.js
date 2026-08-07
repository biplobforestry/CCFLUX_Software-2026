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
    const cards = [
      card('Images evaluated', number(evaluated),
        delivered && delivered !== evaluated ? `${number(delivered)} delivered` : 'Inside the Time Filter'),
      card('Captures', number(summary.capture_count),
        `${number(summary.complete_capture_count)} complete · ${number(summary.incomplete_capture_count)} incomplete`),
      card('Bands', (summary.bands || []).join(', ') || '—', 'Present in the delivery'),
      card('Trigger interval',
        Number.isFinite(Number(interval)) ? `${Number(interval).toFixed(2)} s` : '—', 'Median between captures'),
      card('GPS present', number(summary.gps_present_count), 'Of the evaluated images'),
      card('Exposure present', number(summary.exposure_present_count), 'Of the evaluated images')
    ];
    // The standalone QA's own metrics, on its definitions.
    const hz = summary.capture_frequency_hz;
    if (Number.isFinite(Number(hz))) {
      cards.push(card('Capture frequency', `${Number(hz).toFixed(3)} Hz`,
        `${number(summary.cadence_outliers)} interval(s) past ${
          Number(summary.cadence_warning_threshold_seconds || 0).toFixed(2)} s`));
    }
    if (summary.sharpness_samples) {
      cards.push(card('Sharpness sampled', number(summary.sharpness_samples),
        `${number(summary.relative_blur_flags)} in this flight's softest 5% · ${
          number(summary.saturation_flags_over_1_percent)} over 1% saturated`));
    }
    $('summaryGrid').innerHTML = cards.join('');
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

  // The reference's definition, not a wall-clock difference: seconds per trigger
  // from the boot clock divided by the image-number gap, plotted against image
  // number. A break in the numbering then reads as one slow interval instead of
  // a spike, and the boot clock keeps counting when the GPS never locks.
  function renderCadence(quality) {
    const intervals = (quality || {}).normalised_intervals || [];
    if (intervals.length < 1) return noData('cadencePlot',
      'No capture pair carries both an image number and a boot timestamp, so there is no interval to show.');
    const x = intervals.map(item => item.image_number);
    const y = intervals.map(item => item.seconds);
    const target = quality.expected_interval_seconds || quality.median_capture_interval_seconds;
    const limit = quality.cadence_warning_threshold_seconds;
    const traces = [{type:'scatter',mode:'lines',x,y,name:'Seconds per trigger',
      line:{color:COLOURS.a,width:1},
      hovertemplate:'Image %{x}<br>%{y:.3f} s<extra></extra>'}];
    const span = [x[0], x[x.length-1]];
    if (target) traces.push({type:'scatter',mode:'lines',x:span,y:[target,target],
      name:`Target ${Number(target).toFixed(2)} s`,line:{color:COLOURS.c,width:1.6}});
    if (limit) traces.push({type:'scatter',mode:'lines',x:span,y:[limit,limit],
      name:`Warning ${Number(limit).toFixed(2)} s`,
      line:{color:COLOURS.b,width:1.4,dash:'dash'}});
    Plotly.react('cadencePlot', traces,
      layout('Normalized capture interval','Image number [–]','Seconds per trigger [s]'),
      plotConfig);
  }

  // Every trigger and what it delivered, against image number as the reference
  // plots it. Kept separate from the band histogram: the histogram says how many
  // captures were short, this says which ones and when.
  function renderIntegrity(captures) {
    const rows = captures || [];
    if (!rows.length) return noData('integrityPlot', 'No capture was grouped for this flight.');
    const groups = [
      ['Complete, timed', COLOURS.c, r => r.complete && r.trigger_time && !String(r.trigger_time).startsWith('19')],
      ['Complete, no usable time', COLOURS.warn, r => r.complete && (!r.trigger_time || String(r.trigger_time).startsWith('19'))],
      ['Bands missing', COLOURS.b, r => !r.complete]
    ];
    const traces = groups.map(([name, colour, test]) => {
      const picked = rows.filter(test);
      return picked.length ? {
        type:'scatter',mode:'markers',
        x:picked.map(r => r.image_number),
        y:picked.map(() => name),
        marker:{color:colour,size:6},name:`${name}: ${picked.length}`,
        text:picked.map(r => (r.found_bands||[]).length + ' band(s)'),
        hovertemplate:'Image %{x}<br>%{text}<extra></extra>'
      } : null;
    }).filter(Boolean);
    Plotly.react('integrityPlot', traces,
      layout('Capture integrity timeline','Image number [–]','Capture outcome',
        {yaxis:{type:'category',automargin:true}}), plotConfig);
  }

  // Sampled panchromatic sharpness, with this flight's bottom 5% marked - a
  // relative threshold, as the reference computes it, not an absolute blur limit.
  function renderSharpness(captures, quality) {
    const rows = (captures || []).filter(row => finite(row.sharpness) !== null);
    if (!rows.length) return noData('sharpnessPlot',
      'No panchromatic band was sampled for this flight.');
    const x = rows.map(row => row.image_number);
    const traces = [{type:'scatter',mode:'markers',x,y:series(rows,'sharpness'),
      name:`Sharpness sample (${rows.length})`,marker:{color:COLOURS.a,size:7},
      hovertemplate:'Image %{x}<br>sharpness %{y:.5f}<extra></extra>'}];
    const limit = (quality || {}).sharpness_bottom_5_percent;
    if (limit) traces.push({type:'scatter',mode:'lines',x:[x[0],x[x.length-1]],
      y:[limit,limit],name:'Bottom 5% of this flight',
      line:{color:COLOURS.b,width:1.4,dash:'dash'}});
    Plotly.react('sharpnessPlot', traces,
      layout('Panchromatic image quality sample','Image number [–]',
        'Normalized sharpness [–]'),
      plotConfig);
  }

  // The track is drawn on a base map so a position can be read against the
  // ground rather than against bare degrees. MapLibre carries the OpenStreetMap
  // raster, the same tiles every other map in this application uses. Where that
  // cannot render - no WebGL on a VM or remote desktop, no network for tiles -
  // the plain degree scatter is drawn instead, because a track with no basemap
  // is still the measurement and an error panel is not.
  function trackTraceAndLayout(rows, mapped) {
    const altitudes = series(rows, 'gps_altitude');
    const latitudes = series(rows, 'gps_latitude');
    const longitudes = series(rows, 'gps_longitude');
    const marker = {
      color: hasAny(altitudes) ? altitudes : COLOURS.a,
      size: mapped ? 6 : 5, colorscale: 'Viridis',
      showscale: hasAny(altitudes),
      colorbar: {title: {text: 'GPS altitude [m]', side: 'right'}}
    };
    const text = rows.map(row => row.trigger_time || row.capture_id || '');
    if (!mapped) {
      return [[{
        type: 'scatter', mode: 'markers', x: longitudes, y: latitudes, marker, text,
        hovertemplate: '%{text}<br>%{y:.5f}°, %{x:.5f}°<extra></extra>',
        name: 'Capture position'
      }], layout('Capture positions from image GPS',
        'Longitude [°E]', 'Latitude [°N]',
        {yaxis: {scaleanchor: 'x', scaleratio: 1}})];
    }
    const valid = latitudes.filter(value => value !== null);
    const centre = {
      lat: valid.length ? (Math.min(...valid) + Math.max(...valid)) / 2 : 0,
      lon: (() => {
        const values = longitudes.filter(value => value !== null);
        return values.length ? (Math.min(...values) + Math.max(...values)) / 2 : 0;
      })()
    };
    const spread = Math.max(
      valid.length ? Math.max(...valid) - Math.min(...valid) : 0.01, 0.01);
    return [[{
      type: 'scattermap', mode: 'markers', lat: latitudes, lon: longitudes,
      marker, text,
      hovertemplate: '%{text}<br>%{lat:.5f}°N, %{lon:.5f}°E<extra></extra>',
      name: 'Capture position'
    }], {
      title: {text: 'Capture positions from image GPS on OpenStreetMap',
        x: .5, y: .97, yanchor: 'top', font: {size: 17}},
      paper_bgcolor: '#f7fafc',
      font: {family: 'Arial, sans-serif', size: 13, color: '#172431'},
      margin: {l: 12, r: 58, t: 86, b: 24},
      map: {style: 'open-street-map', center: centre,
        zoom: Math.max(6, Math.min(13, Math.log2(180 / spread) - 1))},
      legend: {orientation: 'h', y: 1.06, x: 0, yanchor: 'bottom'},
      hovermode: 'closest',
      // An exported map has to stand on its own. Plotly draws no furniture on a
      // map subplot, so the orientation and the coordinate range are stated as
      // paper-anchored annotations: north is up because the projection is Web
      // Mercator and unrotated, and the corner coordinates tie the figure to
      // everything else in the campaign. The scale bar is the tile scale, which
      // OpenStreetMap prints in the tiles themselves.
      annotations: mapFurnitureAnnotations(latitudes, longitudes)
    }];
  }

  // North arrow and the covered latitude/longitude range, in paper coordinates
  // so they survive Plotly.toImage at any export size.
  function mapFurnitureAnnotations(latitudes, longitudes) {
    const lats = latitudes.filter(value => value !== null);
    const lons = longitudes.filter(value => value !== null);
    if (!lats.length || !lons.length) return [];
    const range = (values, positive, negative) => {
      const low = Math.min(...values), high = Math.max(...values);
      const mark = value =>
        `${Math.abs(value).toFixed(4)}°${value < 0 ? negative : positive}`;
      return `${mark(low)} – ${mark(high)}`;
    };
    const common = {xref: 'paper', yref: 'paper', showarrow: false,
      bgcolor: 'rgba(255,255,255,.82)', borderpad: 3};
    return [
      {...common, x: 0.012, y: 0.985, xanchor: 'left', yanchor: 'top',
        text: '▲<br><b>N</b>', align: 'center', font: {size: 15, color: '#07182a'}},
      {...common, x: 0.012, y: 0.012, xanchor: 'left', yanchor: 'bottom',
        text: `Lat ${range(lats, 'N', 'S')} · Lon ${range(lons, 'E', 'W')}`
          + '<br>WGS 84 · north up · OpenStreetMap basemap',
        align: 'left', font: {size: 11, color: '#07182a'}}
    ];
  }

  function renderTrack(captures) {
    const rows = (captures || []).filter(row =>
      finite(row.gps_latitude) !== null && finite(row.gps_longitude) !== null
      && Number(row.gps_latitude) !== 0 && Number(row.gps_longitude) !== 0);
    if (!rows.length) return noData('trackPlot', 'No capture carries a usable GPS position.');
    const [mapTraces, mapLayout] = trackTraceAndLayout(rows, true);
    Plotly.react('trackPlot', mapTraces, mapLayout, plotConfig).catch(() => {
      const [flat, flatLayout] = trackTraceAndLayout(rows, false);
      Plotly.react('trackPlot', flat, flatLayout, plotConfig);
    });
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
      layout('Altitude per capture','Capture time [UTC]','Altitude [m]'), plotConfig);
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
      layout('Downwelling light sensor irradiance','Capture time [UTC]','Irradiance [W/m²/nm]'),
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
      name:'ISO speed',yaxis:'y2',line:{color:COLOURS.b,width:1.3}});
    Plotly.react('exposurePlot', traces,
      layout('Exposure and gain per capture','Capture time [UTC]','Exposure time [s]',
        {yaxis2:{title:'ISO speed [–]',overlaying:'y',side:'right',gridcolor:'transparent',automargin:true}}),
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
    // Radians, not degrees. The DLS writes its attitude in radians and the
    // adapter stores every one of these fields raw, so that a value here and
    // the same value in the standalone MicaSense overview CSV are the same
    // number. The axis said "deg", which made a level sensor read as tilted.
    Plotly.react('orientationPlot', traces,
      layout('Light sensor attitude','Capture time [UTC]',
        'Angle [rad, as recorded]'), plotConfig);
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
      layout('Solar geometry as recorded by the light sensor','Capture time [UTC]','Elevation [rad, as recorded]',
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
      layout('Imager body temperature','Capture time [UTC]','Temperature [°C]'), plotConfig);
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
    }], layout('Bands delivered per capture','Bands in the capture [count]','Captures [count]',
      // Band counts are categories, not a continuous scale. Left numeric,
      // Plotly auto-ranged a single "6" across 5.6-6.4 and drew one bar the
      // full width of the panel.
      {xaxis:{type:'category'},bargap:.6}), plotConfig);
  }

  function renderPlots(data) {
    const captures = data.captures || [];
    const quality = data.capture_quality || {};
    renderTraceability(data.summary || {});
    renderIntegrity(captures);
    renderCadence(quality);
    renderSharpness(captures, quality);
    renderTrack(captures);
    renderAltitude(captures);
    renderIrradiance(captures);
    renderExposure(captures);
    renderOrientation(captures);
    renderSolar(captures);
    renderTemperature(captures);
    renderCompleteness(captures);
  }
  // ------------------------------------------------------------ figure export
  // Every panel, at the width a manuscript column expects and a type size that
  // is legible at it. The figure is specified in points and the browser is told
  // what pixel size that is, so nine points is what lands on paper rather than
  // whatever the panel happened to be on screen: a 13 px label in a 700 px wide
  // panel prints at 6.5 pt, which is why this does not simply snapshot the page.
  const FIGURE_WIDTH_INCHES = 7;
  const FIGURE_HEIGHT_INCHES = 4.4;
  const MINIMUM_FONT_POINTS = 9;
  const POINTS = {
    base: 9.5, title: 12, axisTitle: 10, tick: 9.5, legend: 9.5, colorbar: 9.5
  };
  // Margins in points, so they hold the type at any resolution.
  const MARGIN_POINTS = {l: 62, r: 54, t: 48, b: 46};

  const PANELS = [
    ['integrityPlot', 'capture_integrity'],
    ['cadencePlot', 'capture_cadence'],
    ['trackPlot', 'flight_track'],
    ['altitudePlot', 'altitude'],
    ['irradiancePlot', 'downwelling_irradiance'],
    ['exposurePlot', 'exposure_and_gain'],
    ['orientationPlot', 'light_sensor_attitude'],
    ['solarPlot', 'solar_geometry'],
    ['temperaturePlot', 'imager_temperature'],
    ['sharpnessPlot', 'panchromatic_quality'],
    ['completenessPlot', 'band_completeness']
  ];

  function scaledFont(font, points, perPoint) {
    return {...(font || {}), size: Math.round(points * perPoint)};
  }

  // A copy of the panel's own layout with every type size restated in points.
  // The panel on screen is never touched.
  function exportLayout(source, perPoint) {
    const layout = JSON.parse(JSON.stringify(source || {}));
    layout.width = Math.round(FIGURE_WIDTH_INCHES * 72 * perPoint);
    layout.height = Math.round(FIGURE_HEIGHT_INCHES * 72 * perPoint);
    layout.margin = {
      l: Math.round(MARGIN_POINTS.l * perPoint), r: Math.round(MARGIN_POINTS.r * perPoint),
      t: Math.round(MARGIN_POINTS.t * perPoint), b: Math.round(MARGIN_POINTS.b * perPoint),
      pad: 0
    };
    layout.paper_bgcolor = '#ffffff';
    if (layout.plot_bgcolor) layout.plot_bgcolor = '#ffffff';
    layout.font = scaledFont(layout.font, POINTS.base, perPoint);
    if (layout.title) {
      layout.title = {...layout.title,
        font: scaledFont(layout.title.font, POINTS.title, perPoint)};
    }
    if (layout.legend) {
      layout.legend = {...layout.legend,
        font: scaledFont(layout.legend.font, POINTS.legend, perPoint)};
    }
    Object.keys(layout).forEach(key => {
      if (!/^[xy]axis\d*$/.test(key) || !layout[key]) return;
      const axis = layout[key];
      const title = typeof axis.title === 'string' ? {text: axis.title} : (axis.title || {});
      layout[key] = {...axis,
        title: {...title, font: scaledFont(title.font, POINTS.axisTitle, perPoint)},
        tickfont: scaledFont(axis.tickfont, POINTS.tick, perPoint)};
    });
    return layout;
  }

  function exportTraces(source, perPoint) {
    return JSON.parse(JSON.stringify(source || [])).map(trace => {
      if (trace.marker && trace.marker.colorbar) {
        const bar = trace.marker.colorbar;
        const title = typeof bar.title === 'string' ? {text: bar.title} : (bar.title || {});
        trace.marker.colorbar = {...bar,
          title: {...title, font: scaledFont(title.font, POINTS.axisTitle, perPoint)},
          tickfont: scaledFont(bar.tickfont, POINTS.colorbar, perPoint)};
      }
      return trace;
    });
  }

  async function captureFigures(dpi) {
    const perPoint = dpi / 72;
    const captured = [];
    const skipped = [];
    for (const [id, name] of PANELS) {
      const node = $(id);
      // A panel that reported "no data" holds a message, not a figure.
      if (!node || !node.data || !node.data.length) { skipped.push(name); continue; }
      try {
        const image = await Plotly.toImage(
          {data: exportTraces(node.data, perPoint),
           layout: exportLayout(node.layout, perPoint)},
          {format: 'png',
           width: Math.round(FIGURE_WIDTH_INCHES * dpi),
           height: Math.round(FIGURE_HEIGHT_INCHES * dpi),
           scale: 1});
        captured.push({name, image});
      } catch (error) {
        skipped.push(name);
      }
    }
    return {captured, skipped};
  }

  async function exportFigures() {
    const button = $('exportFiguresBtn');
    const format = $('exportFormat').value;
    const dpi = Number($('exportDpi').value) || 300;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Preparing…';
    try {
      const {captured, skipped} = await captureFigures(dpi);
      if (!captured.length) throw new Error('No panel had a figure to export yet.');
      button.textContent = `Rendering ${captured.length}…`;
      const response = await fetch('/api/micasense/figures/export', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({format, dpi, figures: captured})
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.error || `Export failed (${response.status})`);
      }
      const blob = await response.blob();
      const name = (response.headers.get('Content-Disposition') || '')
        .match(/filename="([^"]+)"/);
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = name ? name[1] : `micasense_figures.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      $('statusText').textContent =
        `Exported ${captured.length} figure(s) at ${FIGURE_WIDTH_INCHES} inch wide, `
        + `${dpi} DPI, minimum ${MINIMUM_FONT_POINTS} pt`
        + (skipped.length ? ` · ${skipped.length} panel(s) had no figure` : '');
    } catch (error) {
      $('statusText').textContent = `Figure export failed: ${error.message}`;
      $('statusText').style.color = 'var(--danger)';
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
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
  $('exportFiguresBtn').onclick = exportFigures;
  load();
})();
