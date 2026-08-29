# archive-tool

Custom CLI for archiving a finished digitization project from a Mac to the library
archives: masters to the CentOS server (always), Special Collections to **basil**
(optional), an optional Box upload, and a row in the turn-in Google Sheet. A second
command, `archive-project ocr`, adds a searchable PDF + OCR transcript to an archived
project everywhere it lives.

Full design and rationale: [`archive-tool-brief.md`](archive-tool-brief.md).

---

## Launching it

**There is no venv to activate and nothing to `pip install`.** The `archive-project`
launcher runs the CLI through `uv run`, which builds and updates the repo's `.venv`
automatically on every invocation.

Once installed (below), from any directory:

```sh
archive-project            # run the full archive flow
archive-project --help     # see all commands
```

Before it's installed on your PATH, run it from the repo:

```sh
cd ~/code/archive-tool
./bin/archive-project
```

The first run takes a few seconds while uv creates `.venv`; after that it's instant.
If you ever want to pre-warm the environment (e.g. before going offline), run
`uv sync` — but it is never required.

---

## Install on a new machine

### 1. Prerequisites

| What | Why | Install |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | runs the CLI and manages `.venv` | `brew install uv` |
| **GNU rsync 3.x** | Apple's bundled `openrsync` lacks `--append-verify` and `--info=progress2` | `brew install rsync` |
| SSH key auth to CentOS and basil | transfers run non-interactively (`BatchMode=yes`) | see **SSH setup** below |

Check rsync is the right one — it must be the Homebrew build, not `/usr/bin/rsync`:

```sh
rsync --version | head -1     # want: rsync  version 3.x  protocol version 3x
command -v rsync              # want: /opt/homebrew/bin/rsync
```

### 2. Clone and install the launcher

```sh
git clone https://github.com/jmpucd/archive-tool.git ~/code/archive-tool
mkdir -p ~/.local/bin
ln -sfn ~/code/archive-tool/bin/archive-project ~/.local/bin/archive-project
```

Make sure `~/.local/bin` is on your PATH (add to `~/.zshrc` if not):

```sh
export PATH="$HOME/.local/bin:$PATH"
```

The launcher resolves symlinks back to the checkout, so the symlink keeps working
and always uses this repo's environment.

### 3. Configure

```sh
mkdir -p ~/.config/archive-tool
cp ~/code/archive-tool/config.example.toml ~/.config/archive-tool/config.toml
$EDITOR ~/.config/archive-tool/config.toml
```

`config.example.toml` documents every field inline. The real config is gitignored and
is **per-machine** — hostnames, queue paths, and labels differ on each Mac.

Config is read from `~/.config/archive-tool/config.toml`, falling back to a
`config.toml` in the repo root if that doesn't exist.

### 4. Optional pieces

- **Google Sheet logging** — put the service-account JSON at
  `~/.config/archive-tool/google-creds.json` (`chmod 600`) and fill in `[google]`.
  Omit the whole `[google]` section on machines that shouldn't log; the flow warns
  and continues.
- **Box upload** — needs an rclone remote configured *on CentOS* (not on the Mac);
  fill in `[remote.box]`. Omit the section to drop the Box prompt entirely.

### 5. SSH setup

Both remotes need working key auth, since the tool runs SSH with `BatchMode=yes`.

- **CentOS** — reachable via Tailscale off-VPN and campus DNS on-VPN, but not both at
  once (the campus VPN captures Tailscale's CGNAT range). Use a failover `Host
  digitization` block in `~/.ssh/config`; the template is in the `[remote.centos]`
  comments of `config.example.toml`, and `host = "digitization"` in your config.
- **basil** — runs OpenSSH 5.3 (2009) and **cannot use ed25519 keys**. It needs an RSA
  key plus algorithm overrides:

  ```
  Host basil.lib.ucdavis.edu
      User <you>
      IdentityFile ~/.ssh/id_rsa
      HostKeyAlgorithms +ssh-rsa
      PubkeyAcceptedAlgorithms +ssh-rsa
  ```

Verify before the first real run:

```sh
ssh -o BatchMode=yes digitization true && echo "centos ok"
ssh -o BatchMode=yes basil.lib.ucdavis.edu true && echo "basil ok"
```

---

## Preparing a project to archive

Projects are picked up from an **archive queue** — a folder listed under
`archive_queue_paths` in your config. On the laptop that's:

```
/Users/jmpike/archive_queue/
```

No external drive is required; the `/Volumes/...` queues in the config are silently
skipped whenever those drives aren't mounted.

A queue folder needs one `.archive-source` marker file, created once:

```sh
mkdir -p ~/archive_queue
touch ~/archive_queue/.archive-source
```

**The marker goes in the queue folder itself, not in each project.** After that, any
folder you drop directly inside the queue shows up in the picker:

```
~/archive_queue/
├── .archive-source          <- marker, once
├── MC-247_ledgers/          <- a project
└── D-014_diaries/           <- another project
```

Only the immediate children are scanned — nested folders below a project are just part
of that project. Dotfiles are ignored.

If the picker says `No projects found in any mounted archive_queue`, it now prints each
configured queue and why it was skipped.

---

## What the flow does

Run `archive-project` with no arguments. It collects **every** decision up front, then
runs unattended:

1. Pick the source project from your local `archive_queue`.
2. Pick the destination collection path (browsed live from basil's real tree).
3. Ask whether to also send to basil, whether to upload to Box and who to share with,
   and whether to delete the local source after everything verifies.
4. Print the full plan and ask once for confirmation.
5. Write an MD5 manifest, rsync to CentOS masters, verify; optionally have basil pull
   from CentOS, verify; optionally rclone to Box from CentOS; log the row to the Sheet;
   optionally delete the local source.

Add `--yes` / `-y` to skip the final confirmation prompt.

Destination collection folders are **never created automatically** — if the collection
doesn't exist yet, the picker prints the `mkdir` command to run by hand.

### OCR derivatives (searchable PDF + transcript)

```sh
archive-project ocr                     # pick an archived project from the Sheet
archive-project ocr -p D-698_Unprocessed --yes
```

Builds one searchable PDF and one paged plain-text transcript for a project that has
**already been archived**, and files them next to the masters:

- Runs **digtk** (Tesseract, parallel) **on CentOS** against the masters copy — nothing
  is re-uploaded from the Mac, and it works after the local source was deleted. Needs a
  digtk checkout on CentOS (`[remote.centos].ocr_python`, default `~/digtk/.venv/bin/python`).
- Writes `<project>.pdf` and `<project>_transcript.txt` **inside the project folder**
  on CentOS. The masters' `manifest.md5` is not touched.
- If the Sheet row has a Basil path, basil pulls the two files into its copy of the
  project (md5-verified). If the project isn't on basil, that leg is skipped.
- If the row has a Box path, the files are rclone-copied into that Box folder;
  otherwise it asks whether to create `box:Archives/<project>` holding just the OCR
  files (`--box` / `--no-box` to decide without asking).
- Records `Derivatives`, `Derivatives on` (centos/basil/box) and `Derivatives date` on
  the row — the files sit inside the folders named in the row's CentOS/Basil/Box path
  columns. `--skip-basil` leaves basil for a later re-run; re-runs reuse the existing
  outputs on CentOS and only redo the copies (`--force` re-OCRs). Note the basil leg
  needs the Mac to reach basil (campus/VPN) even though the bytes flow CentOS->basil:
  the pull is started over ssh on basil.

### JPEG access copies

```sh
archive-project jpeg -p D-738_044 --yes        # rectos only, quality 80, full resolution
archive-project jpeg --match "" -q 85          # every page
```

Same pattern as `ocr`: converts on CentOS (Pillow, ICC profile + DPI preserved, no
resizing) into `<project>/JPEG/`, pulls that folder to basil and copies it to Box
wherever the project lives, and adds e.g. `JPEG/ (33 recto JPEG q80)` to the row's
`Derivatives` column.

The transcript is Tesseract output as-is (uncorrected), one `Page N of M - <file>`
header per image in filename order. `--min-conf` (default 30) drops low-confidence
words from both the PDF text layer and the transcript; raise it (~40) for pages with
handwriting or heavy marginalia. `--centos-path /full/path` OCRs a folder that isn't
in the Sheet (nothing gets logged).

### Other commands

| Command | What it does |
| --- | --- |
| `archive-project` | the full archive flow |
| `archive-project ocr` | searchable PDF + transcript for an archived project (see above) |
| `archive-project jpeg` | full-res JPEG access copies (rectos by default) into `<project>/JPEG/` everywhere the project lives |
| `archive-project to-basil` | send a project that was archived without basil there after the fact: basil pulls the whole CentOS folder (masters, manifest, OCR files), verifies the manifest, fills in the row's Basil path |
| `archive-project pick-source` | pick a local project and print its path (debug) |
| `archive-project pick-dest` | pick a basil collection folder and print it (debug) |
| `archive-project collaborators` | list the frequent Box collaborators |
| `archive-project add-collaborator <email>` | add to that list |

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `archive-project: command not found` | `~/.local/bin` isn't on your PATH, or the symlink in step 2 is missing. |
| `archive-project: uv is not installed` | `brew install uv` |
| `error: No config found...` | Step 3 — copy `config.example.toml` into `~/.config/archive-tool/config.toml`. |
| `unknown option --append-verify` / `--info=progress2` | You're on Apple's `openrsync`. `brew install rsync` and confirm `command -v rsync` is the Homebrew path. |
| `Permission denied (publickey)` on basil | basil can't use ed25519 — use an RSA key and the `+ssh-rsa` overrides above. |
| SSH to CentOS hangs or fails only on VPN (or only off it) | You're using a raw Tailscale IP. Switch to the failover `digitization` alias. |
| `No projects found in any mounted archive_queue` | The command lists each configured queue and why it was skipped. Usually: the queue is empty, the `.archive-source` marker is missing from the *queue* folder, or that drive isn't mounted. |
