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


def pull_from_remote(
    puller_host: str,
    puller_user: str,
    src_host: str,
    src_user: str,
    src_path: str,
    dest_path: str,
    make_parents: bool = False,
) -> None:
    """Rsync `src_path` -> `dest_path` with the rsync process running ON `puller_host`.

    Used for the basil leg: basil PULLS the already-landed masters copy from CentOS.
    (basil->CentOS:22 is open; CentOS->basil is firewalled, so a push won't work.) The
    rsync runs over the puller's own ssh, so `src_host` must be a name the puller can
    resolve/reach — CentOS's campus DNS, not the Mac's `digitization` ssh alias — and the
    puller's key must be authorized on src_host.

    basil's rsync is 3.0.6, which predates `--info=progress2` (3.1.0), so this uses the
    3.0-safe arg set (plain `--progress`) and `--protect-args` so spaces in project names
    survive the extra remote-shell hop.
    """
    tokens = _rsync_args(progress2=False, protect_args=True)
    tokens.append(f"{src_user}@{src_host}:{src_path}/")
    tokens.append(f"{dest_path}/")
    rsync_cmd = " ".join(shlex.quote(t) for t in tokens)
    prefix = f"mkdir -p {shlex.quote(dest_path)} && " if make_parents else ""
    ssh.run_remote_streaming(puller_host, puller_user, prefix + rsync_cmd)


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


def _rsync_args(progress2: bool = True, protect_args: bool = False) -> list[str]:
    args = [
        "rsync",
        "-a",                # archive mode (recursive, symlinks, perms, times, devices)
        "--no-owner",        # cross-machine UIDs differ; don't try to preserve them
        "--no-group",
        "--partial",         # keep partial transfers for resume
        "--append-verify",   # resume by appending, then verifying the existing chunk
        "-h",
    ]
    if protect_args:
        # -s: don't let the remote shell re-split paths; needed for spaces in project
        # names when rsync itself runs on a remote host (the basil pull).
        args.append("--protect-args")
    # --info=progress2 (overall %) needs rsync 3.1.0+; basil's 3.0.6 only has --progress.
    args.append("--info=progress2" if progress2 else "--progress")
    for ex in EXCLUDES:
        args += [f"--exclude={ex}"]
    return args


def _run_local(args: list[str]) -> None:
    result = subprocess.run(args)
    if result.returncode != 0:
        raise TransferError(f"command failed (exit {result.returncode}): {' '.join(args)}")
