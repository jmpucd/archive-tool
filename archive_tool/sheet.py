"""Google Sheet turn-in log.

The sheet is the project's dashboard and the Box poller's control surface. This module
owns the schema (column order) and the append/find/update primitives. It opens the sheet
by ID with a service-account key; the sheet must be shared with the service account's
email as Editor.
"""

from datetime import datetime

import gspread
from gspread.utils import rowcol_to_a1

from archive_tool.config import GoogleConfig

# Column order IS the schema. The poller (Step 5) reads by header name, so renames here
# must stay in sync with it. "Basil path" replaces the brief's "Synology path" per the
# 2026-07 topology change (final files land on CentOS + basil, not Synology staging).
COLUMNS = [
    "Project ID",
    "Project name",
    "Source machine",
    "Source path",
    "CentOS path",
    "Basil path",
    "Status",
    "Archived date",
    "MD5 manifest checksum",
    "Share on Box",
    "Share with",
    "Box path",
    "Shared date",
]

STATUS_ARCHIVED = "archived, not shared"
STATUS_ON_BOX = "on box, share manually"  # uploaded to Box; John adds collaborators by hand

# 0-based column index of "Share on Box", used to attach checkbox data validation.
_SHARE_ON_BOX_COL = COLUMNS.index("Share on Box")


class SheetError(Exception):
    pass


def open_worksheet(google: GoogleConfig) -> "gspread.Worksheet":
    """Open the first worksheet of the configured spreadsheet, ready for header/append."""
    try:
        gc = gspread.service_account(filename=str(google.service_account_path))
        ws = gc.open_by_key(google.sheet_id).sheet1
    except Exception as e:  # gspread/google-auth raise a zoo of exception types
        raise SheetError(
            f"could not open sheet {google.sheet_id} with key "
            f"{google.service_account_path}: {e}"
        ) from e
    ensure_header(ws)
    return ws


def ensure_header(ws: "gspread.Worksheet") -> None:
    """Write the header row if the sheet is empty. Refuse to touch a mismatched header."""
    existing = ws.row_values(1)
    if existing == COLUMNS:
        return
    if existing:
        raise SheetError(
            "sheet header does not match the expected schema.\n"
            f"  expected: {COLUMNS}\n"
            f"  found:    {existing}\n"
            "Fix the sheet's first row (or start from an empty sheet) and retry."
        )
    ws.update(values=[COLUMNS], range_name="A1")
    # Trim the 999 empty default rows: otherwise reads return them as blank records and
    # appends land below the whole grid. Rows grow back naturally as projects append.
    ws.resize(rows=1)


def make_project_id() -> str:
    """Timestamp-based key, human-readable in the sheet and unique per turn-in.

    Idempotency across re-runs is handled by the caller keying on a stable natural
    identity (the CentOS destination path), not by this ID, which regenerates each run.
    """
    return f"{datetime.now():%Y%m%d-%H%M%S}"


def append_project(
    ws: "gspread.Worksheet",
    *,
    project_id: str,
    project_name: str,
    source_machine: str,
    source_path: str,
    centos_path: str,
    basil_path: str,
    manifest_checksum: str,
    box_path: str = "",
    share_with: str = "",
) -> None:
    """Append one turn-in row. If box_path is set, the project was uploaded to Box and
    the row is flagged for manual sharing (Shared date stays blank until John shares)."""
    on_box = bool(box_path)
    row = [
        project_id,
        project_name,
        source_machine,
        source_path,
        centos_path,
        basil_path,
        STATUS_ON_BOX if on_box else STATUS_ARCHIVED,
        f"{datetime.now():%Y-%m-%d %H:%M}",
        manifest_checksum,
        on_box,      # Share on Box (checkbox): ticked when uploaded
        share_with,  # Share with (emails John intends to share with)
        box_path,    # Box path
        "",          # Shared date (John fills when he actually shares)
    ]
    resp = ws.append_row(row, value_input_option="USER_ENTERED")
    _apply_checkbox_validation(ws, _appended_row_index(resp))


def find_row(ws: "gspread.Worksheet", column: str, value: str) -> int | None:
    """Return the 1-based row number where `column` holds `value`, or None. Skips header.

    Used two ways: the orchestrator dedups on 'CentOS path' (stable archive identity);
    the poller looks up 'Project ID'.
    """
    if column not in COLUMNS:
        raise SheetError(f"unknown column {column!r}; expected one of {COLUMNS}")
    cells = ws.col_values(COLUMNS.index(column) + 1)
    for i, cell in enumerate(cells[1:], start=2):
        if cell == value:
            return i
    return None


def update_fields(ws: "gspread.Worksheet", row: int, fields: dict[str, str]) -> None:
    """Write one or more named columns of a single row. Used by the poller for writeback
    (Status lock, Box path, Shared date). value_input_option=USER_ENTERED so dates/bools
    are interpreted, not stored as literal strings."""
    data = []
    for column, value in fields.items():
        if column not in COLUMNS:
            raise SheetError(f"unknown column {column!r}; expected one of {COLUMNS}")
        a1 = rowcol_to_a1(row, COLUMNS.index(column) + 1)
        data.append({"range": a1, "values": [[value]]})
    ws.batch_update(data, value_input_option="USER_ENTERED")


def _appended_row_index(append_response: dict) -> int:
    """Pull the 1-based row number out of an append_row response's updatedRange.

    updatedRange looks like "'Archive turn-in log'!A5:M5"; we want the 5.
    """
    rng = append_response["updates"]["updatedRange"]
    first_cell = rng.split("!")[-1].split(":")[0]  # e.g. "A5"
    digits = "".join(ch for ch in first_cell if ch.isdigit())
    return int(digits)


def _apply_checkbox_validation(ws: "gspread.Worksheet", row: int) -> None:
    """Make the 'Share on Box' cell of one row render as a real checkbox (TRUE/FALSE).

    Applied per-row rather than to the whole column so the sheet's used range doesn't
    balloon to the full grid (which pushes appends past the data and pads reads).
    """
    request = {
        "setDataValidation": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": _SHARE_ON_BOX_COL,
                "endColumnIndex": _SHARE_ON_BOX_COL + 1,
            },
            "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True},
        }
    }
    ws.spreadsheet.batch_update({"requests": [request]})
