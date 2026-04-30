import hashlib
from pathlib import Path

EXCLUDE_NAMES = {"@eaDir", "lost+found", ".DS_Store", "Thumbs.db", "manifest.md5"}
EXCLUDE_GLOBS = ("*.tmp",)
MANIFEST_NAME = "manifest.md5"


def iter_files(root: Path) -> list[Path]:
    """Return sorted relative paths of every file under root, applying excludes."""
    matches: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_NAMES for part in rel.parts):
            continue
        if any(rel.match(g) for g in EXCLUDE_GLOBS):
            continue
        matches.append(rel)
    matches.sort()
    return matches


def md5_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Hex MD5 of a file. Reads in chunks so multi-GB files don't blow memory."""
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def write_manifest(project_root: Path) -> Path:
    """Generate <project_root>/manifest.md5 in standard md5sum format.

    Each line: "<32-hex>  <relative-path>\\n" (two spaces, per md5sum convention).
    Overwrites any existing manifest.
    """
    manifest_path = project_root / MANIFEST_NAME
    if manifest_path.exists():
        manifest_path.unlink()

    lines = []
    for rel in iter_files(project_root):
        full = project_root / rel
        digest = md5_file(full)
        lines.append(f"{digest}  {rel.as_posix()}\n")

    manifest_path.write_text("".join(lines))
    return manifest_path


def manifest_checksum(manifest_path: Path) -> str:
    """MD5 of the manifest file itself, for tamper detection in the Sheet."""
    return md5_file(manifest_path)
