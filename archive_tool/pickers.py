from dataclasses import dataclass
from pathlib import Path

import questionary
import typer

from archive_tool.config import ArchiveQueue


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
