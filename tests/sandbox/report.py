"""Turns one or more Sandbox result folders (from run-tests.ps1) into a markdown table, one row per plan test.

    python tests/sandbox/report.py <results folder> [<results folder> ...]
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

PLAN = {  # plan test number -> (what, how to find its results)
    1: ("Silent install: files, Start Menu, Add/Remove Programs, launches", ["A1 "]),
    2: ("Wizard install with the desktop shortcut ticked", ["A2 "]),
    3: ("Upgrade from 1.1, upgrade while Bigfoot runs", ["A3 "]),
    4: ("Uninstall: clean, settings kept, refused while running", ["A4 "]),
    5: ("Portable zip runs, nothing in Program Files", ["A5 "]),
    6: ("Totals match ground truth", ["test_totals_match_ground_truth"]),
    7: ("Access-denied folder", ["test_access_denied_folder"]),
    8: ("Junctions and symlinks not followed", ["test_junction_is_not_followed", "test_symlinks_are_not"]),
    9: ("Paths longer than 260", ["test_path_longer_than_260"]),
    10: ("Odd names", ["test_odd_names", "test_reserved_device_names_are_scanned"]),
    11: ("Empty folder, 0-byte files, 20,000 files", ["test_empty_folder_zero_byte"]),
    12: ("Stop mid-scan", ["test_stop_mid_scan"]),
    13: ("F5, Scan only this folder, Browse, Ctrl+L, bad path", ["test_rescan_scan_only"]),
    14: ("Drive picker: only a deliberate pick scans", ["test_drive_picker"]),
    15: ("Network share, offline share", ["test_network_share"]),
    16: ("Tree sorting, expand/collapse, Files toggle", ["test_tree_sort"]),
    17: ("Map: click, zoom, hover, legend colours", ["test_map_click"]),
    18: ("Types add up, sorting", ["test_types_add_up"]),
    19: ("Top 100: order, cap, Open, Explorer", ["test_top_100"]),
    20: ("Live refresh during a scan", ["test_live_refresh"]),
    21: ("Window, splitter, columns, last folder remembered", ["test_window_size_splitter", "BB remembered",
                                                              "BB window size", "BB last folder"]),
    22: ("Recycle file / folder / multi: real bin, restorable", ["test_recycle_file_folder"]),
    23: ("Only the focused view's selection is recycled", ["test_only_the_focused_views"]),
    24: ("Folder plus its own subfolder", ["test_folder_plus_its_own"]),
    25: ("Deselected rows and Cancel", ["test_deselected_rows"]),
    26: ("Refused names, unscanned folders", ["test_refused_names"]),
    27: ("Locked file reported, nothing lost", ["test_locked_file"]),
    28: ("Totals update; 50 recycle-and-click cycles", ["test_totals_update_and_50"]),
    29: ("Monkey: 3 x 500 + 1 x 3000 random clicks and keys", ["test_monkey_500"]),
    30: ("Whole C:\\ drive, memory released", ["test_whole_c_drive"]),
    31: ("Error log written, app keeps running", ["test_error_log_written"]),
    32: ("Files changing during the scan", ["test_files_changing_during"]),
    33: ("Folder changed behind the app's back", ["test_folder_changed_behind", "test_open_a_file_that_was_deleted"]),
    34: ("Drive disappears mid-scan", ["test_drive_disappears"]),
    35: ("300 levels deep; 200k in one folder; 1M files", ["test_300_levels", "test_200k_files"]),
    36: ("Hard links, streams, attributes, compressed, sparse", ["test_hard_links"]),
    37: ("Unicode, RTL, emoji, reserved names", ["test_odd_names", "test_reserved_device_names"]),
    38: ("High DPI, extreme sizes, keyboard only", ["test_high_dpi", "test_keyboard_only"]),
    39: ("Corrupt settings", ["test_corrupt_settings"]),
    40: ("Two windows / two copies, close one mid-scan", ["test_two_windows", "BB two copies",
                                                       "BB closing a copy", "BB the other copy"]),
    41: ("error.log location unusable", ["test_error_log_location", "BB starts when"]),
    42: ("Clipboard: 1,000 paths, unicode, Notepad", ["test_clipboard", "BB path pasted", "BB Ctrl+C"]),
    43: ("Killing Bigfoot mid-recycle loses nothing", ["test_killing_bigfoot"]),
    44: ("Locales and time zones", ["test_locale_and_time_zone"]),
    45: ("Fuzz: 300 random trees (3 shapes) vs ground truth", ["test_fuzz_random_trees"]),
    "G1": ("Never deleted for good: too big, size-limit edges, recycling off, subst, share, mapped folder",
           ["test_file_too_big", "test_recycle_bin_size_limit_edges", "test_windows_set_to_delete",
            "test_subst_drive_recycle", "test_network_share_recycle", "test_mapped_folder_recycle"]),
    "G2": ("Model consistency: 15 seeds x 300 random operations", ["test_model_consistency"]),
    "G3": ("Scan / Stop / F5 hammered", ["test_rapid_scan_stop_rescan"]),
    "G4": ("Recycle the map's focus and its parent", ["test_recycle_the_map_focus"]),
    "G5": ("Path box accepts anything", ["test_path_box_accepts_anything"]),
    "G6": ("Delete during a scan refused; Top 100 recycle with Files off", ["test_delete_during_scan"]),
    "G7": ("20 rescans don't leak memory", ["test_rescans_do_not_leak"]),
    "G8": ("Strange timestamps (1601-30000)", ["test_strange_timestamps"]),
    "G9": ("Treemap extremes", ["test_treemap_extremes"]),
    "G10": ("Recycle long paths, read-only/hidden/system/unicode, folders holding links",
            ["test_recycle_long_path", "test_recycle_read_only", "test_recycling_a_folder_never"]),
    "G11": ("Selection bigger than the Recycle Bin together", ["test_selection_bigger_than"]),
    "G12": ("Recycle on fresh NTFS / FAT32 / exFAT drives", ["test_recycle_on_other_volumes"]),
    "G13": ("Default Recycle Bin size fallback is safe", ["test_default_recycle_bin_size"]),
    "G14": ("Round 2: removable drive, size policy, MaxCapacity 0, 5,000 refused, no drive probing",
            ["test_removable_drive", "test_recycle_bin_size_policy", "test_max_capacity_zero",
             "test_large_all_refused", "test_drive_list_never_probes"]),
}


def load(folder):
    res = {}  # key -> list of outcomes
    xmlp = os.path.join(folder, "pytest.xml")
    if os.path.exists(xmlp):
        for tc in ET.parse(xmlp).getroot().iter("testcase"):
            name = tc.get("name")
            outcome = "PASS"
            if tc.find("failure") is not None or tc.find("error") is not None:
                outcome = "FAIL"
            elif tc.find("skipped") is not None:
                outcome = "SKIP"
            res.setdefault(name, []).append((outcome, float(tc.get("time", 0))))
    sp = os.path.join(folder, "summary.txt")
    if os.path.exists(sp):
        for line in open(sp, encoding="utf-8", errors="replace"):
            m = re.match(r"(PASS|FAIL)  (.*)", line.strip())
            if m:
                res.setdefault(m.group(2), []).append((m.group(1), 0.0))
    return res


def main(folders):
    runs = [load(f) for f in folders]
    print("| # | What | " + " | ".join(f"Run {i + 1}" for i in range(len(runs))) + " |")
    print("|---|---|" + "---|" * len(runs))
    for n, (what, keys) in PLAN.items():
        cells = []
        for r in runs:
            hits = [(k, v) for k, vs in r.items() for v in vs if any(k.startswith(p) for p in keys)]
            if not hits:
                cells.append("—")
                continue
            outs = [v[0] for _, v in hits]
            secs = sum(v[1] for _, v in hits)
            st = "FAIL" if "FAIL" in outs else ("PASS" if "PASS" in outs else "SKIP")
            cells.append(f"{st} ({len(hits)} checks{f', {secs:.0f}s' if secs else ''})")
        print(f"| {n} | {what} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main(sys.argv[1:])
