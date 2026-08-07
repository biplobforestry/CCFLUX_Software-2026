"""MIRO reads text only, and both rack loaders repair rather than refuse.

A campaign folder collects second copies of a log and loggers that restart, and
neither is a reason to refuse a delivery. Both are a reason to say so: a run
that silently dropped 16,527 rows and one that had nothing to drop looked
identical afterwards.

MIRO also writes TDMS beside its text export. Its timestamp schema was never
confirmed for this campaign, so it is not read - and while detection still
matched it, one binary file in the candidate list failed validation for the
whole instrument, because it parses as a text file with no t-stamp column.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("scipy")

from core.detector import InputCandidate
from core.legacy_paths import legacy_integration_path


def _legacy(name: str):
    path = legacy_integration_path("MIRO_Rack") / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"ccflux_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


miro = _legacy("miro")
picarro = _legacy("picarro")

MIRO_HEADER = "t-stamp;NO2 wet;CO2 wet;VValve 0"
PICARRO_HEADER = "DATE TIME CO2 CH4 H2O"


def _miro_row(second: int, value: str = "0,0000012") -> str:
    return f"06.08.2026 05:55:{second:02d},800;{value};0,00042;0"


def _miro_file(path: Path, seconds) -> Path:
    path.write_text(
        "\n".join([MIRO_HEADER, *(_miro_row(value) for value in seconds)]) + "\n",
        encoding="utf-8",
    )
    return path


def _picarro_row(second: int) -> str:
    return f"2026-08-06 06:44:{second:02d}.100 415.5 2.01 1.10"


def _picarro_file(path: Path, seconds) -> Path:
    path.write_text(
        "\n".join([PICARRO_HEADER, *(_picarro_row(value) for value in seconds)]) + "\n",
        encoding="utf-8",
    )
    return path


class TestMiroReadsTextOnly:
    def test_discovery_ignores_everything_that_is_not_text(self, tmp_path):
        _miro_file(tmp_path / "flight.txt", [1, 2, 3])
        (tmp_path / "flight.tdms").write_bytes(b"\x00TDMS binary")
        (tmp_path / "notes.csv").write_text("a,b\n", encoding="utf-8")

        assert [path.name for path in miro.discover_files(tmp_path)] == ["flight.txt"]
        assert {path.name for path in miro.ignored_files(tmp_path)} == {
            "flight.tdms", "notes.csv"
        }

    def test_the_load_says_what_it_passed_over(self, tmp_path):
        _miro_file(tmp_path / "flight.txt", [1, 2, 3])
        (tmp_path / "flight.tdms").write_bytes(b"\x00TDMS binary")

        _, meta = miro.load_folder(tmp_path)

        assert meta["rows"] == 3
        assert [Path(value).name for value in meta["ignored_files"]] == ["flight.tdms"]
        assert any("non-text file" in text for text in meta["warnings"])

    def test_a_tdms_only_folder_is_refused_clearly(self, tmp_path):
        (tmp_path / "flight.tdms").write_bytes(b"\x00TDMS binary")

        with pytest.raises(FileNotFoundError, match="No MIRO .txt files"):
            miro.load_folder(tmp_path)

    def test_detection_no_longer_matches_tdms(self):
        from core.configuration import load_detection_configuration

        root = Path(__file__).resolve().parents[1]
        configuration = load_detection_configuration(
            root / "configs" / "instrument_detection.yaml",
            root / "configs" / "file_patterns.yaml",
        )
        extensions = configuration.pattern_sets["miro"].file_extensions

        assert ".txt" in extensions
        assert ".tdms" not in extensions

    def test_the_adapter_drops_a_stray_binary_instead_of_failing(self, tmp_path):
        """A candidate restored from an older project can still carry one."""
        from instruments.miro.adapter import MiroAdapter

        text = _miro_file(tmp_path / "flight.txt", [1, 2, 3])
        binary = tmp_path / "flight.tdms"
        binary.write_bytes(b"\x00TDMS binary")
        adapter = MiroAdapter(output_root=tmp_path / "out", flight_name="F")

        kept, ignored = adapter.text_only(
            InputCandidate("miro", (text, binary), 1.0, "test")
        )

        assert kept.paths == (text,)
        assert ignored == [binary]
        result = adapter.validate(InputCandidate("miro", (text, binary), 1.0, "test"))
        assert not result.errors, "one binary must not fail the whole instrument"
        assert any("non-text file" in text for text in result.warnings)


class TestASecondCopyIsReadOnce:
    @pytest.mark.parametrize("module,writer", [("miro", _miro_file), ("picarro", _picarro_file)])
    def test_a_byte_identical_copy_is_detected(self, tmp_path, module, writer):
        loader = miro if module == "miro" else picarro
        suffix = ".txt" if module == "miro" else ".dat"
        writer(tmp_path / f"a{suffix}", [1, 2, 3])
        writer(tmp_path / f"a - Copy{suffix}", [1, 2, 3])

        _, meta = loader.load_folder(tmp_path)

        assert len(meta["duplicate_files"]) == 1
        assert "Copy" in Path(meta["duplicate_files"][0]).name
        assert meta["files_used"] == 1
        assert any("byte-identical" in text for text in meta["warnings"])

    def test_only_files_sharing_a_size_are_hashed(self, tmp_path, monkeypatch):
        """Hashing every file was a second full read of the whole delivery."""
        _miro_file(tmp_path / "a.txt", [1, 2, 3])
        _miro_file(tmp_path / "b.txt", [1, 2, 3, 4, 5, 6])
        _miro_file(tmp_path / "c.txt", [7, 8, 9, 10, 11, 12, 13, 14])
        hashed: list[Path] = []
        real = miro._sha256
        monkeypatch.setattr(
            miro, "_sha256", lambda path: (hashed.append(path), real(path))[1]
        )

        assert miro.duplicate_files_by_content(miro.discover_files(tmp_path)) == {}
        assert hashed == [], "no two files share a size, so none needed hashing"

    def test_a_size_collision_is_hashed_and_may_not_be_a_duplicate(self, tmp_path):
        """Same length, different content: kept, not silently merged."""
        _miro_file(tmp_path / "a.txt", [1, 2, 3])
        _miro_file(tmp_path / "b.txt", [4, 5, 6])
        assert (tmp_path / "a.txt").stat().st_size == (tmp_path / "b.txt").stat().st_size

        _, meta = miro.load_folder(tmp_path)

        assert meta["duplicate_files"] == []
        assert meta["files_used"] == 2


class TestBrokenTimeIsRepairedAndReported:
    def test_miro_duplicate_timestamps(self, tmp_path):
        _miro_file(tmp_path / "a.txt", [1, 2, 2, 3])

        data, meta = miro.load_folder(tmp_path)

        assert meta["duplicate_timestamps_removed"] == 1
        assert len(data) == 3
        assert any("duplicated MIRO timestamp" in text for text in meta["warnings"])

    def test_miro_unreadable_clock_rows(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text(
            "\n".join([MIRO_HEADER, _miro_row(1), "not-a-time;0,1;0,2;0", _miro_row(3)])
            + "\n",
            encoding="utf-8",
        )

        data, meta = miro.load_folder(tmp_path)

        assert meta["unparsable_rows_removed"] == 1
        assert len(data) == 2
        assert any("no readable t-stamp" in text for text in meta["warnings"])

    def test_miro_out_of_order_rows(self, tmp_path):
        _miro_file(tmp_path / "a.txt", [5, 1, 3])

        data, meta = miro.load_folder(tmp_path)

        assert meta["out_of_order_rows_repaired"] >= 1
        assert data["timestamp"].is_monotonic_increasing
        assert any("out-of-order MIRO" in text for text in meta["warnings"])

    def test_picarro_duplicate_timestamps(self, tmp_path):
        _picarro_file(tmp_path / "a.dat", [1, 2, 2, 3])

        data, meta = picarro.load_folder(tmp_path)

        assert meta["duplicate_timestamps_removed"] == 1
        assert len(data) == 3
        assert any("duplicated Picarro timestamp" in text for text in meta["warnings"])

    def test_picarro_unreadable_clock_rows(self, tmp_path):
        path = tmp_path / "a.dat"
        path.write_text(
            "\n".join(
                [PICARRO_HEADER, _picarro_row(1), "bad stamp 415.5 2.01 1.10",
                 _picarro_row(3)]
            )
            + "\n",
            encoding="utf-8",
        )

        data, meta = picarro.load_folder(tmp_path)

        assert meta["unparsable_rows_removed"] == 1
        assert len(data) == 2
        assert any("no readable DATE/TIME" in text for text in meta["warnings"])

    def test_picarro_out_of_order_rows(self, tmp_path):
        _picarro_file(tmp_path / "a.dat", [5, 1, 3])

        data, meta = picarro.load_folder(tmp_path)

        assert meta["out_of_order_rows_repaired"] >= 1
        assert data["timestamp"].is_monotonic_increasing
        assert any("out-of-order Picarro" in text for text in meta["warnings"])

    @pytest.mark.parametrize("module,writer,suffix", [
        ("miro", _miro_file, ".txt"), ("picarro", _picarro_file, ".dat"),
    ])
    def test_a_repaired_delivery_is_still_usable(self, tmp_path, module, writer, suffix):
        """Repair must leave a frame the analysis can actually run on."""
        loader = miro if module == "miro" else picarro
        writer(tmp_path / f"a{suffix}", [5, 1, 3, 3])

        data, meta = loader.load_folder(tmp_path)

        assert data["timestamp"].is_monotonic_increasing
        assert not data["timestamp"].duplicated().any()
        assert data["timestamp"].notna().all()
        assert meta["rows"] == len(data)
        assert meta["warnings"], "a repaired delivery must not be silent"


def test_the_miro_adapter_puts_load_warnings_on_the_result(tmp_path):
    """They reached the metadata but never the card."""
    from instruments.miro.adapter import MiroAdapter

    path = _miro_file(tmp_path / "a.txt", [1, 2, 2, 3, 4, 5, 6, 7, 8, 9])
    adapter = MiroAdapter(output_root=tmp_path / "out", flight_name="F")
    loaded = adapter.load(InputCandidate("miro", (path,), 1.0, "test"))

    assert any("duplicated MIRO timestamp" in text
               for text in loaded.load_metadata["warnings"])

    result = adapter.process_quicklook(loaded, {"gas": "NO2 wet"})

    assert any("duplicated MIRO timestamp" in text for text in result.warnings)
