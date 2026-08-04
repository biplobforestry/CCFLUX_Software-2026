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

  const sizeMap=window.CCFLUX.createSizeMap({
    dataUrl:'/api/opc/map', exportUrl:'/api/opc/map/export', viewPath:'/opc/map',
    ids:{map:'opcMap', channel:'mapChannel'}
  });
  const VIEW_PATHS={size:'size-distribution',diagnostics:'diagnostics',map:'map',overview:'overview'};
  function showView(view,push=true){
    document.querySelectorAll('[data-section]').forEach(card=>card.hidden=card.dataset.section!==view);
    document.querySelectorAll('[data-view]').forEach(link=>link.classList.toggle('active',link.dataset.view===view));
    if(push)history.pushState({view},'',`/opc/${VIEW_PATHS[view]||'overview'}`);
    setTimeout(resizePlots,50);
    // Leaflet measures the container, so it can only lay out once the card is
    // on screen; a map built while its card was hidden renders as grey tiles.
    if(view==='map')setTimeout(()=>sizeMap.show(),60);
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
  $('mapSensor').onchange=()=>sizeMap.onSensorChange();
  ['mapChannel','mapPalette','mapLog'].forEach(id=>$(id).onchange=()=>sizeMap.draw());
  $('mapResetBtn').onclick=()=>sizeMap.resetPosition();
  $('mapNewTabBtn').onclick=()=>window.open(sizeMap.permalink(),'_blank','noopener');
  $('mapFullscreenBtn').onclick=async()=>{
    await $('opcMap').closest('.chart-card').requestFullscreen();
    setTimeout(()=>sizeMap.invalidate(),150);
  };
  $('mapPdfBtn').onclick=event=>sizeMap.exportPdf(event.currentTarget);
  document.querySelectorAll('[data-view]').forEach(a=>a.onclick=e=>{e.preventDefault();showView(a.dataset.view);});
  document.querySelectorAll('[data-fullscreen]').forEach(b=>b.onclick=async()=>{const card=$(b.dataset.fullscreen).closest('.chart-card');await card.requestFullscreen();setTimeout(resizePlots,120);});
  addEventListener('popstate',()=>showView(pathView(),false));addEventListener('resize',resizePlots);addEventListener('fullscreenchange',()=>setTimeout(resizePlots,120));load();

})();
