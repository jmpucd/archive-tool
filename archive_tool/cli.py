from pathlib import Path

import typer

from archive_tool import box_upload
from archive_tool import checksums
from archive_tool import collaborators as collaborators_mod
from archive_tool import config as config_mod
from archive_tool import pickers
from archive_tool import sheet
from archive_tool import ssh
from archive_tool import transfer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Archive a finished digitization project to the library archives.",
)


def _load_config() -> config_mod.Config:
    try:
        return config_mod.load_config()
    except config_mod.ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Run the full archive flow when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    _run_archive_flow(yes=yes)


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
        typer.echo("error: [remote.centos] section not configured", err=True)
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


@app.command(name="collaborators")
def list_collaborators() -> None:
    """List the frequent Box collaborators offered by the share picker."""
    for c in collaborators_mod.load():
        typer.echo(c.label())


@app.command(name="add-collaborator")
def add_collaborator(
    email: str = typer.Argument(..., help="email (accepts 'Name <email>' or mailto: forms)"),
    name: str = typer.Option("", "--name", "-n", help="display name shown in the picker"),
) -> None:
    """Add an email to the frequent-collaborators list."""
    collab, was_new = collaborators_mod.add(email, name)
    if collab is None:
        typer.echo(f"error: no email found in {email!r}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{'added' if was_new else 'already present'}: {collab.label()}")


def _run_archive_flow(yes: bool) -> None:
    cfg = _load_config()
    if cfg.synology is None or cfg.centos is None:
        typer.echo(
            "error: archive flow requires both [remote.synology] and [remote.centos] in config",
            err=True,
        )
        raise typer.Exit(2)

    # Pick source.
    projects = pickers.scan_archive_queues(cfg.local.archive_queue_paths)
    if not projects:
        typer.echo("No projects found in any mounted archive_queue.", err=True)
        raise typer.Exit(1)
    source = pickers.pick_project(projects)
    if source is None:
        raise typer.Exit(130)

    # Pick dest.
    try:
        dest_parent = pickers.pick_collection_path(
            cfg.centos.host, cfg.centos.user, cfg.centos.archives_root
        )
    except ssh.SSHError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(3)
    if dest_parent is None:
        raise typer.Exit(130)

    # If "+ new collection" produced a path that doesn't exist on CentOS yet, abort
    # with the same hint pickers already printed. We don't auto-mkdir collections.
    if not ssh.path_exists(cfg.centos.host, cfg.centos.user, dest_parent):
        typer.echo(
            f"\nerror: {dest_parent} does not exist on CentOS. Create it manually first.",
            err=True,
        )
        raise typer.Exit(2)

    project_name = source.path.name
    syn_staging = f"{cfg.synology.staging_dir.rstrip('/')}/{project_name}"
    centos_final = f"{dest_parent.rstrip('/')}/{project_name}"

    typer.echo()
    typer.echo("Plan:")
    typer.echo(f"  source:        {source.path}")
    typer.echo(f"  synology:      {cfg.synology.user}@{cfg.synology.host}:{syn_staging}")
    typer.echo(f"  centos final:  {cfg.centos.user}@{cfg.centos.host}:{centos_final}")
    typer.echo()
    if not yes and not typer.confirm("Proceed?", default=False):
        typer.echo("aborted.")
        raise typer.Exit(1)

    try:
        _execute_transfer(source.path, syn_staging, centos_final, dest_parent, cfg, yes)
    except (transfer.TransferError, ssh.SSHError) as e:
        typer.echo(f"\nerror: {e}", err=True)
        raise typer.Exit(4)


def _execute_transfer(
    source_path: Path,
    syn_staging: str,
    centos_final: str,
    dest_parent: str,
    cfg: config_mod.Config,
    yes: bool,
) -> None:
    typer.echo("\n[1/5] computing MD5 manifest...")
    manifest_path = checksums.write_manifest(source_path)
    typer.echo(f"  wrote {manifest_path}")

    typer.echo("\n[2/5] rsync laptop -> synology staging...")
    transfer.push_to_synology(source_path, cfg.synology)

    typer.echo("\n[3/5] verify manifest on synology...")
    transfer.verify_manifest_remote(cfg.synology.host, cfg.synology.user, syn_staging)
    typer.echo("  ok")

    typer.echo("\n[4/5] rsync synology -> centos archives...")
    transfer.push_synology_to_centos(syn_staging, cfg.synology, cfg.centos, dest_parent)

    typer.echo("\n[5/5] verify manifest on centos...")
    transfer.verify_manifest_remote(cfg.centos.host, cfg.centos.user, centos_final)
    typer.echo("  ok")

    mc = checksums.manifest_checksum(manifest_path)

    box_path, share_with = _maybe_upload_to_box(source_path.name, centos_final, cfg, yes)

    typer.echo("\n[log] recording turn-in to Google Sheet...")
    _log_to_sheet(source_path, centos_final, mc, cfg, box_path, share_with)

    typer.echo()
    typer.echo("done.")
    typer.echo(f"  centos:            {cfg.centos.user}@{cfg.centos.host}:{centos_final}")
    if box_path:
        typer.echo(f"  box:               {box_path}")
        if share_with:
            typer.echo(f"  share manually with: {share_with}")
    typer.echo(f"  manifest checksum: {mc}")


def _maybe_upload_to_box(
    project: str, centos_final: str, cfg: config_mod.Config, yes: bool
) -> tuple[str, str]:
    """Optionally rclone the archived project to Box and collect share recipients.

    Returns (box_path, share_with_csv), both empty if skipped or on failure. Sharing on
    Box is manual; this only uploads the files and records who to share with.
    """
    if cfg.box is None or yes:  # --yes is non-interactive; skip the optional Box prompt
        return "", ""
    if not typer.confirm("\nUpload to Box?", default=False):
        return "", ""
    try:
        box_path = box_upload.upload_to_box(cfg.centos, centos_final, cfg.box, project)
    except box_upload.BoxUploadError as e:
        typer.echo(f"  warning: Box upload failed (project IS archived): {e}", err=True)
        return "", ""
    emails = pickers.pick_share_recipients() or []
    typer.echo(f"  uploaded to {box_path}")
    if emails:
        typer.echo(f"  will share manually with: {', '.join(emails)}")
    return box_path, ", ".join(emails)


def _log_to_sheet(
    source_path: Path,
    centos_final: str,
    manifest_checksum: str,
    cfg: config_mod.Config,
    box_path: str,
    share_with: str,
) -> None:
    """Append the turn-in row. Never fails the run — the files are already archived.

    Dedups on the CentOS path: if the project was already logged, update it with any new
    Box info rather than duplicating the row.
    """
    if cfg.google is None:
        typer.echo("  skipped: no [google] section in config", err=True)
        return
    try:
        ws = sheet.open_worksheet(cfg.google)
        existing = sheet.find_row(ws, "CentOS path", centos_final)
        if existing is not None:
            if box_path:
                sheet.update_fields(
                    ws,
                    existing,
                    {
                        "Share on Box": True,
                        "Share with": share_with,
                        "Box path": box_path,
                        "Status": sheet.STATUS_ON_BOX,
                    },
                )
                typer.echo(f"  already logged; updated row {existing} with Box info")
            else:
                typer.echo(f"  already logged for {centos_final}; leaving existing row")
            return
        sheet.append_project(
            ws,
            project_id=sheet.make_project_id(),
            project_name=source_path.name,
            source_machine=cfg.local.hostname_label,
            source_path=str(source_path),
            centos_path=centos_final,
            basil_path="",  # TODO: populate once the basil transfer leg exists (task #6)
            manifest_checksum=manifest_checksum,
            box_path=box_path,
            share_with=share_with,
        )
        typer.echo("  logged.")
    except sheet.SheetError as e:
        typer.echo(f"  warning: sheet logging failed (files ARE archived): {e}", err=True)
