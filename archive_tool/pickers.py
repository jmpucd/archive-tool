import re
from dataclasses import dataclass
from pathlib import Path

import questionary
import typer

from archive_tool import collaborators, ssh
from archive_tool.config import ArchiveQueue

NEW_COLLECTION_LABEL = "+ new collection"
ADD_EMAIL_VALUE = "__add_new_email__"


@dataclass(frozen=True)
class Project:
    label: str   # drive label from config
    name: str    # project folder name
    path: Path   # absolute path to the project folder


def scan_archive_queues(queues: list[ArchiveQueue]) -> list[Project]:
    """Scan all configured archive queues, returning a flat list of projects.

    Silently skips queues whose path doesn't exist (drive not mounted).
    Warns and skips queues whose path exists but lacks the `.archive-source` marker.
    """
    projects: list[Project] = []
    for q in queues:
        if not q.path.exists():
            continue
        if not (q.path / ".archive-source").exists():
            typer.echo(
                f"warning: {q.path} has no .archive-source marker, skipping",
                err=True,
            )
            continue
        for child in sorted(q.path.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                projects.append(Project(label=q.label, name=child.name, path=child))
    return projects


def pick_project(projects: list[Project]) -> Project | None:
    """Show an arrow-key picker with search-as-you-type. Returns None if nothing picked."""
    choices = [
        questionary.Choice(title=f"[{p.label}] {p.name}", value=p)
        for p in projects
    ]
    return questionary.select(
        "Pick a project to archive",
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()


def pick_collection_path(
    host: str, user: str, root: str, auto_creates: bool = False
) -> str | None:
    """Two-level remote picker over SSH.

    Lists top-level dirs under root. If the picked dir matches `*-Collections`,
    recurses one level and offers a "new collection" option. Otherwise returns the
    picked top-level path directly. Returns None if the user cancels.

    Does not create any directories itself. If "new collection" is chosen, the path is
    returned along with a stderr note — either that it'll be auto-created on transfer
    (auto_creates=True, e.g. CentOS's organic tree), or that the user must mkdir it
    manually first (auto_creates=False, e.g. basil, which never auto-spawns collections).
    """
    parents = ssh.list_dirs(host, user, root)
    if not parents:
        typer.echo(
            f"No directories found at {root} on {host}. Nothing to pick.",
            err=True,
        )
        return None

    parent = questionary.select(
        f"Pick a destination folder under {root}",
        choices=parents,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()
    if parent is None:
        return None

    parent_path = f"{root.rstrip('/')}/{parent}"
    if not parent.endswith("-Collections"):
        return parent_path

    children = ssh.list_dirs(host, user, parent_path)
    choices = children + [NEW_COLLECTION_LABEL]
    child = questionary.select(
        f"Pick a collection in {parent}",
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()
    if child is None:
        return None

    if child == NEW_COLLECTION_LABEL:
        return _prompt_new_collection(host, user, parent, parent_path, auto_creates)

    return f"{parent_path}/{child}"


def pick_share_recipients() -> list[str] | None:
    """Checklist of frequent collaborators + an inline 'add new email' option.

    Returns the chosen emails (possibly empty), or None if the user cancels. Newly
    typed emails are saved to the collaborator store so they appear next time.
    """
    choices = [
        questionary.Choice(title=c.label(), value=c.email) for c in collaborators.load()
    ]
    choices.append(questionary.Choice(title="+ add a new email", value=ADD_EMAIL_VALUE))
    selected = questionary.checkbox(
        "Share with (space to toggle, enter to confirm; leave empty for none)",
        choices=choices,
    ).ask()
    if selected is None:
        return None

    emails = [s for s in selected if s != ADD_EMAIL_VALUE]
    if ADD_EMAIL_VALUE in selected:
        added = _prompt_new_emails()
        if added is None:
            return None
        emails.extend(added)

    seen: set[str] = set()
    return [e for e in emails if not (e in seen or seen.add(e))]


def _prompt_new_emails() -> list[str] | None:
    raw = questionary.text(
        "New email(s), comma-separated (saved for next time):"
    ).ask()
    if raw is None:
        return None
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        collab, was_new = collaborators.add(part)
        if collab is None:
            typer.echo(f"  skipped (no email found): {part}", err=True)
        else:
            out.append(collab.email)
    return out


def _prompt_new_collection(
    host: str, user: str, parent: str, parent_path: str, auto_creates: bool
) -> str | None:
    prefix = parent.removesuffix("-Collections")
    # Base ID (digits) plus optional appended name(s), e.g. D-492 or D-492_Snyder or
    # D-738_Chicago_Cafe — an underscore-joined suffix after the number, not a dash.
    pattern = re.compile(rf"^{re.escape(prefix)}-\d+(_[A-Za-z0-9]+)*$")

    def validate(v: str) -> bool | str:
        return (
            True
            if pattern.match(v)
            else f"must look like {prefix}-NNN or {prefix}-NNN_Name (digits, optional _name)"
        )

    new_id = questionary.text(
        f"New collection ID (e.g. {prefix}-450 or {prefix}-450_Name):",
        validate=validate,
    ).ask()
    if new_id is None:
        return None

    new_path = f"{parent_path}/{new_id}"
    if auto_creates:
        typer.echo(f"\nNote: {new_path} doesn't exist yet on {host} — it'll be created automatically.", err=True)
    else:
        typer.echo(
            f"\nNote: {new_path} does not exist yet on {host}.\n"
            f"Create it manually before transferring:\n"
            f"  ssh {user}@{host} mkdir {new_path}\n",
            err=True,
        )
    return new_path
