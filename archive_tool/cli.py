import typer

from archive_tool import config as config_mod
from archive_tool import pickers
from archive_tool import ssh

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Archive a finished digitization project to the library archives.",
)


def _load_config() -> config_mod.Config:
    try:
        return config_mod.load_config()
    except config_mod.ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)


@app.command(name="pick-source")
def pick_source() -> None:
    """Pick a source project from a local archive_queue and print its path."""
    cfg = _load_config()
    projects = pickers.scan_archive_queues(cfg.local.archive_queue_paths)
    if not projects:
        typer.echo("No projects found in any mounted archive_queue.", err=True)
        raise typer.Exit(1)
    selected = pickers.pick_project(projects)
    if selected is None:
        raise typer.Exit(130)
    typer.echo(str(selected.path))


@app.command(name="pick-dest")
def pick_dest() -> None:
    """Pick a destination collection folder on CentOS and print its path."""
    cfg = _load_config()
    if cfg.centos is None:
        typer.echo(
            "error: [remote.centos] section not configured in your config.toml",
            err=True,
        )
        raise typer.Exit(2)
    try:
        selected = pickers.pick_collection_path(
            cfg.centos.host, cfg.centos.user, cfg.centos.archives_root
        )
    except ssh.SSHError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(3)
    if selected is None:
        raise typer.Exit(130)
    typer.echo(selected)
