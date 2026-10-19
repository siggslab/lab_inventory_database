"""Configuration: which backend, where the data lives, where credentials sit.

Stored as JSON at ``~/.config/inventory-mcp/config.json`` unless overridden by
the ``INVENTORY_MCP_CONFIG`` environment variable or an explicit ``--config``
flag. A single file is read by both the MCP server and any library consumer, so
a dashboard on the same machine needs only the path to this file -- no code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKENDS = ("local", "drive")

#: Backends that existed and what became of them. A stale name in a copied config
#: otherwise fails as an opaque list of valid options, which tells nobody that the
#: config is from before a rename rather than mistyped.
_RETIRED_BACKENDS = {
    "drive_sheets": "renamed to 'drive' when drive_csv was removed",
    "drive_csv": "removed; use 'drive'. Storing tables as CSV files in Drive asked "
                 "Google not to convert them on upload, and it converted them anyway",
}

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "inventory-mcp" / "config.json"
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "inventory-mcp" / "oauth_token.json"
DEFAULT_CLIENT_SECRET_PATH = Path.home() / ".config" / "inventory-mcp" / "client_secret.json"
DEFAULT_SERVICE_ACCOUNT_PATH = Path.home() / ".config" / "inventory-mcp" / "service_account.json"

#: Subfolders inside the data root. Tables are authoritative; exports and
#: backups are derived and disposable.
TABLES_DIR = "tables"
EXPORTS_DIR = "exports"
BACKUPS_DIR = "backups"

# The id sits after a "folder" segment wherever that segment lands in the path, so
# match the segment rather than its position: plain ``/drive/folders/<id>``, the
# signed-in ``/drive/u/1/folders/<id>``, and the legacy anchor ``/drive/#folder/<id>``
# are all one pattern. Pinning the prefix instead made two of those three fail.
_DRIVE_FOLDER_PATTERNS = (
    re.compile(r"[/#]folders?/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)


def parse_drive_folder(value: str) -> str:
    """Accept a pasted Drive URL or a bare folder id; return the id.

    Handles the variants people actually paste: ``/drive/folders/<id>``,
    legacy ``/drive/#folder/<id>``, ``?id=<id>`` with trailing ``usp`` or
    ``resourcekey`` parameters.
    """
    v = value.strip()
    if not v:
        raise ValueError("empty Drive folder reference")
    if "://" not in v and "/" not in v:
        # Already a bare id.
        if re.fullmatch(r"[A-Za-z0-9_-]{10,}", v):
            return v
        raise ValueError(f"{value!r} is neither a Drive URL nor a plausible folder id")
    for pattern in _DRIVE_FOLDER_PATTERNS:
        m = pattern.search(v)
        if m:
            return m.group(1)
    raise ValueError(f"could not find a folder id in {value!r}")


@dataclass
class Config:
    backend: str = "local"
    #: directory holding tables/ exports/ backups/ when backend == "local"
    local_root: str = ""
    #: Drive folder id when backend startswith "drive_"
    drive_folder_id: str = ""
    #: the workbook holding every table, when the name lookup cannot be trusted --
    #: somebody renamed it, or two databases share a folder. One value, because the
    #: layout is one spreadsheet; it used to be a mapping per table.
    drive_spreadsheet_id: str = ""
    token_path: str = str(DEFAULT_TOKEN_PATH)
    client_secret_path: str = str(DEFAULT_CLIENT_SECRET_PATH)
    #: if this key exists it wins over the OAuth flow -- useful on a shared
    #: machine where an RA should not have to consent per project
    service_account_path: str = str(DEFAULT_SERVICE_ACCOUNT_PATH)
    #: seconds a loaded table may be served from cache before refetching
    cache_ttl_seconds: float = 5.0
    #: how many backup generations to retain per commit
    keep_backups: int = 20
    actor: str = ""  # default identity for updated_by; empty means derive

    @property
    def is_drive(self) -> bool:
        return self.backend == "drive"

    def __post_init__(self) -> None:
        # Normalise at construction, not only on load: `init --drive-folder <URL>`
        # builds a Config directly, and using a pasted URL as a folder id fails with
        # an opaque API error far from the cause.
        if self.drive_folder_id:
            try:
                self.drive_folder_id = parse_drive_folder(self.drive_folder_id)
            except ValueError:
                # Leave it as given; validate() reports a missing id better than
                # silently rewriting something we don't understand.
                pass

    def validate(self) -> None:
        if self.backend not in BACKENDS:
            retired = _RETIRED_BACKENDS.get(self.backend)
            raise ConfigError(
                f"backend must be one of {BACKENDS}, got {self.backend!r}"
                + (f" -- that backend was {retired}" if retired else "")
            )
        if self.backend == "local" and not self.local_root:
            raise ConfigError("backend 'local' requires local_root")
        if self.is_drive and not self.drive_folder_id:
            raise ConfigError(f"backend {self.backend!r} requires drive_folder_id")

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        p = cls.resolve_path(path)
        if not p.exists():
            raise ConfigError(
                f"no config at {p}. Run `inventory-mcp init` or set INVENTORY_MCP_CONFIG."
            )
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(raw) - known
        cfg = cls(**{k: v for k, v in raw.items() if k in known})
        cfg.unknown_keys = sorted(unknown)  # type: ignore[attr-defined]
        # Backend before folder id: a config copied from before a rename should be
        # told about the rename, not accused of having a bad folder id.
        cfg.validate()
        # Accept a pasted URL anywhere a folder id is expected. The ValueError has
        # to become ConfigError -- this runs on a file nobody typed by hand, and
        # the CLI prints config errors, not tracebacks.
        if cfg.drive_folder_id:
            try:
                cfg.drive_folder_id = parse_drive_folder(cfg.drive_folder_id)
            except ValueError as exc:
                raise ConfigError(f"drive_folder_id: {exc}") from exc
        return cfg

    def to_dict(self) -> dict[str, Any]:
        out = {}
        for f in self.__dataclass_fields__:  # type: ignore[attr-defined]
            if f == "unknown_keys":
                continue
            out[f] = getattr(self, f)
        return out

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        p = self.resolve_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.chmod(p, 0o600)
        return p

    @staticmethod
    def resolve_path(path: str | os.PathLike[str] | None = None) -> Path:
        if path:
            return Path(path).expanduser()
        env = os.environ.get("INVENTORY_MCP_CONFIG")
        if env:
            return Path(env).expanduser()
        return DEFAULT_CONFIG_PATH


class ConfigError(RuntimeError):
    pass
