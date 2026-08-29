"""Full-resolution JPEG access copies for an already-archived project.

Same shape as ocr.py: the conversion runs ON CentOS next to the masters
(jpeg_runner.py shipped over ssh stdin), output lands in <project>/<subdir>/, and the
folder is then pulled to basil and rclone-copied to Box wherever the project lives.
"""

import shlex
from pathlib import Path

from archive_tool import box_upload, ssh, transfer
from archive_tool.config import BasilConfig, BoxConfig, CentosConfig
from archive_tool.ocr import OCRError

RUNNER_PATH = Path(__file__).with_name("jpeg_runner.py")

DEFAULT_SUBDIR = "JPEG"
DEFAULT_QUALITY = 80
DEFAULT_WORKERS = 4  # each worker holds one full-res TIFF in RAM; CentOS has ~8 GB


def _remote_python(centos: CentosConfig) -> str:
    python = centos.ocr_python
    if python.startswith("~/"):
        return "$HOME/" + python[2:]
    if not python.startswith("/"):
        raise OCRError(f"[remote.centos].ocr_python must be absolute or ~-prefixed: {python}")
    return python


def run_on_centos(
    centos: CentosConfig,
    project_dir: str,
    subdir: str = DEFAULT_SUBDIR,
    match: str = "_recto",
    quality: int = DEFAULT_QUALITY,
    workers: int = DEFAULT_WORKERS,
    overwrite: bool = False,
) -> None:
    python = _remote_python(centos)
    cmd = " ".join(
        [
            f"test -x {python} || {{ echo 'digtk python not found: {python}' >&2; exit 3; }};",
            "PYTHONIOENCODING=utf-8",
            python,
            "-",
            shlex.quote(project_dir),
            "--subdir", shlex.quote(subdir),
            "--match", shlex.quote(match),
            "--quality", str(quality),
            "--workers", str(workers),
        ]
        + (["--overwrite"] if overwrite else [])
    )
    try:
        ssh.run_remote_streaming(centos.host, centos.user, cmd, stdin=RUNNER_PATH.read_bytes())
    except ssh.SSHError as e:
        raise OCRError(f"JPEG run on {centos.host} failed: {e}") from e


def list_outputs(centos: CentosConfig, project_dir: str, subdir: str) -> list[str]:
    """Sorted basenames of the JPEGs in <project_dir>/<subdir> on CentOS ([] if none)."""
    d = f"{project_dir}/{subdir}"
    if not ssh.path_exists(centos.host, centos.user, d):
        return []
    out = ssh.run_remote(
        centos.host, centos.user,
        f"find {shlex.quote(d)} -maxdepth 1 -type f -name '*.jpg' -printf '%f\\n' | sort",
    )
    return [line for line in out.splitlines() if line]


def pull_dir_to_basil(
    centos: CentosConfig, basil: BasilConfig, centos_dir: str, basil_dir: str,
    subdir: str, names: list[str],
) -> None:
    if not centos.host_from_basil:
        raise OCRError("[remote.centos].host_from_basil is not set; cannot pull to basil")
    if not ssh.path_exists(basil.host, basil.user, basil_dir, strict=True):
        raise OCRError(f"basil project folder does not exist: {basil_dir}")
    try:
        transfer.pull_from_remote(
            puller_host=basil.host,
            puller_user=basil.user,
            src_host=centos.host_from_basil,
            src_user=centos.user,
            src_path=f"{centos_dir}/{subdir}",
            dest_path=f"{basil_dir}/{subdir}",
            make_parents=True,
        )
    except ssh.SSHError as e:
        raise OCRError(f"basil pull failed: {e}") from e
    verify_dir(centos, centos_dir, basil.host, basil.user, basil_dir, subdir, names)


def verify_dir(
    centos: CentosConfig, centos_dir: str, host: str, user: str, remote_dir: str,
    subdir: str, names: list[str],
) -> None:
    want = ssh.md5sums(centos.host, centos.user, [f"{centos_dir}/{subdir}/{n}" for n in names])
    got = ssh.md5sums(host, user, [f"{remote_dir}/{subdir}/{n}" for n in names])
    bad = [
        n for n in names
        if want.get(f"{centos_dir}/{subdir}/{n}") != got.get(f"{remote_dir}/{subdir}/{n}")
    ]
    if bad:
        raise OCRError(f"md5 mismatch on {host}:{remote_dir}/{subdir} for {len(bad)} file(s): "
                       + ", ".join(bad[:5]))


def copy_dir_to_box(centos: CentosConfig, centos_dir: str, box_target: str, subdir: str) -> None:
    """rclone copy <centos_dir>/<subdir> -> <box_target>/<subdir> (hash-verified by rclone)."""
    src = f"{centos_dir}/{subdir}"
    dst = f"{box_target.rstrip('/')}/{subdir}"
    cmd = f"rclone copy {shlex.quote(src)} {shlex.quote(dst)} {box_upload._PROGRESS}"
    try:
        ssh.run_remote_streaming(centos.host, centos.user, cmd)
    except ssh.SSHError as e:
        raise OCRError(f"rclone copy to {dst} failed: {e}") from e
