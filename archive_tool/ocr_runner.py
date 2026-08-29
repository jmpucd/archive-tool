"""OCR runner: builds a searchable PDF + paged transcript for an archived project.

This file is NOT imported by archive-tool. It is shipped over SSH and executed ON CentOS
(`python - <args>` with the script on stdin) using the digtk venv there, next to the
masters, so nothing has to be re-uploaded from the Mac and it works after the local
source is deleted. Keep it self-contained, ASCII-only (the remote locale may be POSIX),
and Python 3.9-compatible (CentOS venv).

Per page: digtk rasterizes the TIFF to a working JPEG, Tesseract emits word boxes
(TSV), and the same TSV is grouped into lines/paragraphs for the transcript. Pages run
in a process pool; then digtk assembles one aligned, highlightable searchable PDF and
the transcript is written as one text file with a header per page.
"""
import argparse
import csv
import io
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}
SKIP_DIRS = {"@eaDir", "lost+found"}
RULE = "=" * 78


def parse_args():
    ap = argparse.ArgumentParser(description="archive-tool OCR runner (runs on CentOS)")
    ap.add_argument("project_dir")
    ap.add_argument("--pdf", required=True, help="output searchable PDF path")
    ap.add_argument("--txt", required=True, help="output transcript path")
    ap.add_argument("--title", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-px", type=int, default=2200)
    ap.add_argument("--min-conf", type=float, default=30.0)
    ap.add_argument("--lang", default="eng")
    return ap.parse_args()


def page_files(project_dir):
    """Sorted relative paths of every page image under project_dir (recursive)."""
    rels = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in files:
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                rels.append(os.path.relpath(os.path.join(root, f), project_dir))
    rels.sort()
    return rels


def tesseract_tsv(jpg, lang):
    """Raw Tesseract TSV for one page ('' on failure). Bytes-safe decode."""
    from digtk import config as dcfg
    try:
        raw = subprocess.run(
            [dcfg.TESSERACT_BIN, jpg, "stdout", "-l", lang, "--psm", "3", "tsv"],
            capture_output=True, timeout=300,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""
    return raw.decode("utf-8", errors="replace")


def parse_tsv(tsv, min_conf):
    """-> (words [{text,bbox}], text). Words below min_conf are dropped from both the
    PDF layer and the transcript, so what you can highlight is what you can read."""
    words = []
    lines = []  # [(paragraph_key, [word, ...])] in Tesseract's reading order
    cur_key = None
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        try:
            if row.get("level") != "5":  # 5 = word
                continue
            txt = (row.get("text") or "").strip()
            conf = float(row.get("conf", "-1"))
            if not txt or conf < 0 or conf < min_conf:
                continue
            l, t = int(row["left"]), int(row["top"])
            w, h = int(row["width"]), int(row["height"])
            key = (row["block_num"], row["par_num"], row["line_num"])
        except (ValueError, KeyError, TypeError):
            continue
        words.append({"text": txt, "bbox": [l, t, l + w, t + h]})
        if key != cur_key:
            lines.append((key[:2], []))
            cur_key = key
        lines[-1][1].append(txt)
    paras, prev_par = [], None
    for par, ws in lines:
        if prev_par is not None and par != prev_par:
            paras.append("")  # blank line between Tesseract paragraphs/blocks
        paras.append(" ".join(ws))
        prev_par = par
    return words, "\n".join(paras).strip()


def ocr_page(job):
    """Pool worker: (idx, src, jpg, max_px, min_conf, lang) -> (idx, words, text)."""
    idx, src, jpg, max_px, min_conf, lang = job
    from digtk import raster
    with open(jpg, "wb") as f:
        f.write(raster.to_jpeg(src, max_px=max_px))
    words, text = parse_tsv(tesseract_tsv(jpg, lang), min_conf)
    return idx, words, text


def write_transcript(path, title, rels, texts, tool_line):
    out = io.StringIO()
    out.write("%s - OCR transcript\n" % title)
    out.write("Generated %s by archive-tool (%s). Uncorrected machine OCR; "
              "page order follows the image filenames.\n" % (
                  datetime.now().strftime("%Y-%m-%d %H:%M"), tool_line))
    out.write("%d pages\n" % len(rels))
    for i, (rel, text) in enumerate(zip(rels, texts), 1):
        out.write("\n%s\nPage %d of %d - %s\n%s\n\n" % (RULE, i, len(rels), rel, RULE))
        out.write((text or "[no text detected]") + "\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(out.getvalue())
    os.replace(tmp, path)


def main():
    a = parse_args()
    from digtk import pdfbuild, config as dcfg
    rels = page_files(a.project_dir)
    if not rels:
        sys.stderr.write("no page images found under %s\n" % a.project_dir)
        return 2
    try:
        tess_ver = subprocess.run([dcfg.TESSERACT_BIN, "--version"], capture_output=True,
                                  timeout=30).stdout.decode("utf-8", "replace").split("\n")[0]
    except (subprocess.SubprocessError, OSError):
        tess_ver = "tesseract"
    sys.stderr.write("%d pages, %d workers, max_px=%d, min_conf=%g, %s\n" % (
        len(rels), a.workers, a.max_px, a.min_conf, tess_ver))
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="archive-ocr-") as td:
        jobs = [(i, os.path.join(a.project_dir, rel), os.path.join(td, "p%05d.jpg" % i),
                 a.max_px, a.min_conf, a.lang) for i, rel in enumerate(rels)]
        results = [None] * len(rels)
        done = 0
        with mp.get_context("fork").Pool(a.workers) as pool:
            for idx, words, text in pool.imap_unordered(ocr_page, jobs):
                results[idx] = (words, text)
                done += 1
                if done % 25 == 0 or done == len(rels):
                    sys.stderr.write("  %d/%d pages  (%.0fs)\n" % (done, len(rels), time.time() - t0))
        jpgs = [j[2] for j in jobs]
        word_lists = [r[0] for r in results]
        texts = [r[1] for r in results]
        sys.stderr.write("assembling PDF...\n")
        pdfbuild.build_searchable_pdf(jpgs, word_lists, a.pdf, meta={"title": a.title})
    write_transcript(a.txt, a.title or os.path.basename(a.project_dir.rstrip("/")),
                     rels, texts, "%s via digtk" % tess_ver)
    n_words = sum(len(w) for w in word_lists)
    n_blank = sum(1 for t in texts if not t)
    sys.stderr.write("done in %.0fs: %d pages, %d words, %d page(s) with no text\n" % (
        time.time() - t0, len(rels), n_words, n_blank))
    sys.stdout.write("pdf=%s\ntxt=%s\npages=%d\nwords=%d\nblank=%d\n" % (
        a.pdf, a.txt, len(rels), n_words, n_blank))
    return 0


if __name__ == "__main__":
    sys.exit(main())
