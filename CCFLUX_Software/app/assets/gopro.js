(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  let map;
  let imageUrl;
  let captureLayer;
  let trackLayer;
  let mapBounds;
  let latestCaptures = [];

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
    });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try { message = (await response.json()).error || message; } catch (_) {}
      throw new Error(message);
    }
    return response.json();
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[character]);
  }

  function display(value, digits = 6) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(digits) : '—';
  }

  function ensureMap() {
    if (map) return;
    if (!window.L) throw new Error('Street-map library could not be loaded.');
    map = L.map('map', { zoomControl: true, preferCanvas: true }).setView([47.64, 9.38], 10);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);
    L.control.scale({ metric: true, imperial: false }).addTo(map);
  }

  function popupHtml(capture) {
    return `<div class="capture-popup"><table>
      <tr><td>Latitude</td><td>${display(capture.latitude)}</td></tr>
      <tr><td>Longitude</td><td>${display(capture.longitude)}</td></tr>
      <tr><td>Altitude</td><td>${display(capture.altitude_m, 1)} m</td></tr>
      <tr><td>Image ID</td><td>${escapeHtml(capture.image_id)}</td></tr>
      <tr><td>Capture time</td><td>${escapeHtml(capture.capture_time_utc)} UTC</td></tr>
    </table><button class="btn" data-show-capture="${escapeHtml(capture.capture_id)}">Show more</button></div>`;
  }

  function selectedLineWidth() {
    const value = Number(byId('trackWidth').value);
    return Number.isFinite(value) ? Math.min(20, Math.max(1, value)) : 3;
  }

  function renderCaptures(captures, fitPosition = false) {
    ensureMap();
    if (captureLayer) captureLayer.remove();
    if (trackLayer) trackLayer.remove();
    captureLayer = L.layerGroup().addTo(map);
    const coordinates = captures.map(capture => [
      Number(capture.latitude), Number(capture.longitude)
    ]).filter(coordinate => coordinate.every(Number.isFinite));
    const width = selectedLineWidth();
    if (coordinates.length > 1) {
      trackLayer = L.polyline(coordinates, {
        color: '#ff3030', weight: width, opacity: .82
      }).addTo(map);
    } else {
      trackLayer = null;
    }
    captures.forEach(capture => {
      const coordinate = [Number(capture.latitude), Number(capture.longitude)];
      if (!coordinate.every(Number.isFinite)) return;
      L.circleMarker(coordinate, {
        radius: 5.5, color: '#8c0000', weight: Math.max(1, width / 2),
        fillColor: '#ff2020', fillOpacity: .96
      }).bindPopup(popupHtml(capture), { maxWidth: 350 }).addTo(captureLayer);
    });
    mapBounds = coordinates.length ? L.latLngBounds(coordinates) : null;
    if (fitPosition && mapBounds?.isValid()) {
      map.fitBounds(mapBounds, { padding: [25, 25], maxZoom: 17 });
    }
  }

  function resetMapPosition() {
    if (mapBounds?.isValid()) {
      map.fitBounds(mapBounds, { padding: [25, 25], maxZoom: 17 });
    } else {
      map?.setView([47.64, 9.38], 10);
    }
  }

  async function loadImage(captureId) {
    const modal = byId('imageModal');
    const image = byId('captureImage');
    const loading = byId('imageLoading');
    const actions = byId('imageActions');
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
    image.style.display = 'none';
    image.removeAttribute('src');
    loading.style.display = '';
    actions.classList.remove('show');
    byId('imageProgress').style.width = '1%';
    byId('imagePercent').textContent = '1%';
    modal.classList.add('show');
    try {
      const response = await fetch(`/api/gopro/image/${encodeURIComponent(captureId)}`);
      if (!response.ok) throw new Error(`Image search failed (${response.status})`);
      const total = Number(response.headers.get('Content-Length')) || 0;
      const reader = response.body?.getReader();
      const chunks = [];
      let received = 0;
      if (reader) {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          received += value.length;
          const percentage = total
            ? Math.min(99, Math.max(1, Math.round(received * 100 / total)))
            : Math.min(99, Math.max(1, chunks.length * 2));
          byId('imageProgress').style.width = `${percentage}%`;
          byId('imagePercent').textContent = `${percentage}%`;
        }
      }
      const blob = reader
        ? new Blob(chunks, { type: response.headers.get('Content-Type') || 'image/jpeg' })
        : await response.blob();
      byId('imageProgress').style.width = '100%';
      byId('imagePercent').textContent = '100%';
      imageUrl = URL.createObjectURL(blob);
      image.src = imageUrl;
      image.onload = () => {
        loading.style.display = 'none';
        image.style.display = 'block';
      };
      byId('downloadImage').href = `/api/gopro/image/${encodeURIComponent(captureId)}?download=1`;
      byId('downloadImage').download = '';
      api('/api/gopro/log', {
        method: 'POST',
        body: JSON.stringify({ message: `GoPro image opened: capture ${captureId}` })
      }).catch(() => {});
    } catch (error) {
      byId('imageTitle').textContent = 'Image unavailable';
      byId('imagePercent').textContent = error.message;
      // A saved project carries capture identity but never the pictures. When
      // the camera disk is not reachable, offer to reconnect it rather than
      // leaving the operator at a dead end.
      await offerMediaReconnect(captureId);
    }
  }

  async function offerMediaReconnect(captureId) {
    let status;
    try {
      status = await api('/api/gopro/media-status');
    } catch (_) {
      return;
    }
    if (status.media_available) return;
    const loading = byId('imageLoading');
    loading.style.display = '';
    loading.innerHTML = `
      <p><strong>${escapeHtml(status.prompt)}</strong></p>
      <p class="muted">The images stay on the campaign hard disk; the project
      stores only the capture identity and its matched position.</p>
      <div class="reconnect-actions">
        <button class="btn primary" id="gpHasDisk">Yes</button>
        <button class="btn" id="gpNoDisk">No</button>
      </div>
      <div id="gpReconnectDetail"></div>`;
    byId('gpNoDisk').onclick = () => {
      byId('gpReconnectDetail').innerHTML =
        `<p class="warn">${escapeHtml(status.contact_message)}</p>`;
      api('/api/gopro/log', {
        method: 'POST',
        body: JSON.stringify({ message: 'GoPro hard disk reported unavailable' })
      }).catch(() => {});
    };
    byId('gpHasDisk').onclick = () => {
      byId('gpReconnectDetail').innerHTML = `
        <p>${escapeHtml(status.folder_requirement)}</p>
        <label class="detail-row">Folder
          <input type="text" id="gpDirectory" placeholder="/Volumes/&lt;disk&gt;/Camera_System/GoPro">
        </label>
        <button class="btn primary" id="gpSync">Synchronise</button>
        <p id="gpSyncResult" class="muted"></p>`;
      byId('gpSync').onclick = async () => {
        const directory = byId('gpDirectory').value.trim();
        const result = byId('gpSyncResult');
        result.textContent = 'Synchronising…';
        try {
          const response = await api('/api/gopro/reconnect', {
            method: 'POST',
            body: JSON.stringify({ has_hard_disk: true, directory })
          });
          result.textContent = response.message;
          if (response.reconnected) {
            result.classList.remove('warn');
            // The disk is back; load the picture the operator asked for.
            setTimeout(() => loadImage(captureId), 400);
          }
        } catch (error) {
          result.classList.add('warn');
          result.textContent = error.message;
        }
      };
    };
  }

  function closeImage() {
    byId('imageModal').classList.remove('show');
    byId('imageActions').classList.remove('show');
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
  }

  async function refresh() {
    byId('statusText').textContent = 'Matching GoPro camera time to Noseboom UTC…';
    try {
      const payload = await api('/api/gopro');
      byId('flightName').textContent = payload.flight_id || 'No project';
      const data = payload.data || {};
      const captures = data.captures || [];
      latestCaptures = captures;
      byId('summary').textContent = `${Number(data.matched_count || 0).toLocaleString()} matched · ${Number(data.unmatched_count || 0).toLocaleString()} unmatched · Camera ${data.camera_timezone || 'Europe/Berlin'} → Noseboom UTC`;
      ensureMap();
      renderCaptures(captures, true);
      if (!payload.ready || !captures.length) {
        byId('mapMessage').hidden = false;
        byId('mapMessage').textContent = data.reason || (
          payload.processing_status === 'processing'
            ? payload.processing_step
            : 'Process Noseboom and GoPro from the Main GUI first.'
        );
        byId('mapInfo').textContent = 'No matched captures';
        return;
      }
      byId('mapMessage').hidden = true;
      byId('mapInfo').textContent = `${captures.length.toLocaleString()} georeferenced captures`;
      byId('statusDot').classList.add('ready');
      byId('statusText').textContent = 'GoPro captures matched to Noseboom navigation';
    } catch (error) {
      byId('mapMessage').hidden = false;
      byId('mapMessage').textContent = error.message;
      byId('statusText').textContent = `GoPro view failed: ${error.message}`;
    }
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('[data-show-capture]');
    if (button) loadImage(button.dataset.showCapture);
  });
  byId('mainGuiBtn').onclick = () => { window.location.href = '/'; };
  byId('refreshBtn').onclick = refresh;
  byId('resetMapBtn').onclick = resetMapPosition;
  byId('trackWidth').oninput = () => renderCaptures(latestCaptures, false);
  byId('mapFullscreenBtn').onclick = async () => {
    if (!document.fullscreenElement) await byId('mapCard').requestFullscreen();
    else await document.exitFullscreen();
    setTimeout(() => map?.invalidateSize(false), 150);
  };
  byId('imageClose').onclick = closeImage;
  byId('imageModal').onclick = event => {
    if (event.target === byId('imageModal')) closeImage();
  };
  byId('captureImage').onclick = () => byId('imageActions').classList.toggle('show');
  byId('fullscreenImage').onclick = async () => {
    if (!document.fullscreenElement) await byId('captureImage').requestFullscreen();
    else await document.exitFullscreen();
  };
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !document.fullscreenElement) closeImage();
  });
  window.addEventListener('beforeunload', () => {
    if (imageUrl) URL.revokeObjectURL(imageUrl);
  });
  refresh();
})();
