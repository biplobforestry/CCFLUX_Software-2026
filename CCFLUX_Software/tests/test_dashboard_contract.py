from pathlib import Path


ASSETS = Path(__file__).parents[1] / "app" / "assets"


def test_dashboard_contains_required_controls_and_sections():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    for control_id in (
        "openFolderBtn", "cameraFolderBtn", "outputFolderBtn", "saveProjectBtn",
        "openProjectBtn", "refreshBtn",
        "runBtn", "initialCheckBtn", "resetSystemBtn", "exitAppBtn",
        "customTimeBtn", "customTimeEditor", "cpuAllocation", "ramAllocation",
        "remoteSensingBtn", "dataProductsBtn", "softwareUpdateBtn", "manualBtn",
        "licenseBtn",
        "flightScanWindow", "cameraScanWindow",
    ):
        assert f'id="{control_id}"' in html
    for label in (
        "Flight summary", "Time Filter", "Instrument Systems",
        "Processing Priority", "Processing Queue", "Remote sensing data processing",
        "Instrument Availability Timeline", "Processing Log &amp; Diagnostics",
    ):
        assert label.casefold() in html.casefold()


def test_dashboard_preserves_campaign_theme_contract():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    assert "CC-FLUX Campaign 2026" in html
    assert "A Joint CHANEL – ClimStress Zeppelin Campaign" in html
    assert "Across European Cities and Forests" in html
    assert "Integrated Post-Flight Scientific Payload Review" in html
    assert "© 2026 Biplob Dey · Forschungszentrum Jülich GmbH" in html
    assert 'class="topbar"' in html
    assert 'class="app-footer"' in html
    assert 'aria-label="CC-FLUX 2026 airship logo"' in html
    assert 'id="ccfluxLogoBody"' in html
    assert 'id="displayTimezone"' in html
    assert 'value="UTC" selected' in html
    assert "utcDisplayToggle" not in html
    assert html.count('class="institution-logo-item"') == 9
    assert html.count("institution-logo-item main-partner") == 1
    assert html.count("institution-logo-item campaign-logo-item") == 1
    assert html.index("institution-logo-banner") < html.rindex('class="app-footer"')
    assert html.index("Forschungszentrum Jülich logo — main partner") < html.index(
        "CC-FLUX 2026 campaign logo"
    )
    assert "<label>Takeoff UTC</label>" in html
    assert "<label>Landing UTC</label>" in html
    assert "Atmospheric particle and Remote sensing payload" in html
    assert 'id="goproText"' in html
    assert 'id="goproProgress"' in html
    assert 'data-name="GoPro" href="/gopro"' in html
    assert 'class="btn danger" id="resetSystemBtn"' not in html
    assert 'class="btn danger" id="exitAppBtn"' not in html
    assert "grid-template-columns: repeat(11, minmax(0, 1fr))" in html
    assert 'class="campaign-logo-title"' in html
    assert 'class="campaign-logo-year"' in html
    assert 'class="action-group system-actions"' in html
    assert "grid-template-columns: minmax(0, 1fr) auto" in html
    assert ".system-actions {" in html
    assert "justify-self: end;" in html


def test_editable_information_documents_are_wired():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    javascript = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    products = (ASSETS / "data_products.txt").read_text(encoding="utf-8")
    update = (ASSETS / "software_update.txt").read_text(encoding="utf-8")
    license_text = (ASSETS.parents[1] / "License.txt").read_text(encoding="utf-8")
    manual_text = (ASSETS.parents[1] / "manual.text").read_text(encoding="utf-8")

    assert 'id="dataProductsBtn"' in html
    assert 'id="softwareUpdateBtn"' in html
    assert 'id="licenseBtn"' in html
    assert 'id="manualBtn"' in html
    assert "/data_products.txt" in javascript
    assert "/software_update.txt" in javascript
    assert "/License.txt" in javascript
    assert "/manual.text" in javascript
    assert "Scientific Software License" in javascript
    assert "Copyright © 2026 Biplob Dey" in license_text
    assert "uni-koeln.sciebo.de/s/CCFLUX" in products
    assert "e.pfannerstill@fz-juelich.de" in products
    assert "g.gkatzelis@fz-juelich.de" in products
    # The update notice is operator-editable and is intentionally empty
    # until there is something to announce; only the wiring is asserted.
    assert (ASSETS / "software_update.txt").is_file()
    assert "Distribution folder structure" in manual_text
    assert "Instrument descriptions" in manual_text


def test_cards_render_live_processing_contract_and_camera_queue_is_wired():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    javascript = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    for field in (
        "detection_status", "file_count", "utc_start_time", "utc_end_time",
        "processing_status", "processing_progress", "processing_step",
        "processing_elapsed_seconds",
    ):
        assert field in javascript
    assert "toggleCameraQueue" in javascript
    assert "queueRefreshPending" in javascript
    assert "initialCheck" in javascript
    assert "confirmSystemReset" in javascript
    assert "confirmApplicationExit" in javascript
    assert "synchronizeFlightTimes" in javascript
    assert "activateCustomTimeframe" in javascript
    assert "customTimeEditing" in javascript
    assert "Never overwrite a date/time" in javascript
    assert "/api/project/discover" in javascript
    assert "showSavedProjectChoices" in javascript
    assert "Search Another Folder" in javascript
    assert "Select a folder and search it for saved .ccflux Flight Projects" in html


def test_independent_scan_windows_and_remote_sensing_contract():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    javascript = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    # Remote sensing is selected after the camera scan, against camera coverage:
    # products first, then the period, then a verification step before anything
    # starts. It no longer triggers a scan of its own or borrows the flight
    # Time Filter.
    assert "Select the products to process and the period to process them over." in javascript
    assert "Detected global minimum and maximum" in javascript
    assert "Common overlapping timeframe" in javascript
    assert "Custom period" in javascript
    assert "Verifying your request" in javascript
    assert "/api/remote-sensing/coverage" in javascript
    assert "/api/remote-sensing/preview" in javascript
    assert "announceCameraCoverage" in javascript
    # The old scan-then-process chain is gone.
    assert "pendingRemoteWorkflow" not in javascript
    assert "Do you want to proceed scanning?" not in javascript
    assert "Do you want to stop scanning?" in javascript
    assert 'data-scan-source="flight"' in html
    assert 'data-scan-source="camera"' in html
    assert 'data-window-action="minimize"' in html
    assert 'data-window-action="maximize"' in html
    assert 'data-window-action="close"' in html
    assert "remote-inactive" in html
    assert "remote-ready" in html
    assert "/api/select-scan-folders" in javascript
    assert "/api/select-camera-folder" in javascript
    assert "Camera Folder selected. No scan started." in javascript
    assert "A Camera Folder is defined. Do you want to include it in this scan?" in javascript
    assert "Scan Flight Only" in javascript
    assert "Scan Flight + Camera" in javascript
    assert "include_camera" in javascript
    assert 'id="flightScanPercentage"' in html
    assert 'id="cameraScanPercentage"' in html
    assert "Processing is done! close the window!" in javascript
    assert "post_scan_checks" in javascript
    assert "Checking..." in javascript
    assert "Preparing..." in html
    assert "positionScanWindows" in javascript
    assert "/api/remote-sensing/log" in javascript
    assert "/api/remote-sensing/start" in javascript
    # The interval now comes from an explicit mode chosen against camera
    # coverage, so there is no "replay the current flight selection" fallback.
    assert "requested_time_mode" not in javascript


def test_gopro_capture_map_contract():
    html = (ASSETS / "gopro.html").read_text(encoding="utf-8")
    javascript = (ASSETS / "gopro.js").read_text(encoding="utf-8")
    assert "GoPro capture locations" in html
    assert 'id="trackWidth"' in html
    assert 'id="resetMapBtn"' in html
    assert 'id="imageClose"' in html
    assert 'id="downloadImage"' in html
    assert 'id="fullscreenImage"' in html
    assert "/api/gopro/image/" in javascript
    assert "capture_time_utc" in javascript
    assert "image_id" in javascript
    assert "altitude_m" in javascript
    assert "imagePercent" in javascript
    assert "fillColor: '#ff2020'" in javascript
    assert "L.polyline" in javascript
    assert "resetMapPosition" in javascript
    server = (ASSETS.parents[0] / "server.py").read_text(encoding="utf-8")
    assert 'path == "/api/gopro"' in server
    assert 'path.startswith("/api/gopro/image/")' in server


def test_processing_workflow_uses_global_time_filter_and_explicit_selection():
    html = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    javascript = (ASSETS / "dashboard.js").read_text(encoding="utf-8")

    assert 'id="priorityWorkflowState"' in html
    assert "Select the instruments to process, use one global time interval" in html
    assert "data-queue-select" in javascript
    assert "confirmed_limited_coverage" in javascript
    assert "Please wait! System is busy now!" in javascript
    assert "single global Time Filter" in javascript
    assert "Apply instrument interval" not in javascript
    assert "Activating command" in javascript
    assert "Checking health" in javascript
    assert "Do you want to proceed?" in javascript
    assert "available time periods" in javascript
    assert "automatic recommendation" in javascript
    assert "minimum_worker_count" in javascript
    assert "Select an Output Folder to continue processing." in javascript
    assert "Processing was not started." in javascript
    assert 'id="runBtn" disabled' in html
    assert 'id="priorityPanel"' in html
def test_noseboom_live_recalculation_and_opc_size_view_contract():
    noseboom_script = (ASSETS / "noseboom.js").read_text(encoding="utf-8")
    opc_html = (ASSETS / "opc.html").read_text(encoding="utf-8")
    opc_script = (ASSETS / "opc.js").read_text(encoding="utf-8")
    partector_html = (ASSETS / "partector.html").read_text(encoding="utf-8")
    partector_script = (ASSETS / "partector.js").read_text(encoding="utf-8")

    assert "/api/noseboom/straight-settings/progress" in noseboom_script
    assert "Elapsed:" in noseboom_script
    assert "status.progress" in noseboom_script
    assert "Rendering recalculated flight legs on the map." in noseboom_script
    assert "Robust size-distribution summary" not in opc_html
    assert 'id="distributionPlot"' not in opc_html
    assert "fullLogTickLabels" in noseboom_script
    assert "Frequency [Hz]" in noseboom_script
    assert "Power spectral density" in noseboom_script
    assert "layout.margin={l:150" in noseboom_script
    assert "The supplied instrument documentation" not in opc_html
    assert "Data integrity and QC" not in opc_html
    assert 'id="qualityGrid"' not in opc_html
    assert "renderQuality" not in opc_script
    assert "Robust size-distribution summary" not in partector_html
    assert 'id="distributionPlot"' not in partector_html
    assert "Sessions and quality control" not in partector_html
    assert 'id="qualityGrid"' not in partector_html
    assert (
        '<article class="chart-card wide" data-section="size">'
        '<div class="card-head"><h2>Integrated number size bands</h2>'
    ) in partector_html
    assert "renderDistribution" not in partector_script
    assert "renderQuality" not in partector_script


def test_flir_camera_workspace_is_wired_to_gui_and_temperature_map():
    dashboard = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    html = (ASSETS / "flir.html").read_text(encoding="utf-8")
    script = (ASSETS / "flir.js").read_text(encoding="utf-8")
    server = (ASSETS.parents[0] / "server.py").read_text(encoding="utf-8")

    assert 'href="/flir"' in dashboard
    assert 'id="thermalMap"' in html
    assert 'id="resetMapBtn"' in html
    assert 'id="acquisitionPlot"' in html
    assert 'id="gapPlot"' in html
    assert 'id="temperatureTimePlot"' in html
    assert 'id="temperatureDistributionPlot"' in html
    assert 'id="temperatureVariabilityPlot"' in html
    assert 'id="gallery"' in html
    assert "/api/flir" in script
    assert "temperature_median_c" in script
    assert "Noseboom time difference" in script
    assert 'path == "/api/flir"' in server
    assert 'path.startswith("/api/flir/asset/")' in server


def test_all_subpages_use_the_clean_main_gui_campaign_logo_treatment():
    logo = (ASSETS / "campaign-logo.html").read_text(encoding="utf-8")
    shared_css = (ASSETS / "hatchbox_science.css").read_text(encoding="utf-8")
    bridge = (ASSETS.parents[0] / "miro_rack_bridge.py").read_text(
        encoding="utf-8"
    )

    assert 'viewBox="70 90 700 330"' in logo
    assert "background:transparent!important" in logo
    assert ".ccflux-logo{width:100%;height:100%;padding:0" in logo
    assert (
        ".brand-logo-shell{width:137px;height:98px;flex:0 0 137px;"
        "padding:0;border:0;border-radius:0;background:transparent;"
        "box-shadow:none;overflow:visible}"
    ) in shared_css

    for name in (
        "flir.html",
        "gopro.html",
        "ins_gimbal.html",
        "miro_rack_map.html",
        "noseboom.html",
        "opc.html",
        "partector.html",
        "sif.html",
    ):
        page = (ASSETS / name).read_text(encoding="utf-8")
        # Every standalone subpage embeds the shared airship mark directly.
        # The earlier iframe-to-/campaign-logo.html treatment survives only in
        # the injected MIRO Rack header, where the legacy page's own markup
        # cannot be edited.
        assert 'src="/campaign-main-airship.svg"' in page
        assert 'class="brand-logo"' in page
    assert 'src="/campaign-logo.html"' in bridge


def test_dashboard_and_server_expose_complete_sif_workspace():
    dashboard = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    dashboard_script = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
    sif_html = (ASSETS / "sif.html").read_text(encoding="utf-8")
    sif_script = (ASSETS / "sif.js").read_text(encoding="utf-8")
    server = (ASSETS.parents[0] / "server.py").read_text(encoding="utf-8")

    assert 'href="/sif/overview"' in dashboard
    assert "Configure SIF" in dashboard_script
    assert "/api/sif/options" in dashboard_script
    assert "sifRawMinKb" in dashboard_script
    # The workspace is opened by clicking the SIF card, not automatically when
    # processing starts. Auto-opening it left a second window acting as a
    # progress monitor, stuck at 0% for the whole run - and for good if the run
    # never started. Progress belongs in the window that owns the run.
    assert "ccflux-sif-workspace" not in dashboard_script
    assert "window.open(\n        '/sif/overview'" not in dashboard_script
    assert "openSifConfiguration({ beforeProcessing: true })" in dashboard_script
    assert "openSifProgressWindow()" in dashboard_script
    for label in ("Variables", "Vegetation Index", "Manual", "Map view"):
        assert label in sif_html
    # The altitude-relationship plot and the spectra panel were removed at the
    # scientist's request: the first related two quantities with no expected
    # relationship, and the second was not used. See
    # test_sif_workspace_views.py, which holds them removed.
    for plot_id in (
        "overviewPlot",
        "histogramPlot",
        "timePlot",
        "sifMap",
    ):
        assert plot_id in sif_html
    for removed in ("altitudePlot", "spectraPlot"):
        assert removed not in sif_html
    assert "Reset position" in sif_html
    assert "/api/sif" in server
    assert "Gimbal + Noseboom" in sif_script
    assert "100 KB" in sif_script
    assert "processing_progress" in sif_script
