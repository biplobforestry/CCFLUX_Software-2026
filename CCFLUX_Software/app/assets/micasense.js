(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const number = value => Number(value ?? 0).toLocaleString();
  async function api(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(`Request failed (${r.status})`);return r.json();}

  function card(name, value, note) {
    return `<div class="summary"><span>${name}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;
  }
  function renderSummary(summary) {
    const evaluated = summary.image_count ?? 0;
    const delivered = summary.delivered_image_count;
    const interval = summary.median_trigger_interval_seconds;
    $('summaryGrid').innerHTML = [
      card('Images evaluated', number(evaluated),
        delivered && delivered !== evaluated ? `${number(delivered)} delivered` : 'Inside the Time Filter'),
      card('Captures', number(summary.capture_count),
        `${number(summary.complete_capture_count)} complete · ${number(summary.incomplete_capture_count)} incomplete`),
      card('Bands', (summary.bands || []).join(', ') || '—', 'Present in the delivery'),
      card('Trigger interval',
        Number.isFinite(Number(interval)) ? `${Number(interval).toFixed(2)} s` : '—', 'Median between captures'),
      card('GPS present', number(summary.gps_present_count), 'Of the evaluated images'),
      card('Exposure present', number(summary.exposure_present_count), 'Of the evaluated images')
    ].join('');
  }
  // A file the camera wrote badly is reported, not hidden, and never stops the
  // rest of the delivery being shown.
  function renderQuality(summary, warnings) {
    const items = [];
    const corrupt = (summary.corrupt_files || []).length;
    const small = (summary.unusually_small_files || []).length;
    if (corrupt) items.push([`${number(corrupt)} file(s) could not be read and were skipped`, true]);
    if (small) items.push([`${number(small)} file(s) are unusually small`, true]);
    (warnings || []).forEach(text => items.push([String(text), true]));
    (summary.excluded_operations || []).forEach(name =>
      items.push([`Not performed here: ${name}`, false]));
    if (!items.length) items.push(['No acquisition problem was found.', false]);
    $('qcList').innerHTML = items
      .map(([text, warn]) => `<li class="${warn ? 'warn' : ''}">${text}</li>`).join('');
  }
  function renderThumbnails(thumbnails) {
    const shown = thumbnails || [];
    $('thumbs').innerHTML = shown.length
      ? shown.map(item =>
          `<figure><img loading="lazy" src="${item.url}" alt="${item.name}"
             onerror="this.closest('figure').remove()"><figcaption>${item.name}</figcaption></figure>`).join('')
      : '<p class="muted">No thumbnail was written for this flight.</p>';
  }
  function renderExports(exports) {
    $('exportList').innerHTML = (exports || []).length
      ? exports.map(path => `<li>${path}</li>`).join('')
      : '<li>No export was written.</li>';
  }
  async function load() {
    $('busy').classList.add('show');
    try {
      const response = await api('/api/micasense');
      $('flightName').textContent = response.flight_id || 'No project';
      if (!response.ready) {
        $('statusText').textContent = response.message || 'MicaSense products are not ready';
        document.querySelector('main').innerHTML =
          `<div class="empty"><div><strong>MicaSense processing is required</strong>${response.message || ''}</div></div>`;
        return;
      }
      const data = response.data || {};
      $('statusDot').classList.add('ready');
      $('statusText').textContent = 'Processed MicaSense data loaded from the active Flight Project';
      renderSummary(data.summary || {});
      renderQuality(data.summary || {}, data.warnings);
      renderThumbnails(data.thumbnails);
      renderExports(data.exports);
    } catch (error) {
      $('statusText').textContent = `MicaSense view failed: ${error.message}`;
      $('statusText').style.color = 'var(--danger)';
    } finally {
      $('busy').classList.remove('show');
    }
  }
  $('refreshBtn').onclick = load;
  load();
})();
