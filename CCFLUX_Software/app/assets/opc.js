(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const config = {responsive:true,displaylogo:false,scrollZoom:true,doubleClick:'reset',toImageButtonOptions:{format:'png',scale:2}};
  const labels = {
    number:['Total number concentration','Number concentration [#/cm³]'],
    pm1:['PM1','Mass concentration [µg/m³]'],
    pm25:['PM2.5','Mass concentration [µg/m³]'],
    pm10:['PM10','Mass concentration [µg/m³]'],
    flow:['Sample flow','Flow [mL/s]'],sampling_period:['Sampling period','Time [s]'],
    temperature:['Internal temperature','Temperature [°C]'],rh:['Internal relative humidity','RH [%]'],
    laser:['Laser status','Raw value'],reject_ratio:['Reject ratio','Raw value']
  };
  const colors = {hbx4:'#0072B2',hbx5:'#D55E00'};
  let payload = null;

  async function api(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(`Request failed (${r.status})`);return r.json();}
  function layout(title,ytitle,extra={}){return {title:{text:title,x:.5,y:1,yanchor:'top',pad:{t:10},font:{size:18}},paper_bgcolor:'#f7fafc',plot_bgcolor:'#fff',font:{family:'Arial, sans-serif',size:14,color:'#172431'},margin:{l:82,r:42,t:96,b:70},xaxis:{title:'Recorded UTC',gridcolor:'#dce5ea',rangeslider:{visible:false}},yaxis:{title:ytitle,gridcolor:'#dce5ea',zerolinecolor:'#b9c7cf'},legend:{orientation:'h',y:1.02,yanchor:'bottom',x:0},hovermode:'closest',...extra};}
  function dualAxisLayout(title,ytitle){return layout(title,ytitle,{margin:{l:92,r:104,t:96,b:70},hovermode:'x unified',yaxis:{title:{text:`HBX-4 · ${ytitle}`,font:{color:colors.hbx4}},tickfont:{color:colors.hbx4},gridcolor:'#dce5ea',zerolinecolor:'#b9c7cf',automargin:true,fixedrange:false},yaxis2:{title:{text:`HBX-5 · ${ytitle}`,font:{color:colors.hbx5}},tickfont:{color:colors.hbx5},overlaying:'y',side:'right',showgrid:false,zeroline:false,automargin:true,fixedrange:false}});}
  const format=value=>Number(value).toPrecision(3).replace(/\.?0+$/,'');
  // Most bins of a clean flight read exactly zero - 87% on HBX-4 and 99% on
  // HBX-5 for Flight_CCT0803 - so colouring from the lowest to the highest
  // value left every structure in one shade at the bottom of the ramp. The
  // colour range is taken from the values that carry signal, and the colour
  // bar states both where the colour saturates and the true peak, so nothing
  // the sensor recorded is hidden by the choice.
  function heatBounds(z){
    const values=[];
    (z||[]).forEach(row=>(row||[]).forEach(value=>{
      const number=Number(value);
      if (Number.isFinite(number)) values.push(number);
    }));
    if (!values.length) return {low:0,high:1,max:1,positiveLow:0};
    values.sort((a,b)=>a-b);
    const low=values[0], max=values[values.length-1];
    const positive=values.filter(value=>value>0);
    const at=(list,fraction)=>list[Math.min(list.length-1,Math.floor(list.length*fraction))];
    if (!positive.length || !(max>low)) return {low,high:max>low?max:low+1,max,positiveLow:0};
    return {
      low,
      // The linear range stops at the 99th percentile of the bins that
      // counted something, so a handful of spikes cannot flatten the rest.
      high:Math.max(at(positive,.99),positive[0]),
      max,
      // The logarithmic floor ignores the lowest 1%, so one near-zero count
      // cannot stretch the ramp over a decade nothing else occupies.
      positiveLow:at(positive,.01)
    };
  }
  function tracesBySession(sensor,key,label,axis='y'){
    const s=sensor.series,sessions=[...new Set(s.session.filter(v=>v!==null))],traces=[];
    sessions.forEach((session,index)=>{
      const x=[],y=[];s.session.forEach((value,i)=>{if(value===session){x.push(s.time[i]);y.push(s[key][i]);}});
      traces.push({type:'scattergl',mode:'lines',x,y,yaxis:axis,name:label,legendgroup:label,showlegend:index===0,line:{width:1.45,color:colors[label]},connectgaps:false,hovertemplate:`${label==='hbx4'?'HBX-4 · without inlet':'HBX-5 · with inlet'}<br>%{x}<br>%{y:.4g}<extra></extra>`});
    });
    return traces;
  }
  function independentTraces(key){return [
    ...tracesBySession(payload.sensors.hbx4,key,'hbx4','y'),
    ...tracesBySession(payload.sensors.hbx5,key,'hbx5','y2')
  ].map(t=>({...t,name:t.name==='hbx4'?'HBX-4 · without inlet · left axis':'HBX-5 · with inlet · right axis'}));}
  function renderSummary(){
    const a=payload.sensors.hbx4,b=payload.sensors.hbx5;
    const card=(name,value,text)=>`<div class="summary"><span>${name}</span><strong>${value}</strong>${text?`<small>${text}</small>`:''}</div>`;
    $('summaryGrid').innerHTML=[
      card('HBX-4 selected rows',Number(a.summary.selected_rows ?? a.summary.rows ?? 0).toLocaleString(),'Without inlet · left axis'),
      card('HBX-5 selected rows',Number(b.summary.selected_rows ?? b.summary.rows ?? 0).toLocaleString(),'With inlet · right axis')
    ].join('');
  }
  function renderTime(){
    const key=$('timeMetric').value,[title,ytitle]=labels[key];
    Plotly.react('timePlot',independentTraces(key),dualAxisLayout(`${title} · independent sensor scales · gaps preserved`,ytitle),config);
  }
  function renderHeat(){
    const sensor=payload.sensors[$('heatSensor').value],h=sensor.heatmap;
    const bounds=heatBounds(h.z), logarithmic=$('heatLog').checked && bounds.high>0;
    const trace={type:'heatmap',x:h.time,y:h.bin_index,colorscale:'Viridis',zauto:false,
      customdata:h.z,hovertemplate:'UTC %{x}<br>Bin %{y}<br>%{customdata:.4g} #/cm³<extra></extra>'};
    if (logarithmic) {
      // A bin that counted nothing has no logarithm. Those cells are left
      // empty rather than pinned to the bottom of the ramp, so "counted
      // nothing" and "counted a little" no longer share one colour.
      const floor=bounds.positiveLow>0?bounds.positiveLow:bounds.max/1000;
      const ceiling=Math.max(bounds.max,floor*10);
      trace.z=h.z.map(row=>(row||[]).map(value=>{
        const number=Number(value);
        return Number.isFinite(number) && number>0 ? Math.log10(Math.min(Math.max(number,floor),ceiling)) : null;
      }));
      trace.zmin=Math.log10(floor); trace.zmax=Math.log10(ceiling);
      const ticks=[];
      for (let power=Math.ceil(trace.zmin); power<=Math.floor(trace.zmax); power+=1) ticks.push(power);
      trace.colorbar={title:{text:'#/cm³',side:'right'},tickmode:'array',tickvals:ticks,
        ticktext:ticks.map(power=>format(Math.pow(10,power)))};
    } else {
      trace.z=h.z; trace.zmin=bounds.low; trace.zmax=bounds.high;
      trace.colorbar={title:{text:'#/cm³',side:'right'}};
    }
    const scaleNote=logarithmic?'logarithmic colour scale':'linear colour scale';
    const peakNote=!logarithmic && bounds.high<bounds.max
      ? `; colour saturates at ${format(bounds.high)}, peak ${format(bounds.max)}`
      : `; peak ${format(bounds.max)}`;
    Plotly.react('heatPlot',[trace],
      layout(`${sensor.label} · bin-resolved concentration · ${scaleNote}${peakNote}`,'OPC-N3 software bin index',{yaxis:{title:'OPC-N3 software bin index',dtick:1},margin:{l:88,r:104,t:96,b:70}}),config);
  }
  function renderDiagnostics(){
    const key=$('diagnosticMetric').value,[title,ytitle]=labels[key];
    Plotly.react('diagnosticPlot',independentTraces(key),dualAxisLayout(`${title} · independent recorded scales`,ytitle),config);
  }

  // --- Size distribution on the flight track -------------------------------
  // The OPC carries no position of its own. Each sample takes the position of
  // the nearest Noseboom fix in time, which the server pairs; a sample with no
  // fix close enough is left off the map rather than placed by guesswork.
  const PALETTES={
    Viridis:['#440154','#414487','#2a788e','#22a884','#7ad151','#fde725'],
    Plasma:['#0d0887','#6a00a8','#b12a90','#e16462','#fca636','#f0f921'],
    Inferno:['#000004','#420a68','#932667','#dd513a','#fca50a','#fcffa4'],
    Cividis:['#00224e','#35456c','#666970','#948e77','#c8b866','#fee838'],
    YlOrRd:['#ffffcc','#fed976','#feb24c','#fd8d3c','#fc4e2a','#e31a1c','#b10026'],
    Turbo:['#30123b','#4145ab','#4675ed','#39a2fc','#1bcfd4','#62fc6b','#d1e935','#fe9b2d','#db3a07','#7a0403']
  };
  let mapData=null,opcMap=null,mapLayer=null,mapTrack=null,mapHome=null,mapBuilt=false;

  const mapSensor=()=>mapData&&mapData.sensors?mapData.sensors[$('mapSensor').value]:null;
  function binLabel(index){return index===''?'All sizes summed':`Bin ${index}`;}
  function selectedBin(){const raw=$('mapBin').value;return raw===''?null:Number(raw);}
  function valueOf(point){
    const bin=selectedBin();
    return bin===null?point.total:(point.bins&&point.bins[bin]!==undefined?point.bins[bin]:null);
  }
  function paletteStops(){return PALETTES[$('mapPalette').value]||PALETTES.Viridis;}
  function colourAt(fraction){
    const stops=paletteStops();
    const position=Math.max(0,Math.min(1,fraction))*(stops.length-1);
    const left=Math.floor(position),right=Math.min(stops.length-1,left+1),mix=position-left;
    const a=stops[left].match(/\w\w/g).map(v=>parseInt(v,16));
    const b=stops[right].match(/\w\w/g).map(v=>parseInt(v,16));
    return `rgb(${a.map((v,i)=>Math.round(v*(1-mix)+b[i]*mix)).join(',')})`;
  }
  function mapBounds(values){
    const finite=values.filter(Number.isFinite).sort((a,b)=>a-b);
    if(!finite.length)return null;
    const positive=finite.filter(value=>value>0);
    const at=(list,fraction)=>list[Math.min(list.length-1,Math.floor(list.length*fraction))];
    const logarithmic=$('mapLog').checked && positive.length>1;
    if(logarithmic)return {low:at(positive,.01),high:at(positive,.99),max:finite[finite.length-1],log:true};
    return {low:finite[0],high:Math.max(at(finite,.99),finite[0]+1e-12),max:finite[finite.length-1],log:false};
  }
  function fractionOf(value,bounds){
    if(!Number.isFinite(value))return null;
    if(bounds.log){
      if(!(value>0))return null;
      const span=Math.log10(bounds.high)-Math.log10(bounds.low);
      if(!(span>0))return 0;
      return (Math.log10(Math.min(Math.max(value,bounds.low),bounds.high))-Math.log10(bounds.low))/span;
    }
    const span=bounds.high-bounds.low;
    return span>0?(Math.min(Math.max(value,bounds.low),bounds.high)-bounds.low)/span:0;
  }
  function fillPalettes(){
    const select=$('mapPalette');
    if(select.options.length)return;
    Object.keys(PALETTES).forEach(name=>{
      const option=document.createElement('option');
      option.value=name;option.textContent=name;select.appendChild(option);
    });
  }
  function fillBins(){
    const sensor=mapSensor(),select=$('mapBin'),previous=select.value;
    select.innerHTML='';
    const all=document.createElement('option');
    all.value='';all.textContent='All sizes summed';select.appendChild(all);
    ((sensor&&sensor.bin_index)||[]).forEach(index=>{
      const option=document.createElement('option');
      option.value=String(index);option.textContent=binLabel(index);select.appendChild(option);
    });
    if([...select.options].some(option=>option.value===previous))select.value=previous;
  }
  function renderLegend(bounds,title,note){
    const legend=$('mapLegend');
    if(!bounds){legend.hidden=true;return;}
    legend.hidden=false;
    $('legendTitle').textContent=title;
    // Bottom of the bar is the low end, so the gradient runs upwards.
    $('legendRamp').style.background=
      `linear-gradient(to top,${paletteStops().join(',')})`;
    const ticks=$('legendTicks');ticks.innerHTML='';
    const count=5;
    for(let step=0;step<count;step+=1){
      const fraction=step/(count-1);
      const value=bounds.log
        ? Math.pow(10,Math.log10(bounds.low)+fraction*(Math.log10(bounds.high)-Math.log10(bounds.low)))
        : bounds.low+fraction*(bounds.high-bounds.low);
      const tick=document.createElement('span');
      tick.className='tick';
      tick.style.bottom=`${fraction*100}%`;
      tick.textContent=format(value);
      ticks.appendChild(tick);
    }
    $('legendNote').textContent=note;
  }
  function showMessage(text){
    const box=$('mapMessage');
    box.hidden=!text;box.textContent=text||'';
  }
  function buildMap(){
    if(mapBuilt)return;
    opcMap=L.map('opcMap',{preferCanvas:true,zoomControl:true});
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      {maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(opcMap);
    opcMap.setView([50.9,6.9],9);
    mapBuilt=true;
  }
  function drawMap(){
    if(!mapBuilt||!mapData)return;
    if(mapLayer){mapLayer.remove();mapLayer=null;}
    if(mapTrack){mapTrack.remove();mapTrack=null;}
    const sensor=mapSensor();
    if(!sensor||!sensor.points.length){
      renderLegend(null);
      showMessage(`${sensor?sensor.label:'This sensor'} has no sample that could be placed on the flight track.`);
      return;
    }
    const track=(mapData.flight_track||[]).filter(p=>Number.isFinite(p.lat)&&Number.isFinite(p.lon));
    if(track.length>1){
      mapTrack=L.polyline(track.map(p=>[p.lat,p.lon]),
        {color:'#8fa9bb',weight:1.5,opacity:.55,interactive:false}).addTo(opcMap);
    }
    const values=sensor.points.map(valueOf);
    const bounds=mapBounds(values);
    const bin=selectedBin();
    const drawn=[],positions=[];
    sensor.points.forEach((point,index)=>{
      const fraction=bounds?fractionOf(values[index],bounds):null;
      positions.push([point.lat,point.lon]);
      if(fraction===null)return;
      drawn.push(L.circleMarker([point.lat,point.lon],{
        radius:4,stroke:false,fillOpacity:.9,fillColor:colourAt(fraction)
      }).bindPopup(
        `<strong>${sensor.label}</strong><br>${point.time}<br>`+
        `${bin===null?'All sizes summed':binLabel(bin)}: `+
        `${values[index]===null||values[index]===undefined?'no reading':format(values[index])+' #/cm³'}<br>`+
        `${point.altitude_m===null||point.altitude_m===undefined?'':'Altitude '+format(point.altitude_m)+' m<br>'}`+
        `Noseboom fix ${point.delta_s} s away`
      ));
    });
    mapLayer=L.layerGroup(drawn).addTo(opcMap);
    if(positions.length){
      mapHome=L.latLngBounds(positions);
      if(!drawMap.fitted){opcMap.fitBounds(mapHome,{padding:[30,30]});drawMap.fitted=true;}
    }
    const unplaced=sensor.unmatched_count+sensor.undated_count;
    renderLegend(bounds,
      `${bin===null?'Summed concentration':binLabel(bin)} [#/cm³]`,
      `${sensor.label} ${bounds&&bounds.log?'logarithmic':'linear'} `+
      `peak ${bounds?format(bounds.max):'—'}`);
    showMessage(
      `${sensor.matched_count.toLocaleString()} of ${sensor.sampled_from.toLocaleString()} samples placed`+
      (unplaced?`; ${unplaced.toLocaleString()} had no Noseboom fix within ${mapData.maximum_time_delta_seconds} s`:'')+
      (drawn.length<sensor.points.length?`; ${(sensor.points.length-drawn.length).toLocaleString()} carried no reading for this size class`:'')
    );
  }
  async function showMap(){
    buildMap();
    if(opcMap)opcMap.invalidateSize();
    if(mapData){drawMap();return;}
    try{
      const response=await api('/api/opc/map');
      if(!response.ready){
        renderLegend(null);
        showMessage(response.message||'The OPC samples could not be placed on a map.');
        return;
      }
      mapData=response.data;
      fillPalettes();fillBins();applyPermalink();drawMap();
    }catch(error){
      renderLegend(null);
      showMessage(`Map data failed to load: ${error.message}`);
    }
  }
  function mapPermalink(){
    const query=new URLSearchParams({
      sensor:$('mapSensor').value,bin:$('mapBin').value,
      palette:$('mapPalette').value,log:$('mapLog').checked?'1':'0'
    });
    return `/opc/map?${query}`;
  }
  function applyPermalink(){
    const query=new URLSearchParams(location.search);
    if(!query.has('sensor'))return;
    $('mapSensor').value=query.get('sensor')||'hbx4';
    fillBins();
    if(query.has('bin'))$('mapBin').value=query.get('bin')||'';
    if(query.has('palette'))$('mapPalette').value=query.get('palette')||'Viridis';
    $('mapLog').checked=query.get('log')!=='0';
  }
  async function exportMapPdf(){
    const button=$('mapPdfBtn'),original=button.textContent;
    button.disabled=true;button.textContent='Exporting…';
    try{
      const image=await composeMapImage();
      const response=await fetch('/api/opc/map/export',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          image,flight_name:$('flightName').textContent,
          subject:`${mapSensor()?mapSensor().label:'OPC'} · ${$('mapBin').selectedOptions[0]?.textContent||''}`
        })
      });
      if(!response.ok)throw new Error(await response.text()||`Export failed (${response.status})`);
      const blob=await response.blob();
      const disposition=response.headers.get('Content-Disposition')||'';
      const named=/filename="([^"]+)"/.exec(disposition);
      const link=document.createElement('a');
      link.href=URL.createObjectURL(blob);
      link.download=named?named[1]:'opc_size_distribution_map.pdf';
      document.body.appendChild(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(link.href),4000);
    }catch(error){
      showMessage(`PDF export failed: ${error.message}`);
    }finally{
      button.disabled=false;button.textContent=original;
    }
  }
  // Leaflet paints tiles and vectors into separate layers, so the export
  // redraws the visible extent onto one canvas: tiles first, then the track,
  // then the coloured samples, then the legend.
  async function composeMapImage(){
    const container=$('opcMap');
    const width=Math.max(800,container.clientWidth),height=Math.max(500,container.clientHeight);
    const canvas=document.createElement('canvas');
    canvas.width=width*2;canvas.height=height*2;
    const context=canvas.getContext('2d');
    context.scale(2,2);
    context.fillStyle='#ffffff';context.fillRect(0,0,width,height);
    const tiles=[...container.querySelectorAll('img.leaflet-tile-loaded')];
    for(const tile of tiles){
      const rect=tile.getBoundingClientRect(),base=container.getBoundingClientRect();
      try{context.drawImage(tile,rect.left-base.left,rect.top-base.top,rect.width,rect.height);}
      catch(error){/* a tile that refused to taint the canvas is skipped */}
    }
    const sensor=mapSensor();
    if(sensor){
      const values=sensor.points.map(valueOf),bounds=mapBounds(values);
      const base=container.getBoundingClientRect();
      const track=(mapData.flight_track||[]);
      if(track.length>1){
        context.strokeStyle='#8fa9bb';context.lineWidth=1.5;context.globalAlpha=.55;
        context.beginPath();
        track.forEach((point,index)=>{
          const at=opcMap.latLngToContainerPoint([point.lat,point.lon]);
          index?context.lineTo(at.x,at.y):context.moveTo(at.x,at.y);
        });
        context.stroke();context.globalAlpha=1;
      }
      sensor.points.forEach((point,index)=>{
        const fraction=bounds?fractionOf(values[index],bounds):null;
        if(fraction===null)return;
        const at=opcMap.latLngToContainerPoint([point.lat,point.lon]);
        if(at.x<0||at.y<0||at.x>width||at.y>height)return;
        context.fillStyle=colourAt(fraction);
        context.beginPath();context.arc(at.x,at.y,4,0,Math.PI*2);context.fill();
      });
      drawExportLegend(context,width,bounds);
    }
    return canvas.toDataURL('image/png');
  }
  function drawExportLegend(context,width,bounds){
    if(!bounds)return;
    const boxWidth=150,boxHeight=250,x=width-boxWidth-18,y=18;
    context.save();
    context.fillStyle='#071827';context.globalAlpha=.94;
    context.beginPath();context.roundRect(x,y,boxWidth,boxHeight,8);context.fill();
    context.globalAlpha=1;
    context.fillStyle='#ffffff';context.font='700 13px Arial';
    const bin=selectedBin();
    context.fillText(bin===null?'Summed [#/cm³]':`${binLabel(bin)} [#/cm³]`,x+12,y+22);
    const barX=x+14,barY=y+38,barWidth=22,barHeight=boxHeight-72;
    const gradient=context.createLinearGradient(0,barY+barHeight,0,barY);
    paletteStops().forEach((shade,index,list)=>
      gradient.addColorStop(index/(list.length-1),shade));
    context.fillStyle=gradient;context.fillRect(barX,barY,barWidth,barHeight);
    context.strokeStyle='#7d9db4';context.lineWidth=1;context.strokeRect(barX,barY,barWidth,barHeight);
    context.fillStyle='#eaf4fb';context.font='11px Arial';
    for(let step=0;step<5;step+=1){
      const fraction=step/4;
      const value=bounds.log
        ? Math.pow(10,Math.log10(bounds.low)+fraction*(Math.log10(bounds.high)-Math.log10(bounds.low)))
        : bounds.low+fraction*(bounds.high-bounds.low);
      context.fillText(format(value),barX+barWidth+8,barY+barHeight-fraction*barHeight+4);
    }
    context.fillStyle='#a9c4d6';context.font='10px Arial';
    context.fillText(bounds.log?'logarithmic':'linear',x+12,y+boxHeight-12);
    context.restore();
  }

  const VIEW_PATHS={size:'size-distribution',diagnostics:'diagnostics',map:'map',overview:'overview'};
  function showView(view,push=true){
    document.querySelectorAll('[data-section]').forEach(card=>card.hidden=card.dataset.section!==view);
    document.querySelectorAll('[data-view]').forEach(link=>link.classList.toggle('active',link.dataset.view===view));
    if(push)history.pushState({view},'',`/opc/${VIEW_PATHS[view]||'overview'}`);
    setTimeout(resizePlots,50);
    // Leaflet measures the container, so it can only lay out once the card is
    // on screen; a map built while its card was hidden renders as grey tiles.
    if(view==='map')setTimeout(showMap,60);
  }
  function pathView(){
    const path=location.pathname;
    return path.includes('size-distribution')?'size'
      :path.includes('diagnostics')?'diagnostics'
      :path.endsWith('/map')?'map':'overview';
  }
  function resizePlots(){document.querySelectorAll('.js-plotly-plot').forEach(p=>Plotly.Plots.resize(p));}
  async function load(){
    $('busy').classList.add('show');
    try{
      const response=await api('/api/opc');$('flightName').textContent=response.flight_id||'No project';
      if(!response.ready){$('statusText').textContent=response.message||'OPC products are not ready';document.querySelector('main').innerHTML=`<div class="empty"><div><strong>OPC processing is required</strong>${response.message||''}</div></div>`;return;}
      payload=response.data;$('statusDot').classList.add('ready');$('statusText').textContent='Processed HBX-4 and HBX-5 data loaded from the active Flight Project';
      renderSummary();renderTime();renderHeat();renderDiagnostics();showView(pathView(),false);
    }catch(error){$('statusText').textContent=`OPC view failed: ${error.message}`;$('statusText').style.color='var(--danger)';}
    finally{$('busy').classList.remove('show');}
  }
  $('refreshBtn').onclick=load;$('timeMetric').onchange=renderTime;$('heatSensor').onchange=renderHeat;$('heatLog').onchange=renderHeat;$('diagnosticMetric').onchange=renderDiagnostics;
  ['mapSensor','mapBin','mapPalette','mapLog'].forEach(id=>$(id).onchange=()=>{if(id==='mapSensor')fillBins();drawMap();});
  $('mapResetBtn').onclick=()=>{if(mapHome)opcMap.fitBounds(mapHome,{padding:[30,30]});};
  $('mapNewTabBtn').onclick=()=>window.open(mapPermalink(),'_blank','noopener');
  $('mapFullscreenBtn').onclick=async()=>{
    await $('opcMap').closest('.chart-card').requestFullscreen();
    setTimeout(()=>opcMap&&opcMap.invalidateSize(),150);
  };
  $('mapPdfBtn').onclick=exportMapPdf;
  document.querySelectorAll('[data-view]').forEach(a=>a.onclick=e=>{e.preventDefault();showView(a.dataset.view);});
  document.querySelectorAll('[data-fullscreen]').forEach(b=>b.onclick=async()=>{const card=$(b.dataset.fullscreen).closest('.chart-card');await card.requestFullscreen();setTimeout(resizePlots,120);});
  addEventListener('popstate',()=>showView(pathView(),false));addEventListener('resize',resizePlots);addEventListener('fullscreenchange',()=>setTimeout(resizePlots,120));load();

})();
