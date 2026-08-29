"""Copy an archived project from CentOS up to Box, via rclone.

The project already lives on CentOS after the transfer, and the `box:` rclone remote is
configured there, so we run rclone *on CentOS* over SSH rather than from the laptop.
Collaboration/sharing is manual (done by John in Box); this module only moves the files.
"""

import shlex

from archive_tool import ssh
from archive_tool.config import BoxConfig, CentosConfig


class BoxUploadError(Exception):
    pass


def upload_to_box(centos: CentosConfig, centos_path: str, box: BoxConfig, project: str) -> str:
    """rclone-copy centos_path into <remote><base_folder>/<project>. Returns the Box path."""
    box_dest = f"{box.base_folder.rstrip('/')}/{project}"
    target = f"{box.rclone_remote}{box_dest}"
    cmd = (
        f"rclone copy {shlex.quote(centos_path)} {shlex.quote(target)} "
        f"--create-empty-src-dirs --progress"
    )
    try:
        ssh.run_remote_streaming(centos.host, centos.user, cmd)
    except ssh.SSHError as e:
        raise BoxUploadError(f"rclone copy to {target} failed: {e}") from e
    return target


def upload_files_to_box(
    centos: CentosConfig, project_dir: str, filenames: list[str], box_target: str
) -> None:
    """rclone-copy only `filenames` (top-level names inside project_dir) into an existing
    Box target like "box:Archives/<project>". Used for OCR derivatives, so a project
    that was never uploaded to Box in full still gets a Box folder holding the PDF."""
    if not filenames:
        return
    includes = " ".join(f"--include {shlex.quote('/' + n)}" for n in filenames)
    cmd = (
        f"rclone copy {shlex.quote(project_dir)} {shlex.quote(box_target)} "
        f"{includes} --progress"
    )
    try:
        ssh.run_remote_streaming(centos.host, centos.user, cmd)
    except ssh.SSHError as e:
        raise BoxUploadError(f"rclone copy to {box_target} failed: {e}") from e
