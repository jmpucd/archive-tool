#!/usr/bin/env python3
"""Box share poller — deployed only to CentOS, run every 2 minutes by cron.

Reads the turn-in Google Sheet and reconciles Box sharing:
  - share:   Share on Box checked, Shared date empty, Status "archived, not shared"
  - unshare: Status "remove", OR Share on Box unchecked while a Box path is present

Status doubles as a lock: a row is flipped to "sharing in progress" before the slow
rclone/Box work so the next tick doesn't double-process it. Any error parks the row at
"error: <reason>" and moves on — no automatic retry (brief Step 5).

Run from the repo root on CentOS:  uv run poller/box_share_poller.py
Cron:  */2 * * * * cd /path/to/archive-tool && uv run poller/box_share_poller.py >> /var/log/box-poller.log 2>&1
"""

import sys
from datetime import datetime

from archive_tool import boxops
from archive_tool import config as config_mod
from archive_tool import sheet

STATUS_ARCHIVED = "archived, not shared"
STATUS_SHARING = "sharing in progress"
STATUS_SHARED = "shared"
STATUS_REMOVE = "remove"


def main() -> int:
    try:
        cfg = config_mod.load_config()
    except config_mod.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if cfg.google is None or cfg.box is None:
        print("error: poller requires [google] and [remote.box] in config", file=sys.stderr)
        return 2

    ws = sheet.open_worksheet(cfg.google)
    for row_num, rec in sheet.read_rows(ws):
        if _wants_share(rec):
            _process_share(ws, row_num, rec, cfg)
        elif _wants_unshare(rec):
            _process_unshare(ws, row_num, rec, cfg)
    return 0


def _wants_share(rec: dict) -> bool:
    return (
        _truthy(rec.get("Share on Box"))
        and not _text(rec.get("Shared date"))
        and _text(rec.get("Status")) == STATUS_ARCHIVED
    )


def _wants_unshare(rec: dict) -> bool:
    if _text(rec.get("Status")) == STATUS_REMOVE:
        return True
    return not _truthy(rec.get("Share on Box")) and bool(_text(rec.get("Box path")))


def _process_share(ws, row_num: int, rec: dict, cfg: config_mod.Config) -> None:
    # Lock first so a concurrent tick skips this row while rclone runs.
    sheet.update_fields(ws, row_num, {"Status": STATUS_SHARING})
    try:
        emails = _parse_emails(rec.get("Share with"))
        if not emails:
            raise ValueError("'Share on Box' is checked but 'Share with' is empty")

        centos_path = _text(rec.get("CentOS path"))
        if not centos_path:
            raise ValueError("row has no CentOS path to copy from")
        project = _text(rec.get("Project name")) or centos_path.rstrip("/").split("/")[-1]
        box_dest = f"{cfg.box.base_folder.rstrip('/')}/{project}"

        boxops.rclone_copy(centos_path, cfg.box.rclone_remote, box_dest)
        client = boxops.client_as_user(cfg.box)
        folder_id = boxops.resolve_folder_id(client, box_dest)
        boxops.add_collaborators(client, folder_id, emails, cfg.box.collaborator_role)

        sheet.update_fields(
            ws,
            row_num,
            {
                "Box path": f"{cfg.box.rclone_remote}{box_dest}",
                "Shared date": f"{datetime.now():%Y-%m-%d}",
                "Status": STATUS_SHARED,
            },
        )
        print(f"row {row_num}: shared {project} with {', '.join(emails)}")
    except Exception as e:
        sheet.update_fields(ws, row_num, {"Status": f"error: {e}"})
        print(f"row {row_num}: error sharing: {e}", file=sys.stderr)


def _process_unshare(ws, row_num: int, rec: dict, cfg: config_mod.Config) -> None:
    try:
        box_path = _text(rec.get("Box path")).replace(cfg.box.rclone_remote, "", 1)
        client = boxops.client_as_user(cfg.box)
        folder_id = boxops.resolve_folder_id(client, box_path)
        removed = boxops.remove_collaborators(client, folder_id)  # leaves the files
        sheet.update_fields(
            ws,
            row_num,
            {"Status": STATUS_ARCHIVED, "Box path": "", "Shared date": ""},
        )
        print(f"row {row_num}: unshared ({removed} collaborator(s) removed)")
    except Exception as e:
        sheet.update_fields(ws, row_num, {"Status": f"error: {e}"})
        print(f"row {row_num}: error unsharing: {e}", file=sys.stderr)


def _truthy(value) -> bool:
    return str(value).strip().upper() in ("TRUE", "YES", "1", "CHECKED")


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _parse_emails(value) -> list[str]:
    return [e.strip() for e in str(value or "").replace(";", ",").split(",") if e.strip()]


if __name__ == "__main__":
    sys.exit(main())
