import shlex
import subprocess
from pathlib import Path

from archive_tool import ssh

EXCLUDES = ("@eaDir", "lost+found", ".DS_Store", "Thumbs.db", "*.tmp")


class TransferError(Exception):
    pass


def push_to_remote(
    source: Path, host: str, user: str, dest: str, make_parents: bool = False
) -> None:
    """Rsync the *contents* of `source` into `dest` on user@host. The Mac is the source.

    The trailing slash on source copies the contents into `dest` (named after the
    project), not nested one level deeper. When make_parents is set, `mkdir -p dest` runs
    on the remote first — used for the CentOS masters tree, which is organic and may not
    have the collection path yet. (basil collections come from its own picker, so they
    already exist; only the project dir is created there by rsync.)
    """
    if make_parents:
        ssh.run_remote(host, user, f"mkdir -p {shlex.quote(dest)}")
    target = f"{user}@{host}:{dest}/"
    _run_local(_rsync_args() + [f"{source}/", target])


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
