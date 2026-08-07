(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const plotConfig={responsive:true,displaylogo:false,scrollZoom:true,doubleClick:'reset',toImageButtonOptions:{format:'png',scale:2}};
  // Colour scales as stop lists, interpolated the same way. Thermal stays the
  // default: it is perceptually ordered, which is what a quantitative
  // temperature map needs. The sequential ramps are offered for figures that
  // have to match an existing publication, and bwr for showing departure either
  // side of a midpoint.
  const PALETTES={
    Thermal:[[48,18,59],[59,99,232],[46,199,201],[108,227,79],[246,196,69],[231,53,45]],
    YlOrRd:[[255,255,204],[254,217,118],[253,141,60],[240,59,32],[189,0,38]],
    OrRd:[[255,247,236],[253,212,158],[253,141,60],[227,74,51],[127,0,0]],
    PuRd:[[247,244,249],[212,185,218],[223,101,176],[206,18,86],[103,0,31]],
    bwr:[[0,0,255],[255,255,255],[255,0,0]]
  };
  const DEFAULT_PALETTE='Thermal';
  const thermalPalette=['#30123b','#3b63e8','#2ec7c9','#6ce34f','#f6c445','#e7352d'];
  function paletteName(){
    const select=$('mapPalette');
    return select&&PALETTES[select.value]?select.value:DEFAULT_PALETTE;
  }
  function paletteStops(name){return PALETTES[name]||PALETTES[DEFAULT_PALETTE];}
  function paletteGradient(name){
    return `linear-gradient(to top,${paletteStops(name).map(c=>`rgb(${c.join(',')})`).join(',')})`;
  }
  function setPalettes(){
    const select=$('mapPalette');
    if(!select||select.options.length)return;
    const requested=new URLSearchParams(location.search).get('palette');
    select.innerHTML=Object.keys(PALETTES).map(n=>`<option value="${n}">${n}</option>`).join('');
    select.value=PALETTES[requested]?requested:DEFAULT_PALETTE;
  }
  // Ticks formatted from the range they span, so the column reads as one scale.
  function tickFormatter(low,high){
    const span=Math.abs(high-low),largest=Math.max(Math.abs(low),Math.abs(high));
    if(!(span>0))return v=>Number(v).toPrecision(3);
    if(largest>=1e5||(largest>0&&largest<1e-3)){
      const sup={'0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶','7':'⁷','8':'⁸','9':'⁹','-':'⁻'};
      return v=>{const[m,e]=Number(v).toExponential(2).split('e');
        return `${m} × 10${String(Number(e)).split('').map(c=>sup[c]||c).join('')}`;};
    }
    const decimals=Math.min(6,Math.max(0,2-Math.floor(Math.log10(span))));
    return v=>Number(v).toFixed(decimals);
  }
  const LEGEND_TICKS=6;
  function renderLegend(title,low,high,palette,count){
    const heading=$('legendTitle'),ramp=$('legendRamp'),ticks=$('legendTicks'),note=$('legendNote');
    if(!heading||!ramp||!ticks)return;
    heading.textContent=title;
    ramp.style.background=paletteGradient(palette);
    const format=tickFormatter(low,high),rows=[];
    for(let step=0;step<LEGEND_TICKS;step+=1){
      const fraction=step/(LEGEND_TICKS-1),value=high-(high-low)*fraction;
      rows.push(`<span class="tick" style="top:${(fraction*100).toFixed(4)}%">${format(value)}</span>`);
    }
    ticks.innerHTML=rows.join('');
    if(note)note.textContent=Number.isFinite(count)?`${count.toLocaleString()} frames`:'';
  }
  const metricLabels={
    temperature_median_c:'Median temperature [°C]',
    temperature_mean_c:'Mean temperature [°C]',
    temperature_max_c:'Maximum temperature [°C]',
    temperature_min_c:'Minimum temperature [°C]'
  };
  let payload=null,map=null,mapLayers=[],mapBounds=null;

  async function api(url,options={}){const response=await fetch(url,{cache:'no-store',...options});if(!response.ok)throw new Error(`Request failed (${response.status})`);return response.json();}
  function finite(value){return Number.isFinite(Number(value));}
  function layout(title,xTitle,yTitle,extra={}) {
    const base={title:{text:title,x:.5,font:{size:18}},paper_bgcolor:'#f7fafc',plot_bgcolor:'#fff',font:{family:'Arial, sans-serif',size:14,color:'#172431'},margin:{l:92,r:46,t:68,b:76},xaxis:{title:xTitle,gridcolor:'#dce5ea',automargin:true},yaxis:{title:yTitle,gridcolor:'#dce5ea',automargin:true,separatethousands:true,exponentformat:'none'},legend:{orientation:'h',y:1.14,x:0},hovermode:'closest'};
    return {...base,...extra,xaxis:{...base.xaxis,...(extra.xaxis||{})},yaxis:{...base.yaxis,...(extra.yaxis||{})}};
  }
  function quantile(values,fraction){const sorted=values.map(Number).filter(Number.isFinite).sort((a,b)=>a-b);if(!sorted.length)return null;return sorted[Math.round((sorted.length-1)*fraction)];}
  function color(value,low,high,palette=paletteName()){
    const stops=paletteStops(palette);
    const position=Math.max(0,Math.min(.999,(Number(value)-low)/Math.max(high-low,1e-12)))*(stops.length-1);
    const left=Math.floor(position),mix=position-left,a=stops[left],b=stops[left+1]||stops[left];
    return `rgb(${a.map((v,i)=>Math.round(v*(1-mix)+b[i]*mix)).join(',')})`;
  }
  function summaryCard(name,value,note=''){return `<div class="summary"><span>${name}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;}
  // Investigation time filter. Every FLIR view reads the same three arrays, so
  // one interval reaches all of them: the frame statistics, the distribution,
  // the variability, the map and the summary. The acquisition intervals carry
  // no times of their own, but there is one per radiometric frame in the same
  // order, so they are cut by that frame's time.
  let window_=null;
  function windowMilliseconds(value){
    const text=String(value||'').trim().replace(' ','T');
    if(!text)return NaN;
    return Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)?text:`${text}Z`);
  }
  function insideWindow(stamp){
    if(!window_)return true;
    const value=windowMilliseconds(stamp);
    return Number.isFinite(value)&&value>=window_.start&&value<=window_.end;
  }
  function temperatureRows(){return (payload.temperature_records||[]).filter(row=>insideWindow(row.timestamp_utc));}
  function mapPoints(){return (payload.map_points||[]).filter(point=>insideWindow(point.timestamp_utc));}
  function acquisitionIntervals(){
    const intervals=(payload.acquisition_intervals_seconds||[]).map(Number);
    const rows=payload.temperature_records||[];
    if(!window_||rows.length!==intervals.length)return intervals.filter(Number.isFinite);
    return intervals.filter((value,index)=>Number.isFinite(value)&&insideWindow(rows[index]?.timestamp_utc));
  }
  function acquisitionGaps(){return (payload.gaps||[]).filter(row=>insideWindow(row.gap_start));}
  function windowText(value){
    const date=new Date(Number(value));
    return Number.isFinite(date.getTime())?`${date.toISOString().slice(0,10)} ${date.toISOString().slice(11,19)}`:'';
  }
  function recordedWindow(){
    const stamps=(payload.temperature_records||[]).map(row=>windowMilliseconds(row.timestamp_utc)).filter(Number.isFinite);
    if(!stamps.length){
      const low=windowMilliseconds(payload.utc_start),high=windowMilliseconds(payload.utc_end);
      return Number.isFinite(low)&&Number.isFinite(high)?[low,high]:null;
    }
    return [Math.min(...stamps),Math.max(...stamps)];
  }
  function describeWindow(){
    const note=$('investigationNote'),recorded=recordedWindow();
    if(!note)return;
    const span=recorded?`Recorded ${windowText(recorded[0])} to ${windowText(recorded[1])} UTC`:'';
    note.innerHTML=window_
      ? `<strong>Investigation interval ${windowText(window_.start)} to ${windowText(window_.end)} UTC</strong> · every view and the map export follow it · ${span} · the main GUI Time Filter and the saved project are unchanged.`
      : `${span} · showing every frame inside the main GUI Time Filter. The thermal gallery holds fixed representative frames and is not narrowed.`;
  }
  async function applyWindow(){
    const start=windowMilliseconds($('investigationStart').value.trim());
    const end=windowMilliseconds($('investigationEnd').value.trim());
    if(!Number.isFinite(start)||!Number.isFinite(end)){$('statusText').textContent='Give both investigation times as UTC, for example 2026-08-03 11:30:00.';return;}
    if(end<=start){$('statusText').textContent='The investigation end must be after its start.';return;}
    window_={start,end};
    describeWindow();
    await drawEverything();
    $('statusText').textContent=`Investigation interval applied: ${temperatureRows().length.toLocaleString()} of ${(payload.temperature_records||[]).length.toLocaleString()} frames`;
  }
  async function clearWindow(){
    window_=null;
    const recorded=recordedWindow();
    if(recorded){$('investigationStart').value=windowText(recorded[0]);$('investigationEnd').value=windowText(recorded[1]);}
    describeWindow();
    await drawEverything();
    $('statusText').textContent='Showing every FLIR frame inside the main GUI Time Filter';
  }

  function renderSummary(){
    const summary=payload.summary||{},temperatures=temperatureRows(),mapped=mapPoints();
    const medians=temperatures.map(row=>row.temperature_median_c).filter(finite);
    $('summaryGrid').innerHTML=[
      summaryCard('Selected FLIR frames',Number(summary.frame_count||0).toLocaleString(),`${payload.utc_start||'—'} to ${payload.utc_end||'—'}`),
      summaryCard('Radiometric frames',temperatures.length.toLocaleString(),payload.temperature_available?'Temperature conversion complete':payload.temperature_reason||'Waiting for FLIR processing'),
      summaryCard('Mapped frames',mapped.length.toLocaleString(),payload.matching_method||'Noseboom UTC matching pending'),
      summaryCard('Median apparent temperature',medians.length?`${quantile(medians,.5).toFixed(2)} °C`:'—','Uncorrected FLIR Planck temperature')
    ].join('');
  }
  function renderAcquisition(){
    const intervals=acquisitionIntervals();
    Plotly.react('acquisitionPlot',[{type:'scattergl',mode:'lines',x:intervals.map((_,index)=>index+2),y:intervals,name:'Frame interval',line:{color:'#d55e00',width:1.4},hovertemplate:'Frame %{x}<br>Interval %{y:.6f} s<extra></extra>'}],layout('FLIR frame-to-frame acquisition timing','Frame number','Interval [s]'),plotConfig);
    const gaps=acquisitionGaps(),x=gaps.map((_,index)=>index+1),y=gaps.map(row=>Number(row.gap_seconds||row.seconds||row.duration_seconds)).map(value=>Number.isFinite(value)?value:null);
    Plotly.react('gapPlot',[{type:'bar',x,y,name:'Gap duration',marker:{color:'#8c2d78'},hovertemplate:'Gap %{x}<br>%{y:.6f} s<extra></extra>'}],layout(gaps.length?'Detected FLIR acquisition gaps':'No threshold-exceeding FLIR acquisition gaps','Gap number','Duration [s]'),plotConfig);
  }
  function renderTemperaturePlots(){
    const rows=temperatureRows().filter(row=>row.timestamp_utc);
    const x=rows.map(row=>row.timestamp_utc);
    const traces=[
      ['temperature_min_c','Minimum','#2c7fb8'],['temperature_median_c','Median','#111111'],
      ['temperature_mean_c','Mean','#e69f00'],['temperature_max_c','Maximum','#d55e00']
    ].map(([key,name,shade])=>({type:'scattergl',mode:'lines',x,y:rows.map(row=>row[key]),name,line:{color:shade,width:name==='Median'?2.2:1.35},connectgaps:false,hovertemplate:`UTC %{x}<br>${name}: %{y:.4f} °C<extra></extra>`}));
    Plotly.react('temperatureTimePlot',traces,layout('FLIR frame temperature statistics','Recorded UTC','Apparent temperature [°C]'),plotConfig);
    const medians=rows.map(row=>row.temperature_median_c).filter(finite);
    Plotly.react('temperatureDistributionPlot',[{type:'histogram',x:medians,nbinsx:50,marker:{color:'#e76f51',line:{color:'#7a2719',width:.5}},hovertemplate:'Temperature %{x:.3f} °C<br>Frames %{y}<extra></extra>'}],layout('Distribution of frame-median temperature','Apparent temperature [°C]','Frame count'),plotConfig);
    Plotly.react('temperatureVariabilityPlot',[{type:'scattergl',mode:'lines',x,y:rows.map(row=>row.temperature_std_c),name:'Within-frame standard deviation',line:{color:'#6a4c93',width:1.6},hovertemplate:'UTC %{x}<br>Standard deviation %{y:.4f} °C<extra></extra>'}],layout('Within-frame thermal variability','Recorded UTC','Temperature standard deviation [°C]'),plotConfig);
  }
  function ensureMap(){
    if(map)return;
    // The page once loaded Leaflet's stylesheet but never its script, so every
    // L.* call threw and the map silently never appeared. Say so plainly.
    if(typeof L==='undefined')throw new Error('the map library did not load');
    map=L.map('thermalMap',{zoomControl:true,preferCanvas:true}).setView([47.64,9.38],10);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors',crossOrigin:true}).addTo(map);
  }
  function renderMap(){
    ensureMap();mapLayers.forEach(layer=>layer.removeFrom(map));mapLayers=[];
    const metric=$('mapMetric').value,points=mapPoints().filter(point=>finite(point.latitude)&&finite(point.longitude)&&finite(point[metric]));
    $('interpretation').textContent=payload.temperature_interpretation||'';
    if(!points.length){$('mapNote').textContent=payload.temperature_reason||'No georeferenced FLIR temperature frames are available.';$('mapLegend').hidden=true;mapBounds=null;return;}
    const palette=paletteName();
    const values=points.map(point=>Number(point[metric])),low=quantile(values,.02),highCandidate=quantile(values,.98),high=highCandidate>low?highCandidate:low+1;
    const step=Math.max(1,Math.ceil(points.length/4000)),shown=points.filter((_,index)=>index%step===0),renderer=L.canvas({padding:.5,tolerance:8});
    const route=L.polyline(shown.map(point=>[point.latitude,point.longitude]),{renderer,color:'#17212b',weight:2,opacity:.58,interactive:false}).addTo(map);mapLayers.push(route);
    // Popup text is built when a marker is opened, not for all of them up
    // front, and the markers join the map as one layer instead of thousands of
    // separate insertions. Both were pure cost: nobody reads 4000 popups.
    const popup=point=>{
      const value=Number(point[metric]);
      return `<strong>FLIR frame ${point.frame_id}</strong><br>${point.timestamp_utc}<br>Latitude: ${Number(point.latitude).toFixed(6)}<br>Longitude: ${Number(point.longitude).toFixed(6)}<br>Altitude: ${finite(point.altitude_m)?Number(point.altitude_m).toFixed(2)+' m':'n/a'}<br>${metricLabels[metric]}: ${value.toFixed(3)}<br>Mean: ${finite(point.temperature_mean_c)?Number(point.temperature_mean_c).toFixed(3)+' °C':'n/a'}<br>Range: ${finite(point.temperature_min_c)&&finite(point.temperature_max_c)?Number(point.temperature_min_c).toFixed(3)+'–'+Number(point.temperature_max_c).toFixed(3)+' °C':'n/a'}<br>Noseboom time difference: ${finite(point.time_delta_seconds)?Number(point.time_delta_seconds).toFixed(3)+' s':'n/a'}`;
    };
    const markers=shown.map(point=>L.circleMarker([point.latitude,point.longitude],
      {renderer,radius:4,color:'#071827',weight:.45,fillColor:color(Number(point[metric]),low,high,palette),fillOpacity:.92}
    ).bindPopup(()=>popup(point)));
    const group=L.layerGroup(markers).addTo(map);mapLayers.push(group);
    mapBounds=L.latLngBounds(shown.map(point=>[point.latitude,point.longitude]));
    if(mapBounds.isValid())map.fitBounds(mapBounds,{padding:[30,30],maxZoom:17});
    renderLegend(metricLabels[metric],low,high,palette,points.length);$('mapLegend').hidden=false;
    // Naming the true frame count, because what is drawn is a reduced view of
    // it and the reader should not mistake one for the other.
    const total=Number(payload.map_points_total)||points.length;
    const reduced=total>points.length?` (reduced from ${total.toLocaleString()} for display; every frame is in temperature_frames.csv)`:'';
    $('mapNote').textContent=`${total.toLocaleString()} georeferenced frames${reduced} · displaying ${shown.length.toLocaleString()} interactive points`;
  }
  function renderGallery(){
    const thumbnails=payload.thumbnails||[];
    $('gallery').innerHTML=thumbnails.length?thumbnails.map((item,index)=>`<figure class="thermal-frame"><a href="${item.url}" target="_blank" rel="noopener"><img src="${item.url}" alt="Representative FLIR frame ${index+1}" loading="lazy"></a><figcaption>${item.caption||`Representative FLIR frame ${index+1}`}</figcaption></figure>`).join(''):'<div class="empty-state">No representative FLIR frames have been generated.</div>';
  }
  // Leaflet paints tiles and vectors into separate layers, so the export
  // redraws the visible extent onto one canvas: tiles, the track, the coloured
  // frames, then the legend. The server sets the physical width to seven inches.
  function composeMapImage(){
    const container=$('thermalMap');
    const width=Math.max(800,container.clientWidth),height=Math.max(500,container.clientHeight);
    const canvas=document.createElement('canvas');
    canvas.width=width*2;canvas.height=height*2;
    const context=canvas.getContext('2d');
    context.scale(2,2);context.fillStyle='#ffffff';context.fillRect(0,0,width,height);
    const base=container.getBoundingClientRect();
    for(const tile of container.querySelectorAll('img.leaflet-tile-loaded')){
      const rect=tile.getBoundingClientRect();
      try{context.drawImage(tile,rect.left-base.left,rect.top-base.top,rect.width,rect.height);}
      catch(error){/* a tile that would taint the canvas is skipped */}
    }
    const metric=$('mapMetric').value,palette=paletteName();
    const points=mapPoints().filter(point=>finite(point.latitude)&&finite(point.longitude)&&finite(point[metric]));
    if(points.length){
      const values=points.map(point=>Number(point[metric]));
      const low=quantile(values,.02),highCandidate=quantile(values,.98),high=highCandidate>low?highCandidate:low+1;
      const step=Math.max(1,Math.ceil(points.length/4000)),shown=points.filter((_,index)=>index%step===0);
      context.strokeStyle='#17212b';context.lineWidth=1.6;context.globalAlpha=.6;context.beginPath();
      shown.forEach((point,index)=>{const at=map.latLngToContainerPoint([point.latitude,point.longitude]);index?context.lineTo(at.x,at.y):context.moveTo(at.x,at.y);});
      context.stroke();context.globalAlpha=1;
      shown.forEach(point=>{
        const at=map.latLngToContainerPoint([point.latitude,point.longitude]);
        if(at.x<0||at.y<0||at.x>width||at.y>height)return;
        context.fillStyle=color(Number(point[metric]),low,high,palette);
        context.beginPath();context.arc(at.x,at.y,4,0,Math.PI*2);context.fill();
      });
      drawMapExportLegend(context,width,metric,low,high,palette,points.length);
    }
    return canvas.toDataURL('image/png');
  }
  // No panel behind the labels, matching the screen; each is stroked white
  // before it is filled dark so it reads over whatever tiles fall behind it.
  function drawMapExportLegend(context,width,metric,low,high,palette,count){
    const boxWidth=168,boxHeight=252,x=width-boxWidth-18,y=18;
    const halo=(text,left,top)=>{
      context.lineJoin='round';context.miterLimit=2;
      context.strokeStyle='#ffffff';context.lineWidth=3.4;context.strokeText(text,left,top);
      context.fillStyle='#07182a';context.fillText(text,left,top);
    };
    context.save();
    context.font='700 13px Arial';
    halo(metricLabels[metric]||metric,x+12,y+22);
    const barX=x+14,barY=y+38,barWidth=22,barHeight=boxHeight-74;
    const gradient=context.createLinearGradient(0,barY+barHeight,0,barY);
    paletteStops(palette).forEach((shade,index,list)=>gradient.addColorStop(index/(list.length-1),`rgb(${shade.join(',')})`));
    context.fillStyle=gradient;context.fillRect(barX,barY,barWidth,barHeight);
    context.strokeStyle='#07182a';context.lineWidth=1;context.strokeRect(barX,barY,barWidth,barHeight);
    const format=tickFormatter(low,high);
    context.font='11px Arial';
    for(let step=0;step<LEGEND_TICKS;step+=1){
      const fraction=step/(LEGEND_TICKS-1);
      halo(format(low+fraction*(high-low)),barX+barWidth+8,barY+barHeight-fraction*barHeight+4);
    }
    context.font='10px Arial';
    halo(`${count.toLocaleString()} frames`,x+12,y+boxHeight-12);
    context.restore();
  }
  async function exportMapFigure(button){
    const original=button.textContent;
    button.disabled=true;button.textContent='Exporting…';
    try{
      if(!map)throw new Error('Open the Temperature map view first');
      const format=$('mapExportFormat').value,dpi=Number($('mapExportDpi').value);
      const body=JSON.stringify({
        image:composeMapImage(),format,dpi,
        flight_name:$('flightName').textContent||'Flight',
        interval:window_?`${windowText(window_.start)} to ${windowText(window_.end)} UTC`:''
      });
      const response=await fetch('/api/flir/map/export',{method:'POST',headers:{'Content-Type':'application/json'},body});
      if(!response.ok){const detail=await response.json().catch(()=>({}));throw new Error(detail.error||`Map export failed (${response.status})`);}
      const blob=await response.blob();
      const name=(response.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||`flir_temperature_map.${format}`;
      const link=document.createElement('a');
      link.href=URL.createObjectURL(blob);link.download=name;link.click();
      setTimeout(()=>URL.revokeObjectURL(link.href),4000);
      $('statusText').textContent=`Thermal map exported as ${name}`;
    }catch(error){$('statusText').textContent=`Map export failed: ${error.message}`;}
    finally{button.disabled=false;button.textContent=original;}
  }
  function pathView(){return location.pathname.includes('temperature-map')?'temperature':location.pathname.includes('temperature-plots')?'temperature-plots':location.pathname.includes('gallery')?'gallery':'overview';}
  function showView(view,push=true){
    document.querySelectorAll('[data-section]').forEach(card=>card.hidden=card.dataset.section!==view);
    document.querySelectorAll('[data-view]').forEach(link=>link.classList.toggle('active',link.dataset.view===view));
    if(push)history.pushState({view},'',`/flir/${view==='temperature'?'temperature-map':view}`);
    setTimeout(()=>{document.querySelectorAll('.js-plotly-plot').forEach(plot=>Plotly.Plots.resize(plot));if(view==='temperature'&&map){map.invalidateSize(false);if(mapBounds?.isValid())map.fitBounds(mapBounds,{padding:[30,30],maxZoom:17});}},80);
  }
  // Lets the browser paint before the next stage, so progress is visible and
  // the window stays answerable instead of appearing frozen.
  const repaint=()=>new Promise(done=>requestAnimationFrame(()=>setTimeout(done,0)));
  async function drawEverything(){
    const stages=[
      ['summary',renderSummary],['acquisition',renderAcquisition],
      ['temperature plots',renderTemperaturePlots],['gallery',renderGallery],
      ['map',renderMap]
    ];
    for(const [name,draw] of stages){
      $('mapNote').textContent=`Drawing ${name}…`;
      await repaint();
      try{draw();}
      catch(error){$('statusText').textContent=`FLIR ${name} could not be drawn: ${error.message}`;}
    }
  }
  let flirRetry=null;
  async function load(){
    if(flirRetry){clearTimeout(flirRetry);flirRetry=null;}
    $('busy').classList.add('show');
    // Whatever else happens, the overlay comes down. It hides the entire page,
    // so leaving it up turns any unhandled failure into an apparent hang with
    // nothing on screen to explain it.
    const failsafe=setTimeout(()=>{
      $('busy').classList.remove('show');
      $('statusText').textContent='FLIR workspace took too long to prepare. Use Refresh, and check the Processing Log.';
    },20000);
    try{
      // The dashboard holds its state lock while a job runs, so this request can
      // wait on processing rather than on itself. Without a bound the page sat
      // on "Preparing FLIR workspace" with nothing to act on.
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),15000);
      let response;
      try{response=await api('/api/flir',{signal:controller.signal});}
      finally{clearTimeout(timeout);}
      $('flightName').textContent=response.flight_id||'No project';
      if(!response.ready){const message=response.message||response.processing_step||'FLIR processing has not run yet';$('statusText').textContent=message;$('summaryGrid').innerHTML=summaryCard('FLIR workspace','Not ready',message);return;}
      payload=response.data;$('statusDot').classList.add('ready');$('statusText').textContent=response.temperature_ready?'FLIR temperature products and Noseboom map loaded':'FLIR acquisition products loaded · temperature and georeferencing are still running';
      // Drawing happens after the overlay is down and with a repaint between
      // each stage. Done in one synchronous run, the browser cannot repaint at
      // all, so the page stayed behind "Preparing FLIR workspace" for the whole
      // of it and looked hung rather than busy.
      // ?metric= is honoured once, when the payload first arrives, so choosing
      // another statistic in the same tab is not undone on every redraw.
      const requested=new URLSearchParams(location.search).get('metric');
      if(requested&&metricLabels[requested])$('mapMetric').value=requested;
      $('busy').classList.remove('show');
      const recorded=recordedWindow();
      if(recorded&&!$('investigationStart').value){
        $('investigationStart').value=windowText(recorded[0]);
        $('investigationEnd').value=windowText(recorded[1]);
      }
      describeWindow();
      await drawEverything();
      showView(pathView(),false);
      if(!response.temperature_ready){flirRetry=setTimeout(load,4000);}
    }catch(error){
      const busy=error.name==='AbortError';
      $('statusText').textContent=busy
        ?'The main window is busy processing. This page will try again shortly.'
        :`FLIR view failed: ${error.message}`;
      if(!busy){$('statusText').style.color='var(--danger)';}
      $('summaryGrid').innerHTML=summaryCard('FLIR workspace',busy?'Waiting for processing':'Unavailable',$('statusText').textContent);
      if(busy){flirRetry=setTimeout(load,4000);}
    }
    finally{clearTimeout(failsafe);$('busy').classList.remove('show');}
  }
  function formatBytes(value){const n=Number(value)||0;if(n<1024)return `${n} B`;if(n<1048576)return `${(n/1024).toFixed(1)} KB`;return `${(n/1048576).toFixed(1)} MB`;}
  async function showExports(){
    const modal=$('exportModal'),list=$('exportList');
    list.innerHTML='<p class="muted">Looking for FLIR products…</p>';
    modal.classList.add('show');
    try{
      const response=await api('/api/flir/exports');
      const exports=response.exports||[];
      list.innerHTML=exports.length?exports.map(item=>`<div class="export-row">
        <div class="grow"><strong>${item.name}</strong><small>${item.description}</small></div>
        <span class="muted">${formatBytes(item.size_bytes)}</span>
        <a class="btn" href="${item.url}" download>Download</a>
      </div>`).join(''):'<p class="muted">No FLIR product has been written yet. Run the FLIR metadata check, and Level 2 for temperature.</p>';
    }catch(error){
      list.innerHTML=`<p class="muted">Could not list FLIR products: ${error.message}</p>`;
    }
  }
  $('exportBtn').onclick=showExports;
  $('exportClose').onclick=()=>$('exportModal').classList.remove('show');
  setPalettes();
  $('refreshBtn').onclick=load;$('mapMetric').onchange=renderMap;$('mapPalette').onchange=renderMap;
  // One statistic on its own, in its own tab: the metric and the colours travel
  // in the URL so the new tab opens on exactly what was being looked at.
  $('mapNewTabBtn').onclick=()=>{
    const parameters=new URLSearchParams({metric:$('mapMetric').value,palette:paletteName()});
    window.open(`/flir/temperature-map?${parameters}`,'_blank','noopener');
  };
  $('mapFullscreenBtn').onclick=()=>{
    const card=document.querySelector('[data-section="temperature"]');
    if(!document.fullscreenElement){card?.requestFullscreen?.().then(()=>setTimeout(()=>map?.invalidateSize(),120));}
    else{document.exitFullscreen();}
  };
  // Leaving full screen resizes the container as much as entering it does.
  addEventListener('fullscreenchange',()=>setTimeout(()=>map?.invalidateSize(),120));$('resetMapBtn').onclick=()=>{if(map&&mapBounds?.isValid())map.fitBounds(mapBounds,{padding:[30,30],maxZoom:17});};
  document.querySelectorAll('[data-view]').forEach(link=>link.onclick=event=>{event.preventDefault();showView(link.dataset.view);});
  document.querySelectorAll('[data-fullscreen]').forEach(button=>button.onclick=async()=>{await $(button.dataset.fullscreen).closest('.chart-card').requestFullscreen();setTimeout(()=>Plotly.Plots.resize($(button.dataset.fullscreen)),120);});
  $('investigationApply').onclick=applyWindow;$('investigationClear').onclick=clearWindow;
  $('mapExportBtn').onclick=event=>exportMapFigure(event.currentTarget);
  addEventListener('popstate',()=>showView(pathView(),false));addEventListener('resize',()=>document.querySelectorAll('.js-plotly-plot').forEach(plot=>Plotly.Plots.resize(plot)));load();

})();
