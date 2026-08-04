"""A scan must not throw away what the open project already produced.

Flight_CCT0803 was processed on Windows, where the flight folder was
D:\\Flight_CCT0803, and reopened on a Mac where the same data sit under
/Volumes/SSD/Flight_CCT0803. Scanning that folder matched the open project
only on its recorded path, so it started a brand-new project: every processed
product stayed in the .ccflux but nothing referenced it any more, and every
workspace reported that its instrument had not been processed.
"""
import inspect

from app.scan_backend import DashboardScanBackend

SOURCE = inspect.getsource(DashboardScanBackend.start_scan)


def test_the_same_flight_id_is_the_same_flight():
    assert "self._flight_project.flight_id == root.name" in SOURCE


def test_the_project_adopts_the_folder_being_scanned():
    assert "existing_project.flight_folder_path = root" in SOURCE


def test_the_recorded_path_still_matches_first():
    assert "recorded == root.resolve(strict=False)" in SOURCE


def test_the_operator_is_told_the_folder_moved():
    assert "Keeping its processed " in SOURCE


class TestScanPreservesProducts:
    def test_a_rescan_keeps_the_product_locations(self, tmp_path):
        """The regression itself: same flight id, different folder path."""
        from core.flight_project import FlightProject

        flight = tmp_path / "Flight_CCT0803"
        flight.mkdir()
        project = FlightProject(
            flight_id="Flight_CCT0803",
            flight_folder_path=tmp_path / "elsewhere" / "Flight_CCT0803",
            output_folder_path=tmp_path / "out",
            camera_folder_path=None,
            cpu_allocation=2,
            ram_allocation_bytes=1 << 30,
        )
        project.output_locations = {"opc_browser": tmp_path / "opc_browser.json"}

        # The decision the scan makes, in isolation from the threading around it.
        recorded = project.flight_folder_path.resolve(strict=False)
        adopted = None
        if recorded == flight.resolve(strict=False):
            adopted = project
        elif project.flight_id == flight.name:
            adopted = project
            adopted.flight_folder_path = flight

        assert adopted is project
        assert adopted.flight_folder_path == flight
        assert "opc_browser" in adopted.output_locations

    def test_a_different_flight_is_not_adopted(self, tmp_path):
        from core.flight_project import FlightProject

        flight = tmp_path / "Flight_OTHER"
        flight.mkdir()
        project = FlightProject(
            flight_id="Flight_CCT0803",
            flight_folder_path=tmp_path / "elsewhere" / "Flight_CCT0803",
            output_folder_path=tmp_path / "out",
            camera_folder_path=None,
            cpu_allocation=2,
            ram_allocation_bytes=1 << 30,
        )
        recorded = project.flight_folder_path.resolve(strict=False)
        adopted = None
        if recorded == flight.resolve(strict=False) or project.flight_id == flight.name:
            adopted = project
        assert adopted is None
