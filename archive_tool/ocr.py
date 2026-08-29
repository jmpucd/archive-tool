"""OCR derivatives for an already-archived project: searchable PDF + paged transcript.

Everything runs against the CentOS masters copy (always present) and then fans out the
two result files the same way the masters did: basil pulls them from CentOS, CentOS
rclone-copies them to Box. The Mac only orchestrates. The MD5 manifest of the masters is
left untouched (it is the fixity record of the originals); derivative copies are verified
by comparing md5s against the CentOS originals instead.
"""

import shlex
from dataclasses import dataclass
from pathlib import Path

from archive_tool import box_upload, ssh, transfer
from archive_tool.config import BasilConfig, BoxConfig, CentosConfig

RUNNER_PATH = Path(__file__).with_name("ocr_runner.py")

DEFAULT_WORKERS = 8
DEFAULT_MAX_PX = 2200
DEFAULT_MIN_CONF = 30.0


class OCRError(Exception):
    pass


@dataclass(frozen=True)
class Derivatives:
    pdf: str         # basename of the searchable PDF, inside the project folder
    transcript: str  # basename of the paged transcript

    @property
    def names(self) -> list[str]:
        return [self.pdf, self.transcript]


def derivative_names(project_name: str) -> Derivatives:
    return Derivatives(pdf=f"{project_name}.pdf", transcript=f"{project_name}_transcript.txt")


def outputs_exist(centos: CentosConfig, project_dir: str, d: Derivatives) -> bool:
    return all(ssh.path_exists(centos.host, centos.user, f"{project_dir}/{n}") for n in d.names)


def run_on_centos(
    centos: CentosConfig,
    project_dir: str,
    d: Derivatives,
    title: str,
    workers: int = DEFAULT_WORKERS,
    max_px: int = DEFAULT_MAX_PX,
    min_conf: float = DEFAULT_MIN_CONF,
) -> None:
    """Ship ocr_runner.py over SSH and run it with the digtk venv on CentOS. Streams
    progress; the outputs land directly inside `project_dir`."""
    python = centos.ocr_python
    if python.startswith("~/"):
        python = "$HOME/" + python[2:]  # let the remote shell expand it (quote would not)
    elif not python.startswith("/"):
        raise OCRError(f"[remote.centos].ocr_python must be absolute or ~-prefixed: {python}")
    cmd = " ".join(
        [
            f"test -x {python} || {{ echo 'digtk python not found: {python}' >&2; exit 3; }};",
            "PYTHONIOENCODING=utf-8",
            python,
            "-",
            shlex.quote(project_dir),
            "--pdf", shlex.quote(f"{project_dir}/{d.pdf}"),
            "--txt", shlex.quote(f"{project_dir}/{d.transcript}"),
            "--title", shlex.quote(title),
            "--workers", str(workers),
            "--max-px", str(max_px),
            "--min-conf", str(min_conf),
        ]
    )
    try:
        ssh.run_remote_streaming(centos.host, centos.user, cmd, stdin=RUNNER_PATH.read_bytes())
    except ssh.SSHError as e:
        raise OCRError(f"OCR run on {centos.host} failed: {e}") from e
    if not outputs_exist(centos, project_dir, d):
        raise OCRError(f"OCR finished but outputs are missing in {centos.host}:{project_dir}")


def pull_to_basil(
    centos: CentosConfig, basil: BasilConfig, centos_dir: str, basil_dir: str, d: Derivatives
) -> None:
    """Copy the derivatives from the CentOS project folder into the basil one and verify
    by md5. Uses the same basil-side pull as the archive flow (CentOS->basil is
    firewalled); falls back to nothing when host_from_basil isn't configured."""
    if not centos.host_from_basil:
        raise OCRError(
            "[remote.centos].host_from_basil is not set; cannot pull derivatives to basil"
        )
    if not ssh.path_exists(basil.host, basil.user, basil_dir):
        raise OCRError(f"basil project folder does not exist: {basil_dir}")
    try:
        transfer.pull_files_from_remote(
            puller_host=basil.host,
            puller_user=basil.user,
            src_host=centos.host_from_basil,
            src_user=centos.user,
            src_files=[f"{centos_dir}/{n}" for n in d.names],
            dest_dir=basil_dir,
        )
    except ssh.SSHError as e:
        raise OCRError(f"basil pull failed: {e}") from e
    verify_copies(centos, centos_dir, basil.host, basil.user, basil_dir, d)


def verify_copies(
    centos: CentosConfig, centos_dir: str, host: str, user: str, remote_dir: str, d: Derivatives
) -> None:
    """md5 of each derivative on `host` must match the CentOS original."""
    want = ssh.md5sums(centos.host, centos.user, [f"{centos_dir}/{n}" for n in d.names])
    got = ssh.md5sums(host, user, [f"{remote_dir}/{n}" for n in d.names])
    bad = [
        n for n in d.names
        if want.get(f"{centos_dir}/{n}") != got.get(f"{remote_dir}/{n}")
    ]
    if bad:
        raise OCRError(f"md5 mismatch on {host}:{remote_dir} for {', '.join(bad)}")


def copy_to_box(centos: CentosConfig, box: BoxConfig, centos_dir: str, box_target: str,
                d: Derivatives) -> None:
    """rclone the derivatives into the project's Box folder (created if absent). rclone
    verifies size + hash itself after each transfer, so no separate md5 pass."""
    try:
        box_upload.upload_files_to_box(centos, centos_dir, d.names, box_target)
    except box_upload.BoxUploadError as e:
        raise OCRError(str(e)) from e


def box_target_for(box: BoxConfig, project_name: str) -> str:
    return f"{box.rclone_remote}{box.base_folder.rstrip('/')}/{project_name}"
