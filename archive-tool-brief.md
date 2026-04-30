# Archive Project Tool: Build Brief

A custom tool to push finished digitization projects from any of John's machines through the Synology, to the CentOS VM, to the Archives server, and onward to Box for collaborator sharing when needed. Tracks every project in a Google Sheet that doubles as the share-controls UI.

## Context

John is a Digitization Services Specialist at UC Davis Library. Finished projects currently get pushed to multiple destinations through a patchwork of one-off scripts on different machines. The goal is a single tool with one consistent interface that runs from any of his machines and handles the full pipeline.

## Architecture decisions (already settled)

These were worked through over a long design session. Don't revisit unless implementation reveals a real blocker.

### Laptop-as-orchestrator

The script runs on whatever machine the user launches it from, but in practice that will usually be the laptop. Tailscale provides flat networking so every machine (laptop, two Mac minis, Mac Studio, Synology, CentOS VM) is reachable by stable hostname. The orchestrator originates SSH connections to other machines as needed.

This means the Synology is no longer the orchestrator. It's a passive storage participant in the pipeline. The script does both legs of the rsync (laptop-to-Synology and Synology-to-CentOS) from the laptop. The benefit: real Python development environment, easy debugging, no fight with Synology's package management.

### Discipline-first source consolidation

The tool operates on one source location at a time. If a finished project is scattered across machines, the user is responsible for rsync'ing the pieces together onto one machine before running the tool. The tool does NOT try to assemble multi-machine projects.

This keeps the flow simple. Assembly and archiving are different cognitive tasks.

### Two-sided picking, not typing

The user never types paths. On the local side, the tool reads a per-machine config that defines a "finished" folder, lists what's there, and presents a picker. On the remote side, the tool SSHes to CentOS, lists existing collection folders live, and presents a picker. Picking from real lists eliminates typo failures.

### Polling, not webhooks

The Box-sharing mechanism is a poller that runs on CentOS every 2 minutes via cron. It checks the Google Sheet for rows marked "share me," does the work, writes status back. This sidesteps inbound firewall problems with campus IT and is dramatically simpler to debug than push architectures. Latency tolerance is high (a couple of minutes is invisible to the user).

### GitHub is the source of truth for all scripts

Every script in this system lives in one repo. Each machine clones it. `git pull` keeps machines in sync. The orchestrator and the poller share utility code through the same repo. Per-machine config files are gitignored.

## The user-facing workflow

### Phase 1: archiving a project (the common case)

User finishes a project, wants to send it to the archives.

1. User runs `archive-project` from any terminal on any machine.
2. Tool reads local config, finds the finished folder for this machine.
3. Tool lists contents of finished folder, presents arrow-key picker.
4. User picks a project.
5. Tool SSHes to CentOS, lists top-level folders in the archives location.
6. User picks a top-level (e.g., `MC-Collections`, or a named collection like `HarrisonCollection`).
7. If a `*-Collections` folder, tool lists numbered subfolders (e.g., `MC-117` through `MC-408`), plus an option for "new collection."
8. User picks the destination subfolder, or selects "new" and confirms the new collection name.
9. Tool shows resolved paths and asks for confirmation: source, Synology staging path, CentOS archives path.
10. On confirm, tool rsyncs source to Synology staging, then Synology to CentOS, then logs the project to the Google Sheet with status "archived, not shared."

### Phase 2: sharing a project on Box (deferred, often days/weeks later)

User gets asked to share an archived project with someone external via Box.

1. User opens the Google Sheet, finds the project's row.
2. User ticks the "Share on Box" checkbox, fills in the "Share with" cell with comma-separated email addresses.
3. Within 2 minutes, the cron poller on CentOS picks up the row.
4. Poller rclones the project from CentOS up to a Box folder, sets the listed emails as collaborators.
5. Poller writes back: Box path, Shared date, Status = "shared."
6. User sees the row updated next time they look at the sheet.

### Phase 3: unsharing (when collaborators no longer need access)

1. User unchecks the "Share on Box" checkbox, or sets Status to "remove."
2. Poller picks it up next cycle.
3. Poller removes Box collaboration (recommendation: just remove collaborators, leave files in Box for cheap re-share later, but confirm with John).
4. Poller writes back: Status = "archived, not shared," clears Box path, clears Shared date.

## Build order

Build incrementally. Each step should be runnable and testable on its own before moving to the next. Do not try to wire end-to-end before the pieces work.

### Step 0: repo setup

- Initialize git repo with sensible `.gitignore` (config files, secrets, logs, virtualenvs, OS junk).
- `pyproject.toml` with dependencies pinned.
- Minimal README with "what this is" and pointer to this brief.
- A `bin/` directory; the orchestrator entry point goes here as `archive-project`.
- A `config.example.toml` showing the structure machines need to populate.

### Step 1: local-side picker

- Use `typer` for the CLI layer. Define commands as type-hinted functions; let Typer handle argument parsing, help text, and validation. Avoid `argparse` or raw Click.
- Read `~/.config/archive-tool/config.toml` (or repo-local fallback for development).
- Resolve `archive_queue_paths` for the current machine. This is a list of folders, one per drive (internal or external) that may contain projects ready to archive.
- For each configured path:
  - Skip silently if the path doesn't exist (drive not mounted).
  - Skip with a warning if the path exists but lacks a `.archive-source` marker file (this prevents accidentally treating an unrelated folder named `archive_queue` as a valid source).
  - Otherwise, list its top-level subdirectories.
- Build a flat list of all projects across all valid mounted paths. Display each with a drive label prefix so duplicates and origin are visible. Example: `[WorkingDrive] MC-247_water_rights_maps`.
- Present the list via `questionary` for arrow-key navigation with search-as-you-type. Avoid heavy TUI frameworks like Textual; this is a one-shot prompt, not an app.
- Print the picked full path, exit.
- This is a complete, runnable thing that does nothing destructive. Test it on multiple machines and with various combinations of drives mounted/unmounted before moving on.

### Step 2: remote collection picker

- SSH to CentOS using the host defined in config (Tailscale hostname).
- Run `ls -1` on the archives root, filter to directories.
- Present picker for the parent folder.
- If parent matches `*-Collections`, recurse: list subfolders, present picker, plus a "new collection" option.
- If "new collection" picked, prompt for collection ID. Validate format against the prefix pattern (e.g., `MC-` followed by digits).
- Print the resolved destination path, exit.
- Still non-destructive. Test from multiple machines.

### Step 3: the transfer

- Wire steps 1 and 2 together.
- Confirmation step shows: source path, Synology staging path, CentOS archives path. Default to "no" on confirm prompt.
- Before any transfer: compute MD5 checksums for every file in the source project. Write a manifest in standard `md5sum` format (one line per file: `<md5hash>  <relative path>`). Save as `manifest.md5` at the project root.
- Implement the rsync chain:
  - rsync source to Synology staging with `--partial --append-verify` for resumability.
  - On Synology, recompute MD5s against the same file list. Compare to the manifest. Abort with a clear error if any file mismatches or is missing.
  - rsync Synology staging to CentOS archives path with same flags.
  - On CentOS, recompute MD5s and verify against the manifest again. Abort on mismatch.
- Exclude list: `@eaDir`, `lost+found`, `.DS_Store`, `Thumbs.db`, `*.tmp`.
- The `manifest.md5` file travels with the project. Final location on CentOS: `<project_path>/manifest.md5`.
- Compute one additional MD5 over the manifest file itself. This is the "manifest checksum" that gets written to the Sheet row, as a tamper-detection check (if the manifest is regenerated later, the sheet's stored checksum won't match).
- Use Python's `hashlib.md5` directly rather than shelling out to `md5sum`. Portable across macOS and Linux, gives progress callbacks for large files.
- Don't delete the Synology staging copy. It stays as a redundant backup.
- Test with a small dummy project first.

Why MD5 (not SHA-256): MD5 is roughly 2x faster, half the hash length, and is the standard for cultural-heritage fixity checking (LoC, NARA, most digitization shops). MD5's cryptographic weaknesses are about adversarial collision attacks, not random bit flips. Random corruption is what fixity checking actually detects, and MD5 is fine for that.

### Step 4: Google Sheet integration

- Use `gspread` with the service account JSON credentials.
- After successful transfer, append a row.
- Sheet columns, in order: Project ID, Project name, Source machine, Source path, Synology path, CentOS path, Status, Archived date, MD5 manifest checksum, Share on Box (checkbox), Share with (emails), Box path, Shared date.
- Project ID can be a UUID or a timestamp-based ID. Whatever's chosen, it's the key for finding rows later (idempotent re-runs, status updates from the poller).
- "MD5 manifest checksum" holds the MD5 of the `manifest.md5` file itself. Used for tamper detection during future fixity checks. Column header should explicitly say "MD5" to leave room for adding SHA-256 later if ever needed.

### Step 5: Box poller (separate program, runs on CentOS only)

This is a sibling tool, not part of the main orchestrator. Lives in the same repo, deployed only to CentOS.

- Reads the same Google Sheet via the same service account.
- For sharing: filters rows where Share on Box is true AND Shared date is empty AND Status is "archived, not shared."
- For each match:
  - Write Status = "sharing in progress" first. This acts as a lock so the next 2-minute tick doesn't double-process if rclone is still running.
  - Use rclone to copy from CentOS path to Box folder.
  - Set collaborators from the Share with cell. Investigate whether rclone's Box backend handles collaborator API calls or if a separate Box SDK call is needed.
  - Write back: Box path, Shared date, Status = "shared."
  - On any error, write Status = "error: <reason>" and stop processing this row. Don't retry automatically.
- For unsharing: filters rows where Status = "remove" OR (Share on Box is false AND Box path is non-empty).
- For each match:
  - Remove Box collaboration (default: leave files, remove collaborators only).
  - Write back: Status = "archived, not shared," clear Box path, clear Shared date.
- Cron entry: `*/2 * * * * /usr/local/bin/box-share-poller.py >> /var/log/box-poller.log 2>&1`

## Repo structure

```
archive-tool/
├── README.md                    (short, points to this brief)
├── BUILD-BRIEF.md               (this document)
├── pyproject.toml
├── .gitignore
├── config.example.toml
├── bin/
│   └── archive-project          (entry point, calls into archive_tool.cli)
├── archive_tool/
│   ├── __init__.py
│   ├── cli.py                   (Typer app, top-level command, picker flow)
│   ├── config.py                (load and validate per-machine config)
│   ├── pickers.py               (local + remote pickers via questionary)
│   ├── transfer.py              (rsync logic)
│   ├── checksums.py             (MD5 manifest generation + verification)
│   ├── sheet.py                 (Google Sheets append/update logic)
│   └── ssh.py                   (thin wrapper around SSH calls to CentOS)
├── poller/
│   └── box_share_poller.py      (standalone, runs on CentOS via cron)
└── tests/
    └── (where appropriate)
```

## Configuration

Per-machine config at `~/.config/archive-tool/config.toml`:

```toml
[local]
hostname_label = "studio"
# List of paths the tool will scan for projects ready to archive.
# Drives that aren't currently mounted are silently skipped.
# Each folder must contain a `.archive-source` marker file to be treated as a valid source.
archive_queue_paths = [
  "/Users/jmpike/archive_queue",
  "/Volumes/WorkingDrive/archive_queue",
  "/Volumes/PortableSSD/archive_queue",
]

[remote.synology]
host = "synology.tail-scale-net.ts.net"
user = "john"
staging_dir = "/volume1/archive-staging"

[remote.centos]
host = "centos-archive.tail-scale-net.ts.net"
user = "john"
archives_root = "/path/to/archives/root"   # CONFIRM WITH JOHN

[google]
service_account_path = "~/.config/archive-tool/google-creds.json"
sheet_id = "..."   # the spreadsheet ID from the URL

[rsync]
exclude = ["@eaDir", "lost+found", ".DS_Store", "Thumbs.db", "*.tmp"]
```

The actual path values are placeholders. The `config.example.toml` shipped in the repo should have these as obvious placeholders so each machine's real config is filled in by hand and gitignored.

## Conventions

### The archive_queue convention

Every drive (internal or external) that may hold projects ready to archive contains a folder named `archive_queue` at a known location. Inside that folder is an empty marker file named `.archive-source`.

The user drops finished projects into the `archive_queue` folder on whichever drive is convenient. The tool finds them by scanning all configured paths at runtime, skipping any that aren't mounted, and validates each via the marker file so unrelated folders named `archive_queue` aren't treated as sources.

External SSDs that move between machines work transparently because macOS mounts a named volume at `/Volumes/<volume_name>/` regardless of which machine it's plugged into. As long as every machine's config lists `/Volumes/<volume_name>/archive_queue` for that drive, the tool finds it wherever it currently lives.

### Writing style in code, comments, errors, and prompts

- Plain direct language. No "Let me help you..." padding in prompts.
- Sentence case for prompts. "Pick a project to archive" not "Pick A Project To Archive."
- Errors should say what failed and what the user can do. Avoid raw stack traces in normal output; reserve them for `--debug` or `--verbose`.

### Code

- Type hints throughout.
- Standard Python conventions (snake_case, lowercase module names).
- Keep functions short. If something is more than ~30 lines, it probably needs decomposition.
- Avoid clever metaprogramming. This needs to be debuggable at 11pm before a deadline.

### Confirmation and idempotency

- Every destructive step requires explicit confirmation by default.
- A `--yes` flag can skip confirmation for scripted use, but is not the default.
- All operations should be safe to retry. If the rsync gets killed mid-transfer, running again should pick up cleanly.
- Every Sheet write should include the Project ID so the same row can be found and updated rather than duplicated.

## Things to confirm with John during the build

These weren't fully nailed down in the design conversation. Ask before deciding.

1. **Exact CentOS archives root path.** From screenshots the structure has folders like `MC-Collections/MC-247/`, but the absolute filesystem path on the server (e.g., `/srv/archives` vs `/mnt/specoll`) is unknown.

2. **Tailscale setup status.** Confirm Tailscale is installed on all six machines and stable hostnames work. If not, that's a prerequisite step before this tool is useful.

3. **SSH key distribution.** From the orchestrator machine (laptop), SSH key auth should be set up to Synology and CentOS, ideally with keys held in macOS keychain. Confirm this works before building Step 2.

4. **Service account email and sheet sharing.** A new service account for this tool should be created in a fresh Google Cloud project, separate from his existing `sheetsclaude` account used for Claude Code access. Confirm before building Step 4:
   - New project created in Google Cloud Console (suggested name: `library-archive` or similar)
   - Sheets API enabled on that project
   - Service account created with descriptive name (e.g., `archive-poller`)
   - JSON key downloaded and placed on the laptop and on CentOS
   - Tracking sheet shared with the service account email as Editor

5. **Collection prefix list.** From screenshots: `D-XXX`, `MC-XXX`, `O-XXX` (numbered) plus named folders: `HarrisonCollection`, `CAVPP`, `Maps`, `Books_and_Pamphlets`, `Library_Events`, `ILL_scans`, `Serials`, `digitized_speccoll`, `nas-clir`. Confirm if there are others. Confirm whether `O-001_01_146` style compound names are projects under `O-001/` or peers of it. The picker logic depends on this.

6. **New collection auto-create vs manual.** If the user picks "new collection" and types `MC-450`, should the script `mkdir` it on CentOS, or should it require him to create the folder manually first to prevent typo-spawned phantom collections? Recommend the latter unless he says otherwise. A confirmation step with the typed name shown in big letters is the minimum bar.

7. **Unshare behavior.** When a Box share is removed, default behavior should be to leave files on Box and just remove collaborators (cheap re-share later). Confirm before building Step 5.

8. **Box auth.** Does he have rclone configured with a Box remote on CentOS already, or is that a separate setup task? If not, sequence that work before Step 5.

9. **Symlinks on the archives server.** Some folders in the screenshots had symlink badges (D-051, D-091, D-165, CAVPP, HarrisonCollection). The destination resolver should follow symlinks for verification, but log the canonical resolved path in the sheet so the spreadsheet always points at where files actually live.

## Out of scope (do NOT build)

- A web UI. The terminal flow is enough. If a web UI ever becomes useful, it can wrap this tool later.
- Multi-machine source assembly. Discipline-first: user consolidates before archiving.
- A daemon on the laptop. The orchestrator is a one-shot CLI invocation.
- Automatic collection inference from project names beyond simple prefix matching for picker pre-selection. The picker IS the inference.
- Email notifications, Slack integrations, status dashboards. The Google Sheet IS the dashboard.
- Backup or recovery beyond what rsync's `--partial` gives for free. The Synology staging copy is the only redundancy this tool provides.
- File integrity verification beyond rsync's own checksum mode. If the user wants checksums logged separately, add as future enhancement.

## Final note

Build incrementally. Step 1 should work end-to-end (run, pick a folder, print path, exit) before Step 2 starts. Each step adds one concern. The temptation to wire everything together speculatively before the pieces work is the failure mode. Resist it.

When in doubt about a design decision not covered here, ask John rather than guessing. He prefers anti-sycophantic, direct responses, and would rather answer a question than have you guess wrong.
