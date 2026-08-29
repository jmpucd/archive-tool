import shlex
import subprocess


class SSHError(Exception):
    pass


_BASE_OPTS = ["-o", "BatchMode=yes"]


def run_remote(host: str, user: str, command: str) -> str:
    """Run a command on `user@host` and return stdout. Raises SSHError on non-zero exit.

    BatchMode=yes makes ssh fail fast (rather than prompt) when key auth isn't set up.
    """
    target = f"{user}@{host}"
    result = subprocess.run(
        ["ssh", *_BASE_OPTS, target, command],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SSHError(
            f"ssh {target} failed (exit {result.returncode}):\n"
            f"  command: {command}\n"
            f"  stderr:  {result.stderr.strip() or '(empty)'}"
        )
    return result.stdout


def run_remote_streaming(
    host: str, user: str, command: str, stdin: bytes | None = None
) -> None:
    """Run a command on `user@host`, stream stdout/stderr to local terminal.

    Use this when the remote command produces progress output the user should see
    (e.g. rsync). `stdin`, when given, is fed to the remote command (used to ship a
    script for `python -` without scp'ing it first). Raises SSHError on non-zero exit.
    """
    target = f"{user}@{host}"
    result = subprocess.run(["ssh", *_BASE_OPTS, target, command], input=stdin)
    if result.returncode != 0:
        raise SSHError(f"ssh {target} command exited {result.returncode}: {command}")


def md5sums(host: str, user: str, paths: list[str]) -> dict[str, str]:
    """{path: hex md5} for the given remote files (md5sum run on the remote)."""
    if not paths:
        return {}
    out = run_remote(host, user, "md5sum " + " ".join(shlex.quote(p) for p in paths))
    result: dict[str, str] = {}
    for line in out.splitlines():
        if len(line) > 34 and line[32] == " ":
            result[line[34:].strip()] = line[:32]
    return result


def list_dirs(host: str, user: str, path: str) -> list[str]:
    """List immediate subdirectory names of a remote path. Returns sorted basenames."""
    cmd = (
        f"find {shlex.quote(path)} -maxdepth 1 -mindepth 1 -type d "
        r"-printf '%f\n' | sort"
    )
    out = run_remote(host, user, cmd)
    return [line for line in out.splitlines() if line]


def path_exists(host: str, user: str, path: str) -> bool:
    """True iff `path` exists on the remote host (file or dir)."""
    target = f"{user}@{host}"
    result = subprocess.run(
        ["ssh", *_BASE_OPTS, target, f"test -e {shlex.quote(path)}"],
        capture_output=True,
    )
    return result.returncode == 0
