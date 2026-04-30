# archive-tool

Custom CLI for archiving finished digitization projects through the Synology to the CentOS archives, with Box sharing tracked in a Google Sheet.

Full design and rationale: [`archive-tool-brief.md`](archive-tool-brief.md).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```sh
uv sync
./bin/archive-project --help
```

## Configuration

Copy `config.example.toml` to `~/.config/archive-tool/config.toml` and fill in machine-specific paths. The real config is gitignored; the example file is the template.
