"""The test helpers themselves (they read the real Recycle Bin while Windows is still writing to it)."""
import builtins
import struct

import helpers as h


def _v2_record(orig, size=2000):
    raw = orig.encode("utf-16-le") + b"\0\0"
    return struct.pack("<qqqi", 2, size, 0, len(orig) + 1) + raw


def test_a_record_that_is_locked_for_a_moment_is_read_when_it_opens(tmp_path, monkeypatch):
    p = tmp_path / "$I123.bin"
    p.write_bytes(_v2_record(r"C:\bftest\x\f001.bin", 2000))
    real, tries = builtins.open, []

    def flaky(path, *a, **k):
        if str(path) == str(p) and len(tries) < 3:
            tries.append(1)
            raise PermissionError(13, "Permission denied", str(path))  # Windows is still writing it
        return real(path, *a, **k)
    monkeypatch.setattr(builtins, "open", flaky)
    assert h._read_record(str(p)) == (r"C:\bftest\x\f001.bin", 2000)
    assert len(tries) == 3


def test_a_record_that_stays_locked_or_is_cut_short_or_is_gone_is_left_out(tmp_path, monkeypatch):
    p = tmp_path / "$I456.bin"
    p.write_bytes(_v2_record("C:\\a.bin")[:10])  # cut short
    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    assert h._read_record(str(p)) is None
    assert h._read_record(str(tmp_path / "$Igone.bin")) is None
    q = tmp_path / "$I789.bin"
    q.write_bytes(_v2_record("C:\\b.bin"))
    real = builtins.open

    def locked(path, *a, **k):
        if str(path) == str(q):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(builtins, "open", locked)
    assert h._read_record(str(q)) is None


def test_a_version_1_record_is_read_too(tmp_path):
    p = tmp_path / "$I1.bin"
    p.write_bytes(struct.pack("<qqq", 1, 777, 0) + "C:\\old\\f.bin".encode("utf-16-le").ljust(520, b"\0"))
    assert h._read_record(str(p)) == ("C:\\old\\f.bin", 777)
