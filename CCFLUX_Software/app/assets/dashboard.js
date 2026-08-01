(() => {
  'use strict';

  // /api/select-flight-folder remains available for backwards-compatible clients.

  const instrumentIds = {
    'Noseboom': 'noseboom',
    'MIRO': 'miro',
    'Picarro': 'picarro',
    'OPC HBX-4': 'opc_hbx4',
    'OPC HBX-5': 'opc_hbx5',
    'Partector Pro': 'partector',
    'INS Gimbal': 'ins_gimbal',
    'SIF / FLOX': 'sif',
    'MicaSense': 'micasense',
    'FLIR Thermal Camera': 'flir',
    'GoPro': 'gopro'
  };
  const statusPresentation = {
    not_detected: ['queued', 'Not detected'],
    detected: ['running', 'Detected'],
    validating: ['running', 'Validating'],
    ready: ['complete', 'Ready'],
    warning: ['warning', 'Warning'],
    failed: ['warning', 'Failed']
  };
  const processingPresentation = {
    queued: ['queued', 'Queued'],
    processing: ['running', 'Processing'],
    complete: ['complete', 'Complete'],
    warning: ['warning', 'Warning'],
    failed: ['warning', 'Failed'],
    cancelled: ['warning', 'Cancelled'],
    paused: ['queued', 'Paused']
  };

  const toast = document.getElementById('toast');
  const modal = document.getElementById('modal');
  const modalTitle = document.getElementById('modalTitle');
  const modalBody = document.getElementById('modalBody');
  const logConsole = document.getElementById('logConsole');
  let scanPoll = null;
  let logPoll = null;
  let queuePoll = null;
  let lastLogSignature = '';
  let autoScroll = true;
  let draggedJobId = null;
  let level2Capabilities = {};
  let currentQueue = { jobs: [] };
  let queueRefreshPending = false;
  let customTimeEditing = false;
  let latestScanState = { scans: {} };
  // Reset on each new scan so the coverage window appears once per scan.
  let cameraCoverageAnnounced = false;

  document.querySelectorAll('.instrument-card').forEach(card => {
    card.dataset.instrumentId = instrumentIds[card.dataset.name] || '';
    updateCard(card, {
      detection_status: 'not_detected',
      file_count: 0,
      warnings: [],
      errors: [],
      ambiguous: false,
      utc_start_time: null,
      utc_end_time: null
    });
    card.addEventListener('click', () => {
      if (['miro', 'picarro'].includes(card.dataset.instrumentId) && card.matches('a[target="_blank"]')) {
        api('/api/miro-rack/log', {
          method: 'POST',
          body: JSON.stringify({ message: `${card.dataset.name} card clicked; opening shared MIRO Rack workspace` })
        }).catch(() => {});
        return;
      }
      if (['opc_hbx4', 'opc_hbx5', 'partector', 'ins_gimbal', 'sif'].includes(card.dataset.instrumentId) && card.matches('a[target="_blank"]')) {
        const page = ['partector', 'ins_gimbal', 'sif'].includes(card.dataset.instrumentId) ? card.dataset.instrumentId : 'opc';
        api('/api/hatchbox/log', {
          method: 'POST',
          body: JSON.stringify({ page, message: `${card.dataset.name} card clicked; opening dedicated scientific workspace` })
        }).catch(() => {});
        return;
      }
      if (card.dataset.instrumentId === 'gopro' && card.matches('a[target="_blank"]')) {
        api('/api/gopro/log', {
          method: 'POST',
          body: JSON.stringify({ message: 'GoPro card clicked; opening georeferenced capture map' })
        }).catch(() => {});
        return;
      }
      if (card.dataset.instrumentId === 'noseboom' && card._scanState?.quicklook?.available) {
        if (card.matches('a[target="_blank"]')) {
          api('/api/noseboom/log', {
            method: 'POST',
            body: JSON.stringify({ message: 'Noseboom card clicked; opening dedicated tab' })
          }).catch(() => {});
          return;
        }
        openNoseboomTab();
      } else {
        showInstrumentSummary(card);
      }
    });
  });
  resetDiscoveryPanels();
  updateSystemPanels();

  document.getElementById('openFolderBtn').addEventListener('click', selectFlightFolder);
  document.querySelectorAll('[data-folder-action]').forEach(card => {
    const activate = () => document.getElementById({ flight: 'openFolderBtn', camera: 'cameraFolderBtn', output: 'outputFolderBtn' }[card.dataset.folderAction])?.click();
    card.addEventListener('click', activate);
    card.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); } });
  });
  document.getElementById('cameraFolderBtn').addEventListener('click', selectCameraFolder);
  document.getElementById('initialCheckBtn').addEventListener('click', initialCheck);
  document.getElementById('resetSystemBtn').addEventListener('click', confirmSystemReset);
  document.getElementById('exitAppBtn').addEventListener('click', confirmApplicationExit);
  document.getElementById('remoteSensingBtn').addEventListener('click', openRemoteSensingDialog);
  document.getElementById('dataProductsBtn').addEventListener('click', () => {
    openEditableInformation('/data_products.txt', 'Data Products');
  });
  let updateStatus = null;

  function renderUpdateButton() {
    const button = document.getElementById('softwareUpdateBtn');
    if (!button || !updateStatus) return;
    if (updateStatus.update_available) {
      button.textContent = `Update available · ${updateStatus.latest_version}`;
      button.classList.add('update-available');
      button.title = `This installation is ${updateStatus.current_version}.`;
    } else {
      button.textContent = 'Update';
      button.classList.remove('update-available');
      button.title = updateStatus.checked
        ? `Version ${updateStatus.current_version} is current.`
        : (updateStatus.reason || 'Update information is unavailable.');
    }
  }

  async function refreshUpdateStatus(announce, { force = false } = {}) {
    try {
      // Without force the backend answers from the launch-time check, so the
      // dialog opens instantly. Check Again contacts the server for real.
      updateStatus = await api(`/api/update/status${force ? '?refresh=1' : ''}`);
    } catch (_) {
      return false;                 // never let a failed check disturb the GUI
    }
    renderUpdateButton();
    if (announce && updateStatus.update_available) {
      showToast(`Version ${updateStatus.latest_version} is available.`);
    }
    return true;
  }

  function updateDialogHtml() {
    const status = updateStatus || {};
    if (!status.checked) {
      return `<p>${escapeHtml(status.reason || 'Update information is unavailable.')}</p>
        <p class="muted">The software works normally without this check.
        It is disabled by setting CCFLUX_UPDATE_CHECK=off.</p>`;
    }
    if (!status.update_available) {
      return `<p><strong>Version ${escapeHtml(status.current_version)} is current.</strong></p>
        ${status.latest_version ? `<p class="muted">Latest published: ${escapeHtml(status.latest_version)}</p>` : ''}`;
    }
    return `<p><strong>Version ${escapeHtml(status.latest_version)} is available.</strong></p>
      <p class="muted">This installation is ${escapeHtml(status.current_version)}${
        status.released_utc ? ` · released ${escapeHtml(status.released_utc)}` : ''}.</p>
      ${status.notice ? `<p>${escapeHtml(status.notice)}</p>` : ''}
      <p class="muted">Nothing is downloaded or installed automatically. Finish
      any processing and save your project before updating.</p>
      <p><a class="btn primary" href="${escapeAttribute(status.download_url)}"
        target="_blank" rel="noopener">Open the download page</a></p>`;
  }

  function renderUpdateDialog() {
    modalBody.innerHTML = updateDialogHtml()
      + `<div class="scan-actions">
           <button class="btn" id="recheckUpdate">Check Again</button>
           <button class="btn" id="closeUpdate">Close</button>
         </div>
         <p class="muted" id="recheckOutcome"></p>`;
    document.getElementById('closeUpdate').onclick = () => modal.classList.remove('show');
    document.getElementById('recheckUpdate').onclick = async () => {
      const button = document.getElementById('recheckUpdate');
      const outcome = document.getElementById('recheckOutcome');
      const previous = updateStatus ? updateStatus.latest_version : null;
      button.disabled = true;
      button.textContent = 'Checking...';
      // The cached answer is discarded first, so a check that fails reports a
      // failure rather than redisplaying the launch-time result as if fresh.
      const reached = await refreshUpdateStatus(false, { force: true });
      renderUpdateDialog();
      const note = document.getElementById('recheckOutcome');
      if (!reached || !updateStatus.checked) {
        note.textContent = 'The update server could not be reached. The previous '
          + 'result is shown; the software works normally without this check.';
      } else if (updateStatus.latest_version && updateStatus.latest_version !== previous) {
        note.textContent = `Checked just now — the published version changed to `
          + `${updateStatus.latest_version}.`;
      } else {
        note.textContent = 'Checked just now.';
      }
    };
  }

  document.getElementById('softwareUpdateBtn').addEventListener('click', async () => {
    await refreshUpdateStatus(false);
    modalTitle.textContent = 'Software Update';
    renderUpdateDialog();
    modal.classList.add('show');
  });

  document.getElementById('softwareUpdateBtn').addEventListener('contextmenu', event => {
    event.preventDefault();
    openEditableInformation('/software_update.txt', 'Upcoming Software Update');
  });
  document.getElementById('licenseBtn').addEventListener('click', () => {
    openEditableInformation('/License.txt', 'License', 'license');
  });
  document.getElementById('manualBtn').addEventListener('click', () => {
    openEditableInformation('/manual.text', 'CC-FLUX Software Manual', 'manual');
  });
  document.querySelectorAll('[data-stop-scan]').forEach(button => {
    button.addEventListener('click', () => {
      const source = button.dataset.stopScan;
      const channel = latestScanState.scans?.[source] || {};
      if (!channel.running) {
        document.getElementById(`${source}ScanWindow`).classList.remove('show');
        return;
      }
      confirmStopScan(source, false);
    });
  });
  document.querySelectorAll('[data-window-action]').forEach(button => {
    button.addEventListener('click', () => handleScanWindowAction(
      button.dataset.scanSource, button.dataset.windowAction
    ));
  });
  document.getElementById('useFullTimeBtn').addEventListener('click', () => changeTimeFilter({ action: 'full' }));
  document.getElementById('useCommonTimeBtn').addEventListener('click', () => changeTimeFilter({ action: 'common' }));
  document.getElementById('customTimeBtn').addEventListener('click', activateCustomTimeframe);
  document.getElementById('resetTimeBtn').addEventListener('click', () => changeTimeFilter({ action: 'reset' }));
  document.getElementById('applyTimeFilterBtn').addEventListener('click', applySelectedTimeFilter);
  ['analysisStartTime', 'analysisEndTime'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => {
      customTimeEditing = true;
      document.getElementById('customTimeEditor').classList.add('custom-active');
    });
  });
  document.getElementById('displayTimezone').addEventListener('change', event => {
    changeTimeFilter({ action: 'display', display_timezone: event.target.value });
  });
  document.getElementById('cpuAllocation').addEventListener('change', updateResources);
  document.getElementById('ramAllocation').addEventListener('change', updateResources);
  ['flightDate', 'flightStartTime', 'flightEndTime'].forEach(id => {
    document.getElementById(id).addEventListener('change', synchronizeFlightTimes);
  });

  document.getElementById('closeModal').addEventListener('click', () => modal.classList.remove('show'));
  modal.addEventListener('click', event => {
    if (event.target === modal) modal.classList.remove('show');
  });

  document.getElementById('refreshBtn').addEventListener('click', async () => {
    const state = await api('/api/scan');
    renderScanState(state);
    showToast('Instrument status refreshed.');
  });

  document.querySelectorAll('.filter-btn').forEach(button => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn').forEach(item => item.classList.remove('active'));
      button.classList.add('active');
      const filter = button.dataset.filter;
      document.querySelectorAll('.instrument-card').forEach(card => {
        const status = card.dataset.status;
        const matches = filter === 'all'
          || (filter === 'complete' && status === 'ready')
          || (filter === 'running' && ['detected', 'validating'].includes(status))
          || (filter === 'warning' && ['warning', 'failed'].includes(status))
          || (filter === 'queued' && status === 'not_detected');
        card.classList.toggle('hidden', !matches);
      });
    });
  });

  document.getElementById('clearLogBtn').addEventListener('click', async () => {
    await api('/api/logs/clear', { method: 'POST' });
    logConsole.innerHTML = '';
    lastLogSignature = '';
  });
  document.getElementById('copyLogBtn').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(logConsole.innerText);
      showToast('Log copied to clipboard.');
    } catch (error) {
      showToast(`Could not copy log: ${error.message}`);
    }
  });
  document.getElementById('toggleLogBtn').addEventListener('click', event => {
    const hidden = logConsole.style.display === 'none';
    logConsole.style.display = hidden ? 'block' : 'none';
    event.target.textContent = hidden ? 'Collapse' : 'Expand';
  });
  logConsole.addEventListener('scroll', () => {
    const distance = logConsole.scrollHeight - logConsole.scrollTop - logConsole.clientHeight;
    autoScroll = distance < 12;
  });
  const priorityList = document.getElementById('priorityList');
  priorityList.addEventListener('change', event => {
    const selector = event.target.closest('input[data-queue-select]');
    if (!selector) return;
    if (isSystemBusy()) {
      selector.checked = !selector.checked;
      showBusyWarning();
      return;
    }
    queueAction({ action: selector.checked ? 'enable' : 'disable', job_id: selector.closest('.priority-job').dataset.jobId });
  });
  priorityList.addEventListener('click', event => {
    const button = event.target.closest('button[data-queue-action]');
    if (!button) return;
    if (button.dataset.queueAction === 'configure_detailed') {
      openLevel2Dialog(button.closest('.priority-job').dataset.jobId);
      return;
    }
    if (button.dataset.queueAction === 'configure_sif') {
      openSifConfiguration();
      return;
    }
    if (button.dataset.queueAction === 'sif_progress') {
      openSifProgressWindow();
      return;
    }
    if (button.dataset.queueAction === 'reprocess') {
      const row = button.closest('.priority-job');
      confirmReprocess(
        row.dataset.jobId,
        row.querySelector('.priority-name')?.textContent || 'instrument'
      );
      return;
    }
    queueAction({
      action: button.dataset.queueAction,
      job_id: button.closest('.priority-job').dataset.jobId
    });
  });
  priorityList.addEventListener('dragstart', event => {
    const row = event.target.closest('.priority-job');
    if (!row) return;
    draggedJobId = row.dataset.jobId;
    row.classList.add('dragging');
  });
  priorityList.addEventListener('dragend', event => {
    event.target.closest('.priority-job')?.classList.remove('dragging');
    draggedJobId = null;
  });
  priorityList.addEventListener('dragover', event => event.preventDefault());
  priorityList.addEventListener('drop', event => {
    event.preventDefault();
    const target = event.target.closest('.priority-job');
    const source = priorityList.querySelector(`[data-job-id="${draggedJobId}"]`);
    if (!target || !source || target === source) return;
    const box = target.getBoundingClientRect();
    priorityList.insertBefore(source, event.clientY < box.top + box.height / 2 ? target : target.nextSibling);
    queueAction({
      action: 'reorder',
      job_ids: Array.from(priorityList.querySelectorAll('.priority-job')).map(row => row.dataset.jobId)
    });
  });

  document.getElementById('runBtn').addEventListener('click', () => {
    startRegisteredProcessing();
  });
  document.getElementById('outputFolderBtn').addEventListener('click', async () => {
    const button = document.getElementById('outputFolderBtn');
    button.disabled = true;
    showToast('Opening Output Folder window...');
    await nextPaint();
    try {
      const result = await api('/api/select-output-folder', { method: 'POST' });
      if (result.cancelled) {
        showToast('Output Folder selection cancelled.');
        return;
      }
      showToast(result.project_saved
        ? `Output Folder selected and recoverable project saved: ${result.project_file}`
        : `Output Folder selected: ${result.folder}. The project will be saved automatically after Initial Check.`);
      renderScanState(await api('/api/scan'));
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });
  document.getElementById('saveProjectBtn').addEventListener('click', async () => {
    try {
      showToast('Saving main project, Noseboom state, and MIRO Rack session...');
      const result = await api('/api/project/save', { method: 'POST' });
      const rackStatus = result.miro_rack?.saved
        ? ' MIRO Rack data and workflow included.'
        : ' MIRO Rack was not loaded; existing saved Rack session was preserved when available.';
      showToast(`Project saved: ${result.project_file}.${rackStatus}`);
      renderScanState(await api('/api/scan'));
    } catch (error) {
      showToast(error.message);
    }
  });

  async function loadSavedProject(projectFile) {
    showToast('Loading the selected Flight Project and saved instrument workflows...');
    const result = await api('/api/project/open', {
      method: 'POST',
      body: JSON.stringify({ project_file: projectFile })
    });
    if (result.cancelled) {
      showToast('Load Project cancelled.');
      return;
    }
    renderScanState(result.state);
    const rackStatus = result.miro_rack?.restored
      ? ' MIRO Rack data and saved plots restored.'
      : ` ${result.miro_rack?.reason || 'No saved MIRO Rack session.'}`;
    showToast(`Project loaded: ${result.project_file}.${rackStatus}`);
  }

  function showSavedProjectChoices(discovery) {
    modalTitle.textContent = 'Select a saved Flight Project';
    const projectButtons = discovery.projects.map((project, index) => {
      const updated = project.updated_at_utc
        ? new Date(project.updated_at_utc).toLocaleString()
        : 'Update time not recorded';
      return `<button type="button" class="project-choice" data-project-index="${index}">
        <strong>${escapeHtml(project.flight_id)}</strong>
        <span>Updated: ${escapeHtml(updated)}</span>
        <span class="project-path">${escapeHtml(project.relative_path || project.project_file)}</span>
      </button>`;
    }).join('');
    modalBody.innerHTML = `
      <p><strong>${discovery.valid_count} saved project${discovery.valid_count === 1 ? '' : 's'} found.</strong></p>
      <p class="muted">Search folder: ${escapeHtml(discovery.folder)}</p>
      ${discovery.invalid_count ? `<p class="muted">${discovery.invalid_count} invalid project file${discovery.invalid_count === 1 ? ' was' : 's were'} ignored.</p>` : ''}
      <div class="project-choice-list">${projectButtons}</div>
      <div class="scan-actions"><button class="btn" id="searchAnotherProjectFolder">Search Another Folder</button></div>`;
    modalBody.querySelectorAll('[data-project-index]').forEach(button => {
      button.addEventListener('click', async () => {
        const project = discovery.projects[Number(button.dataset.projectIndex)];
        modal.classList.remove('show');
        try {
          await loadSavedProject(project.project_file);
        } catch (error) {
          showToast(error.message);
        }
      });
    });
    document.getElementById('searchAnotherProjectFolder').onclick = () => {
      modal.classList.remove('show');
      document.getElementById('openProjectBtn').click();
    };
    modal.classList.add('show');
  }

  document.getElementById('openProjectBtn').addEventListener('click', async () => {
    const button = document.getElementById('openProjectBtn');
    button.disabled = true;
    showToast('Select a folder. The system will search it for saved Flight Projects.');
    await nextPaint();
    try {
      const discovery = await api('/api/project/discover', {
        method: 'POST', body: '{}'
      });
      if (discovery.cancelled) {
        showToast('Saved-project folder selection cancelled.');
        return;
      }
      if (!discovery.projects.length) {
        modalTitle.textContent = 'No saved Flight Project found';
        modalBody.innerHTML = `<p>No valid ${escapeHtml('.ccflux')} project file was found below:</p>
          <p><strong>${escapeHtml(discovery.folder)}</strong></p>
          <p class="muted">Choose the Output Folder used when the project was saved, or one of its parent folders.</p>
          <div class="scan-actions"><button class="btn primary" id="retryProjectSearch">Search Another Folder</button></div>`;
        document.getElementById('retryProjectSearch').onclick = () => {
          modal.classList.remove('show');
          button.click();
        };
        modal.classList.add('show');
        return;
      }
      if (discovery.projects.length === 1) {
        await loadSavedProject(discovery.projects[0].project_file);
        return;
      }
      showSavedProjectChoices(discovery);
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  function openNoseboomTab() {
    const link = document.createElement('a');
    link.href = '/noseboom';
    link.target = '_blank';
    link.rel = 'noopener';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
    api('/api/noseboom/log', {
      method: 'POST',
      body: JSON.stringify({ message: 'Noseboom card clicked; opening dedicated tab' })
    }).catch(() => {});
  }
  document.getElementById('pauseCameraBtn').addEventListener('click', toggleCameraQueue);

  function nextPaint() {
    return new Promise(resolve => requestAnimationFrame(() => resolve()));
  }

  async function selectFlightFolder() {
    const button = document.getElementById('openFolderBtn');
    button.disabled = true;
    showToast('Opening Flight Folder window...');
    await nextPaint();
    try {
      const selection = await api('/api/select-scan-folders', { method: 'POST' });
      if (selection.cancelled) {
        showToast('Flight Folder selection cancelled.');
        return null;
      }
      renderScanState(await api('/api/scan'));
      showToast('Flight Folder selected. Click Initial Check when you are ready to scan.');
      return selection;
    } catch (error) {
      showToast(error.message);
      return null;
    } finally {
      button.disabled = false;
    }
  }

  async function selectCameraFolder() {
    const button = document.getElementById('cameraFolderBtn');
    button.disabled = true;
    showToast('Opening Camera Folder window...');
    await nextPaint();
    try {
      const state = await api('/api/scan');
      if (!state.selected_folder) {
        showToast('Select a Flight Folder first.');
        return;
      }
      const result = await api('/api/select-camera-folder', { method: 'POST' });
      if (result.cancelled) {
        showToast('Camera Folder selection cancelled.');
        return;
      }
      renderScanState(await api('/api/scan'));
      showToast('Camera Folder selected. No scan started. Click Initial Check when you are ready.');
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  function confirmInitialScanSelection(selection) {
    modalTitle.textContent = 'Start Initial Check';
    modalBody.innerHTML = `
      <p><strong>A Camera Folder is defined. Do you want to include it in this scan?</strong></p>
      <p><strong>Flight Folder:</strong> ${escapeHtml(selection.folder)}</p>
      <p><strong>Camera Folder:</strong> ${escapeHtml(selection.camera_folder)}</p>
      <p class="muted">Selecting a folder never starts scanning. Choose Flight only to keep the Camera Folder selected for later, or include both folders now.</p>
      <div class="scan-actions">
        <button class="btn" id="cancelInitialScan">Cancel</button>
        <button class="btn" id="scanFlightOnly">Scan Flight Only</button>
        <button class="btn primary" id="scanFlightAndCamera">Scan Flight + Camera</button>
      </div>`;
    document.getElementById('cancelInitialScan').onclick = () => modal.classList.remove('show');
    document.getElementById('scanFlightOnly').onclick = async () => {
      modal.classList.remove('show');
      await startSelectedScans(selection, false);
    };
    document.getElementById('scanFlightAndCamera').onclick = async () => {
      modal.classList.remove('show');
      await startSelectedScans(selection, true);
    };
    modal.classList.add('show');
  }

  async function startSelectedScans(selection, includeCamera = false) {
    try {
      const result = await api('/api/scan', {
        method: 'POST',
        body: JSON.stringify({
          folder: selection.folder,
          camera_folder: selection.camera_folder || null,
          include_camera: Boolean(includeCamera && selection.camera_folder)
        })
      });
      openScanWindows(result);
      startPolling();
    } catch (error) {
      showToast(`Scanning could not start: ${error.message}`);
    }
  }

  async function initialCheck() {
    const button = document.getElementById('initialCheckBtn');
    button.disabled = true;
    try {
      let state = await api('/api/scan');
      if (!state.selected_folder) {
        const chosen = await selectFlightFolder();
        if (!chosen) return;
        state = await api('/api/scan');
      }
      const selection = {
        folder: state.selected_folder,
        camera_folder: state.selected_camera_folder || null
      };
      if (selection.camera_folder) {
        confirmInitialScanSelection(selection);
      } else {
        await startSelectedScans(selection, false);
      }
    } catch (error) {
      showToast(`Initial Check could not start: ${error.message}`);
    } finally {
      button.disabled = false;
    }
  }
  function confirmSystemReset() {
    modalTitle.textContent = 'Reset System';
    modalBody.innerHTML = `
      <div class="danger-warning"><strong>Warning:</strong> Reset will cancel active dashboard jobs and clear all selected folders, detection results, time filters, and queue state. Raw files and existing outputs will not be deleted.</div>
      <p>Do you want to reset the application?</p>
      <div class="scan-actions">
        <button class="btn" id="cancelSystemReset">Keep Current State</button>
        <button class="btn danger" id="confirmSystemReset">Reset System</button>
      </div>`;
    document.getElementById('cancelSystemReset').onclick = () => modal.classList.remove('show');
    document.getElementById('confirmSystemReset').onclick = resetSystem;
    modal.classList.add('show');
  }

  async function resetSystem() {
    try {
      const response = await api('/api/application/reset', { method: 'POST' });
      modal.classList.remove('show');
      resetDiscoveryPanels();
      renderScanState(response.state);
      document.getElementById('flightId').value = '';
      document.getElementById('flightDate').value = '';
      document.getElementById('flightStartTime').value = '';
      document.getElementById('flightEndTime').value = '';
      showToast('System reset complete. No files were modified.');
    } catch (error) {
      showToast(`Reset failed: ${error.message}`);
    }
  }

  function confirmApplicationExit() {
    modalTitle.textContent = 'Exit CC-FLUX';
    modalBody.innerHTML = `
      <div class="danger-warning"><strong>Warning:</strong> Exit will safely stop the local application and cancel pending work.</div>
      <p>Save the Flight Project first if you need to reopen this state.</p>
      <div class="scan-actions">
        <button class="btn" id="cancelApplicationExit">Continue Working</button>
        <button class="btn danger" id="confirmApplicationExit">Exit Application</button>
      </div>`;
    document.getElementById('cancelApplicationExit').onclick = () => modal.classList.remove('show');
    document.getElementById('confirmApplicationExit').onclick = exitApplication;
    modal.classList.add('show');
  }

  async function exitApplication() {
    try {
      await api('/api/application/exit', { method: 'POST' });
      modalTitle.textContent = 'Application Stopped';
      modalBody.innerHTML = '<p>The CC-FLUX backend has stopped safely. You may close this browser tab.</p>';
      clearInterval(scanPoll);
      clearInterval(logPoll);
      clearInterval(queuePoll);
    } catch (error) {
      showToast(`Exit failed: ${error.message}`);
    }
  }

  function updateFlightCoverage() {
    const date = document.getElementById('flightDate').value;
    const takeoff = document.getElementById('flightStartTime').value;
    const landing = document.getElementById('flightEndTime').value;
    const coverage = document.getElementById('flightCoverageValue');
    if (!coverage) return;
    if (!date || !takeoff || !landing) {
      coverage.textContent = '—';
      return;
    }
    const start = Date.parse(`${date}T${takeoff}:00Z`);
    let end = Date.parse(`${date}T${landing}:00Z`);
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
      coverage.textContent = '—';
      return;
    }
    if (end <= start) end += 24 * 60 * 60 * 1000;
    const seconds = Math.round((end - start) / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    coverage.textContent = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
  }
  async function synchronizeFlightTimes() {
    const flightDate = document.getElementById('flightDate').value;
    const takeoff = document.getElementById('flightStartTime').value;
    const landing = document.getElementById('flightEndTime').value;
    updateFlightCoverage();
    if (!flightDate || !takeoff || !landing) return;
    await changeTimeFilter({
      action: 'set',
      start: inputToIso(`${flightDate}T${takeoff}:00`, 'UTC'),
      end: inputToIso(`${flightDate}T${landing}:00`, 'UTC')
    });
  }

  function positionScanWindows() {
    const topbar = document.querySelector('.topbar');
    const top = Math.ceil((topbar?.getBoundingClientRect().bottom || 138) + 12);
    document.documentElement.style.setProperty('--scan-window-top', `${top}px`);
  }

  function openScanWindows(selection) {
    positionScanWindows();
    const flight = document.getElementById('flightScanWindow');
    flight.classList.remove('minimized', 'maximized');
    flight.classList.add('show');
    document.getElementById('flightScanRoot').textContent = selection.folder || '—';
    if (selection.camera_folder) {
      const camera = document.getElementById('cameraScanWindow');
      camera.classList.remove('minimized', 'maximized');
      camera.classList.add('show');
      document.getElementById('cameraScanRoot').textContent = selection.camera_folder;
    }
  }

  window.addEventListener('resize', positionScanWindows);

  function handleScanWindowAction(source, action) {
    const windowElement = document.getElementById(`${source}ScanWindow`);
    if (action === 'minimize') {
      windowElement.classList.toggle('minimized');
      windowElement.classList.remove('maximized');
    } else if (action === 'maximize') {
      windowElement.classList.toggle('maximized');
      windowElement.classList.remove('minimized');
    } else if (action === 'close') {
      const channel = latestScanState.scans?.[source] || {};
      if (channel.running) confirmStopScan(source, true);
      else windowElement.classList.remove('show');
    }
  }

  function confirmStopScan(source, hideAfter) {
    const channel = latestScanState.scans?.[source] || {};
    if (!channel.running) {
      if (hideAfter) document.getElementById(`${source}ScanWindow`).classList.remove('show');
      return;
    }
    modalTitle.textContent = `Stop ${capitalize(source)} Scan`;
    modalBody.innerHTML = `
      <div class="danger-warning"><strong>Do you want to stop scanning?</strong></div>
      <p>Only the ${escapeHtml(source)}-data scanning process will stop. The other scan will continue independently.</p>
      <div class="scan-actions">
        <button class="btn" id="keepScanning">No</button>
        <button class="btn danger" id="confirmStopScanning">Yes</button>
      </div>`;
    document.getElementById('keepScanning').onclick = () => modal.classList.remove('show');
    document.getElementById('confirmStopScanning').onclick = async () => {
      modal.classList.remove('show');
      await cancelScan(source);
      if (hideAfter) document.getElementById(`${source}ScanWindow`).classList.remove('show');
    };
    modal.classList.add('show');
  }
  function startPolling() {
    cameraCoverageAnnounced = false;
    if (scanPoll) clearInterval(scanPoll);
    pollScan();
    scanPoll = setInterval(pollScan, 250);
  }

  async function pollScan() {
    try {
      const state = await api('/api/scan');
      renderScanState(state);
      await announceCameraCoverage(state);
      if (!state.running && ['complete', 'cancelled', 'failed'].includes(state.phase)) {
        clearInterval(scanPoll);
        scanPoll = null;
      }
    } catch (error) {
      clearInterval(scanPoll);
      scanPoll = null;
      showToast(`Could not read scan status: ${error.message}`);
    }
  }

  function renderScanState(state) {
    level2Capabilities = state.level2_capabilities || level2Capabilities;
    document.getElementById('selectedFlightFolder').textContent =
      state.selected_folder || 'Not selected';
    document.getElementById('selectedFlightFolder').title =
      state.selected_folder || '';
    document.getElementById('selectedOutputFolder').textContent =
      state.selected_output_folder || 'Not selected';
    document.getElementById('selectedOutputFolder').title =
      state.selected_output_folder || '';
    document.getElementById('selectedCameraFolder').textContent =
      state.selected_camera_folder || 'Optional · Flight Folder-only scan';
    document.getElementById('selectedCameraFolder').title =
      state.selected_camera_folder || '';
    latestScanState = state;
    if (state.selected_folder) {
      document.getElementById('flightId').value = state.flight_id || '';
    } else {
      document.getElementById('flightId').value = '';
    }
    renderScanChannel('flight', state.scans?.flight || {});
    renderScanChannel('camera', state.scans?.camera || {});
    renderControlStates(state);
    renderRemoteSensingState(state);
    Object.values(state.instruments || {}).forEach(instrument => {
      const card = document.querySelector(`[data-instrument-id="${instrument.instrument_id}"]`);
      if (card) updateCard(card, instrument);
    });
    updateSystemPanels();
    updateSummary(state);
    renderTimeState(state.time_filter || {});
    renderResources(state.resources || {});
    renderQueue(state.processing_queue || {});
    renderCameraStatus(state.processing_queue || {});

    if (!state.running && state.error) showToast(state.error);
  }

  function renderScanChannel(source, channel) {
    const prefix = `${source}Scan`;
    document.getElementById(`${prefix}Root`).textContent = channel.root || '—';
    document.getElementById(`${prefix}Folder`).textContent = channel.current_folder || '—';
    document.getElementById(`${prefix}File`).textContent = channel.current_file || '—';
    document.getElementById(`${prefix}Instrument`).textContent =
      channel.current_instrument ||
      (channel.detected_instruments || []).join(', ') ||
      (channel.running ? 'Identifying instrument' : 'None detected');
    document.getElementById(`${prefix}Count`).textContent =
      Number(channel.files_scanned || 0).toLocaleString();
    // A camera delivery is thousands of GoPro frames, a few hundred MicaSense
    // files and one FLIR export, so the single "current file" line sits in
    // GoPro for almost the whole scan. This shows the others are being read.
    const breakdown = document.getElementById(`${prefix}Breakdown`);
    if (breakdown) {
      const counts = (channel.folder_counts || []).filter(entry => entry.files > 0);
      breakdown.textContent = counts.length
        ? counts.map(entry =>
            `${entry.name} ${Number(entry.files).toLocaleString()}`).join(' · ')
        : '—';
    }
    const isComplete = channel.phase === 'complete';
    const completionText = 'Processing is done! close the window!';
    document.getElementById(`${prefix}Messages`).textContent = isComplete
      ? (channel.message || '').includes(completionText)
        ? channel.message
        : `${completionText}\n${channel.message || ''}`.trim()
      : channel.error || channel.message || 'Waiting for scan status.';
    const progress = document.getElementById(`${prefix}Progress`);
    if (channel.progress == null && channel.running) {
      progress.classList.add('indeterminate');
    } else {
      progress.classList.remove('indeterminate');
      progress.querySelector('span').style.width = `${Math.max(0, Math.min(100, Number(channel.progress) || 0))}%`;
      progress.querySelector('span').style.transform = 'none';
    }
    const numericProgress = Math.max(0, Math.min(100, Number(channel.progress) || 0));
    const phase = channel.phase || 'idle';
    const waitingForChecks = channel.progress == null && channel.running;
    const progressText = waitingForChecks
      ? (phase === 'inventory' ? 'Counting...' : 'Checking...')
      : `${Math.round(numericProgress)}%`;
    const percentage = document.getElementById(`${prefix}Percentage`);
    percentage.textContent = progressText;
    percentage.classList.toggle('waiting', waitingForChecks);
    const status = document.getElementById(`${prefix}Status`);
    document.getElementById(`${source}ScanWindow`).dataset.phase = phase;
    const cssClass = phase === 'complete' ? 'complete'
      : ['cancelled', 'failed'].includes(phase) ? 'warning'
        : channel.running ? 'running' : 'queued';
    const label = phase === 'not_selected' ? 'Not selected'
      : phase === 'complete' ? 'Complete'
        : phase === 'cancelled' ? 'Cancelled'
          : phase === 'failed' ? 'Failed'
            : phase === 'inventory' ? 'Counting files'
              : phase === 'post_scan_checks' ? 'Validating'
                : channel.running ? 'Scanning' : capitalize(phase);
    status.className = `status-pill ${cssClass}`;
    status.innerHTML = `<span class="dot"></span>${label}`;
    const stopButton = document.querySelector(`[data-stop-scan="${source}"]`);
    if (stopButton) {
      stopButton.disabled = false;
      stopButton.textContent = channel.running
        ? `Stop ${capitalize(source)} Scan`
        : ['complete', 'cancelled', 'failed'].includes(phase)
          ? 'Close Window'
          : `Stop ${capitalize(source)} Scan`;
    }
  }

  function setConfirmed(element, confirmed) {
    if (!element) return;
    element.classList.toggle('confirmed', Boolean(confirmed));
    element.setAttribute('aria-pressed', String(Boolean(confirmed)));
  }

  function renderControlStates(state) {
    const flightSelected = Boolean(state.selected_folder);
    const cameraSelected = Boolean(state.selected_camera_folder);
    const outputSelected = Boolean(state.selected_output_folder);
    const projectSaved = Boolean(state.project_saved);
    const flightCheckComplete = state.scans?.flight?.phase === 'complete';
    const cameraCheckComplete = state.scans?.camera?.phase === 'complete';
    const enabledJobs = (state.processing_queue?.jobs || []).filter(job =>
      job.enabled && !job.detailed && job.available_for_selection
    );
    const processingComplete = enabledJobs.length > 0 && enabledJobs.every(job =>
      ['complete', 'warning'].includes(job.status)
    );

    setConfirmed(document.getElementById('openFolderBtn'), flightSelected);
    setConfirmed(document.getElementById('cameraFolderBtn'), cameraSelected);
    setConfirmed(document.getElementById('outputFolderBtn'), outputSelected);
    setConfirmed(document.getElementById('initialCheckBtn'), flightCheckComplete);
    setConfirmed(document.getElementById('openProjectBtn'), projectSaved);
    setConfirmed(document.getElementById('saveProjectBtn'), projectSaved);
    setConfirmed(document.getElementById('runBtn'), processingComplete);

    document.querySelectorAll('[data-folder-action]').forEach(card => {
      const selected = card.dataset.folderAction === 'flight' ? flightSelected
        : card.dataset.folderAction === 'camera' ? cameraSelected
          : outputSelected;
      card.classList.toggle('confirmed', selected);
    });

    const remote = document.getElementById('remoteSensingBtn');
    remote.classList.toggle('confirmed', cameraCheckComplete);
  }

  function renderRemoteSensingState(state) {
    const button = document.getElementById('remoteSensingBtn');
    const cameraReady = Boolean(state.camera_scan_ready);
    // Products cannot be selected until the camera scan has finished: their
    // coverage is not known before that, so there is nothing to select against.
    const available = Boolean(state.selected_folder) && !state.running && cameraReady;
    button.disabled = !available;
    button.setAttribute('aria-disabled', String(!available));
    button.classList.toggle('remote-ready', cameraReady);
    button.classList.toggle('remote-inactive', !cameraReady);
    button.title = !state.selected_folder
      ? 'Select and scan a Flight Folder first'
      : state.running
        ? 'Wait for the active scan to finish'
        : cameraReady
          ? 'Select the remote-sensing products and the period to process'
          : 'Run Initial Check with a Camera Folder; selection opens when scanning finishes';
  }
  function updateCard(card, instrument) {
    const activeProcessing = instrument.processing_status &&
      instrument.processing_status !== 'idle';
    const [cssClass, label] = activeProcessing
      ? (processingPresentation[instrument.processing_status] || processingPresentation.failed)
      : (statusPresentation[instrument.detection_status] || statusPresentation.failed);
    card.dataset.status = activeProcessing
      ? instrument.processing_status
      : instrument.detection_status;
    card.classList.toggle('is-processing', instrument.processing_status === 'processing');
    card.classList.toggle('is-complete', instrument.processing_status === 'complete');
    card.classList.toggle('is-warning', instrument.processing_status === 'warning');
    card.classList.toggle('is-failed', instrument.processing_status === 'failed');
    const pill = card.querySelector('.status-pill');
    pill.className = `status-pill ${cssClass}`;
    pill.innerHTML = `<span class="dot"></span>${label}`;
    const detected = instrument.detection_status !== 'not_detected';
    const details = [
      `Source: ${detected ? 'detected' : 'not detected'}`,
      `${Number(instrument.file_count || 0).toLocaleString()} matching files`
    ];
    if (instrument.utc_start_time && instrument.utc_end_time) {
      details.push(`${formatTime(instrument.utc_start_time)}–${formatTime(instrument.utc_end_time)} UTC`);
    }
    if (instrument.ambiguous) details.push('Candidate confirmation required');
    if (instrument.errors?.length) details.push(instrument.errors[0]);
    else if (instrument.warnings?.length) details.push(instrument.warnings[0]);
    details.push(`Processing: ${capitalize(instrument.processing_status || 'idle')} · ${Number(instrument.processing_progress || 0).toFixed(0)}%`);
    details.push(`Step: ${instrument.processing_step || 'Not started'} · Elapsed: ${formatElapsed(instrument.processing_elapsed_seconds || 0)}`);
    card.querySelector('.instrument-meta').innerHTML = details.map(escapeHtml).join('<br>');
    card._scanState = instrument;
  }

  function updateSummary(state) {
    const summary = state.summary || {};
    document.getElementById('readyCount').textContent =
      `${summary.ready_count || 0} / ${document.querySelectorAll('.instrument-card').length}`;
    document.getElementById('warningCount').textContent =
      String((summary.warning_count || 0) + (summary.failed_count || 0));
    updateFlightCoverage();

    document.getElementById('cameraState').textContent = 'Not started';
  }

  function renderResources(resources) {
    const cpu = document.getElementById('cpuAllocation');
    const ram = document.getElementById('ramAllocation');
    if (Array.isArray(resources.worker_options)) {
      cpu.innerHTML = resources.worker_options.map(value =>
        `<option value="${value}">${value} worker${Number(value) === 1 ? '' : 's'}${Number(value) === Number(resources.recommended_worker_count) ? ' — recommended' : ''}</option>`
      ).join('');
      cpu.value = String(resources.selected_worker_count);
    }
    if (Array.isArray(resources.ram_options)) {
      ram.innerHTML = resources.ram_options.map(value =>
        `<option value="${value}">${formatBytes(value)}${Number(value) === Number(resources.recommended_ram_bytes) ? ' — recommended' : ''}</option>`
      ).join('');
      ram.value = String(resources.selected_ram_bytes);
    }
    const allocationMode = resources.selection_mode === 'automatic'
      ? 'automatic recommendation'
      : 'operator selected';
    document.getElementById('cpuDetected').textContent =
      `Min ${resources.minimum_worker_count ?? 1} · Using ${resources.selected_worker_count ?? '—'} (${allocationMode}) · Max ${resources.maximum_worker_count ?? resources.safe_worker_count ?? '—'} · ${resources.total_logical_cores ?? '—'} logical cores detected, ${resources.reserved_gui_cores ?? 1} reserved`;
    document.getElementById('ramDetected').textContent =
      `Min ${formatBytes(resources.minimum_ram_bytes)} · Using ${formatBytes(resources.selected_ram_bytes)} (${allocationMode}) · Max ${formatBytes(resources.maximum_ram_bytes ?? resources.safe_ram_bytes)} · ${formatBytes(resources.total_ram_bytes)} detected`;
  }
  function renderQueue(queue) {
    currentQueue = queue || { jobs: [] };
    const jobs = currentQueue.jobs || [];
    const busy = isSystemBusy();
    const workflow = currentQueue.workflow || {};
    const selected = Number(currentQueue.selected_count || 0);
    const workflowPill = document.getElementById('priorityWorkflowState');
    if (workflowPill) {
      workflowPill.className = `status-pill ${busy ? 'running' : selected ? 'complete' : 'queued'}`;
      workflowPill.innerHTML = `<span class="dot"></span>${escapeHtml(busy ? 'System busy' : selected ? `${selected} selected` : 'Selection required')}</span>`;
    }
    const runButton = document.getElementById('runBtn');
    runButton.disabled = !currentQueue.can_start;
    runButton.title = currentQueue.can_start ? 'Start selected instruments with the global Time Filter' : (workflow.next_step || 'Complete the workflow first');
    priorityList.innerHTML = `<div class="queue-guide"><strong>1. Complete scan and Time Filter</strong><span>2. Select instruments</span><span>3. Check health and start</span><small>${escapeHtml(workflow.next_step || 'Select instruments after the flight scan is complete.')}</small></div>` + jobs.map(job => {
      const rowClass = job.status === 'processing' ? 'is-processing' : job.status === 'complete' ? 'is-complete' : job.status === 'failed' ? 'is-failed' : job.status === 'warning' ? 'is-warning' : '';
      const statusClass = job.status === 'complete' ? 'complete' : job.status === 'processing' ? 'running' : ['warning', 'failed'].includes(job.status) ? 'warning' : 'queued';
      const detailedAvailable = job.detailed && (level2Capabilities[job.instrument_id] || []).some(item => item.available);
      const actions = [];
      if (job.previously_completed && !job.detailed) {
        actions.push(`<button class="btn" data-queue-action="reprocess" ${busy ? 'disabled' : ''}>Reprocess</button>`);
      } else if (job.instrument_id === 'sif' && !job.detailed) {
        actions.push(`<button class="btn" data-queue-action="configure_sif" ${busy ? 'disabled' : ''}>Configure SIF</button>`);
        actions.push(`<button class="btn" data-queue-action="sif_progress">SIF Progress</button>`);
      } else if (job.detailed && !job.enabled) {
        actions.push(`<button class="btn primary" data-queue-action="configure_detailed" ${detailedAvailable && !busy ? '' : 'disabled'}>${detailedAvailable ? 'Configure detailed processing' : 'Not yet available'}</button>`);
      }
      const selectable = Boolean(job.available_for_selection);
      const selectionNote = selectable ? '' : ` · ${escapeHtml(job.selection_reason || 'Not available for selection')}`;
      return `<div class="priority-job ${rowClass}" draggable="${busy ? 'false' : 'true'}" data-job-id="${escapeAttribute(job.job_id)}"><span class="priority-handle" aria-label="Drag to reorder">☷</span><label class="queue-select" title="${escapeAttribute(job.selection_reason || '')}"><input type="checkbox" data-queue-select ${job.enabled ? 'checked' : ''} ${job.detailed || busy || !selectable ? 'disabled' : ''}><span>${job.previously_completed ? 'Skipped' : job.detailed ? 'Detailed only' : 'Include'}</span></label><div class="priority-copy"><div class="priority-name">${escapeHtml(job.display_name)}</div><div class="priority-meta"><strong>${escapeHtml(job.current_step || 'Waiting')}</strong> · ${Number(job.progress).toFixed(0)}% complete · ${formatElapsed(job.elapsed_seconds)}${selectionNote}</div><div class="priority-progress" aria-label="${Number(job.progress).toFixed(0)} percent complete"><span style="width:${Math.max(0, Math.min(100, Number(job.progress) || 0))}%"></span></div></div><span>Priority ${job.priority}</span><span class="status-pill ${statusClass}"><span class="dot"></span>${escapeHtml(capitalize(job.status))}</span><div class="priority-actions">${actions.join('')}</div></div>`;
    }).join('');
  }

  function isSystemBusy() {
    return Boolean(currentQueue.busy) || (currentQueue.jobs || []).some(job => job.enabled && ['queued', 'processing'].includes(job.status) && job.current_step !== 'Disabled' && Boolean(job.task_registered));
  }

  function showBusyWarning() { showToast('Please wait! System is busy now!'); }
  function renderCameraStatus(queue) {
    const jobs = new Map((queue.jobs || []).map(job => [job.job_id, job]));
    const cameraRows = [
      ['micasense_quick', 'micaText', 'micaProgress'],
      ['flir_quick', 'flirText', 'flirProgress'],
      ['gopro_quick', 'goproText', 'goproProgress']
    ];
    let active = 0;
    cameraRows.forEach(([jobId, textId, progressId]) => {
      const job = jobs.get(jobId);
      if (!job) return;
      const row = document.getElementById(textId)?.closest('.camera-job');
      if (!row) return;
      const status = job.enabled ? job.status : 'paused';
      if (['queued', 'processing'].includes(status)) active += 1;
      document.getElementById(textId).textContent =
        `${job.current_step} · ${Number(job.progress).toFixed(0)}% · ${formatElapsed(job.elapsed_seconds)}`;
      document.getElementById(progressId).style.width =
        `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`;
      const [cssClass, label] = processingPresentation[status] || ['queued', 'Idle'];
      const pill = row.querySelector('.status-pill');
      pill.className = `status-pill ${cssClass}`;
      pill.innerHTML = `<span class="dot"></span>${job.enabled ? label : 'Disabled'}`;
    });
    document.getElementById('cameraState').textContent =
      active ? `${active} active` : 'Not started';
    const cameraJobs = (queue.jobs || []).filter(job =>
      ['camera_metadata', 'camera_detailed'].includes(job.worker_group)
    );
    const pausable = cameraJobs.some(job => job.status === 'queued' && job.enabled);
    const resumable = cameraJobs.some(job => job.status === 'paused' && job.enabled);
    const cameraButton = document.getElementById('pauseCameraBtn');
    cameraButton.textContent = resumable && !pausable ? 'Resume Cameras' : 'Pause Cameras';
    cameraButton.disabled = !pausable && !resumable;
  }

  async function queueAction(payload) {
    try {
      const response = await api('/api/queue', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      renderQueue(response.processing_queue);
      const selected = Number(response.processing_queue?.selected_count || 0);
      showToast(selected
        ? `${selected} instrument${selected === 1 ? '' : 's'} selected. Start Processing is now ready when the workflow checks are complete.`
        : 'Instrument selection updated. No processing was started automatically.');
      return true;
    } catch (error) {
      showToast(error.message);
      const scan = await api('/api/scan');
      renderQueue(scan.processing_queue || {});
      return false;
    }
  }

  function openLevel2Dialog(jobId) {
    const instrumentId = jobId.replace('_detailed', '');
    const routines = level2Capabilities[instrumentId] || [];
    modalTitle.textContent = `${capitalize(instrumentId)} detailed processing`;
    modalBody.innerHTML = `
      <p class="muted">Detailed processing is optional and will not start without your explicit selection and confirmation.</p>
      <div class="detail-grid">${routines.map(item => `
        <label class="detail-row" title="${escapeAttribute(item.reason)}">
          <input type="checkbox" name="level2Routine" value="${escapeAttribute(item.routine_id)}" ${item.available ? '' : 'disabled'}>
          <span><strong>${escapeHtml(item.label)}</strong><br><small>${escapeHtml(item.available ? item.reason : `Not yet available — ${item.reason}`)}</small></span>
        </label>`).join('')}</div>
      <label class="detail-row">
        <input type="checkbox" id="confirmLevel2">
        <span>I explicitly confirm that the selected detailed processing may start now.</span>
      </label>
      <div class="modal-actions"><button class="btn" id="cancelLevel2">Cancel</button><button class="btn primary" id="startLevel2">Start selected job</button></div>`;
    modal.classList.add('show');
    document.getElementById('cancelLevel2').addEventListener('click', () => modal.classList.remove('show'));
    document.getElementById('startLevel2').addEventListener('click', async () => {
      const selected = Array.from(modalBody.querySelectorAll('input[name="level2Routine"]:checked')).map(input => input.value);
      const confirmed = document.getElementById('confirmLevel2').checked;
      try {
        const response = await api('/api/processing/detailed/start', {
          method: 'POST',
          body: JSON.stringify({ job_id: jobId, selected_routines: selected, confirmed })
        });
        modal.classList.remove('show');
        renderScanState(response.state);
        showToast('Confirmed job started in the dedicated camera worker pool.');
      } catch (error) {
        showToast(error.message);
      }
    });
  }

  function confirmReprocess(jobId, displayName) {
    modalTitle.textContent = `Reprocess ${displayName}?`;
    modalBody.innerHTML = `
      <div class="danger-warning"><strong>Existing result:</strong> This instrument
      has already been processed and is skipped by default.</div>
      <p>Only continue if its source data, processing settings, or selected Time
      Filter changed. A new run will be created; raw data will not be modified.</p>
      <div class="modal-actions">
        <button class="btn" id="cancelReprocess">Keep previous result</button>
        <button class="btn danger" id="confirmReprocess">Queue reprocessing</button>
      </div>`;
    modal.classList.add('show');
    document.getElementById('cancelReprocess').onclick = () => {
      modal.classList.remove('show');
    };
    document.getElementById('confirmReprocess').onclick = async () => {
      modal.classList.remove('show');
      const queued = await queueAction({
        action: 'reprocess',
        job_id: jobId,
        confirmed: true
      });
      if (queued) {
        showToast(`${displayName} is selected for explicit reprocessing. Click Start Processing when ready.`);
      }
    };
  }

  function fileName(path) {
    if (!path) return null;
    const parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || String(path);
  }

  function sifEssentialRow(kind, label, current) {
    const chosen = fileName(current);
    return `<div class="form-row">
      <span>${escapeHtml(label)}</span>
      <div class="file-choice">
        <span class="file-choice-name" data-sif-file-name="${kind}" title="${escapeAttribute(current || '')}">${
          chosen ? escapeHtml(chosen) : 'Bundled default'
        }</span>
        <button class="btn" type="button" data-sif-file="${kind}">Choose file</button>
        <button class="btn" type="button" data-sif-file-clear="${kind}" ${chosen ? '' : 'disabled'}>Use default</button>
      </div>
    </div>`;
  }

  function wireSifEssentialButtons() {
    modalBody.querySelectorAll('[data-sif-file]').forEach(button => {
      button.addEventListener('click', async () => {
        const kind = button.dataset.sifFile;
        button.disabled = true;
        showToast('Select the file. The window opens in front of the browser.');
        try {
          const result = await api('/api/sif/select-file', {
            method: 'POST', body: JSON.stringify({ kind })
          });
          if (result.cancelled) { showToast('File selection cancelled.'); return; }
          latestScanState.sif_options = result.options;
          openSifConfiguration();
          showToast(`Using ${fileName(result.path)}.`);
        } catch (error) {
          showToast(error.message);
        } finally {
          button.disabled = false;
        }
      });
    });
    modalBody.querySelectorAll('[data-sif-file-clear]').forEach(button => {
      button.addEventListener('click', async () => {
        const kind = button.dataset.sifFileClear;
        try {
          const response = await api('/api/sif/options', {
            method: 'POST', body: JSON.stringify({ [kind]: '' })
          });
          latestScanState.sif_options = response.options;
          openSifConfiguration();
          showToast('Reverted to the bundled file.');
        } catch (error) {
          showToast(error.message);
        }
      });
    });
  }

  let sifProgressTimer = null;

  function renderSifProgress(progress) {
    const body = document.getElementById('sifProgressBody');
    if (!body) return;
    const icon = { done: '&#10003;', active: '&#9679;', pending: '&#9675;' };
    const rows = (progress.stages || []).map(stage => `
      <li class="stage stage-${stage.status}">
        <span class="stage-mark">${icon[stage.status] || icon.pending}</span>
        <span class="stage-label">${escapeHtml(stage.label)}</span>
      </li>`).join('');
    const calibration = progress.calibration || {};
    const custom = Object.entries(calibration).filter(([, value]) => value);
    const minutes = Math.floor((progress.elapsed_seconds || 0) / 60);
    const seconds = Math.round((progress.elapsed_seconds || 0) % 60);
    const status = String(progress.status || 'idle');
    const finished = ['complete', 'warning'].includes(status);
    const failed = ['failed', 'cancelled'].includes(status);
    body.innerHTML = `
      <div class="progress-bar"><div class="progress-fill" style="width:${Math.max(0, Math.min(100, progress.percent || 0))}%"></div></div>
      ${finished
        ? '<p><strong class="sif-done">&#10003; Done</strong> — SIF / FLOX processing finished. Open the SIF card for the overview and maps.</p>'
        : failed
          ? `<p><strong>Stopped: ${escapeHtml(status)}</strong> — see Processing Log &amp; Diagnostics.</p>`
          : `<p><strong>${(progress.percent || 0).toFixed(1)}%</strong> — ${escapeHtml(progress.step || 'Not started')}</p>`}
      <p class="muted">Elapsed ${minutes}m ${String(seconds).padStart(2, '0')}s · status ${escapeHtml(status)}</p>
      <ul class="stage-list">${rows}</ul>
      <p class="muted">${custom.length
        ? `Custom files: ${custom.map(([, value]) => escapeHtml(fileName(value))).join(', ')}`
        : 'Using the bundled CAL_FROG and Indices_ICOS files.'}</p>
      <p class="muted">Warnings and errors are written to Processing Log &amp; Diagnostics and saved with the project.</p>`;
  }

  async function refreshSifProgress() {
    try {
      const progress = await api('/api/sif/progress');
      renderSifProgress(progress);
      return String(progress.status || '');
    } catch (_) {
      return '';        // a failed poll must not close the window or stop the run
    }
  }

  function openSifProgressWindow() {
    modalTitle.textContent = 'SIF / FLOX processing progress';
    modalBody.innerHTML = `<div id="sifProgressBody"><p class="muted">Loading…</p></div>
      <div class="modal-actions"><button class="btn" id="closeSifProgress">Close</button></div>`;
    modal.classList.add('show');
    document.getElementById('closeSifProgress').onclick = () => {
      modal.classList.remove('show');
      if (sifProgressTimer) { clearInterval(sifProgressTimer); sifProgressTimer = null; }
    };
    refreshSifProgress();
    if (sifProgressTimer) clearInterval(sifProgressTimer);
    sifProgressTimer = setInterval(async () => {
      if (!modal.classList.contains('show')) {
        clearInterval(sifProgressTimer); sifProgressTimer = null; return;
      }
      const status = await refreshSifProgress();
      if (['complete', 'warning', 'failed', 'cancelled'].includes(status)) {
        clearInterval(sifProgressTimer); sifProgressTimer = null;
      }
    }, 1500);
  }

  // beforeProcessing turns the dialog into the settings step of a processing
  // run: it resolves true when the operator saves and wants to go ahead, false
  // when they back out, so the caller can wait for the answer.
  function openSifConfiguration(options = {}) {
    const beforeProcessing = Boolean(options.beforeProcessing);
    let settle = () => {};
    const answered = new Promise(resolve => { settle = resolve; });
    const options = latestScanState.sif_options || {};
    const modes = options.modes || ['FULL', 'FLUO'];
    const checked = value => value ? 'checked' : '';
    modalTitle.textContent = 'SIF / FLOX processing options';
    modalBody.innerHTML = `
      <p class="muted">The dashboard global Time Filter is applied to both FULL and FLUO after Gimbal attitude is matched with Noseboom position.</p>
      <div class="detail-grid">
        <label class="detail-row"><input type="checkbox" id="sifModeFull" ${checked(modes.includes('FULL'))}><span><strong>FULL / FLOX</strong><br><small>Radiance, reflectance, PAR/APAR and vegetation indices.</small></span></label>
        <label class="detail-row"><input type="checkbox" id="sifModeFluo" ${checked(modes.includes('FLUO'))}><span><strong>FLUO</strong><br><small>High-resolution fluorescence, SIF A/B iFLD and indices.</small></span></label>
        <label class="detail-row"><input type="checkbox" id="sifSpectralShift" ${checked(options.spectral_shift_correction)}><span><strong>FULL spectral shift correction</strong><br><small>Off by default for reproducibility; safe O₂-A shift estimation.</small></span></label>
        <label class="detail-row"><input type="checkbox" id="sifNonlinearity" ${checked(options.apply_nonlinearity_correction)}><span><strong>Nonlinearity correction</strong><br><small>Requires the bundled calibration NL coefficient block.</small></span></label>
        <label class="detail-row"><input type="checkbox" id="sifDropUnmatched" ${checked(options.drop_unmatched_telemetry !== false)}><span><strong>Drop unmatched telemetry rows</strong><br><small>Remove spectra without a Gimbal/Noseboom position match.</small></span></label>
        <label class="detail-row"><input type="checkbox" id="sifDropInvalid" ${checked(options.drop_invalid_spectral_rows)}><span><strong>Drop invalid spectral rows</strong><br><small>Remove rows lacking finite incoming, reflected, or reflectance spectra.</small></span></label>
        <label class="detail-row"><input type="checkbox" id="sifAltitudeFilter" ${checked(options.altitude_filter)}><span><strong>Altitude flight filter</strong><br><small>Use Noseboom altitude to retain the detected flying interval.</small></span></label>
      </div>
      <label class="form-row"><span>Position source</span><select id="sifPositionMode"><option value="uav_airship">UAV/Airship — Gimbal + Noseboom</option><option value="tower">Tower/static</option></select></label>
      <label class="form-row"><span>Ignore raw files smaller than [KB]</span><input id="sifRawMinKb" type="number" min="0" step="1" value="${escapeAttribute(options.raw_min_kb ?? 100)}"><small>Validated default: 100 KB. Small startup or incomplete files are skipped without modifying them.</small></label>
      <label class="form-row"><span>Maximum position time gap [s]</span><input id="sifPositionGap" type="number" min="0.01" max="10" step="0.01" value="${escapeAttribute(options.max_position_gap_seconds ?? 0.2)}"></label>
      <div class="detail-grid">
        <label class="form-row"><span>Static latitude</span><input id="sifStaticLat" type="number" step="any" value="${escapeAttribute(options.static_lat ?? '')}"></label>
        <label class="form-row"><span>Static longitude</span><input id="sifStaticLon" type="number" step="any" value="${escapeAttribute(options.static_lon ?? '')}"></label>
        <label class="form-row"><span>Static altitude [m]</span><input id="sifStaticAlt" type="number" step="any" value="${escapeAttribute(options.static_alt ?? '')}"></label>
      </div>
      <h4 class="section-heading">Calibration and vegetation indices</h4>
      <p class="muted">The validated CAL_FROG and Indices_ICOS files shipped with CC-FLUX are used unless you choose your own. A recalibrated instrument or a different index list goes here.</p>
      <div class="detail-grid">
        ${sifEssentialRow('calibration_full', 'FULL / FLOX calibration', options.calibration_full)}
        ${sifEssentialRow('calibration_fluo', 'FLUO calibration', options.calibration_fluo)}
        ${sifEssentialRow('indices_file', 'Vegetation-index definitions', options.indices_file)}
      </div>
      <div class="modal-actions"><button class="btn" id="cancelSifOptions">${
        beforeProcessing ? 'Cancel processing' : 'Cancel'
      }</button><button class="btn primary" id="saveSifOptions">${
        beforeProcessing ? 'Save and start processing' : 'Save SIF options'
      }</button></div>`;
    modal.classList.add('show');
    document.getElementById('sifPositionMode').value = options.position_mode || 'uav_airship';
    wireSifEssentialButtons();
    document.getElementById('cancelSifOptions').onclick = () => {
      modal.classList.remove('show');
      settle(false);
    };
    document.getElementById('saveSifOptions').onclick = async () => {
      const value = id => document.getElementById(id).value.trim();
      const rawMinKb = Number(value('sifRawMinKb'));
      if (!Number.isFinite(rawMinKb) || rawMinKb < 0) {
        showToast('SIF raw-file size filter must be a non-negative number.');
        return;
      }
      if (rawMinKb > 150 && !window.confirm(
        `The ${rawMinKb.toLocaleString()} KB SIF filter may exclude valid AirFloX files. Continue?`
      )) return;
      const payload = {
        modes: [
          document.getElementById('sifModeFull').checked ? 'FULL' : null,
          document.getElementById('sifModeFluo').checked ? 'FLUO' : null
        ].filter(Boolean),
        position_mode: document.getElementById('sifPositionMode').value,
        raw_min_kb: rawMinKb,
        spectral_shift_correction: document.getElementById('sifSpectralShift').checked,
        apply_nonlinearity_correction: document.getElementById('sifNonlinearity').checked,
        drop_unmatched_telemetry: document.getElementById('sifDropUnmatched').checked,
        drop_invalid_spectral_rows: document.getElementById('sifDropInvalid').checked,
        altitude_filter: document.getElementById('sifAltitudeFilter').checked,
        max_position_gap_seconds: Number(value('sifPositionGap')),
        static_lat: value('sifStaticLat') || null,
        static_lon: value('sifStaticLon') || null,
        static_alt: value('sifStaticAlt') || null
      };
      try {
        const response = await api('/api/sif/options', {
          method: 'POST',
          body: JSON.stringify(payload)
        });
        latestScanState.sif_options = response.options;
        modal.classList.remove('show');
        if (beforeProcessing) {
          settle(true);
        } else {
          showToast('SIF options saved. Select SIF and click Start Processing when ready.');
        }
      } catch (error) {
        showToast(error.message);
      }
    };
    return answered;
  }

  function logRemoteWorkflow(message, step) {
    api('/api/remote-sensing/log', {
      method: 'POST',
      body: JSON.stringify({ message, step })
    }).catch(() => {});
  }

  const REMOTE_SENSING_INSTRUMENTS = [
    ['gopro', 'GoPro'],
    ['flir', 'FLIR'],
    ['micasense', 'MicaSense']
  ];

  function remoteTimeOption(value, label, start, end, note) {
    const usable = Boolean(start && end);
    return `<label class="detail-row${usable ? '' : ' stage-pending'}">
      <input type="radio" name="remoteTimeMode" value="${value}" ${usable ? '' : 'disabled'}>
      <span><strong>${escapeHtml(label)}</strong><br><small>${
        usable
          ? `${escapeHtml(displayDateTime(start, 'UTC'))} – ${escapeHtml(displayDateTime(end, 'UTC'))}`
          : 'Not available for the scanned products'
      }${note ? `<br>${escapeHtml(note)}` : ''}</small></span>
    </label>`;
  }

  async function openRemoteSensingDialog() {
    let coverage;
    try {
      coverage = await api('/api/remote-sensing/coverage');
    } catch (error) {
      showToast(error.message);
      return;
    }
    if (!coverage.ready) {
      showToast(coverage.scanning
        ? 'Camera scanning is still running. The products can be selected when it finishes.'
        : 'Run Initial Check first. Remote-sensing products can be selected once camera scanning has finished.');
      return;
    }
    logRemoteWorkflow('Remote-sensing selection opened', 'confirmation');
    const selectable = (coverage.products || []).filter(item => item.selectable);
    const unusable = (coverage.products || []).filter(
      item => item.detected && !item.selectable
    );
    if (!selectable.length) {
      modalTitle.textContent = 'Remote Sensing Products';
      modalBody.innerHTML = `<p>No scanned camera product has usable UTC coverage.</p>
        ${unusable.length ? `<p class="muted">Found but not selectable: ${
          unusable.map(item => escapeHtml(item.display_name)).join(', ')
        }. A product needs a readable clock before it can be matched to the flight.</p>` : ''}
        <div class="modal-actions"><button class="btn" id="closeRemote">Close</button></div>`;
      document.getElementById('closeRemote').onclick = () => modal.classList.remove('show');
      modal.classList.add('show');
      return;
    }
    modalTitle.textContent = 'Remote Sensing Products';
    modalBody.innerHTML = `
      <p>Select the products to process and the period to process them over.</p>
      <h4 class="section-heading">Products</h4>
      <div class="detail-grid" id="remoteInstruments">
        ${selectable.map(item => `
          <label class="detail-row">
            <input type="checkbox" name="remoteInstrument" value="${escapeAttribute(item.instrument_id)}" checked>
            <span><strong>${escapeHtml(item.display_name)}</strong><br><small>${
              Number(item.file_count || 0).toLocaleString()} file(s) · ${
              escapeHtml(displayDateTime(item.utc_start, 'UTC'))} – ${
              escapeHtml(displayDateTime(item.utc_end, 'UTC'))}</small></span>
          </label>`).join('')}
      </div>
      ${unusable.length ? `<p class="muted">Not selectable: ${
        unusable.map(item => escapeHtml(item.display_name)).join(', ')
      } — no usable UTC coverage.</p>` : ''}
      <h4 class="section-heading">Period</h4>
      ${remoteTimeOption('global', 'Detected global minimum and maximum',
        coverage.detected_global_start, coverage.detected_global_end,
        'Everything the selected products cover.')}
      ${remoteTimeOption('overlap', 'Common overlapping timeframe',
        coverage.common_overlap_start, coverage.common_overlap_end,
        'The period every scanned product covers.')}
      <label class="detail-row">
        <input type="radio" name="remoteTimeMode" value="custom">
        <span><strong>Custom period</strong></span>
      </label>
      <div class="time-edit-grid" id="remoteCustomTime" hidden>
        <label>Start (UTC)<input type="datetime-local" id="remoteStart" value="${
          escapeAttribute(inputDateTime(coverage.detected_global_start, 'UTC'))}"></label>
        <label>End (UTC)<input type="datetime-local" id="remoteEnd" value="${
          escapeAttribute(inputDateTime(coverage.detected_global_end, 'UTC'))}"></label>
      </div>
      <div class="modal-actions">
        <button class="btn" id="cancelRemote">Cancel</button>
        <button class="btn primary" id="verifyRemote">Verify request</button>
      </div>`;
    const firstUsable = modalBody.querySelector('input[name="remoteTimeMode"]:not([disabled])');
    if (firstUsable) firstUsable.checked = true;
    modalBody.querySelectorAll('input[name="remoteTimeMode"]').forEach(input => {
      input.addEventListener('change', () => {
        document.getElementById('remoteCustomTime').hidden =
          !(input.value === 'custom' && input.checked);
      });
    });
    document.getElementById('cancelRemote').onclick = () => {
      logRemoteWorkflow('Remote-sensing selection cancelled', 'confirmation');
      modal.classList.remove('show');
    };
    document.getElementById('verifyRemote').onclick = () => verifyRemoteSensing(coverage);
    modal.classList.add('show');
  }

  async function verifyRemoteSensing(coverage) {
    const instruments = [
      ...modalBody.querySelectorAll('input[name="remoteInstrument"]:checked')
    ].map(input => input.value);
    if (!instruments.length) {
      showToast('Select at least one product to process.');
      return;
    }
    const chosen = modalBody.querySelector('input[name="remoteTimeMode"]:checked');
    if (!chosen) {
      showToast('Select a period to process.');
      return;
    }
    const payload = { instruments, time_mode: chosen.value };
    if (chosen.value === 'custom') {
      const start = document.getElementById('remoteStart').value;
      const end = document.getElementById('remoteEnd').value;
      if (!start || !end) {
        showToast('Both custom start and end are required.');
        return;
      }
      payload.start = inputToIso(start, 'UTC');
      payload.end = inputToIso(end, 'UTC');
      if (new Date(payload.start) >= new Date(payload.end)) {
        showToast('The start must be before the end.');
        return;
      }
    }
    let preview;
    try {
      preview = await api('/api/remote-sensing/preview', {
        method: 'POST', body: JSON.stringify(payload)
      });
    } catch (error) {
      showToast(error.message);
      return;
    }
    logRemoteWorkflow(
      `Remote-sensing request verified: ${instruments.join(', ')} over ${preview.start} to ${preview.end}`,
      'timeframe'
    );
    showRemoteSensingConfirmation(preview, payload, coverage);
  }

  function showRemoteSensingConfirmation(preview, payload, coverage) {
    const hours = Math.floor(preview.duration_seconds / 3600);
    const minutes = Math.round((preview.duration_seconds % 3600) / 60);
    const covered = preview.products.filter(item => item.covers_interval);
    const missed = preview.products.filter(item => !item.covers_interval);
    modalTitle.textContent = 'Verifying your request';
    modalBody.innerHTML = `
      <p><strong>Period</strong><br>${escapeHtml(displayDateTime(preview.start, 'UTC'))} – ${
        escapeHtml(displayDateTime(preview.end, 'UTC'))} UTC<br>
      <span class="muted">${hours}h ${String(minutes).padStart(2, '0')}m</span></p>
      <p><strong>Products to process</strong></p>
      <ul class="stage-list">
        ${covered.map(item => `<li class="stage stage-done"><span class="stage-mark">&#10003;</span>
          <span class="stage-label">${escapeHtml(item.display_name)} — ${
            Number(item.file_count || 0).toLocaleString()} file(s)</span></li>`).join('')}
        ${missed.map(item => `<li class="stage stage-pending"><span class="stage-mark">&#9675;</span>
          <span class="stage-label">${escapeHtml(item.display_name)} — no data in this period</span></li>`).join('')}
      </ul>
      ${preview.ignored?.length ? `<p class="muted">Ignored, no usable coverage: ${
        preview.ignored.map(escapeHtml).join(', ')}</p>` : ''}
      ${preview.warnings?.length ? preview.warnings.map(
        warning => `<p class="muted">${escapeHtml(warning)}</p>`).join('') : ''}
      ${preview.ready_to_start
        ? '<p class="muted">Processing runs in its own worker pool and does not affect the flight instruments.</p>'
        : '<p><strong>Nothing would be processed in this period.</strong> Go back and widen it.</p>'}
      <div class="modal-actions">
        <button class="btn" id="backRemote">Back</button>
        ${preview.ready_to_start
          ? '<button class="btn primary" id="startRemote">Start processing</button>'
          : ''}
      </div>`;
    document.getElementById('backRemote').onclick = () => openRemoteSensingDialog();
    const startButton = document.getElementById('startRemote');
    if (startButton) {
      startButton.onclick = async () => {
        startButton.disabled = true;
        try {
          const response = await api('/api/remote-sensing/start', {
            method: 'POST', body: JSON.stringify(payload)
          });
          renderScanState(response.state);
          modal.classList.remove('show');
          showToast('Remote-sensing processing started in the independent camera worker pool.');
          logRemoteWorkflow('Remote-sensing processing started', 'processing');
        } catch (error) {
          showToast(error.message);
          logRemoteWorkflow(`Remote-sensing processing failed to start: ${error.message}`, 'error');
          startButton.disabled = false;
        }
      };
    }
    modal.classList.add('show');
  }


  async function announceCameraCoverage(state) {
    // The camera scan runs on its own. When it finishes, show what it found and
    // over what period, so the operator can select products without hunting for
    // the coverage first.
    const camera = state.scans?.camera || {};
    if (camera.running || state.running) return;
    const finished = camera.phase === 'complete' && !camera.cancelled && !camera.error;
    if (!finished || cameraCoverageAnnounced) return;
    cameraCoverageAnnounced = true;
    let coverage;
    try {
      coverage = await api('/api/remote-sensing/coverage');
    } catch (_) {
      return;
    }
    const found = (coverage.products || []).filter(item => item.detected);
    if (!found.length) return;
    modalTitle.textContent = 'Remote sensing — camera scan finished';
    modalBody.innerHTML = `
      <p>Scanning found ${found.length} camera product${found.length === 1 ? '' : 's'}
      and their available time.</p>
      <ul class="stage-list">
        ${found.map(item => `<li class="stage ${item.selectable ? 'stage-done' : 'stage-pending'}">
          <span class="stage-mark">${item.selectable ? '&#10003;' : '&#9675;'}</span>
          <span class="stage-label"><strong>${escapeHtml(item.display_name)}</strong> — ${
            Number(item.file_count || 0).toLocaleString()} file(s)<br><small>${
            item.selectable
              ? `${escapeHtml(displayDateTime(item.utc_start, 'UTC'))} – ${escapeHtml(displayDateTime(item.utc_end, 'UTC'))} UTC`
              : 'No usable UTC coverage; this product cannot be selected.'
          }</small></span></li>`).join('')}
      </ul>
      ${coverage.detected_global_start ? `<p><strong>Available overall</strong><br>${
        escapeHtml(displayDateTime(coverage.detected_global_start, 'UTC'))} – ${
        escapeHtml(displayDateTime(coverage.detected_global_end, 'UTC'))} UTC</p>` : ''}
      ${coverage.common_overlap_start ? `<p><strong>Common to all</strong><br>${
        escapeHtml(displayDateTime(coverage.common_overlap_start, 'UTC'))} – ${
        escapeHtml(displayDateTime(coverage.common_overlap_end, 'UTC'))} UTC</p>` : ''}
      <div class="modal-actions">
        <button class="btn" id="closeCameraCoverage">Close</button>
        <button class="btn primary" id="openRemoteFromCoverage">Select products</button>
      </div>`;
    document.getElementById('closeCameraCoverage').onclick = () => modal.classList.remove('show');
    document.getElementById('openRemoteFromCoverage').onclick = () => openRemoteSensingDialog();
    modal.classList.add('show');
    logRemoteWorkflow('Camera coverage presented after scanning', 'scan-result');
  }
  async function startRegisteredProcessing() {
    if (isSystemBusy()) { showBusyWarning(); return; }
    modalTitle.textContent = 'Checking health';
    modalBody.innerHTML = '<p><strong>Checking the selected instruments and their available time periods...</strong></p><div class="scan-progress indeterminate"><span></span></div>';
    modal.classList.add('show');
    try {
      let scan = await api('/api/scan');
      renderScanState(scan);
      if (!scan.selected_output_folder) {
        modal.classList.remove('show');
        showToast('Select an Output Folder to continue processing.');
        await nextPaint();
        const outputSelection = await api('/api/select-output-folder', { method: 'POST' });
        if (outputSelection.cancelled) {
          showToast('Output Folder selection cancelled. Processing was not started.');
          return;
        }
        showToast(`Output Folder selected: ${outputSelection.folder}`);
        scan = await api('/api/scan');
        renderScanState(scan);
        modalTitle.textContent = 'Checking health';
        modalBody.innerHTML = '<p><strong>Checking the selected instruments and their available time periods...</strong></p><div class="scan-progress indeterminate"><span></span></div>';
        modal.classList.add('show');
      }
      const selected = (currentQueue.jobs || []).filter(job =>
        job.enabled && !job.detailed && job.available_for_selection
      );
      if (!selected.length) {
        modalBody.innerHTML = '<p class="danger-warning">No healthy instrument is selected. Close this window and select at least one instrument in Processing Priority.</p><div class="modal-actions"><button class="btn" id="closeHealthReport">Close</button></div>';
        document.getElementById('closeHealthReport').addEventListener('click', () => modal.classList.remove('show'));
        return;
      }
      const timeState = scan.time_filter || {};
      const ranges = timeState.instruments || {};
      const displayTimezone = timeState.display_timezone || document.getElementById('displayTimezone').value;
      const rows = selected.map(job => {
        const range = ranges[job.instrument_id] || {};
        const percentage = Number(range.availability_percentage);
        const valid = Number.isFinite(percentage) && percentage > 0 && range.available_start && range.available_end;
        return {
          job,
          range,
          percentage,
          valid,
          limited: valid && percentage < 100
        };
      });
      const unavailable = rows.filter(row => !row.valid);
      const limited = rows.filter(row => row.limited);
      const selectedPeriod = timeState.selected_analysis_start && timeState.selected_analysis_end
        ? `${displayDateTime(timeState.selected_analysis_start, displayTimezone)} – ${displayDateTime(timeState.selected_analysis_end, displayTimezone)}`
        : 'No valid global interval';
      modalBody.innerHTML = `
        <p><strong>Selected Time Filter:</strong> ${escapeHtml(selectedPeriod)}</p>
        <div class="detail-grid">${rows.map(row => `
          <div class="detail-row">
            <span><strong>${escapeHtml(row.job.display_name)}</strong><br><small>${row.valid
              ? `${escapeHtml(displayDateTime(row.range.available_start, displayTimezone))} – ${escapeHtml(displayDateTime(row.range.available_end, displayTimezone))} · ${row.percentage.toFixed(1)}% available`
              : 'No valid data are available inside the selected interval'}</small></span>
            <span class="status-pill ${row.valid ? (row.limited ? 'warning' : 'complete') : 'warning'}"><span class="dot"></span>${row.valid ? (row.limited ? 'Partial' : 'Healthy') : 'Unavailable'}</span>
          </div>`).join('')}</div>
        ${limited.length ? '<p class="time-warning">Shorter records will be processed only over their available overlap. Timestamps and scientific calculations are unchanged.</p>' : ''}
        ${unavailable.length ? '<p class="danger-warning">Processing cannot start because one or more selected instruments have no data in this Time Filter. Change the filter or deselect them.</p>' : '<p><strong>Do you want to proceed?</strong></p>'}
        <div class="modal-actions"><button class="btn" id="cancelHealthCheck">Cancel</button><button class="btn primary" id="confirmHealthCheck" ${unavailable.length ? 'disabled' : ''}>Proceed with processing</button></div>`;
      document.getElementById('cancelHealthCheck').addEventListener('click', () => modal.classList.remove('show'));
      document.getElementById('confirmHealthCheck').addEventListener('click', () => {
        modal.classList.remove('show');
        beginRegisteredProcessing(limited.length > 0);
      });
    } catch (error) {
      modalBody.innerHTML = `<p class="danger-warning">Health checking could not complete: ${escapeHtml(error.message)}</p><div class="modal-actions"><button class="btn" id="closeHealthError">Close</button></div>`;
      document.getElementById('closeHealthError').addEventListener('click', () => modal.classList.remove('show'));
    }
  }
  async function beginRegisteredProcessing(confirmedLimitedCoverage) {
    const sifSelected = (currentQueue.jobs || []).some(job =>
      job.job_id === 'sif' && job.enabled && !job.previously_completed
    );
    // SIF is the one instrument whose run depends on choices the operator has
    // to make - modes, position source, corrections, calibration files - so it
    // asks for them here rather than using whatever was left from last time.
    if (sifSelected) {
      const proceed = await openSifConfiguration({ beforeProcessing: true });
      if (!proceed) {
        showToast('Processing cancelled. No instrument was started.');
        return;
      }
    }
    try {
      const response = await api('/api/processing/start', { method: 'POST', body: JSON.stringify({ confirmed_limited_coverage: Boolean(confirmedLimitedCoverage) }) });
      renderScanState(response.state);
      if (sifSelected) {
        // Progress is shown here, in the window that owns the run. The SIF
        // workspace used to be opened automatically to act as a progress
        // monitor, which left a second window sitting at 0% for the whole run -
        // and for good if the run never started. It now opens only when the
        // operator clicks the SIF card, and only ever shows finished products.
        openSifProgressWindow();
      }
      showToast(sifSelected
        ? 'Processing started. SIF progress is shown here; open the SIF card for the maps when it is done.'
        : 'Processing started with the selected instruments and global Time Filter.');
    } catch (error) { showToast(error.message); }
  }

  async function refreshProcessingState() {
    if (queueRefreshPending) return;
    queueRefreshPending = true;
    try {
      const scan = await api('/api/scan');
      renderScanState(scan);
    } catch (_) {
      // Periodic state refresh retries without disturbing active workers.
    } finally {
      queueRefreshPending = false;
    }
  }

  async function toggleCameraQueue() {
    const jobs = (currentQueue.jobs || []).filter(job =>
      ['camera_metadata', 'camera_detailed'].includes(job.worker_group)
    );
    const resumable = jobs.filter(job => job.status === 'paused' && job.enabled);
    const targets = resumable.length
      ? resumable.map(job => ({ action: 'resume', job_id: job.job_id }))
      : jobs.filter(job => job.status === 'queued' && job.enabled)
        .map(job => ({ action: 'pause', job_id: job.job_id }));
    if (!targets.length) {
      showToast('No queued camera jobs can be paused or resumed.');
      return;
    }
    try {
      for (const payload of targets) {
        await api('/api/queue', { method: 'POST', body: JSON.stringify(payload) });
      }
      const state = await api('/api/scan');
      renderScanState(state);
      showToast(resumable.length
        ? 'Queued camera jobs resumed.'
        : 'Queued camera jobs paused. Running jobs continue safely.');
    } catch (error) {
      showToast(error.message);
      renderScanState(await api('/api/scan'));
    }
  }

  async function updateResources() {
    try {
      const response = await api('/api/resources', {
        method: 'POST',
        body: JSON.stringify({
          worker_count: Number(document.getElementById('cpuAllocation').value),
          memory_bytes: Number(document.getElementById('ramAllocation').value)
        })
      });
      renderResources(response.resources);
      showToast('Resource limits saved. No processing was started.');
    } catch (error) {
      showToast(error.message);
      const scan = await api('/api/scan');
      renderResources(scan.resources || {});
    }
  }

  function renderTimeState(timeState) {
    const displayTimezone = timeState.display_timezone || 'UTC';
    document.getElementById('displayTimezone').value = displayTimezone;
    document.getElementById('detectedMinTime').textContent =
      displayDateTime(timeState.detected_global_start, displayTimezone);
    document.getElementById('detectedMaxTime').textContent =
      displayDateTime(timeState.detected_global_end, displayTimezone);
    document.getElementById('commonOverlapTime').textContent =
      timeState.common_overlap_start && timeState.common_overlap_end
        ? `${displayDateTime(timeState.common_overlap_start, displayTimezone)} – ${displayDateTime(timeState.common_overlap_end, displayTimezone)}`
        : 'No common overlap';
    // Polling may refresh several times per second. Never overwrite a date/time
    // value while the operator is editing a custom interval.
    if (!customTimeEditing) {
      document.getElementById('analysisStartTime').value =
        inputDateTime(timeState.selected_analysis_start, displayTimezone);
      document.getElementById('analysisEndTime').value =
        inputDateTime(timeState.selected_analysis_end, displayTimezone);
    }
    if (timeState.selected_analysis_start) {
      document.getElementById('flightDate').value =
        inputDateTime(timeState.selected_analysis_start, 'UTC').slice(0, 10);
      document.getElementById('flightStartTime').value =
        inputDateTime(timeState.selected_analysis_start, 'UTC').slice(11, 16);
    } else {
      document.getElementById('flightDate').value = '';
      document.getElementById('flightStartTime').value = '';
    }
    if (timeState.selected_analysis_end) {
      document.getElementById('flightEndTime').value =
        inputDateTime(timeState.selected_analysis_end, 'UTC').slice(11, 16);
    } else {
      document.getElementById('flightEndTime').value = '';
    }
    document.getElementById('timeFilterWarnings').textContent =
      (timeState.timezone_warnings || []).join('\n');

    Object.entries(timeState.instruments || {}).forEach(([instrumentId, range]) => {
      const card = document.querySelector(`[data-instrument-id="${instrumentId}"]`);
      if (!card) return;
      card.classList.toggle('outside-range', Boolean(range.outside_selected_range));
      card._timeState = range;
      const metadata = card.querySelector('.instrument-meta');
      const oldOutside = metadata.querySelector('.outside-range-note');
      if (oldOutside) oldOutside.remove();
      const availability = Number(range.availability_percentage);
      if (range.outside_selected_range || !Number.isFinite(availability) || availability <= 0) {
        const note = document.createElement('span'); note.className = 'outside-range-note'; note.textContent = 'No data in the selected global time range'; note.style.color = 'var(--red)'; metadata.appendChild(document.createElement('br')); metadata.appendChild(note);
      } else if (availability < 100) {
        const note = document.createElement('span'); note.className = 'outside-range-note'; note.textContent = `Partial coverage: ${availability.toFixed(1)}% — available overlap will be processed`; note.style.color = 'var(--amber)'; metadata.appendChild(document.createElement('br')); metadata.appendChild(note);
      }
    });
    renderAvailabilityTimeline(timeState);
  }

  async function applySelectedTimeFilter() {
    const start = document.getElementById('analysisStartTime').value;
    const end = document.getElementById('analysisEndTime').value;
    if (!start || !end) {
      showToast('Both analysis start and end times are required.');
      return;
    }
    const displayTimezone = document.getElementById('displayTimezone').value;
    const updated = await changeTimeFilter({
      action: 'set',
      start: inputToIso(start, displayTimezone),
      end: inputToIso(end, displayTimezone)
    });
    if (!updated) return;
    modalTitle.textContent = 'Activating command';
    modalBody.innerHTML = `
      <p><strong>The global Time Filter is active.</strong></p>
      <p>Please select the instrument or instruments to process in the Processing Priority panel. Start Processing will become available after at least one healthy instrument is selected.</p>
      <div class="modal-actions"><button class="btn primary" id="reviewInstrumentSelection">Select instruments</button></div>`;
    modal.classList.add('show');
    document.getElementById('reviewInstrumentSelection').addEventListener('click', () => {
      modal.classList.remove('show');
      document.getElementById('priorityPanel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
  function activateCustomTimeframe() {
    customTimeEditing = true;
    const editor = document.getElementById('customTimeEditor');
    editor.classList.add('custom-active');
    document.getElementById('analysisStartTime').focus();
    showToast(
      'Enter a custom interval in the selected display timezone, then select Apply Time Filter.'
    );
  }

  async function changeTimeFilter(payload) {
    try {
      const response = await api('/api/time-filter', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      customTimeEditing = false;
      document.getElementById('customTimeEditor').classList.remove('custom-active');
      renderTimeState(response.time_filter);
      const scan = await api('/api/scan');
      renderScanState(scan);
      showToast('Time Filter updated. No data processing was started.');
      return response;
    } catch (error) {
      showToast(error.message);
      return null;
    }
  }
  function renderAvailabilityTimeline(timeState) {
    const timeline = document.querySelector('.timeline');
    if (!timeline) return;
    const entries = Object.entries(timeState.instruments || {})
      .filter(([, range]) => range.available_start && range.available_end);
    if (!entries.length) {
      timeline.innerHTML = '<div class="camera-detail">No confirmed UTC instrument ranges are available.</div>';
      return;
    }
    timeline.innerHTML = entries.map(([instrumentId, range]) => {
      const card = document.querySelector(`[data-instrument-id="${instrumentId}"]`);
      const name = card?.dataset.name || instrumentId;
      const percentage = range.availability_percentage == null ? 0 : range.availability_percentage;
      return `<div class="timeline-row"><span>${escapeHtml(name)}</span><div class="track"><div class="fill ${range.outside_selected_range ? 'warn' : ''}" style="width:${Math.max(0, Math.min(100, percentage))}%"></div></div><span>${range.outside_selected_range ? 'OUT' : `${percentage}%`}</span></div>`;
    }).join('');
    const header = timeline.closest('.panel').querySelector('.panel-header .system-count');
    header.textContent = timeState.selected_analysis_start && timeState.selected_analysis_end
      ? `${displayDateTime(timeState.selected_analysis_start, timeState.display_timezone || 'UTC')}–${displayDateTime(timeState.selected_analysis_end, timeState.display_timezone || 'UTC')}`
      : 'No interval selected';
  }

  function updateSystemPanels() {
    document.querySelectorAll('.system-panel').forEach(panel => {
      const statuses = Array.from(panel.querySelectorAll('.instrument-card'))
        .map(card => card.dataset.status);
      let presentation = ['queued', 'Not scanned'];
      if (statuses.some(status => status === 'failed')) presentation = ['warning', 'Failed'];
      else if (statuses.some(status => status === 'warning')) presentation = ['warning', 'Review'];
      else if (statuses.some(status => status === 'validating')) presentation = ['running', 'Validating'];
      else if (statuses.some(status => status === 'detected')) presentation = ['running', 'Detected'];
      else if (statuses.some(status => status === 'ready')) presentation = ['complete', 'Ready'];
      const pill = panel.querySelector('.system-header .status-pill');
      pill.className = `status-pill ${presentation[0]}`;
      pill.innerHTML = `<span class="dot"></span>${presentation[1]}`;
    });
  }

  function resetDiscoveryPanels() {
    const timeline = document.querySelector('.timeline');
    if (timeline) {
      timeline.innerHTML = '<div class="camera-detail">Availability will be calculated after Flight Folder discovery.</div>';
      const timelineRange = timeline.closest('.panel').querySelector('.panel-header .system-count');
      timelineRange.textContent = 'No flight scanned';
    }
    ['micaText', 'flirText', 'goproText'].forEach(id => {
      const element = document.getElementById(id);
      if (element) element.textContent = 'Metadata quick check not started';
    });
    ['micaProgress', 'flirProgress', 'goproProgress'].forEach(id => {
      const element = document.getElementById(id);
      if (element) element.style.width = '0%';
    });
    document.querySelectorAll('.camera-job .status-pill').forEach(pill => {
      pill.className = 'status-pill queued';
      pill.innerHTML = '<span class="dot"></span>Idle';
    });
  }

  function showInstrumentSummary(card) {
    const state = card._scanState || {};
    const timeState = card._timeState || {};
    const displayTimezone = document.getElementById('displayTimezone').value;
    modalTitle.textContent = card.dataset.name;
    const candidates = (state.candidate_paths || []).map((path, index) =>
      state.ambiguous
        ? `<label><input type="radio" name="candidatePath" value="${escapeAttribute(path)}" ${index === 0 ? 'checked' : ''}> ${escapeHtml(path)}</label><br>`
        : `<li>${escapeHtml(path)}</li>`
    ).join('');
    modalBody.innerHTML = `
      <p><strong>Status:</strong> ${escapeHtml(statusPresentation[state.detection_status]?.[1] || 'Not detected')}</p>
      <p><strong>Matching files:</strong> ${Number(state.file_count || 0).toLocaleString()}</p>
      <p><strong>Timestamp range:</strong> ${state.utc_start_time && state.utc_end_time ? `${displayDateTime(state.utc_start_time, displayTimezone)} – ${displayDateTime(state.utc_end_time, displayTimezone)}` : 'Not available'}</p>
      ${state.ambiguous ? '<p><strong>Confirmation required:</strong> Multiple candidates matched.</p>' : ''}
      ${candidates ? `<p><strong>Candidate folders:</strong></p>${state.ambiguous ? `<div>${candidates}</div><p><button class="btn primary" id="confirmCandidateBtn">Confirm Candidate</button></p>` : `<ul>${candidates}</ul>`}` : ''}
      ${timeState.available_start && timeState.available_end ? `
        <p><strong>Instrument time range:</strong> ${displayDateTime(timeState.available_start, displayTimezone)} – ${displayDateTime(timeState.available_end, displayTimezone)}</p>
        <p><strong>Global analysis interval:</strong> ${latestScanState.time_filter?.selected_analysis_start && latestScanState.time_filter?.selected_analysis_end ? `${displayDateTime(latestScanState.time_filter.selected_analysis_start, displayTimezone)} – ${displayDateTime(latestScanState.time_filter.selected_analysis_end, displayTimezone)}` : 'Not selected'}</p>
        <p class="muted">This instrument follows the single global Time Filter. If its record is shorter, only the available overlap is processed.</p>` : ''}      ${(timeState.timezone_warnings || []).length ? `<p class="time-warning">${(timeState.timezone_warnings || []).map(escapeHtml).join('<br>')}</p>` : ''}
      ${false && card.dataset.instrumentId === 'noseboom' && state.quicklook?.available ? `
        <div class="instrument-map-shell">
          <div class="instrument-map-toolbar">
            <strong>Noseboom Flight Track</strong>
            <button class="btn" id="mapZoomIn" aria-label="Zoom in">Zoom +</button>
            <button class="btn" id="mapZoomOut" aria-label="Zoom out">Zoom −</button>
            <button class="btn" id="mapReset">Reset view</button>
            <label>Track width <input id="mapLineWidth" type="range" min="1" max="12" value="4"></label>
          </div>
          <svg class="instrument-map" id="noseboomMap" role="img" aria-label="Noseboom flight track map with north heading and straight-flight legs"></svg>
          <div class="map-legend">
            <span class="map-key"><span class="map-swatch"></span>Measured flight track</span>
            <span class="map-key"><span class="map-swatch straight"></span>Accepted straight-flight leg</span>
            <span class="map-key">N ↑ North · wheel/pinch buttons zoom · drag pans</span>
          </div>
        </div>` : `<p>${state.processing_status === 'idle' ? 'No scientific processing has been started.' : 'Quick-look map becomes available after successful Noseboom processing.'}</p>`}`;
    const confirmButton = document.getElementById('confirmCandidateBtn');
    if (confirmButton) {
      confirmButton.addEventListener('click', async () => {
        const selected = modalBody.querySelector('input[name="candidatePath"]:checked');
        if (!selected) return;
        try {
          await api('/api/scan/candidates/confirm', {
            method: 'POST',
            body: JSON.stringify({
              instrument_id: card.dataset.instrumentId,
              candidate_path: selected.value
            })
          });
          const scan = await api('/api/scan');
          renderScanState(scan);
          modal.classList.remove('show');
          showToast('Instrument candidate confirmed.');
        } catch (error) {
          showToast(error.message);
        }
      });
    }
    modal.classList.add('show');
    if (false && card.dataset.instrumentId === 'noseboom' && state.quicklook?.available) {
      renderNoseboomMap(state.quicklook);
    }
  }

  function renderNoseboomMap(mapData) {
    const svg = document.getElementById('noseboomMap');
    const points = (mapData.points || []).filter(point =>
      Number.isFinite(point.lat) && Number.isFinite(point.lon)
    );
    if (!svg || points.length < 2) return;
    const lats = points.map(point => point.lat);
    const lons = points.map(point => point.lon);
    const minLat = Math.min(...lats), maxLat = Math.max(...lats);
    const minLon = Math.min(...lons), maxLon = Math.max(...lons);
    const latPad = Math.max((maxLat - minLat) * 0.08, 0.0005);
    const lonPad = Math.max((maxLon - minLon) * 0.08, 0.0005);
    const base = {
      x: minLon - lonPad,
      y: -(maxLat + latPad),
      width: maxLon - minLon + 2 * lonPad,
      height: maxLat - minLat + 2 * latPad
    };
    let view = { ...base };
    const routePath = points.map(point => `${point.lon},${-point.lat}`).join(' ');
    const straightRuns = [];
    let run = [];
    points.forEach(point => {
      if (point.straight) run.push(`${point.lon},${-point.lat}`);
      else if (run.length) { if (run.length > 1) straightRuns.push(run); run = []; }
    });
    if (run.length > 1) straightRuns.push(run);
    svg.innerHTML = `
      <rect x="${base.x}" y="${base.y}" width="${base.width}" height="${base.height}" fill="#0b2231"/>
      <g id="mapGrid" stroke="rgba(151,199,220,.16)" stroke-width="${base.width / 500}">
        ${[.2,.4,.6,.8].map(f => `<line x1="${base.x + base.width*f}" y1="${base.y}" x2="${base.x + base.width*f}" y2="${base.y+base.height}"/><line x1="${base.x}" y1="${base.y+base.height*f}" x2="${base.x+base.width}" y2="${base.y+base.height*f}"/>`).join('')}
      </g>
      <polyline id="routeLine" points="${routePath}" fill="none" stroke="#32d3ff" vector-effect="non-scaling-stroke" stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/>
      ${straightRuns.map(values => `<polyline points="${values.join(' ')}" fill="none" stroke="#ffb454" vector-effect="non-scaling-stroke" stroke-width="6" stroke-linecap="round"/>`).join('')}
      <text x="${base.x + base.width*.04}" y="${base.y + base.height*.12}" fill="#eaf8ff" font-size="${base.height*.075}" font-weight="700">N ↑</text>`;
    const applyView = () => svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.width} ${view.height}`);
    const zoom = factor => {
      const nextWidth = Math.max(base.width / 20, Math.min(base.width * 2, view.width * factor));
      const nextHeight = Math.max(base.height / 20, Math.min(base.height * 2, view.height * factor));
      view.x += (view.width - nextWidth) / 2;
      view.y += (view.height - nextHeight) / 2;
      view.width = nextWidth; view.height = nextHeight; applyView();
    };
    applyView();
    document.getElementById('mapZoomIn').onclick = () => zoom(.75);
    document.getElementById('mapZoomOut').onclick = () => zoom(1.25);
    document.getElementById('mapReset').onclick = () => { view = { ...base }; applyView(); };
    document.getElementById('mapLineWidth').oninput = event => {
      document.getElementById('routeLine').setAttribute('stroke-width', event.target.value);
    };
    svg.onwheel = event => { event.preventDefault(); zoom(event.deltaY < 0 ? .82 : 1.22); };
    let drag = null;
    svg.onpointerdown = event => {
      drag = { x: event.clientX, y: event.clientY, view: { ...view } };
      svg.setPointerCapture(event.pointerId);
    };
    svg.onpointermove = event => {
      if (!drag) return;
      view.x = drag.view.x - (event.clientX - drag.x) * drag.view.width / svg.clientWidth;
      view.y = drag.view.y - (event.clientY - drag.y) * drag.view.height / svg.clientHeight;
      applyView();
    };
    svg.onpointerup = () => { drag = null; };
    svg.onpointercancel = () => { drag = null; };
  }

  async function cancelScan(source) {
    try {
      await api('/api/scan/cancel', {
        method: 'POST',
        body: JSON.stringify({ source })
      });
      await pollScan();
    } catch (error) {
      showToast(`Could not stop ${source} scanning: ${error.message}`);
    }
  }

  async function refreshLogs() {
    try {
      const response = await api('/api/logs');
      const signature = JSON.stringify(response.records || []);
      if (signature === lastLogSignature) return;
      lastLogSignature = signature;
      logConsole.innerHTML = '';
      (response.records || []).forEach(record => {
        const line = document.createElement('div');
        line.className = 'log-line';
        const time = new Date(record.timestamp).toLocaleTimeString('en-GB', { hour12: false });
        line.innerHTML = `<span class="log-time">[${escapeHtml(time)}]</span> <span class="log-${String(record.severity).toLowerCase()}">${escapeHtml(record.severity.padEnd(8))}</span> ${escapeHtml(record.message)}`;
        logConsole.appendChild(line);
      });
      if (autoScroll) logConsole.scrollTop = logConsole.scrollHeight;
    } catch (_) {
      // The next poll retries; dashboard interaction must remain responsive.
    }
  }

  async function api(url, options = {}) {
    let response;
    try {
      response = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options
      });
    } catch (error) {
      // A bare "Failed to fetch" tells the operator nothing. The request never
      // reached the server, so the cause is on this side: the launcher window
      // was closed, the machine slept, or the page was reloaded while a native
      // chooser was still open.
      throw new Error(
        'The CC-FLUX server did not respond. Check that the launcher window is '
        + 'still open, then use Refresh Status. If it was closed, start the '
        + 'launcher again and reload this page.'
      );
    }
    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      throw new Error(`The server returned an unreadable response (${response.status}).`);
    }
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function formatTime(value) {
    return new Date(value).toISOString().slice(11, 19);
  }
  function displayDateTime(value, displayTimezone) {
    if (!value) return '—';
    const date = new Date(value);
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: displayTimezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
      timeZoneName: 'short'
    }).format(date);
  }
  function inputDateTime(value, displayTimezone) {
    if (!value) return '';
    const date = new Date(value);
    const parts = zonedParts(date, displayTimezone);
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
  }
  function inputToIso(value, displayTimezone) {
    const [datePart, timePart] = value.split('T');
    const [year, month, day] = datePart.split('-').map(Number);
    const [hour, minute, second = 0] = timePart.split(':').map(Number);
    const wallClockUtc = Date.UTC(year, month - 1, day, hour, minute, second);
    let instant = wallClockUtc;
    for (let iteration = 0; iteration < 2; iteration += 1) {
      const parts = zonedParts(new Date(instant), displayTimezone);
      const represented = Date.UTC(
        Number(parts.year), Number(parts.month) - 1, Number(parts.day),
        Number(parts.hour), Number(parts.minute), Number(parts.second)
      );
      instant += wallClockUtc - represented;
    }
    return new Date(instant).toISOString();
  }
  function zonedParts(date, displayTimezone) {
    const formatted = new Intl.DateTimeFormat('en-CA', {
      timeZone: displayTimezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23'
    }).formatToParts(date);
    return Object.fromEntries(
      formatted
        .filter(part => part.type !== 'literal')
        .map(part => [part.type, part.value])
    );
  }
  function formatBytes(value) {
    if (!Number.isFinite(Number(value)) || Number(value) <= 0) return '—';
    const gib = Number(value) / (1024 ** 3);
    return `${gib >= 10 ? gib.toFixed(0) : gib.toFixed(1)} GB`;
  }
  function formatElapsed(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(value / 60)}m ${String(value % 60).padStart(2, '0')}s`;
  }
  function capitalize(value) {
    return String(value).charAt(0).toUpperCase() + String(value).slice(1);
  }
  async function openEditableInformation(source, title, presentation = 'standard') {
    modalTitle.textContent = title;
    modalBody.innerHTML = '<div class="info-loading">Loading information…</div>';
    modal.classList.add('show');
    try {
      const response = await fetch(source, { cache: 'no-store' });
      if (!response.ok) throw new Error(`Information file could not be loaded (${response.status})`);
      const text = await response.text();
      const isLicense = presentation === 'license';
      const isManual = presentation === 'manual';
      const hero = isLicense ? `
        <div class="license-hero">
          <div class="license-emblem" aria-hidden="true">§</div>
          <div class="license-hero-copy">
            <span class="license-kicker">CC-FLUX 2026</span>
            <strong>Scientific Software License</strong>
            <small>Authorship, permissions, conditions, and scientific-use disclaimer</small>
          </div>
        </div>` : isManual ? `
        <div class="manual-hero">
          <div class="manual-emblem">
            <img src="/campaign-main-airship.svg" alt="CC-FLUX 2026 Zeppelin campaign logo">
          </div>
          <div class="manual-hero-copy">
            <small>CC-FLUX 2026 · Operator guide</small>
            <strong>Software Manual</strong>
            <span>Folders, workflow, instruments, maps, projects, and troubleshooting</span>
          </div>
        </div>` : '';
      const presentationClass = isLicense ? ' license-document' : isManual ? ' manual-document' : '';
      modalBody.innerHTML = `${hero}<article class="info-document${presentationClass}">${renderEditableInformation(text)}</article>`;
      modalBody.scrollTop = 0;
    } catch (error) {
      modalBody.innerHTML = `<div class="danger-warning"><strong>Could not open this information.</strong><br>${escapeHtml(error.message)}</div>`;
    }
  }

  function renderEditableInformation(text) {
    const output = [];
    let listTag = '';
    const closeList = () => {
      if (listTag) {
        output.push(`</${listTag}>`);
        listTag = '';
      }
    };
    const openList = tag => {
      if (listTag === tag) return;
      closeList();
      output.push(`<${tag}>`);
      listTag = tag;
    };
    String(text).replaceAll('\r\n', '\n').split('\n').forEach(rawLine => {
      const line = rawLine.trimEnd();
      if (!line.trim()) {
        closeList();
        return;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        closeList();
        const level = heading[1].length;
        output.push(`<h${level}>${renderInformationInline(heading[2])}</h${level}>`);
        return;
      }
      const bullet = line.match(/^(\s*)\*\s+(.+)$/);
      if (bullet) {
        openList('ul');
        const nested = bullet[1].length >= 2 ? ' class="nested"' : '';
        output.push(`<li${nested}>${renderInformationInline(bullet[2])}</li>`);
        return;
      }
      const numbered = line.match(/^\s*\d+\.\s+(.+)$/);
      if (numbered) {
        openList('ol');
        output.push(`<li>${renderInformationInline(numbered[1])}</li>`);
        return;
      }
      closeList();
      const indented = /^\s{2,}/.test(rawLine) ? ' class="info-indent"' : '';
      output.push(`<p${indented}>${renderInformationInline(line.trim())}</p>`);
    });
    closeList();
    return output.join('');
  }

  function renderInformationInline(value) {
    const links = [];
    let html = escapeHtml(value);
    html = html.replace(/\[([^\]]+)\]\((mailto:[^)]+|https?:\/\/[^)]+)\)/g, (_match, label, href) => {
      const token = `@@INFO_LINK_${links.length}@@`;
      links.push(`<a href="${escapeAttribute(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      return token;
    });
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(^|\s)(https:\/\/[^\s<]+)/g, (_match, prefix, href) =>
      `${prefix}<a href="${escapeAttribute(href)}" target="_blank" rel="noopener noreferrer">${href}</a>`
    );
    links.forEach((link, index) => {
      html = html.replace(`@@INFO_LINK_${index}@@`, link);
    });
    return html;
  }

  function escapeHtml(value) {
    const element = document.createElement('span');
    element.textContent = String(value);
    return element.innerHTML;
  }
  function escapeAttribute(value) {
    return escapeHtml(value).replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }
  function showToast(message) {
    const text = String(message || '');
    const isError = /\b(error|failed|could not|cannot|must |requires|invalid|unavailable|not selected)\b/i.test(text);
    toast.textContent = text;
    toast.classList.toggle('error', isError);
    toast.classList.add('show');
    setTimeout(() => { toast.classList.remove('show'); toast.classList.remove('error'); }, isError ? 5200 : 2600);
  }

  // One check per launch, off the critical path; the server has usually
  // already answered by now because it starts its own on startup.
  refreshUpdateStatus(true);
  logPoll = setInterval(refreshLogs, 700);
  queuePoll = setInterval(refreshProcessingState, 800);
  refreshLogs();
  api('/api/scan').then(renderScanState).catch(() => {});
})();
