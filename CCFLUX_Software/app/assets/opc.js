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
  function layout(title,ytitle,extra={}){return {title:{text:title,x:.5,font:{size:18}},paper_bgcolor:'#f7fafc',plot_bgcolor:'#fff',font:{family:'Arial, sans-serif',size:14,color:'#172431'},margin:{l:82,r:42,t:66,b:70},xaxis:{title:'Recorded UTC',gridcolor:'#dce5ea',rangeslider:{visible:false}},yaxis:{title:ytitle,gridcolor:'#dce5ea',zerolinecolor:'#b9c7cf'},legend:{orientation:'h',y:1.13,x:0},hovermode:'closest',...extra};}
  function dualAxisLayout(title,ytitle){return layout(title,ytitle,{margin:{l:92,r:104,t:68,b:70},hovermode:'x unified',yaxis:{title:{text:`HBX-4 · ${ytitle}`,font:{color:colors.hbx4}},tickfont:{color:colors.hbx4},gridcolor:'#dce5ea',zerolinecolor:'#b9c7cf',automargin:true,fixedrange:false},yaxis2:{title:{text:`HBX-5 · ${ytitle}`,font:{color:colors.hbx5}},tickfont:{color:colors.hbx5},overlaying:'y',side:'right',showgrid:false,zeroline:false,automargin:true,fixedrange:false}});}
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
    Plotly.react('heatPlot',[{type:'heatmap',x:h.time,y:h.bin_index,z:h.z,colorscale:'Viridis',colorbar:{title:'#/cm³'},hovertemplate:'UTC %{x}<br>Bin %{y}<br>%{z:.4g} #/cm³<extra></extra>'}],layout(`${sensor.label} · bin-resolved concentration`,'OPC-N3 software bin index',{yaxis:{title:'OPC-N3 software bin index',dtick:1},margin:{l:88,r:70,t:62,b:70}}),config);
  }
  function renderDiagnostics(){
    const key=$('diagnosticMetric').value,[title,ytitle]=labels[key];
    Plotly.react('diagnosticPlot',independentTraces(key),dualAxisLayout(`${title} · independent recorded scales`,ytitle),config);
  }
  function showView(view,push=true){
    document.querySelectorAll('[data-section]').forEach(card=>card.hidden=card.dataset.section!==view);
    document.querySelectorAll('[data-view]').forEach(link=>link.classList.toggle('active',link.dataset.view===view));
    if(push)history.pushState({view},'',`/opc/${view==='size'?'size-distribution':view==='diagnostics'?'diagnostics':'overview'}`);
    setTimeout(resizePlots,50);
  }
  function pathView(){return location.pathname.includes('size-distribution')?'size':location.pathname.includes('diagnostics')?'diagnostics':'overview';}
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
  $('refreshBtn').onclick=load;$('timeMetric').onchange=renderTime;$('heatSensor').onchange=renderHeat;$('diagnosticMetric').onchange=renderDiagnostics;
  document.querySelectorAll('[data-view]').forEach(a=>a.onclick=e=>{e.preventDefault();showView(a.dataset.view);});
  document.querySelectorAll('[data-fullscreen]').forEach(b=>b.onclick=async()=>{const card=$(b.dataset.fullscreen).closest('.chart-card');await card.requestFullscreen();setTimeout(resizePlots,120);});
  addEventListener('popstate',()=>showView(pathView(),false));addEventListener('resize',resizePlots);addEventListener('fullscreenchange',()=>setTimeout(resizePlots,120));load();
})();
