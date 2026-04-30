import shlex
import subprocess
from pathlib import Path

from archive_tool import ssh
from archive_tool.config import CentosConfig, SynologyConfig

EXCLUDES = ("@eaDir", "lost+found", ".DS_Store", "Thumbs.db", "*.tmp")


class TransferError(Exception):
    pass


def push_to_synology(source: Path, syn: SynologyConfig) -> str:
    """Rsync source dir to Synology staging. Returns the destination path on Synology.

    Includes a trailing slash on source so rsync copies the *contents* into a folder
    named after the project, not nested one level deeper.
    """
    dest = f"{syn.staging_dir.rstrip('/')}/{source.name}"
    target = f"{syn.user}@{syn.host}:{dest}"
    args = _rsync_args() + [f"{source}/", f"{target}/"]
    _run_local(args)
    return dest


def push_synology_to_centos(
    syn_path: str,
    syn: SynologyConfig,
    centos: CentosConfig,
    dest_parent: str,
) -> str:
    """Drive an rsync from Synology to CentOS. Returns the final path on CentOS.

    Synology runs rsync, pushing to CentOS via campus DNS (host_from_synology).
    """
    project_name = Path(syn_path).name
    centos_dest = f"{dest_parent.rstrip('/')}/{project_name}"
    centos_addr = centos.host_from_synology or centos.host
    target = f"{centos.user}@{centos_addr}:{centos_dest}"

    rsync_args = _rsync_args() + [f"{syn_path}/", f"{target}/"]
    rsync_cmd = " ".join(shlex.quote(a) for a in rsync_args)
    ssh.run_remote_streaming(syn.host, syn.user, rsync_cmd)
    return centos_dest


def verify_manifest_remote(host: str, user: str, project_path: str) -> None:
    """Run `md5sum -c --quiet manifest.md5` in project_path on the remote.

    Raises TransferError on any mismatch or missing file. md5sum --quiet only emits
    output for failures, so success is silent.
    """
    cmd = f"cd {shlex.quote(project_path)} && md5sum -c --quiet manifest.md5"
    target = f"{user}@{host}"
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, cmd],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = []
        if result.stdout.strip():
            details.append(f"stdout:\n{result.stdout.strip()}")
        if result.stderr.strip():
            details.append(f"stderr:\n{result.stderr.strip()}")
        raise TransferError(
            f"manifest verification failed on {host}:{project_path}\n" + "\n".join(details)
        )


def _rsync_args() -> list[str]:
    args = [
        "rsync",
        "-a",                # archive mode (recursive, symlinks, perms, times, devices)
        "--no-owner",        # cross-machine UIDs differ; don't try to preserve them
        "--no-group",
        "--partial",         # keep partial transfers for resume
        "--append-verify",   # resume by appending, then verifying the existing chunk
        "-h",
        "--info=progress2",
    ]
    for ex in EXCLUDES:
        args += [f"--exclude={ex}"]
    return args


def _run_local(args: list[str]) -> None:
    result = subprocess.run(args)
    if result.returncode != 0:
        raise TransferError(f"command failed (exit {result.returncode}): {' '.join(args)}")
