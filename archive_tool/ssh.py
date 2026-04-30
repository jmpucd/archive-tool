import shlex
import subprocess


class SSHError(Exception):
    pass


def run_remote(host: str, user: str, command: str) -> str:
    """Run a command on `user@host` over SSH and return stdout.

    BatchMode=yes makes ssh fail fast (rather than prompting) if key auth isn't set up.
    """
    target = f"{user}@{host}"
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, command],
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


def list_dirs(host: str, user: str, path: str) -> list[str]:
    """List immediate subdirectory names of a remote path. Returns sorted basenames."""
    cmd = (
        f"find {shlex.quote(path)} -maxdepth 1 -mindepth 1 -type d "
        r"-printf '%f\n' | sort"
    )
    out = run_remote(host, user, cmd)
    return [line for line in out.splitlines() if line]
