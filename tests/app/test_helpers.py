"""The test helpers themselves (they read the real Recycle Bin while Windows is still writing to it)."""
import builtins
import hashlib
import os
import struct

import pytest

import helpers as h


def _v2_record(orig, size=2000):
    raw = orig.encode("utf-16-le") + b"\0\0"
    return struct.pack("<qqqi", 2, size, 0, len(orig) + 1) + raw


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(h.time, "sleep", lambda s: None)


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


def test_a_record_that_stays_locked_raises_unless_polling(tmp_path, monkeypatch):
    q = tmp_path / "$I789.bin"
    q.write_bytes(_v2_record("C:\\b.bin"))
    real = builtins.open

    def locked(path, *a, **k):
        if str(path) == str(q):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(builtins, "open", locked)
    with pytest.raises(PermissionError):
        h._read_record(str(q))
    assert h._read_record(str(q), poll=True) is None


GOOD = _v2_record("C:\\bftest\\a.bin")
BROKEN = {
    "empty": b"",
    "header only": GOOD[:20],
    "no name length": GOOD[:26],
    "all zeros (version 0)": bytes(60),
    "name cut at an odd byte": GOOD[:28 + 5],
    "name cut short": GOOD[:28 + 6],
    "version 1 cut short": struct.pack("<qqq", 1, 5, 0) + b"C\0:\0",
}


@pytest.mark.parametrize("what", list(BROKEN))
def test_a_record_that_is_not_complete_raises_or_is_left_out_when_polling(tmp_path, what):
    p = tmp_path / "$Ibad.bin"
    p.write_bytes(BROKEN[what])
    with pytest.raises(ValueError):
        h._read_record(str(p))
    assert h._read_record(str(p), poll=True) is None


def test_a_record_that_is_gone_is_none(tmp_path):
    assert h._read_record(str(tmp_path / "$Igone.bin")) is None


def test_a_version_1_record_is_read_too(tmp_path):
    p = tmp_path / "$I1.bin"
    p.write_bytes(struct.pack("<qqq", 1, 777, 0) + "C:\\old\\f.bin".encode("utf-16-le").ljust(520, b"\0"))
    assert h._read_record(str(p)) == ("C:\\old\\f.bin", 777)


def test_a_stray_record_makes_the_bin_not_empty_and_a_cut_one_is_an_orphan(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "my_sid", lambda: "S-1")
    drive = str(tmp_path)  # the helpers look in <drive>\$Recycle.Bin\<sid>
    d = tmp_path / "$Recycle.Bin" / "S-1"
    d.mkdir(parents=True)
    # a complete record with its data, one cut short by a kill with its data, and data with no record at all
    (d / "$Ia.bin").write_bytes(GOOD)
    (d / "$Ra.bin").write_bytes(b"data-a")
    (d / "$Ib.bin").write_bytes(GOOD[:28 + 6])
    (d / "$Rb.bin").write_bytes(b"data-b")
    (d / "$Rc.bin").write_bytes(b"data-c")
    orph = h.recycle_bin_orphans(drive)
    assert set(orph.values()) == {str(d / "$Rb.bin"), str(d / "$Rc.bin")}
    assert hashlib.sha1(b"data-b").hexdigest() in orph and hashlib.sha1(b"data-a").hexdigest() not in orph
    with pytest.raises(ValueError):
        h.recycle_bin_entries(drive)  # a final read doesn't hide the broken record
    assert [e[0] for e in h.recycle_bin_entries(drive, poll=True)] == ["C:\\bftest\\a.bin"]
    h.remove_broken_records(drive)
    assert not os.path.exists(d / "$Ib.bin") and os.path.exists(d / "$Ia.bin")
    assert [e[0] for e in h.recycle_bin_entries(drive)] == ["C:\\bftest\\a.bin"]  # now a strict read passes