"""Frequent Box collaborators — the shortlist the share picker offers.

Stored per-machine at ~/.config/archive-tool/collaborators.json. Seeded from
DEFAULT_COLLABORATORS on first use so every machine starts with John's regulars;
`add()` appends new ones (from the inline picker or the `add-collaborator` command).
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

STORE_PATH = Path("~/.config/archive-tool/collaborators.json").expanduser()

# John's usual share targets (2026-07). name is optional, shown in the picker.
DEFAULT_COLLABORATORS = [
    ("asrussek@ucdavis.edu", ""),
    ("avblecman@ucdavis.edu", ""),
    ("mltrujillo@ucdavis.edu", ""),
    ("csalexander@ucdavis.edu", ""),
    ("kcmiller@ucdavis.edu", ""),
    ("ajsarmiento@ucdavis.edu", "Jason Sarmiento"),
    ("eanebeker@ucdavis.edu", "Eric A Nebeker"),
    ("rlgustafson@ucdavis.edu", ""),
    ("czcheng@ucdavis.edu", "Christine Cheng"),
    ("isanchezalonso@ucdavis.edu", "Ignacio Sanchez-Alonso"),
    ("sgunasekara@ucdavis.edu", "Sara Gunasekara"),
    ("elawood@ucdavis.edu", "Elizabeth Wood"),
    ("ramajors@ucdavis.edu", "Rice A Majors"),
]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Collaborator:
    email: str
    name: str = ""

    def label(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


def normalize_email(raw: str) -> str | None:
    """Pull a bare lowercase email out of messy input (mailto:, 'Name <email>')."""
    match = _EMAIL_RE.search(raw or "")
    return match.group(0).lower() if match else None


def load() -> list[Collaborator]:
    """Return the stored collaborators, seeding the file with defaults on first use."""
    if not STORE_PATH.exists():
        seed = [Collaborator(email=e, name=n) for e, n in DEFAULT_COLLABORATORS]
        _write(seed)
        return seed
    data = json.loads(STORE_PATH.read_text())
    return [Collaborator(email=c["email"], name=c.get("name", "")) for c in data]


def add(raw_email: str, name: str = "") -> tuple[Collaborator | None, bool]:
    """Add a collaborator. Returns (collaborator, was_new); (None, False) if unparseable."""
    email = normalize_email(raw_email)
    if not email:
        return None, False
    collabs = load()
    if any(c.email == email for c in collabs):
        return next(c for c in collabs if c.email == email), False
    new = Collaborator(email=email, name=name.strip())
    collabs.append(new)
    collabs.sort(key=lambda c: (c.name or c.email).lower())
    _write(collabs)
    return new, True


def _write(collabs: list[Collaborator]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps([{"email": c.email, "name": c.name} for c in collabs], indent=2)
    )
