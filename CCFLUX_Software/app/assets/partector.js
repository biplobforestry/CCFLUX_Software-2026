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
    const base={title:{text:title,x:.5,font:{size:18}},paper_bgcolor:'#f7fafc',plot_bgcolor:'#fff',font:{family:'Arial, sans-serif',size:14,color:'#172431'},margin:{l:96,r:50,t:68,b:76},xaxis:{title:'Recorded UTC',gridcolor:'#dce5ea',automargin:true},yaxis:{title:ytitle,gridcolor:'#dce5ea',...numberAxis()},legend:{orientation:'h',y:1.13,x:0},hovermode:'closest'};
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
    Plotly.react('timePlot',sessionTraces(key,title,'#0072B2','Turbo',digits),layout(`${title} · QC-valid records · colour indicates measured value`,ytitle,{yaxis:{...numberAxis(digits),title:ytitle,gridcolor:'#dce5ea'},margin:{l:100,r:118,t:68,b:76}}),config);
  }
  function renderHeat(){
    const heat=payload.heatmap;
    const diameters=(heat.diameter_nm||[]).map(Number);
    Plotly.react('heatPlot',[{type:'heatmap',x:heat.time,y:diameters,z:heat.z,colorscale:'Turbo',colorbar:{title:'dN/dlog₁₀(D)<br>[#/cm³]',tickformat:',.0f',separatethousands:true,exponentformat:'none'},hovertemplate:'UTC %{x}<br>Diameter %{y:.0f} nm<br>%{z:,.2f} #/cm³<extra></extra>'}],layout('Particle number size distribution','Particle diameter [nm]',{yaxis:{title:'Particle diameter [nm]',type:'log',tickmode:'array',tickvals:diameters,ticktext:diameters.map(value=>value.toLocaleString()),gridcolor:'#dce5ea',automargin:true},margin:{l:110,r:120,t:68,b:76}}),config);
  }
  function renderBands(){
    const entries=[['n_10_30_cm3','10–30 nm','#0072B2'],['n_30_50_cm3','30–50 nm','#009E73'],['n_50_100_cm3','50–100 nm','#E69F00'],['n_100_300_cm3','100–300 nm','#D55E00']];
    Plotly.react('bandsPlot',entries.flatMap(([key,name,color])=>sessionTraces(key,name,color)),layout('Logarithmically integrated number-size bands','Number concentration [#/cm³]',{yaxis:{title:'Number concentration [#/cm³]',type:'log',gridcolor:'#dce5ea',...numberAxis(0)}}),config);
  }
  function renderHouse(){const key=$('houseMetric').value,[title,ytitle,digits]=labels[key];Plotly.react('housePlot',sessionTraces(key,title,'#7B2CBF'),layout(`${title} · QC-valid records`,ytitle,{yaxis:{title:ytitle,gridcolor:'#dce5ea',...numberAxis(digits)}}),config);}
  function showView(view,push=true){document.querySelectorAll('[data-section]').forEach(card=>card.hidden=card.dataset.section!==view);document.querySelectorAll('[data-view]').forEach(link=>link.classList.toggle('active',link.dataset.view===view));if(push)history.pushState({view},'',`/partector/${view==='size'?'size-distribution':view==='housekeeping'?'housekeeping':'overview'}`);setTimeout(resizePlots,50);}
  function pathView(){return location.pathname.includes('size-distribution')?'size':location.pathname.includes('housekeeping')?'housekeeping':'overview';}
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
  document.querySelectorAll('[data-view]').forEach(link=>link.onclick=event=>{event.preventDefault();showView(link.dataset.view);});
  document.querySelectorAll('[data-fullscreen]').forEach(button=>button.onclick=async()=>{await $(button.dataset.fullscreen).closest('.chart-card').requestFullscreen();setTimeout(resizePlots,120);});
  addEventListener('popstate',()=>showView(pathView(),false));addEventListener('resize',resizePlots);addEventListener('fullscreenchange',()=>setTimeout(resizePlots,120));load();
})();
