"""Builds the demo folder the README screenshots are taken of.

Every file is real: it holds random bytes and is exactly the size Bigfoot shows for it. Nothing is sparse and
nothing is a placeholder, so the screenshots claim nothing the files don't take. The folder holds about 2.5 GB at
the default scale (0.6); --scale 1 makes about 4 GB. Needs that much free space on the drive.

    python tools\\make_demo.py D:\\Demo            build it
    python tools\\make_demo.py D:\\Demo --scale 1  bigger
"""
import argparse
import os
import random
import shutil
import time

MB = 1 << 20

# folder -> {subfolder or file name: size in MB, or a dict, or (count, MB each, name pattern)}
LAYOUT = {
    "Videos": {
        "Project reel 4K.mov": 640, "Drone footage Iceland.mp4": 360, "Concert multicam.mkv": 260,
        "Family 2024.mp4": 120,
        "Phone clips": (36, 5, "clip_{:03d}.mp4"),
        "Screen recordings": {"tutorial_final.mp4": 24, "bug_report.mp4": 18, "standup.mp4": 14},
    },
    "Games": {
        "Open World RPG": {"data": (8, 48, "pak{:02d}.pak")},
        "Racing Sim": {"data": (5, 36, "track{:02d}.pak")},
        "Indie Platformer": {"levels": (40, 0.5, "level{:02d}.dat")},
    },
    "Music": {
        "Library": (60, 6, "track_{:03d}.flac"),
        "Album sessions": (20, 8, "take_{:02d}.wav"),
    },
    "Photos": {
        "2022": {"RAW": (25, 4, "IMG_{:04d}.cr3")}, "2023": {"RAW": (25, 4, "IMG_{:04d}.cr3")},
        "2024": {"RAW": (25, 4, "IMG_{:04d}.cr3")}, "2025": {"RAW": (25, 4, "IMG_{:04d}.cr3")},
        "Exports": (100, 1.5, "export_{:03d}.jpg"),
    },
    "Projects": {
        "ml-model": {"checkpoints": (6, 40, "epoch_{:04d}.ckpt"), "data": (15, 8, "shard_{:02d}.csv")},
        "website": {"src": (300, 0.04, "module_{:03d}.js")},
    },
    "Downloads": {
        "old-backup.zip": 220, "dataset.7z": 110, "ubuntu-24.04.iso": 160, "installer.exe": 45,
    },
    "Documents": {
        "Taxes": (80, 0.4, "scan_{:04d}.pdf"), "Letters": (50, 0.1, "letter_{:03d}.docx"),
    },
}


def planned_bytes(spec, scale):
    """About how much the layout writes (the random spread of sizes averages out)."""
    total = 0
    for v in spec.values():
        if isinstance(v, dict):
            total += planned_bytes(v, scale)
        elif isinstance(v, tuple):
            total += int(v[0] * v[1] * MB * scale)
        else:
            total += int(v * MB * scale)
    return total


def write_random(path, size, rnd):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    left = size
    with open(path, "wb") as f:
        while left > 0:
            n = min(4 * MB, left)
            f.write(os.urandom(n))
            left -= n
    t = time.time() - rnd.randint(30, 900) * 86400  # spread the dates over a couple of years
    os.utime(path, (t, t))


def build(folder, spec, scale, rnd):
    total = 0
    for name, v in spec.items():
        p = os.path.join(folder, name)
        if isinstance(v, dict):
            total += build(p, v, scale, rnd)
        elif isinstance(v, tuple):
            count, mb, pattern = v
            for i in range(count):
                size = max(1024, int(mb * MB * scale * rnd.uniform(0.7, 1.3)))
                write_random(os.path.join(p, pattern.format(i)), size, rnd)
                total += size
        else:
            size = max(1024, int(v * MB * scale))
            write_random(p, size, rnd)
            total += size
    return total


def backdate_folders(top):
    """A folder's date is when it last changed: today for all of them, since they were just made. Give each the
    date of its newest file, bottom-up, so Modified reads like a real folder."""
    newest = 0
    for e in os.scandir(top):
        newest = max(newest, backdate_folders(e.path) if e.is_dir() else e.stat().st_mtime)
    if newest:
        os.utime(top, (newest, newest))
    return newest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="where to build it (must not exist yet)")
    ap.add_argument("--scale", type=float, default=0.6)
    a = ap.parse_args()
    if os.path.exists(a.folder):
        raise SystemExit(f"{a.folder} already exists: choose a new folder")
    parent = os.path.dirname(os.path.abspath(a.folder))
    while not os.path.isdir(parent):  # (the free space of the drive the folder will be made on)
        parent = os.path.dirname(parent)
    need = int(planned_bytes(LAYOUT, a.scale) * 1.5)  # the sizes vary a little around the plan
    free = shutil.disk_usage(parent).free
    if free < need:
        raise SystemExit(f"needs {need / MB:,.0f} MB free, {free / MB:,.0f} MB is: choose another drive")
    rnd = random.Random(7)
    try:
        total = build(a.folder, LAYOUT, a.scale, rnd)
        backdate_folders(a.folder)
    except BaseException:
        shutil.rmtree(a.folder, ignore_errors=True)  # a half-built folder would block the next run
        raise
    print(f"{a.folder}: {total / MB:,.0f} MB in real files")


if __name__ == "__main__":
    main()
