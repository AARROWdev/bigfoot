"""The Boxes | List switch: the list of a folder's contents next to the treemap boxes."""
import os
import time

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

import bigfoot as bf
import helpers as h
from test_scan import _recycle_stubs

SPEC = {"z.bin": 5000, "a.bin": 3000, "mid": {"m1.bin": 3000, "m2.bin": 1000}, "tiny": {"t.bin": 10}}


def names(w):
    return [x.name for x in w.list_model.items]


def cell(w, row, col):
    return w.list_model.data(w.list_model.index(row, col))


def row_of(w, name):
    return names(w).index(name)


def to_list(w):
    QTest.mouseClick(w.view_list, Qt.LeftButton)
    h.pump(30)
    assert w.view_stack.currentIndex() == 1


def click_header(w, col):
    lh = w.listv.horizontalHeader()
    QTest.mouseClick(lh.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(lh.sectionViewportPosition(col) + 8, 5))
    h.pump(30)


@pytest.fixture
def listed(window, work):
    h.make_tree(work, SPEC)
    root = h.scan(window, work)
    to_list(window)
    return window, root


def test_boxes_is_the_default_and_the_switch_changes_the_view(window):
    assert window.view_boxes.isChecked() and window.view_stack.currentIndex() == 0
    assert window.map.isVisibleTo(window) and not window.listv.isVisibleTo(window)
    QTest.mouseClick(window.view_list, Qt.LeftButton)
    h.pump(30)
    assert window.listv.isVisibleTo(window) and not window.map.isVisibleTo(window)
    QTest.mouseClick(window.view_boxes, Qt.LeftButton)
    h.pump(30)
    assert window.map.isVisibleTo(window) and not window.listv.isVisibleTo(window)


def test_both_views_show_the_same_sizes_and_percentages(listed):
    w, root = listed
    total = bf.folder_total(root)
    assert total == root.size
    kids = [*root.dirs, *root.files]
    assert sorted(names(w)) == sorted(k.name for k in kids)
    for r, it in enumerate(w.list_model.items):
        assert cell(w, r, bf.L_SIZE) == bf.human(it.size)
        assert cell(w, r, bf.L_PCT) == f"{it.size / total * 100:.1f} %"
    # the boxes: each one's share of the area is that item's share of the folder, and it names the same size
    w.map.render_map()
    boxes = [(r, it) for r, it, depth in w.map.hits if depth == 1]
    assert {it.name for _, it in boxes} == {k.name for k in kids if k.size > 0}
    area = sum(r.width() * r.height() for r, _ in boxes)
    for r, it in boxes:
        assert abs(r.width() * r.height() / area - it.size / total) < 0.02, it.name
    w.map.mouseMoveEvent  # (the tooltip text is built from the same human(size))
    for _, it in boxes:
        assert bf.human(it.size) in f"{it.path}\n{bf.human(it.size)} on disk"
    assert cell(w, row_of(w, "z.bin"), bf.L_SIZE) == bf.human(5000)


def test_the_list_sizes_are_on_disk_not_length(window, work):
    h.make_tree(work, {"plain.bin": 4000})
    h.sparse(os.path.join(work, "holes.bin"), 50 * 1024 * 1024)
    root = h.scan(window, work)
    to_list(window)
    r = row_of(window, "holes.bin")
    assert cell(window, r, bf.L_SIZE) == bf.human(h.on_disk(os.path.join(work, "holes.bin")))
    assert cell(window, r, bf.L_SIZE) != bf.human(50 * 1024 * 1024)
    assert root.size == sum(x.size for x in window.list_model.items)


def test_columns_and_icons(listed):
    w, root = listed
    assert [w.list_model.headerData(c, Qt.Horizontal) for c in range(5)] == [
        "Name", "Size on disk", "% of folder", "Type", "Modified"]
    r = row_of(w, "mid")
    assert cell(w, r, bf.L_TYPE) == "Folder"
    assert cell(w, row_of(w, "z.bin"), bf.L_TYPE) == "BIN file"
    assert cell(w, r, bf.L_MOD) == bf.fdate(next(d for d in root.dirs if d.name == "mid").mtime)
    for n in ("mid", "z.bin"):
        assert not w.list_model.data(w.list_model.index(row_of(w, n), 0), Qt.DecorationRole).isNull()


def test_default_sort_is_size_descending_and_folders_are_not_forced_first(listed):
    w, _ = listed
    assert names(w) == ["z.bin", "mid", "a.bin", "tiny"]
    assert w.listv.horizontalHeader().sortIndicatorSection() == bf.L_SIZE


def test_clicking_a_header_sorts_by_that_column(listed):
    w, _ = listed
    lh = w.listv.horizontalHeader()
    click_header(w, bf.L_NAME)
    asc = lh.sortIndicatorOrder() == Qt.AscendingOrder
    want = sorted(names(w), key=str.lower, reverse=not asc)
    assert names(w) == want and lh.sortIndicatorSection() == bf.L_NAME
    assert set(names(w)[:2]) != {"mid", "tiny"}  # folders sit among the files, not above them
    click_header(w, bf.L_NAME)  # the same header again flips it
    assert names(w) == list(reversed(want))
    click_header(w, bf.L_TYPE)
    kinds = [bf.kind_of(x).lower() for x in w.list_model.items]
    assert kinds == sorted(kinds, reverse=lh.sortIndicatorOrder() == Qt.DescendingOrder)


def test_sort_is_kept_while_navigating(listed):
    w, root = listed
    lh = w.listv.horizontalHeader()
    lh.setSortIndicator(bf.L_NAME, Qt.AscendingOrder)
    h.pump(30)
    assert names(w) == ["a.bin", "mid", "tiny", "z.bin"]
    mid = next(d for d in root.dirs if d.name == "mid")
    w.select_in_tree(mid)
    w.set_focus(mid)
    assert names(w) == ["m1.bin", "m2.bin"]
    w.map_up()
    assert w.focus is root
    assert names(w) == ["a.bin", "mid", "tiny", "z.bin"]
    assert lh.sortIndicatorSection() == bf.L_NAME and lh.sortIndicatorOrder() == Qt.AscendingOrder


def test_double_click_a_folder_opens_it_and_selects_it_in_the_tree(listed):
    w, root = listed
    mid = next(d for d in root.dirs if d.name == "mid")
    h.click_index(w.listv, w.list_model.index(row_of(w, "mid"), 0), double=True)
    assert w.focus is mid and w.list_model.dir is mid
    assert names(w) == ["m1.bin", "m2.bin"] or sorted(names(w)) == ["m1.bin", "m2.bin"]
    assert w.tree.currentIndex().internalPointer() is mid
    assert w.selected_items() == [mid]
    assert w.crumb.toolTip() == mid.path


def test_enter_opens_a_folder_too(listed):
    w, root = listed
    mid = next(d for d in root.dirs if d.name == "mid")
    w.listv.setFocus()
    w.listv.setCurrentIndex(w.list_model.index(row_of(w, "mid"), 0))
    QTest.keyClick(w.listv, Qt.Key_Return)
    h.pump(30)
    assert w.focus is mid


def test_double_click_a_file_opens_it_with_its_default_app(listed, no_launch):
    w, root = listed
    focus = w.focus
    h.click_index(w.listv, w.list_model.index(row_of(w, "z.bin"), 0), double=True)
    assert no_launch == [("open", os.path.join(root.path, "z.bin"))]
    assert w.focus is focus  # a file doesn't move the list


def test_a_tree_selection_shows_that_folder_in_the_list(listed):
    w, root = listed
    mid = next(d for d in root.dirs if d.name == "mid")
    w.tree.setCurrentIndex(w.model.index_for(mid))
    h.wait_until(lambda: w.list_model.dir is mid, 5, "the list to follow the tree")
    assert sorted(names(w)) == ["m1.bin", "m2.bin"]
    # a file selected in the tree shows its folder
    f = mid.files[0]
    w.tree.setCurrentIndex(w.model.index_for(f))
    h.pump(300)
    assert w.list_model.dir is mid


def _menu_texts(open_menu):
    return h.run_menu_action(open_menu, "\0never")


def test_right_click_gives_the_same_menu_as_the_tree(listed):
    w, root = listed
    mid = next(d for d in root.dirs if d.name == "mid")
    idx = w.list_model.index(row_of(w, "mid"), 0)
    h.click_index(w.listv, idx)
    pos = w.listv.visualRect(idx).center()
    from_list = _menu_texts(lambda: w.listv.customContextMenuRequested.emit(pos))
    h.expand_to(w, mid)
    tidx = w.model.index_for(mid)
    h.click_index(w.tree, tidx)
    tpos = w.tree.visualRect(tidx).center()
    from_tree = _menu_texts(lambda: w.tree.customContextMenuRequested.emit(tpos))
    assert from_list and from_list == from_tree
    assert from_list[0] == "Open" and "Show in Explorer" in from_list
    assert any(t.startswith("Send to Recycle Bin") for t in from_list)


def test_the_menu_actions_work_from_the_list(listed, no_launch):
    w, root = listed
    idx = w.list_model.index(row_of(w, "z.bin"), 0)
    h.click_index(w.listv, idx)
    pos = w.listv.visualRect(idx).center()
    h.run_menu_action(lambda: w.listv.customContextMenuRequested.emit(pos), "Show in Explorer")
    assert no_launch and no_launch[-1][0] == "explorer" and "z.bin" in no_launch[-1][1]
    h.run_menu_action(lambda: w.listv.customContextMenuRequested.emit(pos), "Open")
    assert ("open", os.path.join(root.path, "z.bin")) in no_launch
    h.run_menu_action(lambda: w.listv.customContextMenuRequested.emit(pos), "Copy path")
    assert bf.QGuiApplication.clipboard().text() == os.path.join(root.path, "z.bin")


def test_recycling_from_the_list_asks_the_same_question_and_leaves_no_stale_row(listed, monkeypatch):
    w, root = listed
    asked = _recycle_stubs(monkeypatch)
    victim = os.path.join(root.path, "z.bin")
    idx = w.list_model.index(row_of(w, "z.bin"), 0)
    h.click_index(w.listv, idx)
    pos = w.listv.visualRect(idx).center()
    seen = h.run_menu_action(lambda: w.listv.customContextMenuRequested.emit(pos), "Send to Recycle Bin")
    assert any(t.startswith("Send to Recycle Bin (4.9 KB)") for t in seen)
    assert len(asked) == 1 and "z.bin" in asked[0]  # the same confirmation as the tree's
    assert not os.path.exists(victim)
    assert "z.bin" not in names(w)  # no stale row
    left = [3000, 4000, 10]
    assert sorted(x.size for x in w.list_model.items) == sorted(left)
    for r, it in enumerate(w.list_model.items):
        assert cell(w, r, bf.L_PCT) == f"{it.size / root.size * 100:.1f} %"  # shares of what is left
    assert root.size == 7010


def test_delete_key_in_the_list_recycles_after_asking(listed, monkeypatch):
    w, root = listed
    asked = _recycle_stubs(monkeypatch)
    h.click_index(w.listv, w.list_model.index(row_of(w, "a.bin"), 0))
    w.listv.setFocus()
    QTest.keyClick(w.listv, Qt.Key_Delete)
    h.pump(100)
    assert len(asked) == 1 and "a.bin" in asked[0]
    assert "a.bin" not in names(w)


def test_recycling_a_folder_that_is_open_in_the_list_moves_the_list_up(listed, monkeypatch):
    w, root = listed
    asked = _recycle_stubs(monkeypatch)
    mid = next(d for d in root.dirs if d.name == "mid")
    w.select_in_tree(mid)
    w.set_focus(mid)
    w.trash([mid])
    assert w.focus is root and w.list_model.dir is root
    assert "mid" not in names(w) and len(asked) == 1


def test_copy_from_the_list_copies_full_paths(listed):
    w, root = listed
    h.click_index(w.listv, w.list_model.index(row_of(w, "a.bin"), 0))
    h.click_index(w.listv, w.list_model.index(row_of(w, "tiny"), 0), Qt.ControlModifier)
    w.listv.setFocus()
    QTest.keyClick(w.listv, Qt.Key_C, Qt.ControlModifier)
    assert sorted(bf.QGuiApplication.clipboard().text().split("\n")) == sorted(
        [os.path.join(root.path, "a.bin"), os.path.join(root.path, "tiny")])


def test_the_chosen_view_is_remembered_across_launches(make_window):
    w1 = make_window()
    assert w1.view_boxes.isChecked()
    QTest.mouseClick(w1.view_list, Qt.LeftButton)
    h.pump(30)
    w1.close()
    w2 = make_window()
    assert w2.view_list.isChecked() and w2.view_stack.currentIndex() == 1
    QTest.mouseClick(w2.view_boxes, Qt.LeftButton)
    h.pump(30)
    w2.close()
    w3 = make_window()
    assert w3.view_boxes.isChecked() and w3.view_stack.currentIndex() == 0


def test_a_damaged_view_setting_falls_back_to_boxes(make_window, ini_settings):
    s = bf.QSettings()
    s.setValue("view", "sideways")
    s.sync()
    w = make_window()
    assert w.view_boxes.isChecked()


def test_a_new_scan_leaves_no_rows_of_the_old_one(window, work):
    a, b = os.path.join(work, "A"), os.path.join(work, "B")
    h.make_tree(a, {"only_in_a.bin": 1000})
    h.make_tree(b, {"only_in_b.bin": 2000})
    h.scan(window, a)
    to_list(window)
    assert names(window) == ["only_in_a.bin"]
    h.scan(window, b)
    assert names(window) == ["only_in_b.bin"]


def test_a_rescan_shows_files_added_since(window, work):
    h.make_tree(work, {"one.bin": 1000})
    h.scan(window, work)
    to_list(window)
    assert names(window) == ["one.bin"]
    h.put(os.path.join(work, "two.bin"), 5000)
    h.scan(window, work)
    assert names(window) == ["two.bin", "one.bin"]
    assert cell(window, 0, bf.L_PCT) == f"{5000 / 6000 * 100:.1f} %"


def test_the_list_follows_a_live_scan_without_flicker(window, work, monkeypatch):
    for i in range(30):
        h.make_tree(os.path.join(work, f"d{i:02d}"), {f"s{j}": {"f.bin": 1000 + i} for j in range(4)})
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.01)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    window.live.setInterval(100)
    to_list(window)
    resets, ticks = [], []
    window.list_model.modelReset.connect(lambda: resets.append(1))
    window.live.timeout.connect(lambda: ticks.append(1))
    window.path.setText(work)
    window.scan()
    h.pump(300)
    assert window.list_model.rowCount() > 0, "the list should fill in as it scans"
    picked = None
    if window.list_model.items:
        picked = window.list_model.items[0]
        h.click_index(window.listv, window.list_model.index(0, 0))
    while not h.scan_finished(window):
        h.pump(120)
        if picked is not None:
            assert picked in window.list_items(), "selection lost in a live refresh"
    h.pump(300)
    assert len(resets) <= len(ticks) + 3, f"{len(resets)} redraws for {len(ticks)} live refreshes"
    root = window.scanner.root
    assert len(window.list_model.items) == 30 and sum(x.size for x in window.list_model.items) == root.size
    for r, it in enumerate(window.list_model.items):
        assert cell(window, r, bf.L_SIZE) == bf.human(it.size)
    # nothing changed any more: refreshing again redraws nothing
    before = len(resets)
    for _ in range(3):
        window.rebuild()
        window.refresh_list()
    h.pump(50)
    assert len(resets) == before


def test_a_refresh_keeps_selection_and_scroll(window, work):
    h.make_tree(work, {f"f{i:03d}.bin": 1000 + i for i in range(200)})
    root = h.scan(window, work)
    to_list(window)
    window.resize(1300, 500)
    h.pump(50)
    sb = window.listv.verticalScrollBar()
    assert sb.maximum() > 0
    h.click_index(window.listv, window.list_model.index(150, 0))
    chosen = window.list_items()[0]
    sb.setValue(sb.maximum() // 2)
    at = sb.value()
    chosen.parent.files[0].size += 1  # something changed: a real reload
    window.rebuild()
    h.pump(30)
    assert window.list_items() == [chosen]
    assert sb.value() == at


def test_an_empty_folder_shows_an_empty_list(window, work):
    os.makedirs(os.path.join(work, "empty"))
    h.make_tree(work, {"x.bin": 100})
    root = h.scan(window, work)
    to_list(window)
    e = next(d for d in root.dirs if d.name == "empty")
    window.select_in_tree(e)
    window.set_focus(e)
    assert window.list_model.rowCount() == 0
    assert window.list_model.total == 0
