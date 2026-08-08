"""Writing a workspace payload where another process may be holding the file.

Flight_CC0807 failed OPC HBX-4 with "[WinError 32] The process cannot access
the file because it is being used by another process" on the rename of
opc_hbx4_browser.json.tmp, while HBX-5 - written moments apart, by the same
code - completed. That is the shape of the whole class of fault: it is not the
writer's doing, it passes in milliseconds, and it looks random.
"""

from pathlib import Path

import pytest



class TestAtomicWritesSurviveAHeldFile:
    """OPC HBX-4 failed with WinError 32 while HBX-5, written moments apart,
    completed - which is why it looked random.

    Windows refuses to rename over, or away from, a file any process holds
    open. Defender opens a freshly written file to scan it, Explorer and the
    search indexer read what appears in a watched folder, and a workspace page
    may be fetching the very payload being replaced. None of it is the writer's
    doing and all of it passes in milliseconds.
    """

    def test_the_payload_lands(self, tmp_path):
        from core.browser_payload import write_text_atomic

        target = write_text_atomic(tmp_path / "opc_hbx4_browser.json", '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'

    def test_an_existing_file_is_replaced(self, tmp_path):
        from core.browser_payload import write_text_atomic

        path = tmp_path / "browser.json"
        path.write_text("old", encoding="utf-8")
        write_text_atomic(path, "new")
        assert path.read_text(encoding="utf-8") == "new"

    def test_a_transient_refusal_is_retried(self, tmp_path, monkeypatch):
        import core.browser_payload as module

        attempts = []
        real = module.os.replace

        def flaky(source, destination):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(32, "used by another process")
            return real(source, destination)

        monkeypatch.setattr(module.os, "replace", flaky)
        monkeypatch.setattr(module, "REPLACE_BACKOFF_SECONDS", 0.0)

        path = module.write_text_atomic(tmp_path / "browser.json", "payload")

        assert len(attempts) == 3
        assert path.read_text(encoding="utf-8") == "payload"

    def test_a_permanent_refusal_still_raises(self, tmp_path, monkeypatch):
        import core.browser_payload as module

        def refuse(source, destination):
            raise PermissionError(32, "used by another process")

        monkeypatch.setattr(module.os, "replace", refuse)
        monkeypatch.setattr(module, "REPLACE_BACKOFF_SECONDS", 0.0)

        with pytest.raises(PermissionError):
            module.write_text_atomic(tmp_path / "browser.json", "payload")

    def test_a_failed_write_leaves_no_temporary_behind(self, tmp_path, monkeypatch):
        """The project bundler would publish it as if it were a product."""
        import core.browser_payload as module

        monkeypatch.setattr(
            module.os, "replace",
            lambda source, destination: (_ for _ in ()).throw(
                PermissionError(32, "held")
            ),
        )
        monkeypatch.setattr(module, "REPLACE_BACKOFF_SECONDS", 0.0)

        with pytest.raises(PermissionError):
            module.write_text_atomic(tmp_path / "browser.json", "payload")

        assert list(tmp_path.iterdir()) == []

    def test_two_writers_do_not_share_a_temporary_name(self, tmp_path, monkeypatch):
        """A fixed ".tmp" let one writer overwrite the other's half-written file
        and then race it to the rename."""
        import core.browser_payload as module

        seen = []
        real = module.os.replace

        def record(source, destination):
            seen.append(Path(source).name)
            return real(source, destination)

        monkeypatch.setattr(module.os, "replace", record)
        module.write_text_atomic(tmp_path / "browser.json", "one")
        module.write_text_atomic(tmp_path / "browser.json", "two")

        assert len(set(seen)) == 2, seen
        assert all(name.endswith(".writing") for name in seen)

    def test_every_quicklook_write_uses_it(self):
        backend = (
            Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
        ).read_text(encoding="utf-8")
        assert "temporary.replace(" not in backend
        assert 'with_suffix(".json.temporary")' not in backend
        assert "write_text_atomic(" in backend

    def test_the_hatchbox_payloads_use_it_too(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "instruments" / "hatchbox_payload.py"
        ).read_text(encoding="utf-8")
        assert "write_text_atomic(" in source
        assert 'suffix + ".tmp"' not in source

