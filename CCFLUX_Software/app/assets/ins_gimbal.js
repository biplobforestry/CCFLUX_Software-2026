(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const config = {responsive:true,displaylogo:false,scrollZoom:true,doubleClick:'reset',toImageButtonOptions:{format:'png',scale:2}};
  const colours = {x:'#0072B2',y:'#D55E00',z:'#009E73',norm:'#111827',rms:'#CC79A7'};
  let payload = null;
  async function api(url){const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(`Request failed (${response.status})`);return response.json();}
  function numericAxis(digits=3){return {separatethousands:true,exponentformat:'none',showexponent:'none',tickformat:`,.${digits}f`,automargin:true};}
  function layout(title,yTitle,extra={}){
    const base={title:{text:title,x:.5,font:{size:18}},paper_bgcolor:'#f7fafc',plot_bgcolor:'#fff',font:{family:'Arial, sans-serif',size:14,color:'#172431'},margin:{l:98,r:52,t:68,b:78},xaxis:{title:'Recorded UTC',gridcolor:'#dce5ea',automargin:true},yaxis:{title:yTitle,gridcolor:'#dce5ea',...numericAxis(3)},legend:{orientation:'h',y:1.13,x:0},hovermode:'closest'};
    return {...base,...extra,xaxis:{...base.xaxis,...(extra.xaxis||{})},yaxis:{...base.yaxis,...(extra.yaxis||{})}};
  }
  function sessionTraces(key,name,color,width=1.2,dash='solid'){
    const series=payload.series,sessions=[...new Set((series.session||[]).filter(value=>value!==null))],traces=[];
    sessions.forEach((session,index)=>{const x=[],y=[];series.session.forEach((value,i)=>{if(value===session){x.push(series.time[i]);y.push(series[key]?.[i]);}});traces.push({type:'scattergl',mode:'lines',x,y,name,legendgroup:name,showlegend:index===0,line:{color,width,dash},connectgaps:false,hovertemplate:`UTC %{x}<br>${name}: %{y:,.6f}<extra></extra>`});});
    return traces;
  }
  function summaryCard(name,value,note=''){return `<div class="summary"><span>${name}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;}
  function valueOrDash(value,digits=3){const number=Number(value);return Number.isFinite(number)?number.toLocaleString(undefined,{maximumFractionDigits:digits}):'—';}
  function renderSummary(){
    const summary=payload.summary||{},dataset=summary.dataset||{},sampling=summary.sampling||{},metrics=summary.metrics||{};
    $('summaryGrid').innerHTML=[
      summaryCard('Evaluated rows',Number(dataset.rows_evaluated||0).toLocaleString(),'Full-resolution export retained'),
      summaryCard('Flight interval',`${valueOrDash(dataset.elapsed_hours,2)} h`,'Selected main-GUI UTC interval'),
      summaryCard('Logger rate',`${valueOrDash(sampling.median_logger_rate_hz,3)} Hz`,'Median positive recorded-time rate'),
      summaryCard('IMU update rate',`${valueOrDash(sampling.median_imu_update_rate_hz,3)} Hz`,'Changed RAW_IMU states'),
      summaryCard('Acceleration RMS',`${valueOrDash(metrics.unfiltered_acceleration_deviation_rms_g,5)} g`,'Unfiltered |a| − 1 g'),
      summaryCard('Angular-rate RMS',`${valueOrDash(metrics.unfiltered_angular_rate_rms_dps,4)} deg/s`,'Unfiltered vector magnitude'),
      summaryCard('Maneuver fraction',`${valueOrDash(100*Number(metrics.maneuver_fraction),2)}%`,'Configured angular-rate threshold')
    ].join('');
  }
  function renderAcceleration(){
    const traces=[...sessionTraces('acc_x_g','X',colours.x),...sessionTraces('acc_y_g','Y',colours.y),...sessionTraces('acc_z_g','Z',colours.z),...sessionTraces('acc_norm_g','Norm',colours.norm,1.5)];
    Plotly.react('accelerationPlot',traces,layout('All recorded RAW_IMU acceleration','Acceleration [g]'),config);
  }
  function renderAngularRate(){
    const threshold=Number(payload.summary?.configuration?.maneuver_threshold_dps||10);
    const traces=[...sessionTraces('gyro_x_dps','X',colours.x),...sessionTraces('gyro_y_dps','Y',colours.y),...sessionTraces('gyro_z_dps','Z',colours.z),...sessionTraces('gyro_norm_dps','Norm',colours.norm,1.5)];
    const plotLayout=layout('All recorded RAW_IMU angular rate','Angular rate [deg/s]',{shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:threshold,y1:threshold,line:{color:colours.rms,width:2,dash:'dash'}}],annotations:[{xref:'paper',x:1,y:threshold,text:`Motion flag ${threshold.toLocaleString()} deg/s`,showarrow:false,xanchor:'right',yshift:12,font:{color:colours.rms}}]});
    Plotly.react('angularRatePlot',traces,plotLayout,config);
  }
  function renderMotion(){
    const seconds=Number(payload.summary?.configuration?.rms_seconds||30);
    Plotly.react('accelerationMotionPlot',[...sessionTraces('acc_deviation_g','|a| − 1 g (unfiltered)',colours.x),...sessionTraces('acc_rms_g',`${seconds.toLocaleString()} s RMS`,colours.y,2)],layout('Unfiltered acceleration deviation','Acceleration [g]'),config);
    Plotly.react('angularMotionPlot',[...sessionTraces('gyro_norm_dps','Gyro magnitude (unfiltered)',colours.z),...sessionTraces('gyro_rms_dps',`${seconds.toLocaleString()} s RMS`,colours.y,2)],layout('Unfiltered angular motion','Angular rate [deg/s]'),config);
  }
  function renderSpectrogram(){
    const spectrogram=payload.spectrogram||{},limits=spectrogram.color_limits_db||[null,null],traces=[];
    (spectrogram.sessions||[]).forEach((session,index)=>traces.push({type:'heatmap',x:session.time,y:session.frequency_hz,z:session.power_db_g2_hz,colorscale:'Viridis',zmin:limits[0],zmax:limits[1],showscale:index===0,colorbar:{title:'Acceleration PSD<br>[dB re g²/Hz]',tickformat:',.1f',exponentformat:'none'},hovertemplate:'UTC %{x}<br>Frequency %{y:,.3f} Hz<br>PSD %{z:,.2f} dB re g²/Hz<extra></extra>'}));
    const nyquist=Number(payload.summary?.sampling?.effective_update_nyquist_hz);
    const extra=Number.isFinite(nyquist)?{shapes:[{type:'line',xref:'paper',x0:0,x1:1,y0:nyquist,y1:nyquist,line:{color:'#ffffff',width:2,dash:'dash'}}],annotations:[{xref:'paper',x:1,y:nyquist,text:`Effective update Nyquist ${nyquist.toLocaleString(undefined,{maximumFractionDigits:3})} Hz`,showarrow:false,xanchor:'right',yshift:12,font:{color:'#172431',size:13},bgcolor:'#ffffffcc'}]}:{};
    Plotly.react('spectrogramPlot',traces,layout('Acceleration spectrogram · unfiltered input','Frequency [Hz]',{margin:{l:100,r:132,t:68,b:78},...extra}),config);
  }
  function renderAsd(){
    const acceleration=payload.asd?.acceleration||{},angular=payload.asd?.angular_rate||{},nyquist=Number(payload.summary?.sampling?.effective_update_nyquist_hz);
    const traces=[
      {type:'scatter',mode:'lines',x:acceleration.frequency_hz,y:acceleration.amplitude_g_sqrt_hz,name:'Acceleration ASD',line:{color:colours.x,width:2},hovertemplate:'Frequency %{x:,.4f} Hz<br>Acceleration ASD %{y:.6e} g/√Hz<extra></extra>'},
      {type:'scatter',mode:'lines',x:angular.frequency_hz,y:angular.amplitude_dps_sqrt_hz,name:'Angular-rate ASD',yaxis:'y2',line:{color:colours.y,width:2},hovertemplate:'Frequency %{x:,.4f} Hz<br>Angular-rate ASD %{y:.6e} (deg/s)/√Hz<extra></extra>'}
    ];
    const shapes=Number.isFinite(nyquist)?[{type:'line',x0:nyquist,x1:nyquist,yref:'paper',y0:0,y1:1,line:{color:'#555',dash:'dash',width:1.5}}]:[];
    Plotly.react('asdPlot',traces,layout('Welch amplitude spectral density · Duration-weighted spectra use every acquisition session','Acceleration ASD [g/√Hz]',{xaxis:{title:'Frequency [Hz]',gridcolor:'#dce5ea',...numericAxis(3)},yaxis:{title:'Acceleration ASD [g/√Hz]',type:'log',gridcolor:'#dce5ea',tickformat:'.3e',automargin:true},yaxis2:{title:'Angular-rate ASD [(deg/s)/√Hz]',type:'log',overlaying:'y',side:'right',tickformat:'.3e',automargin:true,gridcolor:'rgba(0,0,0,0)'},margin:{l:112,r:126,t:68,b:78},shapes}),config);
  }
  function pathView(){if(location.pathname.includes('/motion'))return'motion';if(location.pathname.includes('/frequency'))return'frequency';return'overview';}
  function showView(view,push=true){document.querySelectorAll('[data-section]').forEach(card=>card.hidden=card.dataset.section!==view);document.querySelectorAll('[data-view]').forEach(link=>link.classList.toggle('active',link.dataset.view===view));if(push)history.pushState({view},'',`/ins_gimbal/${view}`);setTimeout(resizePlots,60);}
  function resizePlots(){document.querySelectorAll('.js-plotly-plot').forEach(plot=>Plotly.Plots.resize(plot));}
  async function load(){
    $('busy').classList.add('show');
    try{
      const response=await api('/api/ins-gimbal');$('flightName').textContent=response.flight_id||'No project';
      if(!response.ready){$('statusText').textContent=response.message||'INS Gimbal products are not ready';document.querySelector('main').innerHTML=`<div class="empty"><div><strong>INS Gimbal processing is required</strong>${response.message||''}</div></div>`;return;}
      payload=response.data;$('statusDot').classList.add('ready');$('statusText').textContent='Processed INS Gimbal data loaded from the active Flight Project';
      renderSummary();renderAcceleration();renderAngularRate();renderMotion();renderSpectrogram();renderAsd();
      if(location.pathname.includes('/quality'))history.replaceState({view:'overview'},'', '/ins_gimbal/overview');
      showView(pathView(),false);
    }catch(error){$('statusText').textContent=`INS Gimbal view failed: ${error.message}`;$('statusText').style.color='var(--danger)';}
    finally{$('busy').classList.remove('show');}
  }
  $('refreshBtn').onclick=load;
  document.querySelectorAll('[data-view]').forEach(link=>link.onclick=event=>{event.preventDefault();showView(link.dataset.view);});
  document.querySelectorAll('[data-fullscreen]').forEach(button=>button.onclick=async()=>{await $(button.dataset.fullscreen).closest('.chart-card').requestFullscreen();setTimeout(resizePlots,120);});
  addEventListener('popstate',()=>showView(pathView(),false));addEventListener('resize',resizePlots);addEventListener('fullscreenchange',()=>setTimeout(resizePlots,120));load();

})();
