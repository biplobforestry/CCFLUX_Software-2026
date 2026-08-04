(() => {
  'use strict';
  let payload = null;
  let map = null;
  let layers = {};
  let bufferBox = null;
  let activeLayer = 'route';
  let activeStat = 'hist';
  let straightLegs = [];
  let selectedLegId = null;
  let legInfoControl = null;
  let viewMenuContext = { panel: 'map', path: '/noseboom/flight-route' };
  let renderToken = 0;
  let browserPoints = [];

  const byId = id => document.getElementById(id);
  const finite = value => Number.isFinite(Number(value));
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(() => resolve()));
  const points = () => browserPoints;
  const layerRoutes = { route: '/noseboom/flight-route', wind: '/noseboom/wind-speed', straight: '/noseboom/straight-flight' };
  const statRoutes = { hist: '/noseboom/statistics/histogram', freq: '/noseboom/statistics/frequency', alt: '/noseboom/statistics/altitude-profile', spectra: '/noseboom/statistics/wind-spectra' };
  const schemes = {
    turbo: [[48,18,59],[70,98,215],[53,171,248],[26,228,182],[162,252,60],[249,186,56],[233,75,53],[122,4,3]],
    viridis: [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]],
    plasma: [[13,8,135],[126,3,168],[204,71,120],[248,149,64],[240,249,33]],
    cividis: [[0,34,78],[40,75,112],[101,105,112],[160,137,91],[253,234,69]]
  };
  const settingInfo = {
    min_speed_mps: ['Minimum aircraft speed', 'm/s', 'Samples below this ground speed are excluded.', 8],
    max_turn_rate_dps: ['Maximum turn rate', '°/s', 'Limits rapid heading changes in candidate samples.', 1.5],
    max_roll_deg: ['Maximum absolute roll', '°', 'Limits aircraft bank angle.', 8],
    heading_window_s: ['Heading stability window', 's', 'Window used for heading-range stability.', 20],
    max_heading_range_deg: ['Maximum heading range', '°', 'Maximum heading variation inside the stability window.', 12],
    min_leg_seconds: ['Minimum leg duration', 's', 'Minimum accepted duration for one straight-flight leg.', 60],
    min_leg_distance_m: ['Minimum leg distance', 'm', 'Minimum accepted ground-track distance.', 1000],
    target_leg_distance_m: ['Target leg distance', 'm', 'Target distance used to divide long candidate runs.', 2000],
    max_leg_heading_drift_deg: ['Maximum leg heading drift', '°', 'Maximum unwrapped heading spread over a leg.', 20],
    max_cross_track_m: ['Maximum cross-track deviation', 'm', 'Maximum distance from the endpoint-defined reference line.', 80],
    max_altitude_deviation_m: ['Maximum altitude deviation', 'm', 'Maximum altitude departure from the leg mean.', 50]
  };
  const plotConfig = { responsive: true, scrollZoom: true, displayModeBar: true, displaylogo: false, doubleClick: 'reset', toImageButtonOptions: { format: 'png', scale: 2 } };
  const plotFont = { family: 'Times New Roman, Times, serif', size: 14, color: '#102534' };

  const api = async (url, options = {}) => {
    const response = await fetch(url, { cache: 'no-store', ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    return body;
  };
  async function log(message) { try { await api('/api/noseboom/log', { method: 'POST', body: JSON.stringify({ message }) }); } catch (_) {} }
  function escapeHtml(value) { return String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])); }
  function showBusy(title, detail, progress = null) {
    byId('busyTitle').textContent = title;
    byId('busyDetail').textContent = detail;
    const hasProgress = Number.isFinite(Number(progress));
    byId('busyProgressTrack').hidden = !hasProgress;
    byId('busyPercent').textContent = hasProgress ? `${Math.round(Number(progress))}%` : '';
    if (hasProgress) byId('busyProgress').style.width = `${Math.max(0, Math.min(100, Number(progress)))}%`;
    byId('busyModal').classList.add('show');
  }
  function hideBusy() { byId('busyModal').classList.remove('show'); }

  function viewFromPath(pathname = window.location.pathname) {
    const path = pathname.toLowerCase().replace(/\/+$/, '');
    if (path.endsWith('/wind-speed') || path.endsWith('/windspeed')) return { layer: 'wind' };
    if (path.endsWith('/straight-flight')) return { layer: 'straight' };
    if (path.endsWith('/flight-route') || path.endsWith('/flightroot')) return { layer: 'route' };
    if (path.endsWith('/statistics/frequency')) return { stat: 'freq' };
    if (path.endsWith('/statistics/altitude-profile')) return { stat: 'alt' };
    if (path.endsWith('/statistics/wind-spectra')) return { stat: 'spectra' };
    if (path.includes('/statistics/')) return { stat: 'hist' };
    return { layer: 'route' };
  }
  function updateViewUrl(path) { if (window.location.pathname !== path) window.history.pushState({ noseboomView: path }, '', path); }
  function activateLinkedView(event, callback) { if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); callback(); }

  function deriveStraightLegs(route) {
    if (Array.isArray(payload?.data?.straight_legs) && payload.data.straight_legs.length) {
      return payload.data.straight_legs.map(leg => ({ ...leg, points: (leg.coords || []).map(coord => ({ lat: coord[0], lon: coord[1] })) }));
    }
    const grouped = new Map();
    route.forEach(point => {
      const id = Number(point.straight_leg_id || 0);
      if (point.straight && id > 0) { if (!grouped.has(id)) grouped.set(id, []); grouped.get(id).push(point); }
    });
    return Array.from(grouped, ([id, legPoints]) => ({ id, points: legPoints, windSamples: legPoints.filter(point => finite(point.wind_mps) && (finite(point.wind_dir_deg) || (finite(point.wind_u_mps) && finite(point.wind_v_mps)))).map(point => ({ dir: finite(point.wind_dir_deg) ? Number(point.wind_dir_deg) : (Math.atan2(Number(point.wind_u_mps), Number(point.wind_v_mps)) * 180 / Math.PI + 360) % 360, spd: Number(point.wind_mps) })) }));
  }
  function windLimits(route) {
    const values = route.map(point => Number(point.wind_mps)).filter(Number.isFinite).sort((a, b) => a - b);
    if (!values.length) return [0, 1];
    const low = values[Math.floor((values.length - 1) * .02)];
    let high = values[Math.ceil((values.length - 1) * .98)];
    if (high <= low) high = low + 1;
    return [low, high];
  }
  function windColor(value, low, high) {
    if (!Number.isFinite(value)) return '#64748b';
    const stops = schemes[byId('colorScheme').value] || schemes.turbo;
    const position = Math.max(0, Math.min(1, (value - low) / Math.max(high - low, 1e-9))) * (stops.length - 1);
    const a = Math.floor(position), b = Math.min(stops.length - 1, a + 1), mix = position - a;
    const rgb = stops[a].map((channel, index) => Math.round(channel * (1 - mix) + stops[b][index] * mix));
    return `rgb(${rgb.join(',')})`;
  }
  function gradientCss() { const stops = schemes[byId('colorScheme').value] || schemes.turbo; return `linear-gradient(90deg,${stops.map(rgb => `rgb(${rgb.join(',')})`).join(',')})`; }
  function routeBounds(route, bufferMetres = 0) {
    const lats = route.map(point => Number(point.lat)), lons = route.map(point => Number(point.lon));
    let south = Math.min(...lats), north = Math.max(...lats), west = Math.min(...lons), east = Math.max(...lons);
    const middle = (south + north) / 2, dLat = bufferMetres / 111320, dLon = bufferMetres / (111320 * Math.max(.15, Math.cos(middle * Math.PI / 180)));
    if (south === north) { south -= .0005; north += .0005; } if (west === east) { west -= .0005; east += .0005; }
    return [[south - dLat, west - dLon], [north + dLat, east + dLon]];
  }
  function ensureMap() {
    if (map) return true;
    if (!window.L) { showMapError('Street-map library could not be loaded. Check the network connection and press Refresh.'); return false; }
    map = L.map('map', { zoomControl: true, preferCanvas: true }).setView([47.64, 9.38], 10);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
    L.control.scale({ metric: true, imperial: false }).addTo(map);
    return true;
  }
  function showMapError(message) { byId('mapMessage').textContent = message; byId('mapMessage').classList.add('error'); byId('mapMessage').hidden = false; }

  function windRoseSvg(samples) {
    const vals = (samples || []).filter(item => finite(item.dir) && finite(item.spd)).map(item => ({ dir: Number(item.dir), spd: Number(item.spd) }));
    if (!vals.length) return '<div style="padding:12px;text-align:center;color:#566b78">No valid wind data are available for this leg.</div>';
    const dirs = 16, bins = 6, cx = 112, cy = 112, radius = 82, inner = 15;
    const speeds = vals.map(item => item.spd).sort((a,b) => a-b); let low = speeds[0], high = speeds.at(-1); if (high <= low) high = low + 1;
    const edges = Array.from({length: bins + 1}, (_, index) => low + (high-low)*index/bins);
    const counts = Array.from({length: dirs}, () => Array(bins).fill(0));
    vals.forEach(item => { const speedBin = Math.min(bins-1, Math.max(0, Math.floor((item.spd-low)/(high-low)*bins))); const directionBin = Math.floor(((item.dir+360/dirs/2)%360)/(360/dirs))%dirs; counts[directionBin][speedBin] += 1; });
    const totals = counts.map(row => row.reduce((a,b) => a+b, 0)); const maxPct = Math.max(1, ...totals.map(count => 100*count/vals.length)); const maxRing = Math.ceil(maxPct/2)*2;
    const rings = [.25,.5,.75,1].map(fraction => { const r=inner+(radius-inner)*fraction; return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#b9c8cf"/><text x="${cx+4}" y="${cy-r+10}" font-size="8" fill="#526b77">${(maxRing*fraction).toFixed(0)}%</text>`; }).join('');
    const axes = [0,90,180,270].map(angle => { const rad=(angle-90)*Math.PI/180; return `<line x1="${cx}" y1="${cy}" x2="${cx+radius*Math.cos(rad)}" y2="${cy+radius*Math.sin(rad)}" stroke="#9aaeb8"/>`; }).join('');
    function arc(r0,r1,a0,a1) { const p=Math.PI/180,sa=(a0-90)*p,ea=(a1-90)*p,large=(a1-a0)>180?1:0; return `M${cx+r0*Math.cos(sa)},${cy+r0*Math.sin(sa)} L${cx+r1*Math.cos(sa)},${cy+r1*Math.sin(sa)} A${r1},${r1} 0 ${large} 1 ${cx+r1*Math.cos(ea)},${cy+r1*Math.sin(ea)} L${cx+r0*Math.cos(ea)},${cy+r0*Math.sin(ea)} A${r0},${r0} 0 ${large} 0 ${cx+r0*Math.cos(sa)},${cy+r0*Math.sin(sa)} Z`; }
    let sectors=''; for(let di=0;di<dirs;di+=1){ if(!totals[di]) continue; let r0=inner; for(let bi=0;bi<bins;bi+=1){ if(!counts[di][bi]) continue; const r1=Math.min(radius,r0+(radius-inner)*(100*counts[di][bi]/vals.length)/maxRing); sectors += `<path d="${arc(r0,r1,di*360/dirs-360/dirs/2+2,(di+1)*360/dirs-360/dirs/2-2)}" fill="${windColor((edges[bi]+edges[bi+1])/2,low,high)}" stroke="#334" stroke-width=".5"/>`; r0=r1; } }
    let bar='',legend=''; for(let index=0;index<48;index+=1){ const fraction=1-index/47,y=35+index*3; bar += `<rect x="248" y="${y}" width="12" height="3" fill="${windColor(low+fraction*(high-low),low,high)}"/>`; } for(let index=0;index<=bins;index+=1){ const y=35+(1-index/bins)*144; legend += `<line x1="246" x2="263" y1="${y}" y2="${y}" stroke="#334"/><text x="268" y="${y+3}" font-size="9">${edges[index].toFixed(1)}</text>`; }
    return `<svg viewBox="0 0 305 235" role="img" aria-label="Wind Rose"><text x="248" y="20" font-size="11" font-weight="600">Wind speed</text><text x="248" y="32" font-size="10">m/s</text>${rings}${axes}${sectors}<text x="${cx}" y="14" text-anchor="middle" font-size="13" font-weight="700">N</text><text x="${cx}" y="222" text-anchor="middle" font-size="13" font-weight="700">S</text><text x="8" y="116" font-size="13" font-weight="700">W</text><text x="208" y="116" font-size="13" font-weight="700">E</text><circle cx="${cx}" cy="${cy}" r="${inner}" fill="#cfe8f3" stroke="#555"/>${bar}${legend}</svg>`;
  }
  function hideLegInfo() { if (legInfoControl && map) map.removeControl(legInfoControl); legInfoControl = null; selectedLegId = null; }
  function showLegInfo(leg) {
    if (selectedLegId === leg.id) { hideLegInfo(); return; }
    selectedLegId = leg.id;
    if (!legInfoControl) { legInfoControl = L.control({position:'topright'}); legInfoControl.onAdd = () => L.DomUtil.create('div','leg-info'); legInfoControl.addTo(map); }
    const element = legInfoControl.getContainer(); L.DomEvent.disableClickPropagation(element);
    const display = value => finite(value) ? Number(value).toFixed(2) : 'n/a';
    element.innerHTML = `<h3>Straight Flight leg ${escapeHtml(leg.id)}</h3><table><tr><td>Length</td><td>${display(leg.distance_km)} km</td></tr><tr><td>Duration</td><td>${display(leg.duration_s)} s</td></tr><tr><td>Mean aircraft speed</td><td>${display(leg.mean_speed_mps)} m/s</td></tr><tr><td>Mean wind</td><td>${display(leg.mean_wind_mps)} m/s</td></tr><tr><td>Mean heading</td><td>${display(leg.mean_heading_deg)}°</td></tr><tr><td>Heading drift</td><td>${display(leg.heading_drift_deg)}°</td></tr><tr><td>Maximum cross-track</td><td>${display(leg.max_cross_track_m)} m</td></tr></table>${windRoseSvg(leg.windSamples)}<small>Click this leg again to hide the Wind Rose.</small>`;
  }
  async function makeLayers(route) {
    const width = Math.max(1, Math.min(20, Number(byId('lineWidthInput').value) || 5));
    Object.values(layers).forEach(layer => { try { layer.removeFrom(map); } catch (_) {} }); layers = {}; hideLegInfo();
    const coords = route.map(point => [Number(point.lat), Number(point.lon)]), common = { weight: width, opacity: .96, lineJoin: 'round', lineCap: 'round' };
    layers.route = L.layerGroup([L.polyline(coords, { ...common, color: '#d62828' }).bindTooltip('Measured Noseboom flight route'), L.circleMarker(coords[0], { radius: 7, color: '#07111f', weight: 2, fillColor: '#42d99b', fillOpacity: 1 }).bindTooltip('Takeoff'), L.circleMarker(coords.at(-1), { radius: 7, color: '#07111f', weight: 2, fillColor: '#ff5d73', fillOpacity: 1 }).bindTooltip('Landing')]);
    const [low, high] = windLimits(route), windSegments = [];
    for (let index=0; index<route.length-1; index+=1) {
      const values=[route[index].wind_mps,route[index+1].wind_mps].map(Number).filter(Number.isFinite), wind=values.length?values.reduce((a,b)=>a+b,0)/values.length:NaN;
      windSegments.push(L.polyline([coords[index],coords[index+1]],{...common,color:windColor(wind,low,high)}).bindTooltip(Number.isFinite(wind)?`Wind speed: ${wind.toFixed(2)} m/s`:'Wind speed unavailable',{sticky:true,opacity:.98}));
      if (index && index % 500 === 0) { showBusy('Preparing Noseboom map', `Rendering wind segment ${index.toLocaleString()} of ${(route.length-1).toLocaleString()}`, 8 + 36*index/route.length); await nextFrame(); }
    }
    layers.wind=L.layerGroup(windSegments); straightLegs=deriveStraightLegs(route);
    const straightItems=[L.polyline(coords,{...common,weight:Math.max(2,width-1),color:'#101820',dashArray:'5 9',opacity:.65}).bindTooltip('Measured flight-track reference')];
    straightLegs.forEach(leg => { const legCoords=(leg.coords||leg.points.map(point=>[Number(point.lat),Number(point.lon)])); if(legCoords.length<2)return; const midpoint=leg.label||legCoords[Math.floor(legCoords.length/2)]; const onClick=()=>showLegInfo(leg); straightItems.push(L.polyline(legCoords,{...common,weight:width+3,color:'#ff8b24'}).bindTooltip(`Straight Flight leg ${leg.id}`).on('click',onClick)); straightItems.push(L.marker(midpoint,{icon:L.divIcon({className:'',html:`<div class="leg-label">${leg.id}</div>`,iconSize:[28,28],iconAnchor:[14,14]})}).bindTooltip(`Straight Flight leg ${leg.id}`).on('click',onClick)); });
    layers.straight=L.layerGroup(straightItems); byId('windMin').textContent=low.toFixed(2); byId('windMax').textContent=high.toFixed(2); byId('legendGradient').style.background=gradientCss();
  }
  function drawBuffer(route) { if(bufferBox)bufferBox.removeFrom(map); const buffer=Math.max(0,Number(byId('bufferInput').value)||0); bufferBox=L.rectangle(routeBounds(route,buffer),{color:'#f6b84b',weight:1,dashArray:'6 6',fillOpacity:.025,interactive:false}).addTo(map); }
  function showLayer(name, shouldLog=true, shouldUpdateUrl=true) { if(!map||!layers[name])return; Object.values(layers).forEach(layer=>{try{layer.removeFrom(map)}catch(_){}}); layers[name].addTo(map); activeLayer=name; if(name!=='straight')hideLegInfo(); ['route','wind','straight'].forEach(key=>byId(`${key}Btn`).classList.toggle('active',key===name)); byId('windLegend').hidden=name!=='wind'; const counts={route:`${points().length.toLocaleString()} route samples`,wind:`${Math.max(0,points().length-1).toLocaleString()} wind-speed segments`,straight:`${straightLegs.length.toLocaleString()} accepted straight-flight legs`}; byId('mapInfo').textContent=counts[name]; drawBuffer(points()); if(shouldUpdateUrl)updateViewUrl(layerRoutes[name]); if(shouldLog)log(`Noseboom map layer selected: ${name}`); }
  function resetMap(shouldLog=true) { const route=points(); if(!map||!route.length)return; const buffer=Math.max(0,Number(byId('bufferInput').value)||0); map.fitBounds(routeBounds(route,buffer),{padding:[20,20]}); drawBuffer(route); if(shouldLog)log(`Noseboom map reset with ${buffer} m buffer`); }

  function baseLayout(title, xTitle, yTitle) { return { title:{text:title,font:{size:18}},font:plotFont,margin:{l:72,r:26,t:52,b:62},paper_bgcolor:'#ffffff',plot_bgcolor:'#ffffff',xaxis:{title:xTitle,showgrid:true,gridcolor:'#dfe8ec',zerolinecolor:'#9aadb7',automargin:true},yaxis:{title:yTitle,showgrid:true,gridcolor:'#dfe8ec',zerolinecolor:'#9aadb7',automargin:true},legend:{orientation:'h',x:.01,y:1.12,bgcolor:'rgba(255,255,255,.86)'} }; }
  function fullLogTickLabels(values) {
    const positive=(values||[]).map(Number).filter(value=>Number.isFinite(value)&&value>0);
    if(!positive.length)return {tickmode:'auto'};
    const low=Math.min(...positive),high=Math.max(...positive),tickvals=[];
    for(let exponent=Math.floor(Math.log10(low))-1;exponent<=Math.ceil(Math.log10(high))+1;exponent+=1){
      [1,2,5].forEach(multiplier=>{const value=multiplier*(10**exponent);if(value>=low*.999&&value<=high*1.001)tickvals.push(value);});
    }
    const ticktext=tickvals.map(value=>{
      if(value>=1)return value.toLocaleString('en-US',{useGrouping:false,maximumFractionDigits:6});
      const digits=Math.min(10,Math.max(1,Math.ceil(-Math.log10(value))+1));
      return value.toFixed(digits).replace(/0+$/,'').replace(/\.$/,'');
    });
    return {tickmode:'array',tickvals,ticktext};
  }
  function percentile(values, percent) { const sorted=values.filter(Number.isFinite).sort((a,b)=>a-b); if(!sorted.length)return null; const position=(sorted.length-1)*percent/100, lower=Math.floor(position), upper=Math.ceil(position); return sorted[lower]+(sorted[upper]-sorted[lower])*(position-lower); }
  function frequencyDistribution(values, bins=48) { const clean=values.filter(Number.isFinite); if(clean.length<2)return {centers:[],curve:[],start:0,end:1,size:1}; let start=Math.min(...clean),end=Math.max(...clean); if(start===end){start-=.5;end+=.5;} const size=(end-start)/bins,counts=Array(bins).fill(0); clean.forEach(value=>{const index=Math.min(bins-1,Math.max(0,Math.floor((value-start)/size)));counts[index]+=1;}); const sigma=1.35,radius=4,kernel=[]; for(let offset=-radius;offset<=radius;offset+=1)kernel.push(Math.exp(-.5*(offset/sigma)**2)); const scale=kernel.reduce((a,b)=>a+b,0); const curve=counts.map((_,index)=>kernel.reduce((sum,weight,k)=>sum+weight*(counts[index+k-radius]||0),0)/scale); return {centers:counts.map((_,index)=>start+(index+.5)*size),curve,start,end,size}; }  async function renderStats(kind='hist', shouldUpdateUrl=true, manageBusy=true) {
    if(!payload?.data?.available)return; const token=++renderToken; activeStat=kind; document.querySelectorAll('[data-stat]').forEach(button=>button.classList.toggle('active',button.dataset.stat===kind)); if(shouldUpdateUrl)updateViewUrl(statRoutes[kind]); if(manageBusy)showBusy('Preparing scientific plots','Rendering the selected Noseboom statistical view.'); await nextFrame();
    try {
      if(!window.Plotly)throw new Error('The scientific plotting library is unavailable'); const view=byId('statsView'); Plotly.purge(view); view.innerHTML=''; view.className='chart';
      if(kind==='hist'){
        view.classList.add('histogram-grid'); const definitions=[['wind_mps','Wind speed','Wind speed [m/s]','#2a9d8f','#0b5d56'],['wind_u_mps','Wind u component','u [m/s]','#457b9d','#173f5f'],['wind_v_mps','Wind v component','v [m/s]','#7b2cbf','#4a148c'],['wind_w_mps','Vertical wind component','w [m/s]','#e76f51','#9d291c'],['air_temp_degC','Air temperature','Air temperature [°C]','#f4a261','#b45f06'],['rel_humidity_pct','Relative humidity','Relative humidity [%]','#43aa8b','#1b6b54']];
        for(let index=0;index<definitions.length;index+=1){ if(token!==renderToken)return; const [key,title,xTitle,barColor,lineColor]=definitions[index],element=document.createElement('div'); element.className='histogram-plot'; view.appendChild(element); const values=(payload.data.hist?.[key]||points().map(point=>point[key])).map(Number).filter(Number.isFinite); const distribution=frequencyDistribution(values,48),layout=baseLayout(title,xTitle,'Frequency [count]'); layout.bargap=.035; layout.legend={orientation:'h',x:.02,y:1.12,font:{size:11},bgcolor:'rgba(255,255,255,.82)'}; const traces=[{x:values,type:'histogram',name:'Observed frequency',xbins:{start:distribution.start,end:distribution.end,size:distribution.size},marker:{color:barColor,line:{color:'#263238',width:.55}},opacity:.78,hovertemplate:`${xTitle}: %{x:.3g}<br>Count: %{y}<extra></extra>`},{x:distribution.centers,y:distribution.curve,type:'scatter',mode:'lines',name:'Frequency distribution curve',line:{color:lineColor,width:2.6,shape:'spline'},hovertemplate:`${xTitle}: %{x:.3g}<br>Smoothed count: %{y:.2f}<extra></extra>`}]; await Plotly.react(element,traces,layout,plotConfig); showBusy('Preparing scientific plots',`Rendered histogram ${index+1} of 6`,15+75*(index+1)/6); await nextFrame(); }
      } else if(kind==='freq'){
        const element=document.createElement('div'); element.className='single-plot'; view.appendChild(element); const rows=payload.data.frequency||[], x=rows.map((row,index)=>row.time||index+1), y=rows.map(row=>Number(row.frequency_hz)), min=rows.map(row=>Number(row.frequency_min_hz)), max=rows.map(row=>Number(row.frequency_max_hz)); const traces=[{x,y,mode:'lines+markers',name:'Mean frequency',line:{color:'#1565c0',width:1.5},marker:{size:4}}]; if(min.some(Number.isFinite))traces.push({x,y:min,mode:'lines',name:'Minimum frequency',line:{color:'#55a868',width:1,dash:'dot'}}); if(max.some(Number.isFinite))traces.push({x,y:max,mode:'lines',name:'Maximum frequency',line:{color:'#c44e52',width:1,dash:'dot'}}); await Plotly.react(element,traces,baseLayout('Acquisition frequency time series','Time / one-second frequency bin','Frequency [Hz]'),plotConfig);
      } else if(kind==='alt'){
        const element=document.createElement('div'); element.className='single-plot'; view.appendChild(element); const rows=(payload.data.altitude_profile||points().map(point=>({time:point.time,gnss_msl_m:point.altitude_m,ins_ellipsoid_m:point.height_m,dtm_m:point.terrain_m}))),x=rows.map((row,index)=>row.time||index),gnss=rows.map(row=>finite(row.gnss_msl_m)?Number(row.gnss_msl_m):null),ins=rows.map(row=>finite(row.ins_ellipsoid_m)?Number(row.ins_ellipsoid_m):null),dtm=rows.map(row=>finite(row.dtm_m)?Number(row.dtm_m):null),terrainValues=dtm.filter(Number.isFinite),flightValues=gnss.concat(ins).filter(Number.isFinite),terrainLow=percentile(terrainValues,2),terrainHigh=percentile(terrainValues,98),flightLow=percentile(flightValues,2),flightHigh=percentile(flightValues,98),floor=terrainLow===null?null:Math.max(0,terrainLow-20),limits=[floor,flightLow].filter(Number.isFinite),tops=[terrainHigh,flightHigh].filter(Number.isFinite),yMin=limits.length?Math.min(...limits)-20:undefined,yMax=tops.length?Math.max(...tops)+30:undefined,traces=[]; if(terrainValues.length){traces.push({x,y:dtm.map(value=>Number.isFinite(value)?floor:null),name:'DTM display floor',mode:'lines',line:{color:'rgba(0,0,0,0)',width:0},showlegend:false,hoverinfo:'skip'});traces.push({x,y:dtm,name:'Satellite DTM terrain',mode:'lines',fill:'tonexty',fillcolor:'rgba(88,129,87,.28)',line:{color:'#386641',width:1.6}});} if(gnss.some(Number.isFinite))traces.push({x,y:gnss,name:'Flight altitude (GNSS MSL)',mode:'lines',line:{color:'#d62828',width:2.2}}); if(ins.some(Number.isFinite))traces.push({x,y:ins,name:'INS ellipsoid height',mode:'lines',line:{color:'#5e3c99',width:2.1}}); const layout=baseLayout('Altitude profile with satellite DTM','Time','Height [m]'); if(Number.isFinite(yMin)&&Number.isFinite(yMax)&&yMax>yMin)layout.yaxis.range=[yMin,yMax]; await Plotly.react(element,traces,layout,plotConfig);
      } else {
        const element=document.createElement('div'); element.className='single-plot'; view.appendChild(element);
        const spectra=payload.data.spectra||{},total=spectra.wind_mps,vertical=spectra.wind_w_mps,traces=[];
        if(total?.frequency_hz?.length)traces.push({x:total.frequency_hz,y:total.psd,mode:'lines',name:'Total wind speed',line:{color:'#111',width:2.2},hovertemplate:'Frequency: %{x:.8f} Hz<br>PSD: %{y:.10f}<extra>Total wind speed</extra>'});
        if(vertical?.frequency_hz?.length)traces.push({x:vertical.frequency_hz,y:vertical.psd,mode:'lines',name:'Vertical wind component',line:{color:'#2c7fb8',width:2},hovertemplate:'Frequency: %{x:.8f} Hz<br>PSD: %{y:.10f}<extra>Vertical wind component</extra>'});
        const reference=total||vertical;
        if(reference?.frequency_hz?.length){
          const index=Math.max(0,reference.frequency_hz.findIndex(value=>Number(value)>=.02)),f0=Number(reference.frequency_hz[index]),p0=Number(reference.psd[index]),end=Math.max(f0,Math.max(...reference.frequency_hz.map(Number).filter(Number.isFinite))),fx=[];
          for(let f=Math.max(.01,f0/2);f<=end*1.001;f*=1.035)fx.push(f);
          traces.push({x:fx,y:fx.map(f=>p0*Math.pow(f/f0,-5/3)),mode:'lines',name:'f<sup>−5/3</sup> reference',line:{color:'#777',width:2,dash:'dash'},hoverinfo:'skip'});
        }
        const frequencyValues=traces.flatMap(trace=>trace.x||[]),powerValues=traces.flatMap(trace=>trace.y||[]);
        const layout=baseLayout('Noseboom wind power spectrum','Frequency [Hz]','Power spectral density [(m s<sup>−1</sup>)<sup>2</sup> Hz<sup>−1</sup>]');
        layout.title.x=.34; layout.margin={l:150,r:32,t:58,b:105};
        layout.legend={x:.68,y:.98,xanchor:'left',yanchor:'top',orientation:'v',bgcolor:'rgba(255,255,255,.88)',bordercolor:'#8799a3',borderwidth:1};
        layout.xaxis={...layout.xaxis,type:'log',title:{text:'Frequency [Hz]',standoff:22},...fullLogTickLabels(frequencyValues)};
        layout.yaxis={...layout.yaxis,type:'log',title:{text:'Power spectral density [(m s<sup>−1</sup>)<sup>2</sup> Hz<sup>−1</sup>]',standoff:24},...fullLogTickLabels(powerValues)};
        await Plotly.react(element,traces,layout,plotConfig);
      }
      log(`Noseboom statistics view rendered: ${kind}`);
    } catch(error){byId('statsView').innerHTML=`<div class="chart-note">${escapeHtml(error.message)}</div>`;log(`Noseboom statistics rendering failed: ${error.message}`);} finally {if(manageBusy&&token===renderToken)hideBusy();}
  }

  function showViewMenu(event,panel,path,straight=false){event.preventDefault();viewMenuContext={panel,path};const menu=byId('viewMenu');byId('menuCurrentSettings').hidden=!straight;byId('menuChangeSettings').hidden=!straight;menu.style.left=`${Math.min(event.clientX,window.innerWidth-270)}px`;menu.style.top=`${Math.min(event.clientY,window.innerHeight-190)}px`;menu.classList.add('show');}
  function closeViewMenu(){byId('viewMenu').classList.remove('show');}
  async function openFullscreen(panelName){closeViewMenu();const target=panelName==='map'?byId('mapCard'):byId('statsCard');if(!document.fullscreenElement)await target.requestFullscreen();else await document.exitFullscreen();setTimeout(()=>{if(map)map.invalidateSize(false);document.querySelectorAll('#statsView .js-plotly-plot').forEach(plot=>Plotly.Plots.resize(plot));},180);}
  function showSettings(editable){const settings=payload?.data?.straight_settings||{},body=byId('settingsBody');body.innerHTML=`<p>These thresholds are applied to the preserved 1 Hz straight-flight classifier. Recalculation first creates a temporary visualization; you decide afterward whether it is saved in the Flight Project.</p><div class="settings-grid">${Object.entries(settingInfo).map(([key,[name,unit,help,defaultValue]])=>`<div class="setting"><label>${escapeHtml(name)} [${escapeHtml(unit)}]</label><input data-setting="${key}" type="number" step="any" value="${escapeHtml(settings[key]??defaultValue)}" ${editable?'':'disabled'} /><small>${escapeHtml(help)}</small></div>`).join('')}</div>`;byId('settingsActions').innerHTML=editable?'<button class="btn" id="settingsCancel">Cancel</button><button class="btn" id="settingsReset">Reset settings</button><button class="btn primary" id="settingsSave">Save settings and Proceed</button>':'';byId('settingsModal').classList.add('show');if(editable){byId('settingsCancel').onclick=closeSettings;byId('settingsReset').onclick=resetSettings;byId('settingsSave').onclick=saveSettingsAndProceed;}}
  function closeSettings(){byId('settingsModal').classList.remove('show');}
  function readSettings(){const settings={};for(const input of document.querySelectorAll('[data-setting]')){const value=Number(input.value);if(!Number.isFinite(value)||value<=0)throw new Error(`${settingInfo[input.dataset.setting][0]} must be greater than zero`);settings[input.dataset.setting]=value;}return settings;}
  function defaultSettings(){return Object.fromEntries(Object.entries(settingInfo).map(([key,value])=>[key,value[3]]));}
  async function applyStraightSettings(settings, reset=false) {
    closeSettings();
    const title = reset ? 'Resetting straight-flight settings' : 'Recalculating Straight Flight';
    const initial = reset
      ? 'Restoring the validated default parameters and recalculating all flight legs.'
      : 'Applying the selected parameters to the original Noseboom interval.';
    showBusy(title, initial, 0);
    try {
      await api('/api/noseboom/straight-settings', {
        method: 'POST',
        body: JSON.stringify({ action: 'preview', settings })
      });
      let status = null;
      while (true) {
        await new Promise(resolve => setTimeout(resolve, 400));
        status = await api('/api/noseboom/straight-settings/progress');
        const elapsed = Math.max(0, Number(status.elapsed_seconds) || 0);
        showBusy(
          title,
          `${status.message || 'Processing Noseboom data.'} Elapsed: ${elapsed.toFixed(1)} s`,
          status.progress
        );
        if (status.error) throw new Error(status.error);
        if (status.ready) break;
        if (!status.running) throw new Error('Straight-flight recalculation stopped before completion.');
      }
      const result = status.result;
      if (!result?.data) throw new Error('Straight-flight recalculation returned no visualization data.');
      payload.data = result.data;
      browserPoints = (result.data.points || []).filter(point => finite(point.lat) && finite(point.lon));
      showBusy(title, 'Rendering recalculated flight legs on the map.', 100);
      await makeLayers(browserPoints);
      showLayer('straight', false, true);
      hideBusy();
      const save = window.confirm(`${reset ? 'Default' : 'Recalculated'} straight-flight settings are ready. Do you want to save these settings and legs in the Flight Project?`);
      if (save) {
        showBusy('Saving Flight Project', 'Saving the current straight-flight settings and recalculated legs.');
        const saved = await api('/api/noseboom/straight-settings', {
          method: 'POST', body: JSON.stringify({ action: 'save-preview' })
        });
        payload.data = saved.data;
        log('Straight-flight settings and recalculated legs saved in the Flight Project');
      } else {
        log('Straight-flight settings kept for temporary visualization only');
      }
    } catch (error) {
      hideBusy();
      window.alert(error.message);
      log(`Straight-flight recalculation failed: ${error.message}`);
    } finally {
      hideBusy();
    }
  }
  async function saveSettingsAndProceed(){try{await applyStraightSettings(readSettings(),false);}catch(error){window.alert(error.message);}}
  async function resetSettings(){await applyStraightSettings(defaultSettings(),true);}

  function openDownload(){syncDownloadOptions();byId('downloadModal').classList.add('show');}
  function closeDownload(){byId('downloadModal').classList.remove('show');}
  // Original resolution writes the recorded rows, which only the full variable
  // set offers; the limited set is a resampled table by definition.
  function syncDownloadOptions(){
    // A project opened away from the acquisition machine carries a 10 Hz
    // table rather than the raw CSV, so only what it can serve is offered
    // and the rest names who to ask.
    const download=payload?.download||{};
    const fromProject=download.source==='project';
    const ceiling=Number(download.maximum_frequency_hz);
    const variables=byId('downloadVariables').value;
    const fullOption=byId('downloadVariables').querySelector('option[value="full"]');
    fullOption.disabled=fromProject;
    if(fromProject&&variables==='full')byId('downloadVariables').value='limited';
    const full=byId('downloadVariables').value==='full';
    const frequency=byId('downloadFrequency');
    frequency.querySelectorAll('option').forEach(option=>{
      if(option.value==='original'){option.disabled=!full||fromProject;return;}
      option.disabled=fromProject&&Number.isFinite(ceiling)&&Number(option.value)>ceiling;
    });
    const chosen=frequency.querySelector(`option[value="${frequency.value}"]`);
    if(!chosen||chosen.disabled)frequency.value='1';
    byId('downloadVariablesNote').textContent=fromProject
      ?`This project carries the Noseboom data at ${ceiling} Hz. Any frequency `
        +`from 1 to ${ceiling} Hz is available. For the full variable set or a `
        +`higher resolution, please contact ${download.custodians||'the data custodians'}.`
      :full
        ?'Full writes every recorded column, plus EVENT and Flight ID. CSV or text only.'
        :'Limited writes the 14 columns shown on this page.';
  }

  let downloadPolling=false;
  function showDownloadProgress(){
    byId('downloadProgressStep').textContent='Starting…';
    byId('downloadProgressPercent').textContent='0%';
    byId('downloadProgressPercent').classList.remove('done');
    byId('downloadProgressFill').style.width='0%';
    byId('downloadProgressDetail').textContent='';
    byId('downloadProgressClose').hidden=true;
    byId('downloadProgressModal').classList.add('show');
  }
  function setDownloadProgress(percent,step){
    const value=Math.max(0,Math.min(100,Number(percent)||0));
    byId('downloadProgressFill').style.width=`${value}%`;
    byId('downloadProgressPercent').textContent=`${value.toFixed(0)}%`;
    if(step)byId('downloadProgressStep').textContent=step;
  }
  async function pollDownloadProgress(){
    // The download request is still streaming on another connection; this reads
    // the state it publishes as it writes.
    while(downloadPolling){
      await new Promise(resolve=>setTimeout(resolve,300));
      if(!downloadPolling)return;
      try{
        const state=await api('/api/noseboom/data-export/progress');
        setDownloadProgress(state.percent,state.step);
      }catch(_){/* a missed poll must not stop the download */}
    }
  }

  async function startDataDownload(){
    const variables=byId('downloadVariables').value;
    const raw=byId('downloadFrequency').value;
    const frequency_hz=raw==='original'?'original':Number(raw);
    const format=byId('downloadFormat').value;
    closeDownload();
    showDownloadProgress();
    downloadPolling=true;
    pollDownloadProgress();
    try{
      const response=await fetch('/api/noseboom/data-export',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({variables,frequency_hz,format})
      });
      if(!response.ok){
        let message=`Download failed (${response.status})`;
        try{message=(await response.json()).error||message;}catch(_){}
        throw new Error(message);
      }
      const blob=await response.blob();
      const disposition=response.headers.get('Content-Disposition')||'';
      const match=disposition.match(/filename="?([^";]+)"?/i);
      const label=raw==='original'?'original':`${frequency_hz}Hz`;
      const name=match?match[1]:`noseboom_${variables}_${label}.${format}`;
      downloadPolling=false;
      const url=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=url;link.download=name;document.body.appendChild(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
      let state={};
      try{state=await api('/api/noseboom/data-export/progress');}catch(_){}
      setDownloadProgress(100,'Download complete');
      byId('downloadProgressPercent').classList.add('done');
      byId('downloadProgressDetail').textContent=state.rows
        ? `${Number(state.rows).toLocaleString()} rows · ${state.columns} columns · ${name}`
        : name;
      byId('downloadProgressClose').hidden=false;
      log(`Noseboom data downloaded: ${name}`);
    }catch(error){
      downloadPolling=false;
      byId('downloadProgressStep').textContent=`Download failed: ${error.message}`;
      byId('downloadProgressDetail').textContent='';
      byId('downloadProgressClose').hidden=false;
      log(`Noseboom data download failed: ${error.message}`);
    }finally{
      downloadPolling=false;
    }
  }

  function openExport(){byId('exportResults').innerHTML='';byId('exportModal').classList.add('show');}
  function closeExport(){byId('exportModal').classList.remove('show');}
  async function startStatisticsExport(){const formats=Array.from(document.querySelectorAll('[name=exportFormat]:checked')).map(input=>input.value);if(!formats.length){alert('Select at least one export format.');return;}const dpi=Number(byId('exportDpi').value);closeExport();showBusy('Exporting publication figures','Starting the background renderer.',1);try{await api('/api/noseboom/statistics/export',{method:'POST',body:JSON.stringify({formats,dpi})});await pollStatisticsExport();}catch(error){hideBusy();alert(error.message);log(`Noseboom statistics export failed to start: ${error.message}`);}}
  async function pollStatisticsExport(){for(;;){await new Promise(resolve=>setTimeout(resolve,350));const state=await api('/api/noseboom/statistics/export/progress');showBusy('Exporting publication figures',state.step||'Rendering figures',state.progress||0);if(state.status==='running')continue;if(state.status==='failed'){hideBusy();throw new Error(state.error||'Publication export failed');}hideBusy();byId('exportResults').innerHTML=`<h3>Export complete</h3>${(state.files||[]).map(file=>`<a href="${escapeHtml(file.url)}">Download ${escapeHtml(file.name)}</a>`).join('')}`;byId('exportModal').classList.add('show');log(`Noseboom statistics export complete: ${(state.files||[]).length} files`);return;}}

  async function refresh(){showBusy('Loading Noseboom workspace','Reading the active Flight Project and bounded scientific browser data.');try{payload=await api('/api/noseboom');const rawPoints=(payload?.data?.points||[]).filter(point=>finite(point.lat)&&finite(point.lon)),pointLimit=Math.max(500,Number(payload?.data?.browser_limits?.map_points)||6000),pointStep=Math.max(1,Math.ceil(rawPoints.length/pointLimit));browserPoints=rawPoints.filter((_,index)=>index%pointStep===0);byId('flightName').textContent=payload.flight_id||'No project';if(!payload.ready||!payload.data?.available){byId('statusText').textContent=payload.processing_status==='processing'?payload.processing_step:'Noseboom processing has not produced a map yet.';showMapError(payload.data?.reason||'Noseboom processing has not produced a valid route.');byId('statisticsExportBtn').disabled=true;return;}byId('statusDot').classList.add('ready');byId('statusText').textContent='Processed Noseboom data loaded from the active Flight Project';byId('mapMessage').hidden=true;byId('statisticsExportBtn').disabled=false;const route=points();if(ensureMap()){await makeLayers(route);showLayer(activeLayer,false,false);resetMap(false);setTimeout(()=>map.invalidateSize(false),120);}showBusy('Preparing scientific plots','Rendering the selected Noseboom statistical view.',62);await renderStats(activeStat,false,false);byId('dataExportBtn').style.display='';syncDownloadOptions();log('Noseboom browser data rendered successfully');}catch(error){byId('statusText').textContent=`Noseboom view failed: ${error.message}`;showMapError(error.message);log(`Noseboom browser error: ${error.message}`);}finally{hideBusy();}}

  const initial=viewFromPath();if(initial.layer)activeLayer=initial.layer;if(initial.stat)activeStat=initial.stat;
  byId('mainGuiBtn').onclick=()=>{window.location.href='/';};byId('refreshBtn').onclick=refresh;
  ['route','wind','straight'].forEach(layer=>{const button=byId(`${layer}Btn`);button.addEventListener('click',event=>activateLinkedView(event,()=>showLayer(layer)));button.addEventListener('contextmenu',event=>showViewMenu(event,'map',layerRoutes[layer],layer==='straight'));});
  document.querySelectorAll('[data-stat]').forEach(button=>{button.addEventListener('click',event=>activateLinkedView(event,()=>renderStats(button.dataset.stat)));button.addEventListener('contextmenu',event=>showViewMenu(event,'stats',statRoutes[button.dataset.stat],false));});
  byId('resetMapBtn').onclick=()=>resetMap();byId('mapFullscreenBtn').onclick=()=>openFullscreen('map');byId('menuFullscreen').onclick=()=>openFullscreen(viewMenuContext.panel);byId('menuNewTab').onclick=()=>{closeViewMenu();window.open(viewMenuContext.path, '_blank', 'noopener');};byId('menuCurrentSettings').onclick=()=>showSettings(false);byId('menuChangeSettings').onclick=()=>showSettings(true);byId('settingsClose').onclick=closeSettings;byId('settingsModal').onclick=event=>{if(event.target===byId('settingsModal'))closeSettings();};
  byId('bufferInput').oninput=()=>resetMap(false);byId('lineWidthInput').onchange=async()=>{showBusy('Updating map','Rebuilding bounded route layers.');try{await makeLayers(points());showLayer(activeLayer,false,false);}finally{hideBusy();}};byId('colorScheme').onchange=byId('lineWidthInput').onchange;
  byId('dataExportBtn').onclick=openDownload;byId('downloadClose').onclick=closeDownload;byId('downloadCancel').onclick=closeDownload;byId('downloadStart').onclick=startDataDownload;byId('downloadVariables').onchange=syncDownloadOptions;byId('downloadProgressClose').onclick=()=>byId('downloadProgressModal').classList.remove('show');syncDownloadOptions();byId('statisticsExportBtn').onclick=openExport;byId('exportClose').onclick=closeExport;byId('exportCancel').onclick=closeExport;byId('exportStart').onclick=startStatisticsExport;
  document.addEventListener('click',event=>{if(!event.target.closest('#viewMenu'))closeViewMenu();});window.addEventListener('popstate',()=>{const view=viewFromPath();if(view.layer)showLayer(view.layer,false,false);if(view.stat)renderStats(view.stat,false);});window.addEventListener('fullscreenchange',()=>setTimeout(()=>{if(map)map.invalidateSize(false);document.querySelectorAll('#statsView .js-plotly-plot').forEach(plot=>Plotly.Plots.resize(plot));},180));
  let resizeTimer=null;new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>document.querySelectorAll('#statsView .js-plotly-plot').forEach(plot=>window.Plotly&&Plotly.Plots.resize(plot)),120);}).observe(byId('statsView'));
  byId('logBtn').onclick=async()=>{const panel=byId('logPanel');panel.classList.toggle('show');if(!panel.classList.contains('show'))return;try{const result=await api('/api/logs');panel.innerHTML=(result.records||[]).map(record=>`${escapeHtml(record.timestamp)} [${escapeHtml(record.severity)}] ${escapeHtml(record.message)}`).join('<br>')||'No log entries.';panel.scrollTop=panel.scrollHeight;}catch(error){panel.textContent=error.message;}};
  refresh();

})();
