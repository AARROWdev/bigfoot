"""B. Scanning (plan tests 6-15) and the scanning chaos tests (32, 34-37, 45)."""
import os
import random
import subprocess
import threading
import time

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import bigfoot as bf
import helpers as h


def check_against_truth(root_obj, root_path):
    t = h.truth(root_path)
    seen = 0
    stack = [root_obj]
    while stack:
        d = stack.pop()
        assert not d.denied and not d.skipped, d.path
        assert (d.size, d.nfiles, d.ndirs) == t[d.path], f"{d.path}: {(d.size, d.nfiles, d.ndirs)} != {t[d.path]}"
        seen += 1
        stack.extend(d.dirs)
    assert seen == len(t), "folder count differs from ground truth"


# 6
def test_totals_match_ground_truth(window, work):
    h.make_tree(work, {
        "a": {"a1.bin": 1000, "a2.mp4": 123456, "deep": {"x": {"y": {"z.txt": 7}}}},
        "b": {"empty": {}, "zero.dat": 0},
        "c.iso": 999_999,
        "d": {f"f{i}.jpg": i * 17 for i in range(200)},
    })
    h.sparse(os.path.join(work, "huge.vhdx"), 3 << 40)  # 3 TB logical
    root = h.scan(window, work)
    check_against_truth(root, work)
    assert root.size == sum(os.path.getsize(h.lp(os.path.join(dp, f))) for dp, _, fs in os.walk(h.lp(work)) for f in fs)
    idx = window.model.index_for(root)
    assert window.model.data(idx.siblingAtColumn(bf.C_SIZE)) == bf.human(root.size)
    assert window.model.data(idx.siblingAtColumn(bf.C_FILES)) == f"{root.nfiles:,}"
    assert window.status.text().startswith(f"Scanned {work}")
    assert f"{root.nfiles:,} files" in window.status.text()


# 7
@pytest.mark.sandbox
def test_access_denied_folder_is_marked_and_scan_continues(window, work):
    h.make_tree(work, {"open": {"o.bin": 500}, "locked": {"secret.bin": 900, "sub": {"s": 1}}, "after": {"z": 50}})
    locked = os.path.join(work, "locked")
    subprocess.run(["icacls", locked, "/deny", "*S-1-1-0:(OI)(CI)(RX)"], check=True, capture_output=True)
    try:
        root = h.scan(window, work)
        d = h.find_dir(root, locked)
        assert d.denied
        assert h.find_dir(root, os.path.join(work, "after")).size == 50
        assert root.size == 550
        assert "access denied" in window.status.text()
        idx = window.model.index_for(d)
        assert window.model.data(idx, Qt.ForegroundRole).name() == "#ff8a8a"
        assert "access denied" in window.model.data(idx, Qt.ToolTipRole)
    finally:
        subprocess.run(["icacls", locked, "/remove:d", "*S-1-1-0"], capture_output=True)


# 8
def test_junction_is_not_followed(window, work):
    h.make_tree(work, {"real": {"big.bin": 10_000, "sub": {"s.bin": 5}}})
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(work, "jn"), os.path.join(work, "real")],
                   check=True, capture_output=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(work, "real", "sub", "loop"), work],
                   check=True, capture_output=True)
    root = h.scan(window, work, timeout=60)
    assert root.size == 10_005 and root.nfiles == 2 and root.ndirs == 2
    assert all(d.name not in ("jn", "loop") for d in h.find_dir(root, os.path.join(work, "real", "sub")).dirs + root.dirs)


@pytest.mark.sandbox
def test_symlinks_are_not_followed_or_counted(window, work):
    h.make_tree(work, {"real": {"big.bin": 10_000}, "f.bin": 100})
    os.symlink(os.path.join(work, "real"), os.path.join(work, "dirlink"), target_is_directory=True)
    os.symlink(os.path.join(work, "f.bin"), os.path.join(work, "filelink"))
    os.symlink(os.path.join(work, "nowhere"), os.path.join(work, "dangling"))
    root = h.scan(window, work)
    assert root.size == 10_100 and root.nfiles == 2 and root.ndirs == 1


# 9
def test_path_longer_than_260_is_scanned(window, work):
    deep = work
    while len(deep) < 400:
        deep = os.path.join(deep, "a_fairly_long_folder_name_" + str(len(deep)))
    h.put(os.path.join(deep, "deep_file.bin"), size=4242)
    root = h.scan(window, work)
    assert root.size == 4242 and root.nfiles == 1
    assert len(h.find_dir(root, deep).path) > 260
    assert not any(d.denied for d in _all_dirs(root))


def _all_dirs(root):
    out, st = [], [root]
    while st:
        d = st.pop()
        out.append(d)
        st.extend(d.dirs)
    return out


# 10, 37
ODD = ["trail.", "trail ", "dots...", "日本語のフォルダ", "مجلد عربي", "emoji 😀🎉", "e\u0301 combining",
       "a\ud800lone", "%&;'=,()[]{}!@#$^~`", "C", "C$", "x" * 255]


def test_odd_names_are_scanned_as_themselves(window, work):
    spec = {}
    for i, n in enumerate(ODD):
        spec[n] = {"in.bin": 1000 + i}
        spec[n[:200] + "_file" if len(n) > 200 else n + "f"] = 10 + i
    for n, v in spec.items():
        if isinstance(v, dict):
            h.put(os.path.join(work, n, "in.bin"), size=v["in.bin"])
        else:
            h.put(os.path.join(work, n), size=v)
    root = h.scan(window, work)
    check_against_truth(root, work)
    names = {d.name for d in root.dirs}
    assert set(ODD) <= names, set(ODD) - names
    for n in ODD:
        assert h.find_dir(root, os.path.join(work, n)).size == 1000 + ODD.index(n)  # not its sibling's


def test_reserved_device_names_are_scanned(window, work):
    for n in ("CON", "nul", "aux.txt", "COM1", "lpt9.log"):
        h.put(os.path.join(work, n), size=321)
    root = h.scan(window, work)
    assert root.nfiles == 5 and root.size == 5 * 321


# 11
def test_empty_folder_zero_byte_files_and_20000_files(window, work):
    os.makedirs(os.path.join(work, "empty"))
    h.put(os.path.join(work, "zero.bin"), data=b"")
    many = os.path.join(work, "many")
    os.makedirs(many)
    for i in range(20000):
        with open(os.path.join(many, f"{i:05d}.txt"), "wb") as f:
            f.write(b"x" * (i % 7))
    root = h.scan(window, work)
    check_against_truth(root, work)
    assert h.find_dir(root, many).nfiles == 20000
    e = h.find_dir(root, os.path.join(work, "empty"))
    assert e.size == 0 and e.nfiles == 0
    idx = window.model.index_for(root)
    window.tree.expand(idx)
    h.pump(20)
    assert window.model.rowCount(idx) == 3  # empty, many, zero.bin


# 12
def test_stop_mid_scan_marks_unscanned_and_rescan_works(window, work, monkeypatch):
    for i in range(60):
        h.make_tree(os.path.join(work, f"d{i:02d}"), {f"s{j}": {"f.bin": 10} for j in range(5)})
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.03)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    window.path.setText(work)
    QTest.mouseClick(window.btn_scan, Qt.LeftButton)
    h.wait_until(lambda: window.btn_scan.text() == "Stop", 10)
    h.pump(150)
    QTest.mouseClick(window.btn_scan, Qt.LeftButton)  # Stop
    assert window.status.text() == "Stopping…"
    h.wait_until(lambda: h.scan_finished(window), 60, "stop")
    assert window.status.text().startswith("Stopped")
    root = window.scanner.root
    skipped = [d for d in _all_dirs(root) if d.skipped]
    assert skipped, "nothing was marked as not scanned"
    for d in skipped[:5]:
        h.expand_to(window, d)
        idx = window.model.index_for(d)
        assert "not scanned" in window.model.data(idx, Qt.ToolTipRole)
        assert window.model.data(idx, Qt.ForegroundRole).name() == "#ff8a8a"
    h.pump(400)  # progress() must not overwrite the final message
    assert window.status.text().startswith("Stopped")
    monkeypatch.setattr(bf.Scanner, "_scan", real)
    root = h.scan(window, work)
    check_against_truth(root, work)


# 13
def test_rescan_scan_only_this_folder_browse_typed_path_bad_path(window, work, monkeypatch):
    h.make_tree(work, {"a": {"x.bin": 100, "b": {"y.bin": 7}}, "z.bin": 1})
    root = h.scan(window, work)
    assert root.size == 108
    h.put(os.path.join(work, "new.bin"), size=1000)
    QTest.keyClick(window, Qt.Key_F5)  # F5 rescans
    h.wait_until(lambda: h.scan_finished(window) and window.scanner.root is not root, 30, "F5 rescan")
    assert window.scanner.root.size == 1108

    a = h.find_dir(window.scanner.root, os.path.join(work, "a"))
    h.select_items(window, [a])
    seen = h.run_menu_action(lambda: window.menu(window.tree.mapToGlobal(QPoint(5, 5)), window.selected_items()),
                             "Scan only this folder")
    assert "Scan only this folder" in seen
    h.wait_until(lambda: h.scan_finished(window) and window.scanner.root.path == os.path.join(work, "a"), 30)
    assert window.scanner.root.size == 107 and window.path.text() == os.path.join(work, "a")

    monkeypatch.setattr(bf.QFileDialog, "getExistingDirectory", lambda *a: os.path.join(work, "a", "b").replace("\\", "/"))
    browse = [b for b in window.findChildren(bf.QToolButton) if b.text() == "…"][0]
    QTest.mouseClick(browse, Qt.LeftButton)
    h.wait_until(lambda: h.scan_finished(window) and window.scanner.root.path == os.path.join(work, "a", "b"), 30)
    assert window.scanner.root.size == 7

    QTest.keyClick(window, Qt.Key_L, Qt.ControlModifier)
    h.pump(20)
    assert window.path.hasFocus() and window.path.selectedText() == window.path.text()
    QTest.keyClicks(window.path, f'"{work}"')  # quotes from "Copy as path" are accepted
    QTest.keyClick(window.path, Qt.Key_Return)
    h.wait_until(lambda: h.scan_finished(window) and window.scanner.root.path == work, 30)

    before = window.scanner
    window.path.setText(os.path.join(work, "does not exist"))
    QTest.keyClick(window.path, Qt.Key_Return)
    h.pump(50)
    assert window.status.text().startswith("Not a folder") and window.scanner is before
    window.path.setText(os.path.join(work, "z.bin"))
    QTest.keyClick(window.path, Qt.Key_Return)
    h.pump(50)
    assert window.status.text().startswith("Not a folder") and window.scanner is before


# 14
def test_drive_picker_only_scans_on_a_deliberate_pick(window):
    c = window.drives
    assert c.count() >= 1 and all(c.itemData(i).endswith(":\\") for i in range(c.count()))
    window.drives.setFocus()
    h.pump(10)
    start = c.currentIndex()
    ev = QWheelEvent(QPointF(10, 10), QPointF(c.mapToGlobal(QPoint(10, 10))), QPoint(0, 0), QPoint(0, -120),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(c, ev)
    for k in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_End, Qt.Key_Home):
        QTest.keyClick(c, k)
    QTest.keyClick(c, Qt.Key_Up, Qt.AltModifier)
    QTest.keyClicks(c, "dcz")
    h.pump(50)
    assert window.scanner is None, "a stray wheel/key started a scan"
    assert c.currentIndex() == start
    QTest.keyClick(c, Qt.Key_Down, Qt.AltModifier)  # Alt+Down opens the list
    h.wait_until(lambda: c.view().isVisible(), 5, "drive list")
    c.hidePopup()
    QTest.keyClick(c, Qt.Key_Space)  # Space opens it too
    h.wait_until(lambda: c.view().isVisible(), 5, "drive list (Space)")
    c.hidePopup()
    h.pump(20)
    assert window.scanner is None


@pytest.mark.sandbox
def test_drive_picker_click_scans_that_drive(make_window, work):
    h.make_tree(work, {"p.bin": 4321, "q": {"r.bin": 1}})
    subprocess.run(["subst", "Q:", work], check=True)
    try:
        w = make_window()
        c = w.drives
        i = c.findData("Q:\\")
        assert i >= 0, [c.itemData(j) for j in range(c.count())]
        c.showPopup()
        h.wait_until(lambda: c.view().isVisible(), 5)
        v = c.view()
        r = v.visualRect(v.model().index(i, 0))
        QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.NoModifier, r.center())
        h.wait_until(lambda: h.scan_finished(w), 30, "drive scan")
        assert w.scanner.root.path == "Q:\\" and w.scanner.root.size == 4322
        assert c.currentData() == "Q:\\"
    finally:
        subprocess.run(["subst", "Q:", "/d"])


# 15
@pytest.mark.sandbox
def test_network_share_and_offline_share(make_window, work):
    h.make_tree(work, {"n.bin": 5000, "s": {"t.bin": 6}})
    r = subprocess.run(["net", "share", f"bfshare={work}", "/grant:everyone,full"], capture_output=True, text=True)
    if r.returncode:
        pytest.skip("no SMB server in this Sandbox: " + r.stdout + r.stderr)
    try:
        w = make_window()
        if not h.reachable(r"\\localhost\bfshare"):
            pytest.skip(r"\\localhost isn't reachable (run-tests.ps1 -Networking)")
        root = h.scan(w, r"\\localhost\bfshare", timeout=60)
        assert root.size == 5006 and root.nfiles == 2
        assert 0 <= w.model.pct(root) <= 1
        assert w.drives.currentIndex() == -1  # a UNC path isn't a listed drive
        t0 = time.time()
        w.path.setText(r"\\10.255.255.1\gone")
        w.scan()
        assert time.time() - t0 < 6, "an offline share froze the window"
        assert "not responding" in w.status.text() or "Not a folder" in w.status.text()
    finally:
        subprocess.run(["net", "share", "bfshare", "/delete", "/y"], capture_output=True)


# 32
def test_files_changing_during_the_scan(window, work):
    for i in range(30):
        h.make_tree(os.path.join(work, f"d{i}"), {f"f{j}.bin": 100 for j in range(30)})
    stop = threading.Event()

    def churn():
        rnd = random.Random(1)
        n = 0
        while not stop.is_set():
            d = os.path.join(work, f"d{rnd.randrange(30)}")
            try:
                op = rnd.random()
                if op < 0.3:
                    h.put(os.path.join(d, f"new{n}.bin"), size=rnd.randint(0, 5000))
                elif op < 0.5:
                    os.remove(os.path.join(d, rnd.choice(os.listdir(d))))
                elif op < 0.7:
                    src = os.path.join(d, rnd.choice(os.listdir(d)))
                    os.rename(src, src + ".r")
                elif op < 0.85:
                    os.makedirs(os.path.join(d, f"nd{n}", "x"), exist_ok=True)
                else:
                    with open(os.path.join(d, rnd.choice(os.listdir(d))), "ab") as f:
                        f.write(b"y" * 1000)
            except (OSError, IndexError, ValueError):
                pass
            n += 1
    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(5):
            h.scan(window, work)
            window.live_refresh()
    finally:
        stop.set()
        t.join()
    root = h.scan(window, work)
    check_against_truth(root, work)


# 34
@pytest.mark.sandbox
def test_drive_disappears_mid_scan(window, work, monkeypatch):
    for i in range(40):
        h.make_tree(os.path.join(work, f"d{i}"), {f"s{j}": {"f.bin": 10} for j in range(4)})
    subprocess.run(["subst", "X:", work], check=True)
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.15)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    try:
        window.path.setText("X:\\")
        window.scan()
        h.pump(200)
        assert not window.scanner.t1, "the scan finished before the drive could vanish"
        subprocess.run(["subst", "X:", "/d"], check=True)
        h.wait_until(lambda: h.scan_finished(window), 120, "scan of a vanished drive")
        assert window.status.text().startswith("Scanned X:\\")
        root = window.scanner.root
        assert any(d.denied for d in _all_dirs(root)), "lost folders should count as unreadable"
        window.fill_drives()
        h.pump(50)
        assert window.drives.findData("X:\\") == -1
    finally:
        subprocess.run(["subst", "X:", "/d"], capture_output=True)


# 35
def test_300_levels_deep(window, work):
    d = work
    for i in range(300):
        d = os.path.join(d, f"l{i}")
    h.put(os.path.join(d, "bottom.bin"), size=300)
    root = h.scan(window, work)
    assert root.ndirs == 300 and root.size == 300
    bottom = h.find_dir(root, d)
    window.select_in_tree(bottom)  # expanding 300 levels in the tree view
    h.pump(50)
    assert window.tree.currentIndex().internalPointer() is bottom
    window.set_focus(bottom)
    window.map_up()
    assert window.focus is bottom.parent


@pytest.mark.big
def test_200k_files_in_one_folder_and_a_million_total(window, work, record_property):
    import psutil
    one = os.path.join(work, "one")
    os.makedirs(one)
    for i in range(200_000):
        open(os.path.join(one, f"{i:06d}"), "wb").close()
    for a in range(80):
        for b in range(100):
            d = os.path.join(work, "spread", f"a{a}", f"b{b}")
            os.makedirs(d)
            for c in range(100):
                open(os.path.join(d, f"{c}.txt"), "wb").close()
    proc = psutil.Process()
    m0 = proc.memory_info().rss
    t0 = time.time()
    root = h.scan(window, work, timeout=1800)
    secs = time.time() - t0
    m1 = proc.memory_info().rss
    assert root.nfiles == 1_000_000 and root.ndirs == 1 + 1 + 80 + 8000
    for tab in (1, 2, 0):
        window.tabs.setCurrentIndex(tab)
        h.pump(100)
    assert window.top.rowCount() == 100
    record_property("million_files_scan_seconds", round(secs, 1))
    record_property("million_files_rss_mb", round((m1 - m0) / 2**20))
    print(f"\n1M files: {secs:.1f}s, +{(m1 - m0) / 2**20:.0f} MB")


# 36
def test_hard_links_streams_attributes_compressed_sparse(window, work):
    h.make_tree(work, {"orig.bin": 10_000, "plain": {"p.bin": 1}})
    os.link(os.path.join(work, "orig.bin"), os.path.join(work, "plain", "hardlink.bin"))
    with open(os.path.join(work, "plain", "p.bin:hidden_stream"), "wb") as f:  # alternate data stream
        f.write(b"z" * 50_000)
    h.put(os.path.join(work, "ro.bin"), size=11)
    subprocess.run(["attrib", "+r", os.path.join(work, "ro.bin")], check=True)
    h.put(os.path.join(work, "hid.bin"), size=13)
    subprocess.run(["attrib", "+h", "+s", os.path.join(work, "hid.bin")], check=True)
    h.put(os.path.join(work, "comp.txt"), data=b"a" * 200_000)
    subprocess.run(["compact", "/c", os.path.join(work, "comp.txt")], capture_output=True)
    h.sparse(os.path.join(work, "sparse.img"), 1 << 33)
    root = h.scan(window, work)
    # Decided rules (2026-11-16): sizes are file sizes as Explorer shows them, so a hard link is counted
    # once per name, alternate data streams aren't counted, and compressed / sparse files count their full size.
    assert root.size == 10_000 * 2 + 1 + 11 + 13 + 200_000 + (1 << 33)
    assert root.nfiles == 7


# 45
@pytest.mark.parametrize("seed", range(100))
def test_fuzz_random_trees(window, work, seed):
    h.random_tree(work, seed)
    root = h.scan(window, work)
    check_against_truth(root, work)
    for tab in (1, 2):
        window.tabs.setCurrentIndex(tab)
        h.pump(5)
    files = [f for d in _all_dirs(root) for f in d.files]
    if files:
        assert window.top.rowCount() == min(100, len(files))
        assert window.top.item(0, 1).key == max(f.size for f in files)
        assert sum(window.types.item(r, 4).key for r in range(window.types.rowCount())) == len(files)
    window.tabs.setCurrentIndex(0)
