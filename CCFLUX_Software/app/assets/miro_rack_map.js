(() => {
  'use strict';
  const existingPalettes = [
    ['#30123b','#3b63e8','#2ec7c9','#6ce34f','#f6c445','#e7352d'],
    ['#0b1d78','#1464b8','#45b3c8','#d5e56b','#f2983d','#8f1d2c'],
    ['#211a52','#7b2cbf','#df4d9f','#ff9f68','#f9e55c'],
    ['#003f5c','#2f4b7c','#665191','#a05195','#d45087','#f95d6a','#ffa600']
  ];
  const namedPalettes = {
    cool:['#00ffff','#33ccff','#6699ff','#9966ff','#cc33ff','#ff00ff'],
    YlOrRd:['#ffffcc','#ffeda0','#fed976','#feb24c','#fd8d3c','#fc4e2a','#e31a1c','#b10026'],
    viridis:['#440154','#482878','#3e4989','#31688e','#26828e','#1f9e89','#35b779','#6ece58','#b5de2b','#fde725'],
    plasma:['#0d0887','#5b02a3','#9a179b','#cb4679','#ed7953','#fb9f3a','#fdca26','#f0f921']
  };
  let payload, map, rendered = [], fitted = false, redrawTimer = null;
  let mapHomeBounds = null;
  let flightTrackLayer = null, flightTrackEnabled = true;
  let currentView = {selected:[], groupStyles:new Map()};
  let updateGeneration = 0, busyOwner = null, zoomTimer = null;
  const selections = [...document.querySelectorAll('.selection')];
  const byId = id => document.getElementById(id);
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const canonicalGas = value => String(value).replace(/\s+(wet|dry|raw|sync)$/i,'').trim().toUpperCase();
  const unitFor = item => String(payload.units?.[item.instrument]?.[item.gas] || 'unknown unit');
  const groupKey = item => `${canonicalGas(item.gas)}|${unitFor(item)}|${item.palette}`;
  const finite = value => Number.isFinite(Number(value));
  const clampNumber = (value, low, high, fallback) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(low, Math.min(high, parsed)) : fallback;
  };

  function openBusy(owner, title, message) {
    busyOwner = owner;
    byId('mapBusyTitle').textContent = title;
    byId('mapBusyMessage').textContent = message;
    const dialog = byId('mapBusyDialog');
    if (!dialog.open) dialog.show();
  }
  function closeBusy(owner) {
    if (busyOwner !== owner) return;
    busyOwner = null;
    const dialog = byId('mapBusyDialog');
    if (dialog.open) dialog.close();
  }

  function paletteFor(name, index) {
    return namedPalettes[name] || existingPalettes[index % existingPalettes.length];
  }
  function paletteLabel(name) {
    return name === 'default' ? 'Default' : name;
  }
  function color(value, low, high, palette) {
    const position = Math.max(0, Math.min(1, (Number(value)-low)/Math.max(high-low,1e-12))) * (palette.length-1);
    const left = Math.floor(position), right = Math.min(palette.length-1,left+1), mix = position-left;
    const a = palette[left].match(/\w\w/g).map(v=>parseInt(v,16));
    const b = palette[right].match(/\w\w/g).map(v=>parseInt(v,16));
    const rgb = a.map((v,i)=>Math.round(v*(1-mix)+b[i]*mix));
    return `rgb(${rgb.join(',')})`;
  }

  function offsetTrack(points, metres) {
    const numeric = points.map(point => ({
      ...point, lat:Number(point.lat), lon:Number(point.lon)
    }));
    if (Math.abs(metres) < .001) return numeric;
    return numeric.map((point,index) => {
      const previous = numeric[Math.max(0,index-1)];
      const next = numeric[Math.min(numeric.length-1,index+1)];
      const meanLatitude = (previous.lat + next.lat) * Math.PI / 360;
      const metresPerLongitudeDegree = Math.max(1,111320 * Math.cos(meanLatitude));
      const east = (next.lon-previous.lon) * metresPerLongitudeDegree;
      const north = (next.lat-previous.lat) * 110540;
      const length = Math.hypot(east,north);
      if (!(length > .001)) return point;
      const offsetEast = -north / length * metres;
      const offsetNorth = east / length * metres;
      return {
        ...point,
        lat:point.lat + offsetNorth / 110540,
        lon:point.lon + offsetEast / metresPerLongitudeDegree
      };
    });
  }

  function flightTrackPoints(selected = []) {
    const prepared = (payload.flight_track || []).filter(point => finite(point.lat) && finite(point.lon));
    if (prepared.length > 1) return prepared.map(point => ({...point,lat:Number(point.lat),lon:Number(point.lon)}));
    for (const item of selected) {
      const records = (payload.layers[item.instrument]?.[item.gas] || []).filter(point => finite(point.lat) && finite(point.lon));
      if (records.length > 1) return records.map(point => ({...point,lat:Number(point.lat),lon:Number(point.lon)}));
    }
    return [];
  }

  function gasOptions(selection) {
    const instrument = selection.querySelector('.instrument').value;
    const gas = selection.querySelector('.gas');
    const previous = gas.value;
    gas.innerHTML = '';
    (payload.gases[instrument] || []).forEach(value => {
      const option = document.createElement('option');
      option.value = value; option.textContent = value; gas.appendChild(option);
    });
    if ([...gas.options].some(option => option.value === previous)) gas.value = previous;
  }

  async function load() {
    openBusy('load', 'Opening saved Mapview', 'Reading the map product prepared during main processing.');
    try {
      const response = await fetch('/api/miro-rack/map/data', {cache:'no-store'});
      payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Map data unavailable');
      selections.forEach((selection,index) => {
        if (index === 1) selection.querySelector('.instrument').value = 'Picarro';
        gasOptions(selection);
        selection.querySelector('.instrument').addEventListener('change', () => {
          gasOptions(selection);
          requestUpdate('Changing instrument and trace-gas layer');
        });
        selection.querySelector('.gas').addEventListener('change', () => requestUpdate('Changing trace-gas compound'));
        selection.querySelector('.enabled').addEventListener('change', () => requestUpdate('Updating selected map layers'));
        selection.querySelector('.palette').addEventListener('change', () => requestUpdate('Applying the selected colour scale'));
        selection.querySelector('.layer-width').addEventListener('input', () => requestUpdate('Applying an individual track width'));
        selection.querySelector('.layer-offset').addEventListener('input', () => requestUpdate('Applying an individual lateral track offset'));
      });
      map = L.map('map', {zoomControl:true, preferCanvas:true}).setView([47.6,9.3],10);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom:19, attribution:'&copy; OpenStreetMap contributors', updateWhenZooming:false,
        crossOrigin:true
      }).addTo(map);
      map.on('zoomstart', () => {
        clearTimeout(zoomTimer);
        if (!busyOwner) openBusy('zoom', 'Updating map view', 'Applying the requested zoom level.');
      });
      map.on('zoomend', () => {
        clearTimeout(zoomTimer);
        zoomTimer = setTimeout(() => closeBusy('zoom'), 120);
      });
      closeBusy('load');
      await update('Rendering the saved trace-gas map');
      fetch('/api/miro-rack/log', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:'MIRO Rack Mapview opened from saved map product'})}).catch(()=>{});
    } catch (error) {
      closeBusy('load');
      show(error.message);
    }
  }

  function selectedLayers() {
    return selections.map((selection,index) => ({
      index,
      enabled: selection.querySelector('.enabled').checked,
      instrument: selection.querySelector('.instrument').value,
      gas: selection.querySelector('.gas').value,
      width:clampNumber(selection.querySelector('.layer-width').value,1,16,5),
      offsetMetres:clampNumber(selection.querySelector('.layer-offset').value,-100,100,0),
      palette:selection.querySelector('.palette').value || 'default'
    })).filter(item => item.enabled && item.gas);
  }

  function requestUpdate(reason) {
    clearTimeout(redrawTimer);
    openBusy('update', 'Refreshing trace-gas map', reason || 'Updating the selected scientific layers.');
    redrawTimer = setTimeout(() => update(reason), 110);
  }

  async function update(reason) {
    if (!map || !payload) return;
    const generation = ++updateGeneration;
    openBusy('update', 'Refreshing trace-gas map', reason || 'Updating the selected scientific layers.');
    await nextFrame();
    rendered.forEach(layer => layer.removeFrom(map)); rendered = [];
    flightTrackLayer = null;
    byId('legends').innerHTML = '';
    const selected = selectedLayers();
    if (!selected.length) {
      closeBusy('update');
      show('Enable at least one instrument and trace-gas layer.');
      return;
    }
    hide();
    const groups = [...new Set(selected.map(groupKey))];
    const groupStyles = new Map();
    currentView = {selected,groupStyles};
    groups.forEach((group,index) => {
      const members = selected.filter(item => groupKey(item) === group);
      const allValues = members
        .flatMap(item => payload.layers[item.instrument]?.[item.gas] || [])
        .map(point => Number(point.value)).filter(Number.isFinite).sort((a,b)=>a-b);
      if (!allValues.length) return;
      const low = allValues[Math.floor((allValues.length-1)*.02)];
      let high = allValues[Math.ceil((allValues.length-1)*.98)];
      if (!(high > low)) high = low + Math.max(Math.abs(low)*.01,1e-9);
      const palette = paletteFor(members[0].palette,index);
      groupStyles.set(group,{low,high,palette,index});
      const [gasName,unit,paletteName] = group.split('|');
      addLegend(gasName,unit,low,high,palette,
        members.map(item=>`${item.instrument}: ${item.gas}; ${item.width}px; ${item.offsetMetres}m`).join(' &middot; '),
        paletteLabel(paletteName));
    });
    const bounds = [];
    const canvasRenderer = L.canvas({padding:.5,tolerance:7});
    for (let layerIndex=0; layerIndex<selected.length; layerIndex+=1) {
      if (generation !== updateGeneration) return;
      const item = selected[layerIndex];
      const points = (payload.layers[item.instrument]?.[item.gas] || []).filter(point => finite(point.lat) && finite(point.lon) && finite(point.value));
      const style = groupStyles.get(groupKey(item));
      if (!style || points.length < 2) continue;
      const shifted = offsetTrack(points,item.offsetMetres);
      shifted.forEach(point=>bounds.push([point.lat,point.lon]));
      const segments = [];
      for (let index=0; index<shifted.length-1; index+=1) {
        const a=shifted[index], b=shifted[index+1], value=(Number(a.value)+Number(b.value))/2;
        const tooltip = `<b>${item.instrument} &middot; ${item.gas}</b><br>${value.toPrecision(6)} ${unitFor(item)}<br>Width: ${item.width}px; visual offset: ${item.offsetMetres}m<br>${a.time}`;
        segments.push(L.polyline([[a.lat,a.lon],[b.lat,b.lon]],{
          renderer:canvasRenderer,color:color(value,style.low,style.high,style.palette),weight:item.width,opacity:.9,lineCap:'round'
        }).bindTooltip(tooltip,{sticky:true}));
        if (index && index % 240 === 0) {
          byId('mapBusyMessage').textContent = `${item.instrument} ${item.gas}: ${index.toLocaleString()} of ${shifted.length.toLocaleString()} samples`;
          await nextFrame();
          if (generation !== updateGeneration) return;
        }
      }
      const layer=L.layerGroup(segments).addTo(map); rendered.push(layer);
      await nextFrame();
    }
    const route = flightTrackPoints(selected);
    if (flightTrackEnabled && route.length > 1) {
      route.forEach(point=>bounds.push([point.lat,point.lon]));
      flightTrackLayer = L.polyline(route.map(point=>[point.lat,point.lon]),{
        renderer:canvasRenderer,color:'#050505',weight:2.5,opacity:.95,
        dashArray:'2 8',lineCap:'round',interactive:false
      }).addTo(map);
      rendered.push(flightTrackLayer);
      flightTrackLayer.bringToFront();
    }
    mapHomeBounds = bounds.length ? L.latLngBounds(bounds) : null;
    if (mapHomeBounds?.isValid() && !fitted) {
      map.fitBounds(mapHomeBounds,{padding:[35,35],animate:false});
      fitted=true;
    }
    if (!groupStyles.size) show('The selected layer has no synchronized GPS samples.');
    closeBusy('update');
  }

  function utcMilliseconds(value) {
    const text = String(value || '').trim();
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}Z`;
    return new Date(normalized).getTime();
  }

  function formatUtc(value) {
    const date = new Date(Number(value));
    if (!Number.isFinite(date.getTime())) return '';
    return `${date.toISOString().slice(0,10)} ${date.toISOString().slice(11,19)} UTC`;
  }

  function visibleTimeframe() {
    const times = currentView.selected
      .flatMap(item => payload.layers[item.instrument]?.[item.gas] || [])
      .map(point => utcMilliseconds(point.time)).filter(Number.isFinite).sort((a,b)=>a-b);
    const fallback = flightTrackPoints(currentView.selected)
      .map(point => utcMilliseconds(point.time)).filter(Number.isFinite).sort((a,b)=>a-b);
    const values = times.length ? times : fallback;
    if (!values.length) return 'Time frame unavailable';
    return `${formatUtc(values[0])} to ${formatUtc(values[values.length-1])}`;
  }

  function niceDistance(metres) {
    if (!(metres > 0)) return 1;
    const exponent = Math.floor(Math.log10(metres));
    const unit = 10 ** exponent;
    const fraction = metres / unit;
    const rounded = fraction >= 5 ? 5 : fraction >= 2 ? 2 : 1;
    return rounded * unit;
  }

  function drawNorthArrow(context, x, y) {
    context.save();
    context.fillStyle = '#071827'; context.strokeStyle = '#fff'; context.lineWidth = 1.5;
    context.beginPath(); context.roundRect(x,y,54,80,8); context.fill(); context.stroke();
    context.fillStyle = '#fff'; context.font = '700 18px Arial'; context.textAlign = 'center';
    context.fillText('N',x+27,y+23);
    context.beginPath(); context.moveTo(x+27,y+31); context.lineTo(x+15,y+65); context.lineTo(x+27,y+57); context.lineTo(x+39,y+65); context.closePath();
    context.fillStyle = '#36d7ff'; context.fill(); context.strokeStyle = '#fff'; context.stroke();
    context.restore();
  }

  function drawScaleBar(context, mapHeight, headerHeight, mapWidth) {
    const centreY = mapHeight / 2;
    const first = map.containerPointToLatLng([mapWidth/2,centreY]);
    const second = map.containerPointToLatLng([mapWidth/2+100,centreY]);
    const metresPerPixel = map.distance(first,second) / 100;
    const distance = niceDistance(metresPerPixel * Math.min(180,mapWidth*.18));
    const pixels = distance / metresPerPixel;
    const x = 28, y = headerHeight + mapHeight - 43;
    context.save(); context.strokeStyle='#050505'; context.fillStyle='#fff'; context.lineWidth=4;
    context.beginPath(); context.moveTo(x,y); context.lineTo(x+pixels,y); context.stroke();
    context.strokeStyle='#fff'; context.lineWidth=2; context.stroke();
    context.fillStyle='#071827'; context.font='700 13px Arial'; context.textAlign='left';
    const label = distance >= 1000 ? `${(distance/1000).toPrecision(2)} km` : `${Math.round(distance)} m`;
    context.fillText(label,x,y-9); context.restore();
  }

  function drawExportLegends(context, width, headerHeight) {
    let y = headerHeight + 18;
    const groups = [...currentView.groupStyles.entries()];
    for (const [group,style] of groups) {
      const members = currentView.selected.filter(item=>groupKey(item)===group);
      if (!members.length) continue;
      const [gasName,unit,paletteName] = group.split('|');
      const boxWidth=280, boxHeight=82, x=width-boxWidth-18;
      context.save(); context.globalAlpha=.94; context.fillStyle='#071827';
      context.beginPath(); context.roundRect(x,y,boxWidth,boxHeight,8); context.fill();
      context.globalAlpha=1; context.fillStyle='#fff'; context.font='700 14px Arial'; context.textAlign='left';
      context.fillText(`${gasName}  (${unit})`,x+12,y+20);
      context.fillStyle='#a9c8d5'; context.font='11px Arial';
      context.fillText(members.map(item=>`${item.instrument} ${item.gas}`).join(' · ').slice(0,42),x+12,y+38);
      const gradient=context.createLinearGradient(x+12,0,x+boxWidth-12,0);
      style.palette.forEach((shade,index)=>gradient.addColorStop(index/(style.palette.length-1),shade));
      context.fillStyle=gradient; context.fillRect(x+12,y+46,boxWidth-24,10);
      context.fillStyle='#dcebf2'; context.font='11px Arial';
      context.fillText(style.low.toPrecision(4),x+12,y+72); context.textAlign='right';
      context.fillText(style.high.toPrecision(4),x+boxWidth-12,y+72);
      context.textAlign='center'; context.fillStyle='#42d8ff';
      context.fillText(paletteLabel(paletteName),x+boxWidth/2,y+72);
      context.restore(); y += boxHeight + 8;
    }
  }

  async function composeExportCanvas() {
    const container = byId('map');
    const rect = container.getBoundingClientRect();
    const width = Math.max(800,Math.round(rect.width));
    const mapHeight = Math.max(500,Math.round(rect.height));
    const headerHeight = 92, footerHeight = 82;
    const scale = Math.min(2.5,Math.max(1.25,5200/Math.max(width,mapHeight+headerHeight+footerHeight)));
    const canvas=document.createElement('canvas');
    canvas.width=Math.round(width*scale); canvas.height=Math.round((mapHeight+headerHeight+footerHeight)*scale);
    const context=canvas.getContext('2d'); context.scale(scale,scale);
    context.fillStyle='#eef4f2'; context.fillRect(0,0,width,mapHeight+headerHeight+footerHeight);
    context.fillStyle='#071827'; context.fillRect(0,0,width,headerHeight);
    context.fillStyle='#36d7ff'; context.font='700 27px Arial'; context.textAlign='center';
    context.fillText(payload.flight_name || 'Flight',width/2,38);
    context.fillStyle='#d7e7ed'; context.font='15px Arial';
    context.fillText('MIRO Rack georeferenced trace-gas map',width/2,64);

    context.save(); context.beginPath(); context.rect(0,headerHeight,width,mapHeight); context.clip();
    context.fillStyle='#dce6e4'; context.fillRect(0,headerHeight,width,mapHeight);
    const tiles=[...container.querySelectorAll('.leaflet-tile-loaded')];
    for (let index=0; index<tiles.length; index+=1) {
      const tile=tiles[index], tileRect=tile.getBoundingClientRect();
      context.drawImage(tile,tileRect.left-rect.left,headerHeight+tileRect.top-rect.top,tileRect.width,tileRect.height);
    }
    for (const item of currentView.selected) {
      const points=(payload.layers[item.instrument]?.[item.gas] || []).filter(point=>finite(point.lat)&&finite(point.lon)&&finite(point.value));
      const style=currentView.groupStyles.get(groupKey(item));
      if (!style || points.length<2) continue;
      const shifted=offsetTrack(points,item.offsetMetres);
      context.lineWidth=item.width; context.lineCap='round'; context.globalAlpha=.92;
      for (let index=0; index<shifted.length-1; index+=1) {
        const a=shifted[index], b=shifted[index+1], value=(Number(a.value)+Number(b.value))/2;
        const first=map.latLngToContainerPoint([a.lat,a.lon]);
        const second=map.latLngToContainerPoint([b.lat,b.lon]);
        context.strokeStyle=color(value,style.low,style.high,style.palette);
        context.beginPath(); context.moveTo(first.x,headerHeight+first.y); context.lineTo(second.x,headerHeight+second.y); context.stroke();
      }
      await nextFrame();
    }
    if (flightTrackEnabled) {
      const route=flightTrackPoints(currentView.selected);
      if (route.length>1) {
        context.globalAlpha=.98; context.strokeStyle='#050505'; context.lineWidth=2.5;
        context.setLineDash([2,8]); context.lineCap='round'; context.beginPath();
        route.forEach((point,index)=>{
          const position=map.latLngToContainerPoint([point.lat,point.lon]);
          if (index) context.lineTo(position.x,headerHeight+position.y); else context.moveTo(position.x,headerHeight+position.y);
        });
        context.stroke(); context.setLineDash([]);
      }
    }
    context.restore(); context.globalAlpha=1;
    context.strokeStyle='#21485b'; context.lineWidth=2; context.strokeRect(0,headerHeight,width,mapHeight);
    drawNorthArrow(context,18,headerHeight+18);
    drawScaleBar(context,mapHeight,headerHeight,width);
    drawExportLegends(context,width,headerHeight);
    const bounds=map.getBounds(), southWest=bounds.getSouthWest(), northEast=bounds.getNorthEast();
    context.fillStyle='#071827'; context.font='700 12px Arial'; context.textAlign='left';
    context.fillText(`SW ${southWest.lat.toFixed(5)}°, ${southWest.lng.toFixed(5)}°`,18,headerHeight+mapHeight-16);
    context.textAlign='right'; context.fillText(`NE ${northEast.lat.toFixed(5)}°, ${northEast.lng.toFixed(5)}°`,width-18,headerHeight+mapHeight-16);
    context.fillStyle='#071827'; context.fillRect(0,headerHeight+mapHeight,width,footerHeight);
    context.fillStyle='#fff'; context.font='700 14px Arial'; context.textAlign='center';
    context.fillText(visibleTimeframe(),width/2,headerHeight+mapHeight+31);
    context.fillStyle='#a9c8d5'; context.font='12px Arial';
    context.fillText('North-up · WGS 84 latitude/longitude · OpenStreetMap basemap',width/2,headerHeight+mapHeight+56);
    return canvas;
  }

  async function exportCurrentMapPdf() {
    if (!map || !payload) return;
    openBusy('export','Preparing high-resolution PDF','Composing the visible map, scientific layers, scale, coordinates, north arrow, and time frame.');
    byId('exportMap').disabled=true;
    window.__miroMapExportStatus='running';
    try {
      await nextFrame();
      const canvas=await composeExportCanvas();
      byId('mapBusyMessage').textContent='Writing the high-resolution PDF and saving it with the Flight Project.';
      const image=canvas.toDataURL('image/png');
      const timeframe=visibleTimeframe();
      const response=await fetch('/api/miro-rack/map/export',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({image,flight_name:payload.flight_name || 'Flight',timeframe})
      });
      if (!response.ok) {
        const failure=await response.json().catch(()=>({error:'PDF export failed'}));
        throw new Error(failure.error || 'PDF export failed');
      }
      const blob=await response.blob();
      const disposition=response.headers.get('Content-Disposition') || '';
      const match=disposition.match(/filename="?([^";]+)"?/i);
      const filename=match ? match[1] : `${payload.flight_name || 'Flight'}_MIRO_Rack_Map.pdf`;
      const link=document.createElement('a'); link.href=URL.createObjectURL(blob); link.download=filename;
      document.body.appendChild(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(link.href),1000);
      window.__miroMapExportStatus='complete';
      fetch('/api/miro-rack/log',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:`MIRO Rack current map layout exported as ${filename}`})}).catch(()=>{});
    } catch (error) {
      window.__miroMapExportStatus=`error: ${error.message}`;
      show(`PDF export could not be completed: ${error.message}`);
    } finally {
      byId('exportMap').disabled=false; closeBusy('export');
    }
  }

  function addLegend(group,unit,low,high,palette,detail,scaleName) {
    const element=document.createElement('div'); element.className='legend';
    element.innerHTML=`<div class="legend-head"><span>${group}</span><span>${unit}</span></div>
      <div style="font-size:13px;color:#a9c8d5;margin-top:5px;line-height:1.35">${detail}</div>
      <div class="legend-scale">Colour scale: ${scaleName}</div>
      <div class="gradient" style="background:linear-gradient(90deg,${palette.join(',')})"></div>
      <div class="range"><span>${low.toPrecision(4)}</span><span>${high.toPrecision(4)}</span></div>`;
    byId('legends').appendChild(element);
  }
  function resetMapPosition() {
    if (!map) return;
    map.closePopup();
    if (mapHomeBounds?.isValid()) {
      map.fitBounds(mapHomeBounds,{padding:[35,35],animate:true});
    } else {
      map.setView([47.6,9.3],10,{animate:true});
    }
  }
  function show(message){byId('message').textContent=message;byId('message').style.display='block'}
  function hide(){byId('message').style.display='none'}
  byId('flightTrackToggle').addEventListener('click',event=>{
    flightTrackEnabled=!flightTrackEnabled;
    event.currentTarget.setAttribute('aria-pressed',String(flightTrackEnabled));
    event.currentTarget.textContent=`Flight track: ${flightTrackEnabled ? 'Enabled' : 'Disabled'}`;
    requestUpdate(flightTrackEnabled ? 'Drawing the black dotted flight track' : 'Hiding the flight track');
  });
  byId('exportMap').addEventListener('click',exportCurrentMapPdf);
  byId('resetMapPosition').addEventListener('click',resetMapPosition);
  byId('update').addEventListener('click',()=>requestUpdate('Refreshing all selected map layers'));
  load();

  // Stamps the running build into the footer. A version number alone cannot
  // answer "am I running the code I just pulled?" - it only changes at a
  // release, so a fetch that was never merged looks identical to an update.
  fetch('/api/build', {cache: 'no-store'})
    .then(response => response.json())
    .then(info => {
      document.querySelectorAll('.app-version').forEach(node => {
        node.textContent = `Version ${info.version} · build ${info.build}`;
      });
    })
    .catch(() => {});
})();
