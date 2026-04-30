import tomllib
from dataclasses import dataclass
from pathlib import Path

USER_CONFIG_PATH = Path("~/.config/archive-tool/config.toml").expanduser()
REPO_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "config.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ArchiveQueue:
    label: str
    path: Path


@dataclass(frozen=True)
class LocalConfig:
    hostname_label: str
    archive_queue_paths: list[ArchiveQueue]


@dataclass(frozen=True)
class Config:
    local: LocalConfig
    source_path: Path  # which file the config was loaded from


def find_config_path() -> Path:
    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH
    if REPO_FALLBACK_PATH.exists():
        return REPO_FALLBACK_PATH
    raise ConfigError(
        f"No config found. Create one at {USER_CONFIG_PATH} "
        f"(copy config.example.toml from the repo and fill in the placeholders)."
    )


def load_config() -> Config:
    path = find_config_path()
    with path.open("rb") as f:
        data = tomllib.load(f)

    local_raw = data.get("local")
    if not local_raw:
        raise ConfigError(f"{path}: missing [local] section")

    hostname_label = local_raw.get("hostname_label")
    if not hostname_label:
        raise ConfigError(f"{path}: [local].hostname_label is required")

    queues_raw = local_raw.get("archive_queue_paths")
    if not queues_raw:
        raise ConfigError(f"{path}: [local].archive_queue_paths is required")

    queues = []
    for i, q in enumerate(queues_raw):
        if not isinstance(q, dict) or "label" not in q or "path" not in q:
            raise ConfigError(
                f"{path}: archive_queue_paths[{i}] must be a table with `label` and `path` keys"
            )
        queues.append(
            ArchiveQueue(label=q["label"], path=Path(q["path"]).expanduser())
        )

    return Config(
        local=LocalConfig(hostname_label=hostname_label, archive_queue_paths=queues),
        source_path=path,
    )
