"""JPEG derivative runner: full-resolution JPEGs for selected pages of a project.

Like ocr_runner.py this is NOT imported by archive-tool; it is shipped over SSH and run
ON CentOS (`python - <args>`, script on stdin) with the digtk venv's Pillow, next to the
masters. Keep it self-contained, ASCII-only, and Python 3.9-compatible.

Writes <project>/<subdir>/<stem>.jpg for every page image whose filename matches the
--match substring (e.g. "_recto"), at the requested quality, keeping the source's
ICC profile and DPI. No resizing: these are the full-res access copies.
"""
import argparse
import multiprocessing as mp
import os
import sys
import time

IMAGE_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}


def parse_args():
    ap = argparse.ArgumentParser(description="archive-tool JPEG runner (runs on CentOS)")
    ap.add_argument("project_dir")
    ap.add_argument("--subdir", default="JPEG", help="output folder inside project_dir")
    ap.add_argument("--match", default="", help="only files whose name contains this")
    ap.add_argument("--quality", type=int, default=80)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--overwrite", action="store_true")
    return ap.parse_args()


def convert(job):
    src, dst, quality = job
    from PIL import Image, ImageOps
    im = Image.open(src)
    im.load()
    info = im.info
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    kw = {"quality": quality, "optimize": True}
    if info.get("icc_profile"):
        kw["icc_profile"] = info["icc_profile"]
    if info.get("dpi"):
        kw["dpi"] = tuple(int(round(float(d))) for d in info["dpi"])
    tmp = dst + ".tmp"
    im.save(tmp, "JPEG", **kw)
    os.replace(tmp, dst)
    return os.path.basename(dst), os.path.getsize(dst)


def main():
    a = parse_args()
    out_dir = os.path.join(a.project_dir, a.subdir)
    names = sorted(
        f for f in os.listdir(a.project_dir)
        if not f.startswith(".")
        and os.path.splitext(f)[1].lower() in IMAGE_EXTS
        and (a.match in f)
        and os.path.isfile(os.path.join(a.project_dir, f))
    )
    if not names:
        sys.stderr.write("no page images matching %r in %s\n" % (a.match, a.project_dir))
        return 2
    os.makedirs(out_dir, exist_ok=True)
    jobs = []
    for f in names:
        dst = os.path.join(out_dir, os.path.splitext(f)[0] + ".jpg")
        if a.overwrite or not os.path.exists(dst):
            jobs.append((os.path.join(a.project_dir, f), dst, a.quality))
    sys.stderr.write("%d matching pages, %d to convert, quality %d, %d workers -> %s\n" % (
        len(names), len(jobs), a.quality, a.workers, out_dir))
    t0 = time.time()
    total = 0
    with mp.get_context("fork").Pool(a.workers) as pool:
        for i, (name, size) in enumerate(pool.imap_unordered(convert, jobs), 1):
            total += size
            sys.stderr.write("  [%d/%d] %s  %.1f MB\n" % (i, len(jobs), name, size / 1e6))
    sys.stderr.write("done in %.0fs: %d JPEGs, %.1f MB\n" % (time.time() - t0, len(jobs), total / 1e6))
    sys.stdout.write("dir=%s\ncount=%d\n" % (out_dir, len(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
