import shutil
from datetime import datetime
from pathlib import Path

import typer

from archive_tool import box_upload
from archive_tool import checksums
from archive_tool import collaborators as collaborators_mod
from archive_tool import config as config_mod
from archive_tool import jpeg as jpeg_mod
from archive_tool import ocr as ocr_mod
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
        _report_no_projects(cfg)
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


@app.command(name="ocr")
def ocr_command(
    project: str = typer.Option(
        "", "--project", "-p",
        help="project name as logged in the Sheet (skips the picker)",
    ),
    centos_path: str = typer.Option(
        "", "--centos-path",
        help="OCR this CentOS project folder directly (no Sheet lookup, nothing logged)",
    ),
    workers: int = typer.Option(
        ocr_mod.DEFAULT_WORKERS, "--workers", help="parallel pages on CentOS"
    ),
    max_px: int = typer.Option(
        ocr_mod.DEFAULT_MAX_PX, "--max-px", help="longest page edge (px) in the PDF"
    ),
    min_conf: float = typer.Option(
        ocr_mod.DEFAULT_MIN_CONF, "--min-conf",
        help="drop Tesseract words below this confidence (0-100) from PDF layer + transcript",
    ),
    box: bool | None = typer.Option(
        None, "--box/--no-box",
        help="copy the OCR files to Box (default: yes if the project is on Box, else ask)",
    ),
    skip_basil: bool = typer.Option(
        False, "--skip-basil", help="don't pull to basil this run (e.g. basil unreachable); re-run later"
    ),
    force: bool = typer.Option(
        False, "--force", help="re-run OCR even if the outputs already exist on CentOS"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="skip prompts (existing outputs are reused unless --force)"
    ),
) -> None:
    """Build a searchable PDF + paged OCR transcript for an archived project.

    Runs digtk (Tesseract) on CentOS against the masters copy, writes
    <project>.pdf and <project>_transcript.txt INTO the project folder there, then
    pulls them to basil and copies them to Box wherever that project already lives,
    and records them in the Derivatives columns of the project's Sheet row.
    """
    cfg = _load_config()
    if cfg.centos is None:
        typer.echo("error: [remote.centos] is required for OCR", err=True)
        raise typer.Exit(2)

    row: dict = {}
    if centos_path:
        centos_final = centos_path.rstrip("/")
        project_name = centos_final.rsplit("/", 1)[-1]
        basil_final = ""
        box_path = ""
    else:
        if cfg.google is None:
            typer.echo(
                "error: no [google] section; use --centos-path to OCR a folder "
                "without the Sheet",
                err=True,
            )
            raise typer.Exit(2)
        row = _pick_row(cfg, project, "Pick an archived project to OCR")
        project_name = str(row["Project name"])
        centos_final = str(row["CentOS path"])
        basil_final = str(row["Basil path"])
        box_path = str(row["Box path"])

    if not ssh.path_exists(cfg.centos.host, cfg.centos.user, centos_final):
        typer.echo(f"error: {centos_final} not found on {cfg.centos.host}", err=True)
        raise typer.Exit(1)

    d = ocr_mod.derivative_names(project_name)

    # Decide everything up front, then run unattended (same shape as the archive flow).
    rerun = True
    if ocr_mod.outputs_exist(cfg.centos, centos_final, d):
        rerun = force or (not yes and typer.confirm(
            f"\n{d.pdf} already exists on CentOS. Re-run OCR and overwrite it?",
            default=False,
        ))
    send_basil = bool(basil_final) and not skip_basil
    if send_basil and cfg.basil is None:
        typer.echo("error: project is on basil but [remote.basil] isn't configured", err=True)
        raise typer.Exit(2)
    box_target = _decide_box(cfg, box, box_path, project_name, yes, what="the OCR files")

    typer.echo()
    typer.echo("Plan:")
    typer.echo(f"  project:        {project_name}")
    typer.echo(f"  masters:        {cfg.centos.user}@{cfg.centos.host}:{centos_final}")
    typer.echo(
        "  OCR on CentOS:  "
        + (f"run digtk ({workers} workers)" if rerun else "skip, reuse existing outputs")
    )
    typer.echo(f"  outputs:        {d.pdf}, {d.transcript}  (inside the project folder)")
    typer.echo(
        "  basil:          "
        + (basil_final if send_basil else
           ("(skipped this run)" if basil_final else "(project not on basil - skipped)"))
    )
    typer.echo(f"  box:            {box_target or '(skipped)'}")
    typer.echo(f"  sheet:          {'update row ' + str(row['_row']) if row else '(not logged)'}")
    typer.echo()
    if not yes and not typer.confirm("Proceed?", default=False):
        typer.echo("aborted.")
        raise typer.Exit(1)

    where: list[str] = []
    try:
        if rerun:
            typer.echo("\n[ocr] running digtk on CentOS...")
            ocr_mod.run_on_centos(
                cfg.centos, centos_final, d, title=project_name,
                workers=workers, max_px=max_px, min_conf=min_conf,
            )
        where.append("centos")
        if send_basil:
            typer.echo("\n[basil] pulling OCR files CentOS -> basil (+ md5 verify)...")
            ocr_mod.pull_to_basil(cfg.centos, cfg.basil, centos_final, basil_final, d)
            typer.echo("  ok")
            where.append("basil")
        if box_target:
            typer.echo(f"\n[box] rclone CentOS -> {box_target}...")
            ocr_mod.copy_to_box(cfg.centos, cfg.box, centos_final, box_target, d)
            typer.echo("  ok")
            where.append("box")
    except (ocr_mod.OCRError, ssh.SSHError) as e:
        typer.echo(f"\nerror: {e}", err=True)
        if where:
            typer.echo(f"  OCR files are in place on: {', '.join(where)}", err=True)
            _log_derivatives(row, d.names, where, box_target if "box" in where else "")
        raise typer.Exit(4)

    _log_derivatives(row, d.names, where, box_target if "box" in where else "")

    typer.echo()
    typer.echo("done.")
    typer.echo(f"  centos:  {centos_final}/{d.pdf}")
    if "basil" in where:
        typer.echo(f"  basil:   {basil_final}/{d.pdf}")
    if "box" in where:
        typer.echo(f"  box:     {box_target}/{d.pdf}")
    typer.echo(f"  + {d.transcript} alongside each")


@app.command(name="jpeg")
def jpeg_command(
    project: str = typer.Option(
        "", "--project", "-p", help="project name as logged in the Sheet (skips the picker)"
    ),
    match: str = typer.Option(
        "_recto", "--match", help="only convert page files whose name contains this ('' = all)"
    ),
    quality: int = typer.Option(jpeg_mod.DEFAULT_QUALITY, "--quality", "-q", help="JPEG quality"),
    subdir: str = typer.Option(jpeg_mod.DEFAULT_SUBDIR, "--subdir", help="output folder name inside the project"),
    workers: int = typer.Option(jpeg_mod.DEFAULT_WORKERS, "--workers", help="parallel conversions on CentOS"),
    box: bool | None = typer.Option(
        None, "--box/--no-box",
        help="copy the JPEGs to Box (default: yes if the project is on Box, else ask)",
    ),
    skip_basil: bool = typer.Option(
        False, "--skip-basil", help="don't pull to basil this run (e.g. basil unreachable); re-run later"
    ),
    force: bool = typer.Option(False, "--force", help="reconvert even if JPEGs already exist on CentOS"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip prompts (existing JPEGs are kept unless --force)"),
) -> None:
    """Full-resolution JPEG access copies (default: rectos only, quality 80).

    Converts on CentOS into <project>/JPEG/, then pulls that folder to basil and copies
    it to Box wherever the project already lives, and records it on the Sheet row.
    """
    cfg = _load_config()
    if cfg.centos is None or cfg.google is None:
        typer.echo("error: jpeg needs [remote.centos] and [google] in config", err=True)
        raise typer.Exit(2)
    row = _pick_row(cfg, project, "Pick an archived project to make JPEGs for")
    project_name = str(row["Project name"])
    centos_final = str(row["CentOS path"])
    basil_final = str(row["Basil path"])
    box_path = str(row["Box path"])
    if not ssh.path_exists(cfg.centos.host, cfg.centos.user, centos_final):
        typer.echo(f"error: {centos_final} not found on {cfg.centos.host}", err=True)
        raise typer.Exit(1)

    existing = jpeg_mod.list_outputs(cfg.centos, centos_final, subdir)
    reconvert = True
    if existing:
        reconvert = force or (not yes and typer.confirm(
            f"\n{subdir}/ already holds {len(existing)} JPEGs on CentOS. Reconvert them?",
            default=False,
        ))
    send_basil = bool(basil_final) and not skip_basil
    if send_basil and cfg.basil is None:
        typer.echo("error: project is on basil but [remote.basil] isn't configured", err=True)
        raise typer.Exit(2)
    box_target = _decide_box(cfg, box, box_path, project_name, yes, what="the JPEGs")

    typer.echo()
    typer.echo("Plan:")
    typer.echo(f"  project:        {project_name}")
    typer.echo(f"  masters:        {cfg.centos.user}@{cfg.centos.host}:{centos_final}")
    typer.echo(
        f"  convert:        {'files matching ' + repr(match) if match else 'all pages'}"
        f" -> {subdir}/*.jpg, quality {quality}, full resolution"
        + ("" if reconvert else "  (existing JPEGs kept)")
    )
    typer.echo(f"  basil:          {basil_final + '/' + subdir if send_basil else ('(skipped this run)' if basil_final else '(project not on basil - skipped)')}")
    typer.echo(f"  box:            {box_target + '/' + subdir if box_target else '(skipped)'}")
    typer.echo(f"  sheet:          update row {row['_row']}")
    typer.echo()
    if not yes and not typer.confirm("Proceed?", default=False):
        typer.echo("aborted.")
        raise typer.Exit(1)

    where: list[str] = []
    names: list[str] = existing
    label = ""
    try:
        if reconvert or not existing:
            typer.echo("\n[jpeg] converting on CentOS...")
            jpeg_mod.run_on_centos(
                cfg.centos, centos_final, subdir=subdir, match=match, quality=quality,
                workers=workers, overwrite=reconvert,
            )
            names = jpeg_mod.list_outputs(cfg.centos, centos_final, subdir)
        if not names:
            raise ocr_mod.OCRError("no JPEGs were produced")
        label = f"{subdir}/ ({len(names)} {match.strip('_') or 'page'} JPEG q{quality})"
        where.append("centos")
        if send_basil:
            typer.echo(f"\n[basil] pulling {subdir}/ CentOS -> basil (+ md5 verify)...")
            jpeg_mod.pull_dir_to_basil(cfg.centos, cfg.basil, centos_final, basil_final, subdir, names)
            typer.echo("  ok")
            where.append("basil")
        if box_target:
            typer.echo(f"\n[box] rclone CentOS -> {box_target}/{subdir}...")
            jpeg_mod.copy_dir_to_box(cfg.centos, centos_final, box_target, subdir)
            typer.echo("  ok")
            where.append("box")
    except (ocr_mod.OCRError, ssh.SSHError) as e:
        typer.echo(f"\nerror: {e}", err=True)
        if where:
            typer.echo(f"  JPEGs are in place on: {', '.join(where)}", err=True)
            _log_derivatives(row, [label], where, box_target if "box" in where else "")
        raise typer.Exit(4)

    _log_derivatives(row, [label], where, box_target if "box" in where else "")
    typer.echo()
    typer.echo("done.")
    typer.echo(f"  centos:  {centos_final}/{subdir}/  ({len(names)} files)")
    if "basil" in where:
        typer.echo(f"  basil:   {basil_final}/{subdir}/")
    if "box" in where:
        typer.echo(f"  box:     {box_target}/{subdir}/")


def _pick_row(cfg: config_mod.Config, project: str, prompt: str) -> dict:
    """Resolve a Sheet row by --project or the picker. Exits on error/cancel."""
    try:
        ws = sheet.open_worksheet(cfg.google)
        rows = sheet.list_projects(ws)
    except sheet.SheetError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(3)
    if not rows:
        typer.echo("error: the Sheet has no archived projects yet", err=True)
        raise typer.Exit(1)
    if project:
        matches = [r for r in rows if r["Project name"] == project]
        if not matches:
            typer.echo(f"error: no Sheet row with Project name {project!r}", err=True)
            raise typer.Exit(1)
        row = matches[0]
    else:
        row = pickers.pick_sheet_project(rows, prompt=prompt)
        if row is None:
            raise typer.Exit(130)
    row["_ws"] = ws
    return row


def _decide_box(
    cfg: config_mod.Config, box: bool | None, box_path: str, project_name: str, yes: bool,
    what: str,
) -> str:
    """Box target for derivatives ('' = skip): the row's Box path if any, else ask."""
    if box is None:
        if cfg.box is None:
            return ""
        if box_path:
            return box_path
        target = ocr_mod.box_target_for(cfg.box, project_name)
        if yes or typer.confirm(f"\nProject isn't on Box. Put just {what} in {target}?", default=True):
            return target
        return ""
    if not box:
        return ""
    if cfg.box is None:
        typer.echo("error: --box needs [remote.box] in config", err=True)
        raise typer.Exit(2)
    return box_path or ocr_mod.box_target_for(cfg.box, project_name)


def _log_derivatives(row: dict, items: list[str], where: list[str], new_box_path: str) -> None:
    """Merge derivative entries into the row's Derivatives columns. Best-effort."""
    ws = row.get("_ws")
    if ws is None or not where:
        return
    typer.echo("\n[log] updating Sheet row...")
    have = [s.strip() for s in str(row.get("Derivatives", "")).split(",") if s.strip()]
    for it in items:
        if it and it not in have:
            have.append(it)
    on = [s.strip() for s in str(row.get("Derivatives on", "")).split(",") if s.strip()]
    for w in where:
        if w not in on:
            on.append(w)
    order = {"centos": 0, "basil": 1, "box": 2}
    on.sort(key=lambda w: order.get(w, 9))
    fields = {
        "Derivatives": ", ".join(have),
        "Derivatives on": ", ".join(on),
        "Derivatives date": f"{datetime.now():%Y-%m-%d %H:%M}",
    }
    if new_box_path and not row.get("Box path"):
        fields["Box path"] = new_box_path
    try:
        sheet.update_fields(ws, row["_row"], fields)
        typer.echo("  logged.")
    except sheet.SheetError as e:
        typer.echo(f"  warning: sheet update failed (files ARE in place): {e}", err=True)


@app.command(name="to-basil")
def to_basil_command(
    project: str = typer.Option(
        "", "--project", "-p", help="project name as logged in the Sheet (skips the picker)"
    ),
    parent: str = typer.Option(
        "", "--parent", help="existing basil collection folder to file it under (skips the picker)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Send an already-archived project to basil after the fact.

    For projects archived with "Also send to basil?" = no. basil pulls the whole
    project folder from the CentOS masters copy (TIFFs, manifest, and any OCR files),
    verifies the MD5 manifest there, and fills in the row's Basil path.
    """
    cfg = _load_config()
    if cfg.centos is None or cfg.basil is None or cfg.google is None:
        typer.echo(
            "error: to-basil needs [remote.centos], [remote.basil] and [google] in config",
            err=True,
        )
        raise typer.Exit(2)
    if not cfg.centos.host_from_basil:
        typer.echo(
            "error: [remote.centos].host_from_basil is not set; basil can't pull from CentOS",
            err=True,
        )
        raise typer.Exit(2)

    try:
        ws = sheet.open_worksheet(cfg.google)
        rows = sheet.list_projects(ws)
    except sheet.SheetError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(3)
    candidates = [r for r in rows if not r.get("Basil path")]
    if project:
        matches = [r for r in rows if r["Project name"] == project]
        if not matches:
            typer.echo(f"error: no Sheet row with Project name {project!r}", err=True)
            raise typer.Exit(1)
        row = matches[0]
        if row.get("Basil path"):
            typer.echo(f"error: {project} is already on basil: {row['Basil path']}", err=True)
            raise typer.Exit(1)
    else:
        if not candidates:
            typer.echo("Every logged project already has a Basil path. Nothing to do.")
            raise typer.Exit(0)
        row = pickers.pick_sheet_project(candidates, prompt="Pick a project to send to basil")
        if row is None:
            raise typer.Exit(130)
    project_name = str(row["Project name"])
    centos_final = str(row["CentOS path"])

    if not ssh.path_exists(cfg.centos.host, cfg.centos.user, centos_final):
        typer.echo(f"error: {centos_final} not found on {cfg.centos.host}", err=True)
        raise typer.Exit(1)

    if parent:
        basil_parent = parent.rstrip("/")
    else:
        try:
            basil_parent = pickers.pick_collection_path(
                cfg.basil.host, cfg.basil.user, cfg.basil.uploads_root
            )
        except ssh.SSHError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(3)
        if basil_parent is None:
            raise typer.Exit(130)
    if not ssh.path_exists(cfg.basil.host, cfg.basil.user, basil_parent):
        typer.echo(f"error: {basil_parent} does not exist on basil. Create it manually first.", err=True)
        raise typer.Exit(2)
    basil_final = f"{basil_parent}/{project_name}"
    if ssh.path_exists(cfg.basil.host, cfg.basil.user, basil_final):
        typer.echo(f"note: {basil_final} already exists on basil; rsync will fill in what's missing.")

    d = ocr_mod.derivative_names(project_name)
    has_ocr = ocr_mod.outputs_exist(cfg.centos, centos_final, d)

    typer.echo()
    typer.echo("Plan:")
    typer.echo(f"  project:        {project_name}")
    typer.echo(f"  from (centos):  {cfg.centos.user}@{cfg.centos.host_from_basil}:{centos_final}")
    typer.echo(f"  to (basil):     {cfg.basil.user}@{cfg.basil.host}:{basil_final}")
    typer.echo(f"  includes OCR:   {'yes (' + d.pdf + ', ' + d.transcript + ')' if has_ocr else 'no OCR files on CentOS'}")
    typer.echo(f"  sheet:          set Basil path on row {row['_row']}")
    typer.echo()
    if not yes and not typer.confirm("Proceed?", default=False):
        typer.echo("aborted.")
        raise typer.Exit(1)

    try:
        typer.echo("\n[1/2] rsync CentOS -> basil (pull on basil)...")
        transfer.pull_from_remote(
            puller_host=cfg.basil.host,
            puller_user=cfg.basil.user,
            src_host=cfg.centos.host_from_basil,
            src_user=cfg.centos.user,
            src_path=centos_final,
            dest_path=basil_final,
            make_parents=True,
        )
        typer.echo("\n[2/2] verifying manifest on basil...")
        transfer.verify_manifest_remote(cfg.basil.host, cfg.basil.user, basil_final)
        if has_ocr:
            ocr_mod.verify_copies(cfg.centos, centos_final, cfg.basil.host, cfg.basil.user, basil_final, d)
        typer.echo("  ok")
    except (transfer.TransferError, ssh.SSHError, ocr_mod.OCRError) as e:
        typer.echo(f"\nerror: {e}", err=True)
        raise typer.Exit(4)

    typer.echo("\n[log] updating Sheet row...")
    fields = {"Basil path": basil_final}
    if has_ocr:
        on = [s.strip() for s in str(row.get("Derivatives on", "")).split(",") if s.strip()]
        if "basil" not in on:
            on.insert(1 if on and on[0] == "centos" else 0, "basil")
        fields["Derivatives on"] = ", ".join(on)
    try:
        sheet.update_fields(ws, row["_row"], fields)
        typer.echo("  logged.")
    except sheet.SheetError as e:
        typer.echo(f"  warning: sheet update failed (files ARE on basil): {e}", err=True)

    typer.echo()
    typer.echo("done.")
    typer.echo(f"  basil archive:  {cfg.basil.user}@{cfg.basil.host}:{basil_final}")


def _report_no_projects(cfg: config_mod.Config) -> None:
    """Explain *why* nothing was found and where to put a project. Always exits."""
    typer.echo("No projects found in any mounted archive_queue.\n", err=True)
    typer.echo("Configured queues:", err=True)
    for q in cfg.local.archive_queue_paths:
        if not q.path.exists():
            state = "not mounted / does not exist - skipped"
        elif not (q.path / ".archive-source").exists():
            state = "missing .archive-source marker - skipped"
        else:
            state = "ready, but empty"
        typer.echo(f"  [{q.label}] {q.path}  ({state})", err=True)
    typer.echo(
        "\nA project is any folder placed directly inside a ready queue, e.g."
        f"\n  {cfg.local.archive_queue_paths[0].path}/MyProject/"
        "\nThe .archive-source marker belongs in the queue folder itself, not in each project."
        f"\n\nEdit archive_queue_paths in {cfg.source_path} to add or change queue locations.",
        err=True,
    )
    raise typer.Exit(1)


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
        _report_no_projects(cfg)
    source = pickers.pick_project(projects)
    if source is None:
        raise typer.Exit(130)

    # Ask up front whether this goes to basil at all — decides which tree we pick the
    # destination collection from below. basil is optional (some items aren't for
    # Special Collections); default yes.
    send_to_basil = yes or typer.confirm(
        "\nAlso send to basil (Special Collections)?", default=True
    )

    project_name = source.path.name
    basil_final = ""

    if send_to_basil:
        # Pick from basil's real tree. This sets the filing path used for BOTH the
        # CentOS masters copy and the basil copy, so both land in the same collection.
        try:
            basil_parent = pickers.pick_collection_path(
                cfg.basil.host, cfg.basil.user, cfg.basil.uploads_root
            )
        except ssh.SSHError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(3)
        if basil_parent is None:
            raise typer.Exit(130)

        # basil is the picker source here, so refuse a "+ new collection" path that
        # doesn't exist yet — we don't auto-spawn phantom collections there (the
        # picker printed a mkdir hint).
        if not ssh.path_exists(cfg.basil.host, cfg.basil.user, basil_parent):
            typer.echo(
                f"\nerror: {basil_parent} does not exist on basil. Create it manually first.",
                err=True,
            )
            raise typer.Exit(2)

        rel = _relpath(basil_parent, cfg.basil.uploads_root)
        basil_final = f"{basil_parent.rstrip('/')}/{project_name}"
    else:
        # Not going to basil at all, so pick the collection path from CentOS's own
        # tree instead. Unlike basil, CentOS's masters tree is organic and auto-mkdir's
        # on transfer, so a brand new collection there is never a "phantom folder"
        # problem — no existence check needed.
        try:
            centos_parent = pickers.pick_collection_path(
                cfg.centos.host, cfg.centos.user, cfg.centos.masters_root, auto_creates=True
            )
        except ssh.SSHError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(3)
        if centos_parent is None:
            raise typer.Exit(130)
        rel = _relpath(centos_parent, cfg.centos.masters_root)

    centos_final = f"{cfg.centos.masters_root.rstrip('/')}/{rel}/{project_name}"

    # Collect the Box decision up front too, so nothing after "Proceed?" is interactive:
    # every choice is made before the (long) rsync starts and the run finishes unattended.
    box_wanted, share_intent = _prompt_box(cfg, yes)

    # Same up-front treatment for source deletion: decide now, act only after every
    # destination this run actually uses has verified. Defaults to yes everywhere,
    # same as send_to_basil above - the whole point of this tool is freeing local
    # disk space once a project is safely archived.
    delete_source = yes or typer.confirm(
        "\nDelete the local source once it's verified on every destination above?",
        default=True,
    )

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
    if delete_source:
        typer.echo("  delete source:  yes, once verified everywhere above")
    else:
        typer.echo("  delete source:  no (kept locally)")
    typer.echo()
    if not yes and not typer.confirm("Proceed?", default=False):
        typer.echo("aborted.")
        raise typer.Exit(1)

    try:
        _execute_transfer(
            source.path, centos_final, basil_final, send_to_basil,
            box_wanted, share_intent, delete_source, cfg, yes,
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
    delete_source: bool,
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

    # Deletion is the last thing that happens, and only reachable here because every
    # destination the run actually used (CentOS always, basil above if selected) already
    # verified its manifest remotely without raising. Box upload/sheet logging are not a
    # precondition — they're best-effort and never gate whether the archive is safe.
    deleted = False
    if delete_source:
        typer.echo(
            f"\n[cleanup] source verified on centos"
            f"{' + basil' if logged_basil else ''}; deleting local copy..."
        )
        shutil.rmtree(source_path)
        deleted = True
        typer.echo(f"  removed {source_path}")

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
    typer.echo(f"  local source:      {'deleted' if deleted else source_path}")


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
