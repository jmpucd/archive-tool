import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import questionary
import typer

from archive_tool import collaborators, ssh
from archive_tool.config import ArchiveQueue

NEW_COLLECTION_LABEL = "+ new collection"
FILE_HERE_LABEL = "» file it here"
FILE_HERE_VALUE = "__file_here__"
NEW_SUBFOLDER_LABEL = "+ new subfolder"
NEW_SUBFOLDER_VALUE = "__new_subfolder__"
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


def pick_sheet_project(
    rows: list[dict], prompt: str = "Pick an archived project to OCR"
) -> dict | None:
    """Pick an already-archived project from the Sheet's rows (newest first)."""
    choices = []
    for r in rows:
        ocr = f"  [{r['Derivatives']}]" if r.get("Derivatives") else ""
        choices.append(questionary.Choice(
            title=f"{r['Project name']}  ({r['Archived date']})  {r['CentOS path']}{ocr}",
            value=r,
        ))
    return questionary.select(
        prompt,
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()


def pick_collection_path(
    host: str, user: str, root: str, auto_creates: bool = False
) -> str | None:
    """Walk a remote tree over SSH until a filing folder is chosen. Returns None if the
    user cancels.

    The first level is always a folder pick (nothing is filed straight under root).
    From there the behaviour depends on the folder:

    * `*-Collections` (D-, MC-, AR-, O-): lists its collections plus "+ new collection".
      The picked collection is final - we never drill into a collection's own projects.
    * anything else (Books_and_Pamphlets, Maps, Serials, ...): lists its subfolders plus
      "file it here" and "+ new subfolder", and keeps walking down until the user files.

    Does not create any directories itself. If a new collection/subfolder is chosen, the
    path is returned along with a stderr note - either that it'll be auto-created on transfer
    (auto_creates=True, e.g. CentOS's organic tree), or that the user must mkdir it
    manually first (auto_creates=False, e.g. basil, which never auto-spawns collections).
    """
    root = root.rstrip("/")
    parents = ssh.list_dirs(host, user, root)
    if not parents:
        typer.echo(
            f"No directories found at {root} on {host}. Nothing to pick.",
            err=True,
        )
        return None

    name = _select(f"Pick a destination folder under {root}", parents)
    if name is None:
        return None
    path = f"{root}/{name}"

    while True:
        children = ssh.list_dirs(host, user, path)

        if name.endswith("-Collections"):
            child = _select(f"Pick a collection in {name}", children + [NEW_COLLECTION_LABEL])
            if child is None:
                return None
            if child == NEW_COLLECTION_LABEL:
                return _prompt_new_collection(host, user, name, path, auto_creates)
            return f"{path}/{child}"

        choices = [
            questionary.Choice(title=f"{FILE_HERE_LABEL} ({name}/)", value=FILE_HERE_VALUE),
            *children,
            questionary.Choice(title=NEW_SUBFOLDER_LABEL, value=NEW_SUBFOLDER_VALUE),
        ]
        child = _select(f"Pick a subfolder of {name}, or file it here", choices)
        if child is None:
            return None
        if child == FILE_HERE_VALUE:
            return path
        if child == NEW_SUBFOLDER_VALUE:
            return _prompt_new_subfolder(host, user, name, path, children, auto_creates)
        name = child
        path = f"{path}/{child}"


def _select(message: str, choices: list) -> str | None:
    """Arrow-key picker with search-as-you-type. None if the user cancels."""
    return questionary.select(
        message, choices=choices, use_search_filter=True, use_jk_keys=False
    ).ask()


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
    _note_new_dir(host, user, new_path, auto_creates)
    return new_path


def _prompt_new_subfolder(
    host: str, user: str, parent: str, parent_path: str, siblings: list[str],
    auto_creates: bool,
) -> str | None:
    """Name a not-yet-existing subfolder under a non-Collections folder (any name basil
    already uses is fair game - spaces and colons included - just no slashes)."""

    def validate(v: str) -> bool | str:
        v = v.strip()
        if not v:
            return "name can't be empty"
        if "/" in v or v in (".", ".."):
            return "must be a single folder name (no slashes)"
        if v.startswith("."):
            return "hidden folders (leading dot) aren't allowed"
        if v in siblings:
            return f"{v} already exists in {parent} - pick it from the list instead"
        return True

    new_name = questionary.text(f"New subfolder name under {parent}/:", validate=validate).ask()
    if new_name is None:
        return None
    new_path = f"{parent_path}/{new_name.strip()}"
    _note_new_dir(host, user, new_path, auto_creates)
    return new_path


def _note_new_dir(host: str, user: str, new_path: str, auto_creates: bool) -> None:
    if auto_creates:
        typer.echo(f"\nNote: {new_path} doesn't exist yet on {host} — it'll be created automatically.", err=True)
    else:
        typer.echo(
            f"\nNote: {new_path} does not exist yet on {host}.\n"
            f"Create it manually before transferring:\n"
            f"  ssh {user}@{host} mkdir {shlex.quote(new_path)}\n",
            err=True,
        )
