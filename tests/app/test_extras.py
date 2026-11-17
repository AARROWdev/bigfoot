"""G. Extra tests beyond the plan: the weak spots found while testing (permanent deletes, races, model
consistency, odd input)."""
import gc
import os
import random
import subprocess
import time

import pytest
from PySide6.QtCore import QModelIndex, QPoint, Qt, qInstallMessageHandler
from PySide6.QtTest import QAbstractItemModelTester, QTest

import bigfoot as bf
import helpers as h


def recycle_one(w, path, dialogs):
    root = w.scanner.root
    it = h.find_dir(root, path)
    h.select_items(w, [it])
    w.tree.setFocus()
    QTest.keyClick(w.tree, Qt.Key_Delete)
    h.pump(300)
    return it


def gone_for_good(path):
    return not os.path.exists(h.lp(path)) and not h.in_bin(path)


# G1: the spec says Bigfoot never deletes permanently. Windows deletes instead of recycling when a file is too
# big for the Recycle Bin, or the drive has no Recycle Bin (network shares, subst drives, mapped folders).
@pytest.mark.sandbox
def test_file_too_big_for_the_recycle_bin_is_not_deleted(window, work, dialogs):
    h.empty_bin()
    huge = os.path.join(work, "huge.vhdx")
    h.sparse(huge, 4 << 40)  # 4 TB logical: far over any Recycle Bin size limit
    h.put(os.path.join(work, "small.bin"), size=10)
    h.scan(window, work)
    recycle_one(window, huge, dialogs)
    assert not gone_for_good(huge), "a file too big for the Recycle Bin was deleted permanently"
    assert os.path.exists(huge) and "bigger than this drive's Recycle Bin" in window.status.text()
    assert not dialogs.texts, "refused before the confirm dialog"


@pytest.mark.sandbox
def test_recycle_bin_size_limit_edges(window, work, dialogs):
    """Just under the drive's Recycle Bin size: recycled. Just over: refused, file kept."""
    h.empty_bin()
    import winreg
    import ctypes as c
    guid = c.create_unicode_buffer(64)
    c.windll.kernel32.GetVolumeNameForVolumeMountPointW("C:\\", guid, 64)
    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid.value[10:-1]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        cap = winreg.QueryValueEx(k, "MaxCapacity")[0] * 2**20
    under = os.path.join(work, "under.img")
    over = os.path.join(work, "over.img")
    h.sparse(under, cap - 2**20)
    h.sparse(over, cap + 2**20)
    h.scan(window, work)
    recycle_one(window, under, dialogs)
    assert h.in_bin(under), window.status.text()
    h.scan(window, work)
    recycle_one(window, over, dialogs)
    assert os.path.exists(over) and "bigger than" in window.status.text()


@pytest.mark.sandbox
def test_windows_set_to_delete_instead_of_recycling(window, work, dialogs):
    """'Don't move files to the Recycle Bin' (NukeOnDelete): Bigfoot refuses instead of deleting for good."""
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
    vols = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
        for i in range(winreg.QueryInfoKey(k)[0]):
            vols.append(base + "\\" + winreg.EnumKey(k, i))
    old = {}
    for v in vols:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, v, 0, winreg.KEY_ALL_ACCESS) as k:
            old[v] = winreg.QueryValueEx(k, "NukeOnDelete")[0]
            winreg.SetValueEx(k, "NukeOnDelete", 0, winreg.REG_DWORD, 1)
    try:
        p = h.put(os.path.join(work, "keep_me.bin"), size=100)
        h.scan(window, work)
        recycle_one(window, p, dialogs)
        assert os.path.exists(p), "deleted permanently while Windows was set not to recycle"
        assert "instead of recycling" in window.status.text()
    finally:
        for v, val in old.items():
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, v, 0, winreg.KEY_ALL_ACCESS) as k:
                winreg.SetValueEx(k, "NukeOnDelete", 0, winreg.REG_DWORD, val)


@pytest.mark.sandbox
def test_subst_drive_recycle_is_not_permanent(make_window, work, dialogs):
    h.empty_bin()
    h.put(os.path.join(work, "s.bin"), size=1234)
    subprocess.run(["subst", "S:", work], check=True)
    try:
        w = make_window()
        h.scan(w, "S:\\")
        recycle_one(w, "S:\\s.bin", dialogs)
        assert os.path.exists(os.path.join(work, "s.bin")) or h.in_bin(os.path.join(work, "s.bin")) or \
            h.in_bin("S:\\s.bin"), "recycling on a subst drive deleted the file permanently"
    finally:
        subprocess.run(["subst", "S:", "/d"], capture_output=True)


@pytest.mark.sandbox
def test_network_share_recycle_is_not_permanent(make_window, work, dialogs):
    h.put(os.path.join(work, "n.bin"), size=4321)
    r = subprocess.run(["net", "share", f"bfrec={work}", "/grant:everyone,full"], capture_output=True, text=True)
    if r.returncode:
        pytest.skip("no SMB server in this Sandbox")
    try:
        w = make_window()
        if not h.reachable(r"\\localhost\bfrec"):
            pytest.skip(r"\\localhost isn't reachable (run-tests.ps1 -Networking)")
        h.scan(w, r"\\localhost\bfrec", timeout=60)
        recycle_one(w, r"\\localhost\bfrec\n.bin", dialogs)
        assert os.path.exists(os.path.join(work, "n.bin")), "recycling on a network share deleted the file permanently"
    finally:
        subprocess.run(["net", "share", "bfrec", "/delete", "/y"], capture_output=True)


@pytest.mark.sandbox
def test_mapped_folder_recycle_is_not_permanent(make_window, dialogs):
    """C:\\out is a folder mapped in from the host (like a VM shared folder): no Recycle Bin there."""
    d = os.path.join("C:\\out", "recycle-probe")
    os.makedirs(d, exist_ok=True)
    p = h.put(os.path.join(d, "probe.bin"), size=77)
    w = make_window()
    h.scan(w, d)
    recycle_one(w, p, dialogs)
    assert not gone_for_good(p), "recycling in a mapped folder deleted the file permanently"


# G2: the model stays consistent through sorting, Files on/off, live refreshes, recycling and rescans
@pytest.mark.sandbox
@pytest.mark.parametrize("seed", range(15))
def test_model_consistency_under_random_operations(window, work, dialogs, seed):
    warnings = []
    prev = qInstallMessageHandler(lambda mode, ctx, msg: warnings.append(msg))
    try:
        tester = QAbstractItemModelTester(window.model, QAbstractItemModelTester.FailureReportingMode.Warning)
        rnd = random.Random(seed)
        h.random_tree(work, seed, max_depth=4, max_entries=10)
        h.scan(window, work)
        for n in range(300):
            op = rnd.random()
            root = window.scanner.root
            if op < 0.2:
                col = rnd.randrange(6)
                window.tree.header().setSortIndicator(col, rnd.choice([Qt.AscendingOrder, Qt.DescendingOrder]))
            elif op < 0.3:
                window.btn_files.toggle()
            elif op < 0.5:
                idx = _random_index(window, rnd)
                if idx.isValid():
                    window.tree.setExpanded(idx, not window.tree.isExpanded(idx))
                    h.click_index(window.tree, idx, rnd.choice([Qt.NoModifier, Qt.ControlModifier, Qt.ShiftModifier]))
            elif op < 0.6:
                window.rebuild()
            elif op < 0.7:
                files = [f for f in bf.walk_files(root)] if window.model.show_files else []
                if files:
                    f = rnd.choice(files)
                    if window._safe_path(f.path):
                        h.select_items(window, [f])
                        dialogs.answer = rnd.choice(["yes", "no"])
                        window.tree.setFocus()
                        QTest.keyClick(window.tree, Qt.Key_Delete)
                        h.pump(50)
            elif op < 0.75:
                h.scan(window, work)
            else:
                window.tabs.setCurrentIndex(rnd.randrange(3))
                if window.map.hits:
                    r = rnd.choice(window.map.hits)[0]
                    QTest.mouseClick(window.map, rnd.choice([Qt.LeftButton, Qt.RightButton]), Qt.NoModifier,
                                     r.center().toPoint())
            if n % 20 == 0:
                gc.collect()
            h.pump(5)
        _check_model(window.model)
        root = h.scan(window, work)
        assert (root.size, root.nfiles) == h.truth(work)[work][:2]
    finally:
        qInstallMessageHandler(prev)
    bad = [m for m in warnings if "FAIL" in m or "QAbstractItemModelTester" in m]
    assert not bad, bad[:5]


def _random_index(w, rnd):
    idx = w.model.index_for(w.model.root)
    for _ in range(rnd.randrange(5)):
        n = w.model.rowCount(idx)
        if not n:
            break
        idx = w.model.index(rnd.randrange(n), 0, idx)
    return idx


def _check_model(m):
    """Every index's parent points back to it, and rows match the item's own _row."""
    stack = [QModelIndex()]
    while stack:
        p = stack.pop()
        for r in range(m.rowCount(p)):
            i = m.index(r, 0, p)
            assert i.isValid() and m.parent(i) == p, (r, p)
            it = i.internalPointer()
            assert m.index_for(it) == i
            stack.append(i)


# G3: Stop / Scan / F5 hammered: no stale finish, no negative times, last scan wins
def test_rapid_scan_stop_rescan(window, work, monkeypatch):
    for i in range(30):
        h.make_tree(os.path.join(work, f"d{i}"), {f"f{j}.bin": 10 for j in range(10)})
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.002)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    window.path.setText(work)
    rnd = random.Random(5)
    for _ in range(60):
        rnd.choice([lambda: QTest.mouseClick(window.btn_scan, Qt.LeftButton),
                    lambda: QTest.keyClick(window, Qt.Key_F5),
                    lambda: QTest.keyClick(window.path, Qt.Key_Return)])()
        h.pump(rnd.choice([0, 1, 10, 50]))
        assert "-" not in window.status.text().split("·")[-1], window.status.text()  # no negative time
    h.wait_until(lambda: h.scan_finished(window), 60)
    monkeypatch.setattr(bf.Scanner, "_scan", real)
    if window.status.text().startswith("Stopped"):
        h.scan(window, work)
    root = window.scanner.root
    assert window.model.root is root and (root.size, root.nfiles) == (3000, 300)


# G4: recycling what the map is showing (the focus folder, or its parent)
@pytest.mark.sandbox
def test_recycle_the_map_focus_and_its_parent(window, work, dialogs):
    h.empty_bin()
    h.make_tree(work, {"p": {"c": {"x.bin": 500, "g": {"y.bin": 50}}, "z.bin": 5}, "other.bin": 7})
    root = h.scan(window, work)
    c = h.find_dir(root, os.path.join(work, "p", "c"))
    window.set_focus(c)
    window.tabs.setCurrentIndex(0)
    h.pump(50)
    recycle_one(window, os.path.join(work, "p", "c"), dialogs)
    assert window.focus is c.parent and window.map.focus is c.parent
    assert c.path not in window.crumb.toolTip()
    window.map.repaint()
    for r, it, _ in list(window.map.hits):
        assert not window._inside(it, c), "the map still draws the recycled folder"
    gc.collect()
    QTest.mouseClick(window.map, Qt.LeftButton, Qt.NoModifier, QPoint(40, 60))
    h.pump(50)
    p = h.find_dir(root, os.path.join(work, "p"))
    g = h.find_dir(root, os.path.join(work, "p", "z.bin"))
    window.set_focus(p)
    recycle_one(window, os.path.join(work, "p"), dialogs)
    assert window.focus is root and root.size == 7 and g.parent is p
    QTest.mouseClick(window.btn_up, Qt.LeftButton)
    h.pump(50)


# G5: the path box takes anything without an exception
@pytest.mark.parametrize("text", ["", "   ", '""', "C:", "C:/Windows/../Windows", "..", "%TEMP%", "\\\\", "\\\\?\\",
                                  "\0", "x" * 40000, "C:\\" + "a\\" * 200, "COM1", "\\\\.\\PhysicalDrive0",
                                  "nul", "C:\\Windows\\notepad.exe", "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}"])
def test_path_box_accepts_anything(window, work, text, monkeypatch):
    errors = []
    monkeypatch.setattr(bf.sys, "excepthook", lambda *a: errors.append(a))
    real = bf.Scanner.start

    def no_big_scans(self):  # don't scan whole drives from a test: stop at once
        self.stopped = self.root.path[1:3] == ":\\" and len(self.root.path) <= 12
        real(self)
    monkeypatch.setattr(bf.Scanner, "start", no_big_scans)
    window.path.setText(text)
    t0 = time.time()
    try:
        window.scan()
    except Exception as e:  # noqa: BLE001
        errors.append(e)
    h.wait_until(lambda: window.scanner is None or h.scan_finished(window), 120)
    assert time.time() - t0 < 30
    assert not errors, errors
    assert window.status.text()


# G6: deleting during a scan is refused (nothing recycled), and Files off + Top 100 recycle still works
@pytest.mark.sandbox
def test_delete_during_scan_refused_and_top100_with_files_off(window, work, dialogs, monkeypatch):
    h.empty_bin()
    for i in range(20):
        h.make_tree(os.path.join(work, f"d{i}"), {f"s{j}": {"f.bin": 100} for j in range(5)})
    h.put(os.path.join(work, "big.bin"), size=99_999)
    root = h.scan(window, work)
    before = h.tree_hash(work)
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.03)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    window.scan()
    h.pump(200)
    window.trash([h.find_dir(window.scanner.root, os.path.join(work, "big.bin"))] if
                 any(f.name == "big.bin" for f in window.scanner.root.files) else [window.scanner.root.dirs[0]])
    assert window.status.text() == "Wait for the scan to finish before deleting."
    assert h.tree_hash(work) == before
    h.wait_until(lambda: h.scan_finished(window), 120)
    monkeypatch.setattr(bf.Scanner, "_scan", real)
    window.btn_files.setChecked(False)
    window.tabs.setCurrentIndex(2)
    h.pump(50)
    t = window.top
    assert t.item(0, 0).text() == "big.bin"
    QTest.mouseClick(t.viewport(), Qt.LeftButton, Qt.NoModifier, t.visualItemRect(t.item(0, 0)).center())
    h.run_menu_action(lambda: window.top_menu(t.visualItemRect(t.item(0, 0)).center()), "Send to Recycle Bin")
    assert h.in_bin(os.path.join(work, "big.bin")) and not os.path.exists(os.path.join(work, "big.bin"))
    assert window.top.item(0, 0).text() != "big.bin"


# G7: 20 rescans don't grow memory
@pytest.mark.sandbox
def test_rescans_do_not_leak(window, work):
    import psutil
    for i in range(50):
        h.make_tree(os.path.join(work, f"d{i}"), {f"f{j}.bin": 10 for j in range(200)})
    proc = psutil.Process()
    h.scan(window, work)
    for tab in (1, 2, 0):
        window.tabs.setCurrentIndex(tab)
        h.pump(20)
    gc.collect()
    m1 = proc.memory_info().rss
    for _ in range(20):
        h.scan(window, work)
        window.tabs.setCurrentIndex(2)
        h.pump(10)
        window.tabs.setCurrentIndex(0)
    gc.collect()
    h.pump(100)
    m2 = proc.memory_info().rss
    assert m2 - m1 < 40 * 2**20, f"memory grew {(m2 - m1) / 2**20:.0f} MB over 20 rescans"
    live = sum(1 for o in gc.get_objects() if type(o) in (bf.Dir, bf.File))
    tree = window.scanner.root.ndirs + 1 + window.scanner.root.nfiles
    assert live <= tree + 10, f"{live} folder/file objects alive after 20 rescans; the tree has {tree}"


# G8: strange timestamps sort and display
def test_strange_timestamps(window, work):
    times = {"y1601.bin": -11644473600 + 86400, "y1900.bin": -2208988800, "y1969.bin": -86400, "y2100.bin": 4102444800,
             "y30000.bin": 884541340800, "now.bin": time.time()}
    for n, t in times.items():
        p = h.put(os.path.join(work, n), size=10)
        try:
            os.utime(p, (t, t))
        except (OSError, OverflowError, ValueError):
            pass
    root = h.scan(window, work)
    window.tree.header().setSortIndicator(bf.C_MOD, Qt.AscendingOrder)
    h.pump(20)
    ri = window.model.index_for(root)
    for r in range(window.model.rowCount(ri)):
        window.model.data(window.model.index(r, bf.C_MOD, ri))
    window.tabs.setCurrentIndex(2)
    h.pump(20)
    assert window.top.rowCount() == len(times)


# G9: the treemap at extremes (one giant file next to thousands of tiny ones, empty focus, 1-pixel widget)
def test_treemap_extremes(window, work):
    h.sparse(os.path.join(work, "giant.img"), 10 << 40)
    for i in range(3000):
        h.put(os.path.join(work, "tiny", f"{i}.txt"), data=b"t")
    os.makedirs(os.path.join(work, "empty"))
    root = h.scan(window, work)
    window.tabs.setCurrentIndex(0)
    for size in ((1, 1), (3, 500), (2000, 2), (900, 600)):
        window.map.setFixedSize(*size)
        window.map.repaint()
        h.pump(10)
    window.set_focus(h.find_dir(root, os.path.join(work, "empty")))
    window.map.repaint()
    window.set_focus(h.find_dir(root, os.path.join(work, "tiny")))
    window.map.repaint()
    assert window.map.hits


# G10: recycling things the shell might mishandle: long paths, read-only / hidden / system files, unicode,
# and folders holding junctions or symlinks to data OUTSIDE the selection (which must survive untouched)
@pytest.mark.sandbox
def test_recycle_long_path_items(window, work, dialogs):
    h.empty_bin()
    deep = work
    while len(deep) < 300:
        deep = os.path.join(deep, "deep_folder_name_" + str(len(deep)))
    f = h.put(os.path.join(deep, "deep.bin"), size=500)
    h.put(os.path.join(work, "keep.bin"), size=5)
    before = h.tree_hash(work)
    root = h.scan(window, work)
    top = root.dirs[0]
    for target in (f, top.path):
        h.scan(window, work)
        recycle_one(window, target, dialogs)
        assert not gone_for_good(target) or any(e[0].lower().startswith(top.path.lower()) for e in
                                                h.recycle_bin_entries()), f"{target} deleted permanently"
        if os.path.exists(h.lp(target)):
            assert "couldn't move" in window.status.text()
    for e in h.recycle_bin_entries():
        h.restore(e[0])
    assert h.tree_hash(work) == before


@pytest.mark.sandbox
def test_recycle_read_only_hidden_system_and_unicode_files(window, work, dialogs):
    h.empty_bin()
    names = ["ro.bin", "hid.bin", "sys.bin", "日本 😀 ü.bin", "é.bin"]
    for n in names:
        h.put(os.path.join(work, n), size=100)
    subprocess.run(["attrib", "+r", os.path.join(work, "ro.bin")], check=True)
    subprocess.run(["attrib", "+h", os.path.join(work, "hid.bin")], check=True)
    subprocess.run(["attrib", "+s", os.path.join(work, "sys.bin")], check=True)
    before = h.tree_hash(work)
    root = h.scan(window, work)
    h.select_items(window, list(root.files))
    window.tree.setFocus()
    QTest.keyClick(window.tree, Qt.Key_Delete)
    h.pump(500)
    assert window.status.text() == f"Sent {len(names)} to the Recycle Bin", window.status.text()
    for n in names:
        assert h.in_bin(os.path.join(work, n)), n
        h.restore(os.path.join(work, n))
    assert h.tree_hash(work) == before


@pytest.mark.sandbox
@pytest.mark.parametrize("kind", ["junction", "dir_symlink", "file_symlink"])
def test_recycling_a_folder_never_touches_link_targets(window, work, dialogs, kind):
    h.empty_bin()
    outside = h.make_tree(os.path.join(work, "outside"), {"precious.bin": 9999, "sub": {"more.bin": 42}})
    victim = os.path.join(work, "victim")
    h.make_tree(victim, {"own.bin": 10})
    link = os.path.join(victim, "link")
    if kind == "junction":
        subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], check=True, capture_output=True)
    elif kind == "dir_symlink":
        os.symlink(outside, link, target_is_directory=True)
    else:
        os.symlink(os.path.join(outside, "precious.bin"), link)
    keep = h.tree_hash(outside)
    h.scan(window, work)
    recycle_one(window, victim, dialogs)
    assert h.tree_hash(outside) == keep, f"recycling a folder with a {kind} inside changed the link's target"
    assert h.in_bin(victim)


def _bin_cap(drive="C:\\"):
    import ctypes as c
    import winreg
    guid = c.create_unicode_buffer(64)
    c.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, guid, 64)
    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid.value[10:-1]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        return winreg.QueryValueEx(k, "MaxCapacity")[0] * 2**20


# G11: a multi-selection that together is bigger than the Recycle Bin: Windows makes room by purging the oldest
# items, which can be the ones this same Delete just recycled. Nothing the user sent may end up gone for good.
@pytest.mark.sandbox
@pytest.mark.parametrize("shape", ["files", "folder"])
def test_selection_bigger_than_the_recycle_bin_together(window, work, dialogs, shape):
    h.empty_bin()
    cap = _bin_cap()
    if shape == "files":
        paths = [os.path.join(work, f"part{i}.img") for i in range(3)]
        for p in paths:
            h.sparse(p, int(cap * 0.6))
        root = h.scan(window, work)
        h.select_items(window, [h.find_dir(root, p) for p in paths])
    else:
        folder = os.path.join(work, "bigfolder")
        paths = [os.path.join(folder, f"part{i}.img") for i in range(3)]
        for p in paths:
            h.sparse(p, int(cap * 0.6))
        root = h.scan(window, work)
        h.select_items(window, [h.find_dir(root, folder)])
    window.tree.setFocus()
    QTest.keyClick(window.tree, Qt.Key_Delete)
    h.pump(2000)
    lost = [p for p in paths if gone_for_good(p) and not (shape == "folder" and h.in_bin(os.path.dirname(p)))]
    assert not lost, f"gone for good: {lost}  (status: {window.status.text()})"
    if shape == "files":  # the first fits, the other two are refused, and the status counts both (F53)
        assert "skipped 2 (first" in window.status.text(), window.status.text()


# G12: other volumes: a fresh NTFS / FAT32 / exFAT drive (a virtual disk) has no Recycle Bin settings yet, so
# Bigfoot falls back to Windows' default size. Whatever Windows does there, nothing may be lost for good.
@pytest.mark.sandbox
@pytest.mark.parametrize("fs", ["ntfs", "fat32", "exfat"])
def test_recycle_on_other_volumes(make_window, work, dialogs, fs):
    letter = {"ntfs": "N", "fat32": "F", "exfat": "E"}[fs]
    vd = h.make_vhd(os.path.join(work, f"{fs}.vhd"), 256)
    ps = (f"$d = Mount-DiskImage -ImagePath '{vd}' -PassThru | Get-Disk; Initialize-Disk -Number $d.Number "
          f"-PartitionStyle MBR; New-Partition -DiskNumber $d.Number -UseMaximumSize -DriveLetter {letter} | "
          f"Format-Volume -FileSystem {fs.upper()} -NewFileSystemLabel BFT -Confirm:$false")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if r.returncode or not os.path.isdir(f"{letter}:\\"):
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Dismount-DiskImage -ImagePath '{vd}'"])
        pytest.skip(f"couldn't make a {fs} drive here: {(r.stdout + r.stderr)[-300:]}")
    try:
        root = f"{letter}:\\"
        small = h.put(root + "small.bin", size=5000)
        big = h.put(root + "big.bin", size=40 * 2**20)  # over 5% (and 10%) of the 256 MB drive
        w = make_window()
        assert w.drives.findData(root) >= 0, "the new drive isn't in the picker"
        h.scan(w, root)
        for p in (small, big):
            if os.path.exists(p):
                h.scan(w, root)
                recycle_one(w, p, dialogs)
                assert os.path.exists(p) or h.in_bin(p), f"{fs}: {os.path.basename(p)} deleted for good " \
                                                         f"({w.status.text()})"
    finally:
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Dismount-DiskImage -ImagePath '{vd}'"],
                       capture_output=True)


# G13: the 5% fallback when a drive's Recycle Bin size was never set: Windows must not be stingier than Bigfoot
@pytest.mark.sandbox
def test_default_recycle_bin_size_fallback_is_safe(window, work, dialogs):
    import ctypes as c
    import winreg
    h.empty_bin()
    guid = c.create_unicode_buffer(64)
    c.windll.kernel32.GetVolumeNameForVolumeMountPointW("C:\\", guid, 64)
    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid.value[10:-1]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS) as k:
        old = winreg.QueryValueEx(k, "MaxCapacity")[0]
        winreg.DeleteValue(k, "MaxCapacity")
    try:
        total = bf.QStorageInfo("C:\\").bytesTotal()
        p = os.path.join(work, "just_under_5pct.img")
        h.sparse(p, total // 20 - 2**20)
        h.scan(window, work)
        recycle_one(window, p, dialogs)
        assert os.path.exists(p) or h.in_bin(p), f"deleted for good under the fallback size ({window.status.text()})"
    finally:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS) as k:
            winreg.SetValueEx(k, "MaxCapacity", 0, winreg.REG_DWORD, old)

# G14: round-2 review findings. Removable drives, Group Policy size, MaxCapacity 0, a big all-refused selection,
# and drive probing (F48, F50, F51, F52, F55). Windows settings are changed in the Sandbox only, and put back.
def _set_policy(name, value):
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass
        else:
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)


def _bin_key(drive="C:\\"):
    import ctypes as c
    guid = c.create_unicode_buffer(64)
    c.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, guid, 64)
    return r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid.value[10:-1]


@pytest.mark.sandbox
def test_removable_drive_is_refused(window, work, dialogs, monkeypatch):
    """F48: USB sticks and memory cards have no Recycle Bin (simulated: C: reported as DRIVE_REMOVABLE)."""
    h.empty_bin()
    p = h.put(os.path.join(work, "on_usb.bin"), size=100)
    h.scan(window, work)
    k32 = bf.ctypes.windll.kernel32
    real = k32.GetDriveTypeW
    monkeypatch.setattr(k32, "GetDriveTypeW", lambda root: 2 if str(root).upper().startswith("C:") else real(root))
    n = len(dialogs.texts)
    recycle_one(window, p, dialogs)
    assert os.path.exists(p) and not h.in_bin(p)
    assert "removable drive" in window.status.text() and len(dialogs.texts) == n


@pytest.mark.sandbox
def test_recycle_bin_size_policy_is_honoured(window, work, dialogs):
    """F50: a 1% "Maximum allowed Recycle Bin size" policy overrides the bigger user setting."""
    h.empty_bin()
    total = bf.QStorageInfo("C:\\").bytesTotal()
    p = os.path.join(work, "two_percent.img")
    h.sparse(p, total // 50)  # 2% of the drive: under the user's limit, over the policy's
    _set_policy("RecycleBinSize", 1)
    try:
        h.scan(window, work)
        recycle_one(window, p, dialogs)
        assert os.path.exists(p) and "bigger than" in window.status.text(), window.status.text()
    finally:
        _set_policy("RecycleBinSize", None)


@pytest.mark.sandbox
def test_max_capacity_zero_is_respected(window, work, dialogs):
    """F52: MaxCapacity = 0 is a setting (nothing fits), not "never set"."""
    import winreg
    h.empty_bin()
    key = _bin_key()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS) as k:
        old = winreg.QueryValueEx(k, "MaxCapacity")[0]
        winreg.SetValueEx(k, "MaxCapacity", 0, winreg.REG_DWORD, 0)
    try:
        p = h.put(os.path.join(work, "small.bin"), size=1000)
        h.scan(window, work)
        recycle_one(window, p, dialogs)
        assert os.path.exists(p) and "bigger than" in window.status.text(), window.status.text()
    finally:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS) as k:
            winreg.SetValueEx(k, "MaxCapacity", 0, winreg.REG_DWORD, old)


@pytest.mark.sandbox
def test_large_all_refused_selection_is_quick_and_counted(window, work, dialogs):
    """F51 + F53: 5,000 items all refused (recycling switched off) come back at once, and the status counts them."""
    for i in range(5000):
        with open(os.path.join(work, f"{i:04d}.txt"), "wb") as f:
            f.write(b"x")
    root = h.scan(window, work)
    _set_policy("NoRecycleFiles", 1)
    try:
        t0 = time.time()
        window.trash(list(root.files))
        secs = time.time() - t0
        assert window.status.text().startswith("Can't recycle 5,000 items") or \
            window.status.text().startswith("Can't recycle 5000 items"), window.status.text()
        assert secs < 5, f"refusing 5,000 items took {secs:.1f}s"
        assert len(os.listdir(work)) == 5000 and not dialogs.texts
    finally:
        _set_policy("NoRecycleFiles", None)


@pytest.mark.sandbox
def test_drive_list_never_probes_empty_removable_or_optical_drives(window, monkeypatch):
    """F55: a not-ready card reader or DVD drive must not be touched (it could pop up "no disk in the drive")."""
    k32 = bf.ctypes.windll.kernel32
    real_mask, real_type = k32.GetLogicalDrives, k32.GetDriveTypeW
    fake = {"R:\\": 5, "U:\\": 2}  # an empty DVD drive and an empty card reader that don't exist here
    monkeypatch.setattr(k32, "GetLogicalDrives", lambda: real_mask() | (1 << 17) | (1 << 20))
    monkeypatch.setattr(k32, "GetDriveTypeW", lambda root: fake.get(str(root).upper(), None) or real_type(root))
    probed = []
    real_isdir = bf.os.path.isdir
    monkeypatch.setattr(bf.os.path, "isdir", lambda p: probed.append(p) or real_isdir(p))
    window.fill_drives()
    assert not [p for p in probed if p.upper() in fake], probed
    assert window.drives.findData("R:\\") == -1 and window.drives.findData("U:\\") == -1

# G15 (Q3, decided 2026-11-16: match Explorer). Older Recycle Bin contents don't count against a new
# Delete: if the new item fits the bin on its own, Bigfoot recycles it, and Windows may empty the oldest items
# already in the bin to make room, exactly as Explorer does. Only items of the SAME Delete are protected (G11).
@pytest.mark.sandbox
def test_older_bin_contents_do_not_block_a_new_delete(window, work, dialogs):
    h.empty_bin()
    cap = _bin_cap()
    old = os.path.join(work, "old.img")
    new = os.path.join(work, "new.img")
    h.sparse(old, int(cap * 0.6))
    h.sparse(new, int(cap * 0.6))
    h.scan(window, work)
    recycle_one(window, old, dialogs)  # first Delete: fits, recycled
    assert h.in_bin(old)
    h.scan(window, work)
    n = len(dialogs.texts)
    recycle_one(window, new, dialogs)  # second Delete: fits on its own, so it is not refused
    assert len(dialogs.texts) == n + 1 and window.status.text() == "Sent 1 to the Recycle Bin", window.status.text()
    assert h.in_bin(new) and not os.path.exists(new)
