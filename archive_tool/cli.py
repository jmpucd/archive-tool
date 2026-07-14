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
    """Pick a destination collection folder on basil and print its path."""
    cfg = _load_config()
    if cfg.basil is None:
        typer.echo("error: [remote.basil] section not configured", err=True)
        raise typer.Exit(2)
    try:
        selected = pickers.pick_collection_path(
            cfg.basil.host, cfg.basil.user, cfg.basil.uploads_root
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
    if cfg.centos is None or cfg.basil is None:
        typer.echo(
            "error: archive flow requires both [remote.centos] and [remote.basil] in config",
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

    # Pick the destination collection from basil's real tree. This sets the filing path
    # used for BOTH the CentOS masters copy and (optionally) the basil copy.
    try:
        basil_parent = pickers.pick_collection_path(
            cfg.basil.host, cfg.basil.user, cfg.basil.uploads_root
        )
    except ssh.SSHError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(3)
    if basil_parent is None:
        raise typer.Exit(130)

    # basil is the picker source, so refuse a "+ new collection" path that doesn't exist
    # yet — we don't auto-spawn phantom collections there (the picker printed a mkdir hint).
    if not ssh.path_exists(cfg.basil.host, cfg.basil.user, basil_parent):
        typer.echo(
            f"\nerror: {basil_parent} does not exist on basil. Create it manually first.",
            err=True,
        )
        raise typer.Exit(2)

    project_name = source.path.name
    rel = _relpath(basil_parent, cfg.basil.uploads_root)
    centos_final = f"{cfg.centos.masters_root.rstrip('/')}/{rel}/{project_name}"
    basil_final = f"{basil_parent.rstrip('/')}/{project_name}"

    # basil is optional (some items aren't for Special Collections); default yes.
    send_to_basil = yes or typer.confirm(
        "\nAlso send to basil (Special Collections)?", default=True
    )

    # Collect the Box decision up front too, so nothing after "Proceed?" is interactive:
    # every choice is made before the (long) rsync starts and the run finishes unattended.
    box_wanted, share_intent = _prompt_box(cfg, yes)

    typer.echo()
    typer.echo("Plan:")
    typer.echo(f"  source:        {source.path}")
    typer.echo(f"  centos masters: {cfg.centos.user}@{cfg.centos.host}:{centos_final}")
    if send_to_basil:
        typer.echo(f"  basil archive:  {cfg.basil.user}@{cfg.basil.host}:{basil_final}")
    else:
        typer.echo("  basil archive:  (skipped)")
    if box_wanted:
        typer.echo(
            f"  box upload:     {cfg.box.rclone_remote}"
            f"{cfg.box.base_folder.rstrip('/')}/{source.path.name}"
        )
        typer.echo(f"  share manually: {share_intent or '(no one selected)'}")
    else:
        typer.echo("  box upload:     (skipped)")
    typer.echo()
    if not yes and not typer.confirm("Proceed?", default=False):
        typer.echo("aborted.")
        raise typer.Exit(1)

    try:
        _execute_transfer(
            source.path, centos_final, basil_final, send_to_basil,
            box_wanted, share_intent, cfg, yes,
        )
    except (transfer.TransferError, ssh.SSHError) as e:
        typer.echo(f"\nerror: {e}", err=True)
        raise typer.Exit(4)


def _relpath(full: str, root: str) -> str:
    """Path of `full` relative to `root` (both absolute). '' if they're the same dir."""
    full, root = full.rstrip("/"), root.rstrip("/")
    if full == root:
        return ""
    if full.startswith(root + "/"):
        return full[len(root) + 1:]
    return full.lstrip("/")  # defensive: shouldn't happen for a picked-under-root path


def _execute_transfer(
    source_path: Path,
    centos_final: str,
    basil_final: str,
    send_to_basil: bool,
    box_wanted: bool,
    share_intent: str,
    cfg: config_mod.Config,
    yes: bool,
) -> None:
    steps = 4 if send_to_basil else 2
    n = 0

    n += 1
    typer.echo(f"\n[{n}/{steps}] computing MD5 manifest...")
    manifest_path = checksums.write_manifest(source_path)
    typer.echo(f"  wrote {manifest_path}")

    n += 1
    typer.echo(f"\n[{n}/{steps}] rsync Mac -> CentOS masters (+ verify)...")
    transfer.push_to_remote(
        source_path, cfg.centos.host, cfg.centos.user, centos_final, make_parents=True
    )
    transfer.verify_manifest_remote(cfg.centos.host, cfg.centos.user, centos_final)
    typer.echo("  ok")

    logged_basil = ""
    if send_to_basil:
        n += 1
        if cfg.centos.host_from_basil:
            # basil pulls the just-landed CentOS masters copy (CentOS->basil is firewalled,
            # so a Mac->basil push is the only push that works — but a basil-side pull over
            # the open basil->CentOS path is rack-speed and avoids re-uploading from the Mac).
            typer.echo(f"\n[{n}/{steps}] rsync CentOS -> basil (pull on basil)...")
            transfer.pull_from_remote(
                puller_host=cfg.basil.host,
                puller_user=cfg.basil.user,
                src_host=cfg.centos.host_from_basil,
                src_user=cfg.centos.user,
                src_path=centos_final,
                dest_path=basil_final,
                make_parents=True,
            )
        else:
            typer.echo(f"\n[{n}/{steps}] rsync Mac -> basil archive...")
            transfer.push_to_remote(
                source_path, cfg.basil.host, cfg.basil.user, basil_final, make_parents=True
            )
        n += 1
        typer.echo(f"\n[{n}/{steps}] verifying manifest on basil...")
        transfer.verify_manifest_remote(cfg.basil.host, cfg.basil.user, basil_final)
        typer.echo("  ok")
        logged_basil = basil_final

    mc = checksums.manifest_checksum(manifest_path)

    # Box upload copies from the CentOS masters copy (always present). The decision and
    # share recipients were collected up front, so this step is non-interactive.
    box_path, share_with = _do_box_upload(
        source_path.name, centos_final, cfg, box_wanted, share_intent
    )

    typer.echo("\n[log] recording turn-in to Google Sheet...")
    _log_to_sheet(source_path, centos_final, logged_basil, mc, cfg, box_path, share_with)

    typer.echo()
    typer.echo("done.")
    typer.echo(f"  centos masters:    {cfg.centos.user}@{cfg.centos.host}:{centos_final}")
    if logged_basil:
        typer.echo(f"  basil archive:     {cfg.basil.user}@{cfg.basil.host}:{logged_basil}")
    if box_path:
        typer.echo(f"  box:               {box_path}")
        if share_with:
            typer.echo(f"  share manually with: {share_with}")
    typer.echo(f"  manifest checksum: {mc}")


def _prompt_box(cfg: config_mod.Config, yes: bool) -> tuple[bool, str]:
    """Ask up front whether to upload to Box and, if so, who to share with.

    Collected before the transfer so the rest of the run is unattended. Returns
    (box_wanted, share_with_csv). Sharing itself stays manual; we only record intent.
    """
    if cfg.box is None or yes:  # --yes is non-interactive; skip the optional Box prompt
        return False, ""
    if not typer.confirm("\nUpload to Box afterward?", default=False):
        return False, ""
    emails = pickers.pick_share_recipients() or []
    return True, ", ".join(emails)


def _do_box_upload(
    project: str,
    centos_final: str,
    cfg: config_mod.Config,
    box_wanted: bool,
    share_intent: str,
) -> tuple[str, str]:
    """Run the pre-approved Box upload (non-interactive). Returns (box_path, share_with).

    Both empty if Box was declined up front or the upload failed — the files are already
    archived regardless, so a Box failure never aborts the run.
    """
    if not box_wanted or cfg.box is None:
        return "", ""
    typer.echo("\n[box] rclone CentOS -> Box...")
    try:
        box_path = box_upload.upload_to_box(cfg.centos, centos_final, cfg.box, project)
    except box_upload.BoxUploadError as e:
        typer.echo(f"  warning: Box upload failed (project IS archived): {e}", err=True)
        return "", ""
    typer.echo(f"  uploaded to {box_path}")
    if share_intent:
        typer.echo(f"  will share manually with: {share_intent}")
    return box_path, share_intent


def _log_to_sheet(
    source_path: Path,
    centos_final: str,
    basil_final: str,
    manifest_checksum: str,
    cfg: config_mod.Config,
    box_path: str,
    share_with: str,
) -> None:
    """Append the turn-in row. Never fails the run — the files are already archived.

    Dedups on the CentOS masters path: if the project was already logged, update it with
    any new Box info rather than duplicating the row.
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
            basil_path=basil_final,
            manifest_checksum=manifest_checksum,
            box_path=box_path,
            share_with=share_with,
        )
        typer.echo("  logged.")
    except sheet.SheetError as e:
        typer.echo(f"  warning: sheet logging failed (files ARE archived): {e}", err=True)
