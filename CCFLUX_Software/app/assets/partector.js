(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const config = {responsive:true, displaylogo:false, scrollZoom:true, doubleClick:'reset', toImageButtonOptions:{format:'png', scale:2}};
  const labels = {
    number_cm3:['Particle number concentration','Number concentration [#/cm³]',0],
    ldsa_um2_cm3:['Lung-deposited surface area (LDSA)','LDSA [µm²/cm³]',2],
    mean_diameter_nm:['Mean particle diameter','Diameter [nm]',1],
    mass_ug_m3:['Reported PM0.3 mass','Mass concentration [µg/m³]',3],
    flow_lpm:['Sample flow','Flow [L/min]',3], temperature_c:['Instrument temperature','Temperature [°C]',2],
    rh_percent:['Instrument relative humidity','RH [%]',1], pressure_hpa:['Pressure','Pressure [hPa]',1],
    battery_v:['Battery','Voltage [V]',2], pump_current_ma:['Pump current','Current [mA]',1],
    size_integral_to_reported_ratio:['8-bin / reported number consistency','Ratio',3]
  };
  let payload = null;
  async function api(url){const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(`Request failed (${response.status})`);return response.json();}
  function numberAxis(digits){const axis={separatethousands:true,exponentformat:'none',showexponent:'none',automargin:true};if(Number.isInteger(digits))axis.tickformat=`,.${digits}f`;return axis;}
  function layout(title,ytitle,extra={}){
    const base={title:{text:title,x:.5,y:1,yanchor:'top',pad:{t:10},font:{size:18}},paper_bgcolor:'#f7fafc',plot_bgcolor:'#fff',font:{family:'Arial, sans-serif',size:14,color:'#172431'},margin:{l:96,r:50,t:96,b:76},xaxis:{title:'Recorded UTC',gridcolor:'#dce5ea',automargin:true},yaxis:{title:ytitle,gridcolor:'#dce5ea',...numberAxis()},legend:{orientation:'h',y:1.02,yanchor:'bottom',x:0},hovermode:'closest'};
    return {...base,...extra,xaxis:{...base.xaxis,...(extra.xaxis||{})},yaxis:{...base.yaxis,...(extra.yaxis||{})}};
  }
  function finiteExtent(values){const finite=(values||[]).map(Number).filter(Number.isFinite);return finite.length?[Math.min(...finite),Math.max(...finite)]:[0,1];}
  function sessionTraces(key,name,color='#0072B2',colorScale=null,digits=2){
    const series=payload.series,sessions=[...new Set((series.session||[]).filter(value=>value!==null))],traces=[];
    const [cmin,cmax]=finiteExtent(series[key]);
    sessions.forEach((session,index)=>{
      const x=[],y=[];
      series.session.forEach((value,i)=>{if(value===session){x.push(series.time[i]);y.push(series[key]?.[i]);}});
      const trace={type:'scattergl',mode:colorScale?'lines+markers':'lines',x,y,name,legendgroup:name,showlegend:index===0,line:{color:colorScale?'rgba(23,64,88,.42)':color,width:1.5},connectgaps:false,hovertemplate:`UTC %{x}<br>${name}: %{y:,.6f}<extra></extra>`};
      if(colorScale){trace.marker={size:5,color:y,colorscale:colorScale,cmin,cmax,showscale:index===0,colorbar:{title:name,tickformat:`,.${digits}f`,separatethousands:true,exponentformat:'none',thickness:18}};}
      traces.push(trace);
    });
    return traces;
  }
  function metricSummary(key){return payload.summary?.metrics_valid_records?.[key]||{};}
  function renderSummary(){
    const summary=payload.summary||{},number=metricSummary('number_cm3'),ldsa=metricSummary('ldsa_um2_cm3'),diameter=metricSummary('mean_diameter_nm');
    const card=(name,value,note='')=>`<div class="summary"><span>${name}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;
    const pass=100*Number(summary.selected_qc_pass_fraction||0);
    $('summaryGrid').innerHTML=[
      card('Selected logger rows',Number(summary.selected_rows||0).toLocaleString(),'Raw logger replication preserved'),
      card('Independent records',Number(summary.selected_independent_rows||0).toLocaleString(),'Scientific QC denominator'),
      card('QC-valid independent',Number(summary.selected_valid_rows||0).toLocaleString(),`${pass.toFixed(1)}% pass`),
      card('Logger replication rows',Number(summary.selected_logger_replication_rows||0).toLocaleString(),'Excluded only from independent-record plots'),
      card('Median number',Number(number.median||0).toLocaleString(undefined,{maximumFractionDigits:0})+' #/cm³'),
      card('Median LDSA',Number(ldsa.median||0).toLocaleString(undefined,{maximumFractionDigits:2})+' µm²/cm³'),
      card('Median diameter',Number(diameter.median||0).toLocaleString(undefined,{maximumFractionDigits:1})+' nm'),
      card('Measurement sessions',Number((payload.sessions||[]).length).toLocaleString(),'Clock resets and gaps preserved')
    ].join('');
  }
  function renderTime(){
    const key=$('timeMetric').value,[title,ytitle,digits]=labels[key];
    Plotly.react('timePlot',sessionTraces(key,title,'#0072B2','Turbo',digits),layout(`${title} · QC-valid records · colour indicates measured value`,ytitle,{yaxis:{...numberAxis(digits),title:ytitle,gridcolor:'#dce5ea'},margin:{l:100,r:118,t:96,b:76}}),config);
  }
  const format=value=>Number(value).toPrecision(3).replace(/\.?0+$/,'');
  // The distribution spans six decades and peaks at sixty times its own 99th
  // percentile, so colouring from the lowest to the highest value left the
  // whole flight in one shade. The range is taken from the values that carry
  // signal, and a channel that counted nothing is left empty rather than
  // sharing the lowest colour with one that counted a little.
  function heatBounds(z){
    const values=[];
    (z||[]).forEach(row=>(row||[]).forEach(value=>{
      const number=Number(value);
      if(Number.isFinite(number))values.push(number);
    }));
    if(!values.length)return {low:0,high:1,max:1,positiveLow:0};
    values.sort((a,b)=>a-b);
    const low=values[0],max=values[values.length-1];
    const positive=values.filter(value=>value>0);
    const at=(list,fraction)=>list[Math.min(list.length-1,Math.floor(list.length*fraction))];
    if(!positive.length||!(max>low))return {low,high:max>low?max:low+1,max,positiveLow:0};
    return {low,high:Math.max(at(positive,.99),positive[0]),max,positiveLow:at(positive,.01)};
  }
  function renderHeat(){
    const heat=payload.heatmap;
    const diameters=(heat.diameter_nm||[]).map(Number);
    const bounds=heatBounds(heat.z),logarithmic=$('heatLog').checked&&bounds.max>0;
    const trace={type:'heatmap',x:heat.time,y:diameters,colorscale:'Turbo',zauto:false,
      customdata:heat.z,
      hovertemplate:'UTC %{x}<br>Diameter %{y:.0f} nm<br>%{customdata:,.2f} #/cm³<extra></extra>'};
    if(logarithmic){
      const floor=bounds.positiveLow>0?bounds.positiveLow:bounds.max/1000;
      const ceiling=Math.max(bounds.max,floor*10);
      trace.z=heat.z.map(row=>(row||[]).map(value=>{
        const number=Number(value);
        return Number.isFinite(number)&&number>0?Math.log10(Math.min(Math.max(number,floor),ceiling)):null;
      }));
      trace.zmin=Math.log10(floor);trace.zmax=Math.log10(ceiling);
      const ticks=[];
      for(let power=Math.ceil(trace.zmin);power<=Math.floor(trace.zmax);power+=1)ticks.push(power);
      trace.colorbar={title:'dN/dlog₁₀(D)<br>[#/cm³]',tickmode:'array',tickvals:ticks,
        ticktext:ticks.map(power=>format(Math.pow(10,power)))};
    }else{
      trace.z=heat.z;trace.zmin=bounds.low;trace.zmax=bounds.high;
      trace.colorbar={title:'dN/dlog₁₀(D)<br>[#/cm³]',tickformat:',.0f',separatethousands:true,exponentformat:'none'};
    }
    const scaleNote=logarithmic?'logarithmic colour scale':'linear colour scale';
    const peakNote=!logarithmic&&bounds.high<bounds.max
      ? `; colour saturates at ${format(bounds.high)}, peak ${format(bounds.max)}`
      : `; peak ${format(bounds.max)}`;
    Plotly.react('heatPlot',[trace],layout(`Particle number size distribution · ${scaleNote}${peakNote}`,'Particle diameter [nm]',{yaxis:{title:'Particle diameter [nm]',type:'log',tickmode:'array',tickvals:diameters,ticktext:diameters.map(value=>value.toLocaleString()),gridcolor:'#dce5ea',automargin:true},margin:{l:110,r:126,t:96,b:76}}),config);
  }
  function renderBands(){
    const entries=[['n_10_30_cm3','10–30 nm','#0072B2'],['n_30_50_cm3','30–50 nm','#009E73'],['n_50_100_cm3','50–100 nm','#E69F00'],['n_100_300_cm3','100–300 nm','#D55E00']];
    Plotly.react('bandsPlot',entries.flatMap(([key,name,color])=>sessionTraces(key,name,color)),layout('Logarithmically integrated number-size bands','Number concentration [#/cm³]',{yaxis:{title:'Number concentration [#/cm³]',type:'log',gridcolor:'#dce5ea',...numberAxis(0)}}),config);
  }
  function renderHouse(){const key=$('houseMetric').value,[title,ytitle,digits]=labels[key];Plotly.react('housePlot',sessionTraces(key,title,'#7B2CBF'),layout(`${title} · QC-valid records`,ytitle,{yaxis:{title:ytitle,gridcolor:'#dce5ea',...numberAxis(digits)}}),config);}
  const sizeMap=window.CCFLUX.createSizeMap({
    dataUrl:'/api/partector/map', exportUrl:'/api/partector/map/export',
    viewPath:'/partector/map', ids:{map:'partectorMap', channel:'mapChannel'}
  });
  const VIEW_PATHS={size:'size-distribution',housekeeping:'housekeeping',map:'map',overview:'overview'};
  function showView(view,push=true){
    document.querySelectorAll('[data-section]').forEach(card=>card.hidden=card.dataset.section!==view);
    document.querySelectorAll('[data-view]').forEach(link=>link.classList.toggle('active',link.dataset.view===view));
    if(push)history.pushState({view},'',`/partector/${VIEW_PATHS[view]||'overview'}`);
    setTimeout(resizePlots,50);
    // Leaflet measures its container, so the map can only lay out once the
    // card is on screen; one built while hidden renders as grey tiles.
    if(view==='map')setTimeout(()=>sizeMap.show(),60);
  }
  function pathView(){
    const path=location.pathname;
    return path.includes('size-distribution')?'size'
      :path.includes('housekeeping')?'housekeeping'
      :path.endsWith('/map')?'map':'overview';
  }
  function resizePlots(){document.querySelectorAll('.js-plotly-plot').forEach(plot=>Plotly.Plots.resize(plot));}
  async function load(){
    $('busy').classList.add('show');
    try{
      const response=await api('/api/partector');$('flightName').textContent=response.flight_id||'No project';
      if(!response.ready){$('statusText').textContent=response.message||'Partector products are not ready';document.querySelector('main').innerHTML=`<div class="empty"><div><strong>Partector processing is required</strong>${response.message||''}</div></div>`;return;}
      payload=response.data;$('statusDot').classList.add('ready');$('statusText').textContent='Processed Partector data loaded from the active Flight Project';
      renderSummary();renderTime();renderHeat();renderBands();renderHouse();showView(pathView(),false);
    }catch(error){$('statusText').textContent=`Partector view failed: ${error.message}`;$('statusText').style.color='var(--danger)';}
    finally{$('busy').classList.remove('show');}
  }
  $('refreshBtn').onclick=load;$('timeMetric').onchange=renderTime;$('houseMetric').onchange=renderHouse;
  $('heatLog').onchange=renderHeat;
  ['mapChannel','mapPalette','mapLog'].forEach(id=>$(id).onchange=()=>sizeMap.draw());
  ['mapResetBtn','mapResetTopBtn'].forEach(id=>$(id).onclick=()=>sizeMap.resetPosition());
  $('mapUpdateBtn').onclick=()=>sizeMap.show();
  $('mapNewTabBtn').onclick=()=>window.open(sizeMap.permalink(),'_blank','noopener');
  $('mapFullscreenBtn').onclick=async()=>{
    await $('partectorMap').closest('.chart-card').requestFullscreen();
    setTimeout(()=>sizeMap.invalidate(),150);
  };
  $('mapPdfBtn').onclick=event=>sizeMap.exportPdf(event.currentTarget);
  document.querySelectorAll('[data-view]').forEach(link=>link.onclick=event=>{event.preventDefault();showView(link.dataset.view);});
  document.querySelectorAll('[data-fullscreen]').forEach(button=>button.onclick=async()=>{await $(button.dataset.fullscreen).closest('.chart-card').requestFullscreen();setTimeout(resizePlots,120);});
  addEventListener('popstate',()=>showView(pathView(),false));addEventListener('resize',resizePlots);addEventListener('fullscreenchange',()=>setTimeout(resizePlots,120));load();

})();
