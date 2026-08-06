"""Hybrid processing: what a work package permits, and what it refuses.

A flight can be split across several computers. The primary decides the
campaign settings and who processes what; each worker gets a sealed package and
can change only where its own folders are. Results come back as sealed packages
and are fused into one project.

The security claim is narrow and worth stating precisely. Packages are
encrypted with AES-256-GCM under a scrypt-derived key, which is authenticated
encryption: the same operation that hides the contents detects any change to
them. That gives confidentiality, tamper-evidence and instrument-level
authorisation. It is symmetric - anyone with the passphrase can create packages
too - so it is not proof of authorship, and the tests below assert what it does
do rather than what it might be assumed to.
"""

import json
import zipfile
from pathlib import Path

import pytest

from core.hybrid_processing import (
    CLEARTEXT_HEADER_NAME,
    MAXIMUM_FUSION_PACKAGES,
    SEALED_PAYLOAD_NAME,
    HybridPackageError,
    HybridPlan,
    WorkerAssignment,
    create_work_packages,
    export_result_package,
    fuse,
    load_result_package,
    load_work_package,
    read_package_header,
    review_fusion,
)

PASSPHRASE = "campaign-2026-secret"


def _plan(**overrides):
    base = dict(
        project_id="project-1",
        flight_id="Flight_2707",
        campaign="CC-FLUX Campaign 2026",
        analysis_start="2026-07-27T07:19:28+00:00",
        analysis_end="2026-07-27T10:19:58+00:00",
        available_instruments=("noseboom", "miro", "picarro", "opc_hbx4", "sif"),
        assignments=(
            WorkerAssignment("w1", "Laptop-A", ("miro", "picarro")),
            WorkerAssignment("w2", "Laptop-B", ("opc_hbx4",)),
        ),
        primary_instruments=("noseboom",),
        instrument_options={"sif": {"raw_min_kb": 100}},
    )
    base.update(overrides)
    return HybridPlan(**base)


def _packages(tmp_path, plan=None):
    return create_work_packages(
        plan or _plan(), tmp_path / "packages", PASSPHRASE, software_version="1.0.1"
    )


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def test_a_plan_needs_a_time_range():
    with pytest.raises(HybridPackageError, match="time range"):
        _plan(analysis_start=None).validate()


def test_an_instrument_cannot_go_to_two_computers():
    """Both would process it and fusion would have two answers for one thing."""
    plan = _plan(assignments=(
        WorkerAssignment("w1", "A", ("miro",)),
        WorkerAssignment("w2", "B", ("miro",)),
    ))
    with pytest.raises(HybridPackageError, match="assigned to both"):
        plan.validate()


def test_the_primary_cannot_keep_what_it_gave_away():
    plan = _plan(primary_instruments=("miro",))
    with pytest.raises(HybridPackageError, match="kept on the primary"):
        plan.validate()


def test_an_empty_worker_is_refused():
    plan = _plan(assignments=(WorkerAssignment("w1", "A", ()),))
    with pytest.raises(HybridPackageError, match="no instruments"):
        plan.validate()


def test_an_instrument_not_in_the_flight_is_refused():
    plan = _plan(assignments=(WorkerAssignment("w1", "A", ("nosuch",)),))
    with pytest.raises(HybridPackageError, match="not in this flight"):
        plan.validate()


def test_no_more_packages_than_fusion_can_take():
    plan = _plan(assignments=tuple(
        WorkerAssignment(f"w{index}", f"W{index}", (name,))
        for index, name in enumerate(
            ("noseboom", "miro", "picarro", "opc_hbx4", "sif")
        )
    ), primary_instruments=())
    with pytest.raises(HybridPackageError, match="fused"):
        plan.validate()


def test_instruments_nobody_took_are_reported():
    assert _plan().unassigned == ("sif",)


# --------------------------------------------------------------------------
# Sealing
# --------------------------------------------------------------------------
def test_one_package_per_worker(tmp_path):
    written = _packages(tmp_path)

    assert len(written) == 2
    assert {path.name for path in written} == {
        "Flight_2707_Laptop-A.ccflux", "Flight_2707_Laptop-B.ccflux"
    }


def test_the_header_is_readable_but_carries_no_configuration(tmp_path):
    """Enough to know what the file is before being asked for a passphrase."""
    package = _packages(tmp_path)[0]

    header = read_package_header(package)

    assert header["flight_id"] == "Flight_2707"
    assert header["worker_name"] == "Laptop-A"
    for leak in ("assigned_instruments", "instrument_options", "analysis_start"):
        assert leak not in header


def test_the_passphrase_is_never_written_into_a_package(tmp_path):
    package = _packages(tmp_path)[0]

    assert PASSPHRASE.encode() not in package.read_bytes()


def test_the_payload_is_not_readable_without_the_passphrase(tmp_path):
    package = _packages(tmp_path)[0]

    with zipfile.ZipFile(package) as archive:
        sealed = archive.read(SEALED_PAYLOAD_NAME)

    assert b"miro" not in sealed and b"picarro" not in sealed


def test_a_wrong_passphrase_is_refused(tmp_path):
    package = _packages(tmp_path)[0]

    with pytest.raises(HybridPackageError, match="could not be opened"):
        load_work_package(package, "not-the-passphrase")


def test_an_altered_package_is_refused(tmp_path):
    """Authenticated encryption: changing a byte breaks decryption."""
    package = _packages(tmp_path)[0]
    tampered = tmp_path / "tampered.ccflux"
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == SEALED_PAYLOAD_NAME:
                data = data[:20] + bytes([data[20] ^ 0xFF]) + data[21:]
            target.writestr(item, data)

    with pytest.raises(HybridPackageError, match="could not be opened"):
        load_work_package(tampered, PASSPHRASE)


def test_a_header_from_another_package_is_refused(tmp_path):
    """The header is bound into the ciphertext, so it cannot be swapped."""
    first, second = _packages(tmp_path)
    swapped = tmp_path / "swapped.ccflux"
    with zipfile.ZipFile(first) as a, zipfile.ZipFile(second) as b, \
            zipfile.ZipFile(swapped, "w") as target:
        target.writestr(CLEARTEXT_HEADER_NAME, b.read(CLEARTEXT_HEADER_NAME))
        target.writestr(SEALED_PAYLOAD_NAME, a.read(SEALED_PAYLOAD_NAME))

    with pytest.raises(HybridPackageError, match="could not be opened"):
        load_work_package(swapped, PASSPHRASE)


def test_a_package_carries_the_plan(tmp_path):
    package = load_work_package(_packages(tmp_path)[0], PASSPHRASE)

    assert package.flight_id == "Flight_2707"
    assert package.assigned_instruments == ("miro", "picarro")
    assert package.payload["analysis_start"] == "2026-07-27T07:19:28+00:00"
    assert package.payload["instrument_options"] == {"sif": {"raw_min_kb": 100}}
    assert package.payload["audit"]["primary_instruments"] == ["noseboom"]


# --------------------------------------------------------------------------
# Authorisation
# --------------------------------------------------------------------------
def test_a_worker_may_only_return_what_it_was_given(tmp_path):
    package = load_work_package(_packages(tmp_path)[0], PASSPHRASE)
    project = tmp_path / "Flight_2707.ccflux"
    project.write_bytes(b"PK\x03\x04pretend")

    with pytest.raises(HybridPackageError, match="does not authorise"):
        export_result_package(
            package, project, tmp_path, PASSPHRASE,
            software_version="1.0.1", processed_instruments=["miro", "sif"],
        )


def test_a_result_package_carries_its_authorisation(tmp_path):
    package = load_work_package(_packages(tmp_path)[0], PASSPHRASE)
    project = tmp_path / "Flight_2707.ccflux"
    project.write_bytes(b"PK\x03\x04pretend")

    exported = export_result_package(
        package, project, tmp_path, PASSPHRASE,
        software_version="1.0.1", processed_instruments=["miro"],
    )
    result = load_result_package(exported, PASSPHRASE)

    assert result.processed_instruments == ("miro",)
    assert result.payload["assigned_instruments"] == ["miro", "picarro"]
    assert result.project_bytes() == b"PK\x03\x04pretend"


def test_a_corrupted_result_is_caught_by_its_own_checksum(tmp_path):
    package = load_work_package(_packages(tmp_path)[0], PASSPHRASE)
    project = tmp_path / "Flight_2707.ccflux"
    project.write_bytes(b"PK\x03\x04pretend")
    exported = export_result_package(
        package, project, tmp_path, PASSPHRASE,
        software_version="1.0.1", processed_instruments=["miro"],
    )
    result = load_result_package(exported, PASSPHRASE)
    result.payload["project_sha256"] = "0" * 64

    with pytest.raises(HybridPackageError, match="checksum"):
        result.project_bytes()


# --------------------------------------------------------------------------
# Fusion
# --------------------------------------------------------------------------
def _result(tmp_path, name, instruments, **plan_overrides):
    plan = _plan(
        assignments=(WorkerAssignment("w", name, tuple(instruments)),),
        primary_instruments=(),
        **plan_overrides,
    )
    folder = tmp_path / name
    package = load_work_package(
        create_work_packages(plan, folder, PASSPHRASE, software_version="1.0.1")[0],
        PASSPHRASE,
    )
    project = folder / "p.ccflux"
    project.write_bytes(b"PK\x03\x04" + name.encode())
    return load_result_package(
        export_result_package(
            package, project, folder, PASSPHRASE,
            software_version="1.0.1", processed_instruments=list(instruments),
        ),
        PASSPHRASE,
    )


def test_matching_results_fuse(tmp_path):
    packages = [
        _result(tmp_path, "A", ["miro"]),
        _result(tmp_path, "B", ["picarro"]),
    ]

    report = review_fusion(packages)
    manifest, _ = fuse(packages, tmp_path / "fused", software_version="1.0.1")

    assert report.ok, report.reasons
    assert report.instruments == ("miro", "picarro")
    written = json.loads(manifest.read_text(encoding="utf-8"))
    assert written["flight_id"] == "Flight_2707"
    assert [item["worker_name"] for item in written["contributions"]] == ["A", "B"]
    assert len(list(manifest.parent.glob("*.ccflux"))) == 2


@pytest.mark.parametrize("overrides, expected", [
    ({"project_id": "other"}, "different projects"),
    ({"flight_id": "Flight_9999"}, "different flights"),
    ({"analysis_start": "2026-07-27T08:00:00+00:00"}, "time range differs"),
    ({"instrument_options": {"sif": {"raw_min_kb": 250}}}, "configuration differs"),
])
def test_results_from_a_different_plan_are_refused(tmp_path, overrides, expected):
    packages = [
        _result(tmp_path, "A", ["miro"]),
        _result(tmp_path, "B", ["picarro"], **overrides),
    ]

    report = review_fusion(packages)

    assert not report.ok
    assert any(expected in reason for reason in report.reasons), report.reasons


def test_the_same_instrument_from_two_workers_is_refused(tmp_path):
    packages = [_result(tmp_path, "A", ["miro"]), _result(tmp_path, "B", ["miro"])]

    report = review_fusion(packages)

    assert not report.ok
    assert any("processed by both" in reason for reason in report.reasons)


@pytest.mark.parametrize("count", [1, MAXIMUM_FUSION_PACKAGES + 1])
def test_fusion_takes_two_to_four_packages(tmp_path, count):
    packages = [
        _result(tmp_path, f"W{index}", [f"i{index}"],
                available_instruments=tuple(f"i{n}" for n in range(count)))
        for index in range(count)
    ]

    assert not review_fusion(packages).ok


def test_fusion_writes_nothing_when_it_is_cancelled(tmp_path):
    packages = [_result(tmp_path, "A", ["miro"]), _result(tmp_path, "B", ["miro"])]
    destination = tmp_path / "never"

    with pytest.raises(HybridPackageError, match="cancelled"):
        fuse(packages, destination, software_version="1.0.1")

    assert not any(destination.glob("*.ccflux"))


# --------------------------------------------------------------------------
# The backend and its restrictions
# --------------------------------------------------------------------------
def test_hybrid_is_blocked_until_the_project_is_prepared(tmp_path):
    from app.scan_backend import DashboardScanBackend

    state = DashboardScanBackend(tmp_path).hybrid_state()

    assert state["available"] is False
    assert state["is_worker"] is False
    assert "Scan a Flight Folder" in state["blocked_reasons"]


def test_a_worker_cannot_change_the_science(tmp_path):
    """The read-only rule has to be a property of the software, not a label."""
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    backend._work_package = load_work_package(_packages(tmp_path)[0], PASSPHRASE)

    for call in (
        lambda: backend.update_sif_options({"raw_min_kb": 250}),
        lambda: backend.update_time_filter({"action": "full"}),
    ):
        with pytest.raises(ValueError, match="fixed by the work package"):
            call()


def test_a_worker_cannot_hand_out_further_packages(tmp_path):
    from app.scan_backend import DashboardScanBackend

    backend = DashboardScanBackend(tmp_path)
    backend._work_package = load_work_package(_packages(tmp_path)[0], PASSPHRASE)

    with pytest.raises(ValueError, match="cannot hand out"):
        backend.create_hybrid_packages({"passphrase": PASSPHRASE, "workers": []})


def test_the_errors_reach_the_operator():
    """Every one of these is something to read, so the HTTP layer must not
    replace it with a generic failure."""
    assert issubclass(HybridPackageError, ValueError)


def test_the_interface_offers_the_whole_workflow():
    assets = Path(__file__).parents[1] / "app" / "assets"
    script = (assets / "dashboard.js").read_text(encoding="utf-8")
    markup = (assets / "dashboard.html").read_text(encoding="utf-8")

    assert 'id="hybridBtn"' in markup
    assert 'id="workerBanner"' in markup, "a worker must always see that it is one"
    for piece in ("openHybridDialog", "openWorkPackageLoader", "openFusionDialog",
                  "showWorkPackageDialog", "/api/hybrid/create", "/api/hybrid/load",
                  "/api/hybrid/export", "/api/hybrid/fusion/review",
                  "/api/hybrid/fusion/start"):
        assert piece in script, piece
    assert "the scientific configuration is fixed" in script
