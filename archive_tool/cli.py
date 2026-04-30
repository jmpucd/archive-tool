import typer

from archive_tool import config as config_mod
from archive_tool import pickers

app = typer.Typer(
    add_completion=False,
    help="Archive a finished digitization project to the library archives.",
)


@app.command()
def main() -> None:
    """Pick a finished project from any mounted archive_queue and print its path."""
    try:
        config = config_mod.load_config()
    except config_mod.ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)

    projects = pickers.scan_archive_queues(config.local.archive_queue_paths)
    if not projects:
        typer.echo("No projects found in any mounted archive_queue.", err=True)
        raise typer.Exit(1)

    selected = pickers.pick_project(projects)
    if selected is None:
        raise typer.Exit(130)  # user aborted (Ctrl-C)

    typer.echo(str(selected.path))
