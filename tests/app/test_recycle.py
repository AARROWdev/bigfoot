"""D. Recycle Bin (plan tests 22-28) plus 33 and 43. These use the Sandbox's real Recycle Bin: every recycled
item is found in C:\\$Recycle.Bin, restored the way Explorer restores it, and the whole test tree is hashed
before and after to prove nothing else was touched."""
import os
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

import bigfoot as bf
import helpers as h

pytestmark = pytest.mark.sandbox

SPEC = {
    "keep": {"k1.bin": 1500, "k2.txt": 300, "inner": {"i.bin": 700}},
    "target": {"t1.bin": 2000, "t2.bin": 900, "sub": {"s1.bin": 400, "s2.bin": 50}},
    "loose.mp4": 12345, "other.zip": 999,
}


@pytest.fixture
def tree(window, work):
    h.empty_bin()
    h.make_tree(work, SPEC)
    before = h.tree_hash(work)
    root = h.scan(window, work)
    return window, root, work, before


def press_delete(w):
    w.tree.setFocus()
    QTest.keyClick(w.tree, Qt.Key_Delete)
    h.pump(100)


def without(before, *rels):
    return {k: v for k, v in before.items() if not any(k == r or k.startswith(r + os.sep) for r in rels)}


def assert_recycled_and_restorable(work, before, *rels):
    """The items are gone from disk, in the real Recycle Bin, and restore to exactly what they were."""
    assert h.tree_hash(work) == without(before, *rels), "something other than the selection changed"
    for r in rels:
        assert not os.path.exists(h.lp(os.path.join(work, r)))
        assert h.in_bin(os.path.join(work, r)), f"{r} is not in the Recycle Bin"
    for r in rels:
        h.restore(os.path.join(work, r))
    assert h.tree_hash(work) == before, "restored items differ from the originals"


# 22
def test_recycle_file_folder_and_multi_selection(tree, dialogs):
    w, root, work, before = tree
    loose = h.find_dir(root, os.path.join(work, "loose.mp4"))
    h.select_items(w, [loose])
    press_delete(w)
    assert "loose.mp4" in dialogs.texts[-1] and "12.1 KB" in dialogs.texts[-1]
    assert w.status.text() == "Sent 1 to the Recycle Bin"
    assert (root.size, root.nfiles) == h.truth(work)[work][:2] == (root.size, 8)
    assert_recycled_and_restorable(work, before, "loose.mp4")

    root = h.scan(w, work)
    target = h.find_dir(root, os.path.join(work, "target"))
    h.select_items(w, [target])
    press_delete(w)
    assert "3.3 KB" in dialogs.texts[-1]
    assert_recycled_and_restorable(work, before, "target")

    root = h.scan(w, work)
    picks = [h.find_dir(root, os.path.join(work, p)) for p in ("other.zip", "keep\\k2.txt", "target\\sub")]
    h.select_items(w, picks)
    press_delete(w)
    assert dialogs.texts[-1].startswith("Send 3 items")
    assert w.status.text() == "Sent 3 to the Recycle Bin"
    assert_recycled_and_restorable(work, before, "other.zip", "keep\\k2.txt", "target\\sub")


# 23
def test_only_the_focused_views_selection_is_recycled(tree, dialogs):
    w, root, work, before = tree
    target = h.find_dir(root, os.path.join(work, "target"))
    h.select_items(w, [target])  # tree selection: target
    for tab, table in ((1, w.types), (2, w.top)):
        w.tabs.setCurrentIndex(tab)
        h.pump(30)
        QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier,
                         table.visualItemRect(table.item(0, 0)).center())
        table.setFocus()
        QTest.keyClick(table, Qt.Key_Delete)
        h.pump(100)
        assert not dialogs.texts, "Delete in a table must not recycle the tree's selection"
        assert h.tree_hash(work) == before
    # the Top 100 menu recycles exactly the rows picked there
    w.tabs.setCurrentIndex(2)
    h.pump(30)
    t = w.top
    names = [t.item(r, 0).text() for r in range(t.rowCount())]
    r_t2 = names.index("t2.bin")
    QTest.mouseClick(t.viewport(), Qt.LeftButton, Qt.NoModifier, t.visualItemRect(t.item(r_t2, 0)).center())
    h.run_menu_action(lambda: w.top_menu(t.visualItemRect(t.item(r_t2, 0)).center()), "Send to Recycle Bin")
    assert dialogs.texts and "t2.bin" in dialogs.texts[-1] and "Send 1 item" in dialogs.texts[-1]
    assert_recycled_and_restorable(work, before, "target\\t2.bin")


# 24
def test_folder_plus_its_own_subfolder(tree, dialogs):
    w, root, work, before = tree
    target = h.find_dir(root, os.path.join(work, "target"))
    sub = h.find_dir(root, os.path.join(work, "target", "sub"))
    s1 = h.find_dir(root, os.path.join(work, "target", "sub", "s1.bin"))
    h.select_items(w, [target, sub, s1])
    press_delete(w)
    assert dialogs.texts[-1].startswith("Send 1 item (3.3 KB)"), dialogs.texts[-1]
    assert w.status.text() == "Sent 1 to the Recycle Bin", w.status.text()
    assert len(h.in_bin(os.path.join(work, "target"))) == 1
    # clicking around afterwards doesn't touch freed items (this crashed before F1)
    import gc
    for _ in range(3):
        gc.collect()
        h.click_index(w.tree, w.model.index_for(root))
        w.tabs.setCurrentIndex(0)
        QTest.mouseClick(w.map, Qt.LeftButton, Qt.NoModifier, QPoint(50, 60))
        h.pump(50)
    assert_recycled_and_restorable(work, before, "target")


# 25
def test_deselected_rows_and_cancel(tree, dialogs):
    w, root, work, before = tree
    a = h.find_dir(root, os.path.join(work, "other.zip"))
    b = h.find_dir(root, os.path.join(work, "loose.mp4"))
    h.select_items(w, [a, b])
    h.click_index(w.tree, w.model.index_for(b), Qt.ControlModifier)  # Ctrl+click: deselect b (it stays current)
    assert w.selected_items() == [a]
    w.live_refresh()
    w.rebuild()  # a rebuild must not bring the deselected row back
    assert w.selected_items() == [a]
    dialogs.answer = "no"
    press_delete(w)
    assert dialogs.texts and "other.zip" in dialogs.texts[-1] and "loose.mp4" not in dialogs.texts[-1]
    assert h.tree_hash(work) == before and not h.recycle_bin_entries()
    dialogs.answer = "yes"
    w.tree.clearSelection()
    n = len(dialogs.texts)
    press_delete(w)
    assert len(dialogs.texts) == n, "nothing selected must not offer to recycle the current row"
    assert h.tree_hash(work) == before


# 26
def test_refused_names_and_unscanned_folders(window, work, dialogs, monkeypatch):
    h.empty_bin()
    for n in ("trail.", "trail ", "a\ud800lone.bin", "CON", "nul.txt"):
        h.put(os.path.join(work, n), size=10)
    h.put(os.path.join(work, "trail"), size=99)  # the sibling Windows would act on instead
    h.put(os.path.join(work, "dotdir.", "inside.bin"), size=5)
    before = h.tree_hash(work)
    root = h.scan(window, work)
    for n in ("trail.", "trail ", "a\ud800lone.bin", "CON", "nul.txt", "dotdir.\\inside.bin"):
        it = h.find_dir(root, os.path.join(work, n))
        h.select_items(window, [it])
        press_delete(window)
        assert "Can't recycle" in window.status.text() or "Can't" in window.status.text(), (n, window.status.text())
        assert h.tree_hash(work) == before, f"recycling {n!r} touched the disk"
    assert not h.recycle_bin_entries()
    # folders left unscanned by Stop: the dialog says the size is unknown
    for i in range(30):
        h.make_tree(os.path.join(work, "big", f"d{i}"), {f"s{j}": {"f.bin": 10} for j in range(4)})
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.03)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    window.path.setText(work)
    window.scan()
    h.pump(250)
    window.scan_or_stop()
    h.wait_until(lambda: h.scan_finished(window), 60)
    big = h.find_dir(window.scanner.root, os.path.join(work, "big"))
    assert window._has_skipped(big)
    dialogs.answer = "no"
    h.select_items(window, [big])
    press_delete(window)
    assert "part of it was not scanned" in dialogs.texts[-1]


# 27
def test_locked_file_is_reported_nothing_else_lost(tree, dialogs):
    w, root, work, before = tree
    locked = os.path.join(work, "target", "t1.bin")
    holder = subprocess.Popen([sys.executable, "-c",
                               f"f=open({locked!r},'rb'); import time; print('open', flush=True); time.sleep(120)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "open"
        t1 = h.find_dir(root, locked)
        other = h.find_dir(root, os.path.join(work, "other.zip"))
        h.select_items(w, [t1, other])
        press_delete(w)
        assert "couldn't move" in w.status.text() and "t1.bin" in w.status.text(), w.status.text()
        assert w.status.text().startswith("Sent 1 to the Recycle Bin")
        assert os.path.exists(locked)
        assert w.model.listed(t1), "a file that couldn't be recycled must stay in the tree"
        # a folder holding the locked file: nothing inside it may be lost
        root = h.scan(w, work)
        h.select_items(w, [h.find_dir(root, os.path.join(work, "target"))])
        press_delete(w)
        on_disk = h.tree_hash(work)
        binned = {os.path.relpath(e[0], work) for e in h.recycle_bin_entries()}
        for rel in before:
            if rel.startswith("target"):
                assert rel in on_disk or any(rel == b or rel.startswith(b + os.sep) for b in binned), rel
    finally:
        holder.kill()
        holder.wait()


# 28
def test_totals_update_and_50_recycle_click_cycles(window, work, dialogs):
    import gc
    h.empty_bin()
    for i in range(60):
        h.put(os.path.join(work, f"g{i % 6}", f"f{i:02d}.bin"), size=100 + i)
    root = h.scan(window, work)
    for n in range(50):
        g = h.find_dir(root, os.path.join(work, f"g{n % 6}"))
        if not g.files:
            continue
        f = g.files[0]
        h.select_items(window, [f])
        press_delete(window)
        assert not os.path.exists(f.path)
        assert (root.size, root.nfiles) == h.truth(work)[work][:2], f"cycle {n}: totals not updated"
        gc.collect()
        h.click_index(window.tree, window.model.index_for(g))
        window.tabs.setCurrentIndex(n % 3)
        h.pump(20)
        if n % 3 == 0:
            QTest.mouseClick(window.map, Qt.LeftButton, Qt.NoModifier, QPoint(60 + n, 80))
    assert len(h.recycle_bin_entries()) == 50


# 33
def test_folder_changed_behind_the_apps_back(tree, dialogs, no_launch):
    w, root, work, before = tree
    keep = h.find_dir(root, os.path.join(work, "keep"))
    k1 = h.find_dir(root, os.path.join(work, "keep", "k1.bin"))
    os.rename(os.path.join(work, "keep"), os.path.join(work, "kept"))
    h.click_index(w.tree, w.model.index_for(keep))
    w.open_item(k1)
    w.reveal(k1)
    h.pump(50)
    w.tree_double(w.model.index_for(k1))
    h.select_items(w, [keep])
    press_delete(w)
    assert "couldn't move" in w.status.text(), w.status.text()
    assert os.path.exists(os.path.join(work, "kept", "k1.bin"))
    assert not h.recycle_bin_entries(), "recycled something that wasn't the selection"


def test_open_a_file_that_was_deleted_shows_a_message(window, work, monkeypatch):
    h.make_tree(work, {"gone.txt": 5})
    root = h.scan(window, work)
    os.remove(os.path.join(work, "gone.txt"))
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: errors.append(a))
    window.open_item(root.files[0])
    h.pump(20)
    assert not errors, f"opening a missing file raised {errors[0][1]!r}"
    assert "gone.txt" in window.status.text()


# 43
def test_killing_bigfoot_mid_recycle_loses_nothing(work):
    h.empty_bin()
    for i in range(400):
        h.put(os.path.join(work, f"f{i:03d}.bin"), size=2000)
    before = h.tree_hash(work)
    code = f"""
import os, sys
sys.path[:0] = [{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))!r}, {os.path.dirname(os.path.abspath(__file__))!r}]
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox
import bigfoot as bf, helpers as h
bf.QSettings = lambda *a, **k: QSettings(r"{work}.ini", QSettings.IniFormat)
bf.QMessageBox.question = lambda *a, **k: QMessageBox.Yes
app = QApplication([]); w = bf.Window(); w.show()
root = h.scan(w, {work!r})
print("go", flush=True)
w.trash(list(root.files))
print("done", flush=True)
"""
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert p.stdout.readline().strip() == "go"
    deadline = time.time() + 60
    while len(h.recycle_bin_entries()) < 100 and time.time() < deadline:
        time.sleep(0.05)
    p.kill()
    p.wait()
    binned = {os.path.relpath(e[0], work): e for e in h.recycle_bin_entries()}
    assert 0 < len(binned) < 400, len(binned)
    now = h.tree_hash(work)
    for rel in before:
        assert (rel in now) != (rel in binned), f"{rel} lost or duplicated"
    for rel in binned:
        h.restore(os.path.join(work, rel))
    assert h.tree_hash(work) == before
