"""Opaque surrogate identifiers.

Ids carry no meaning: a sample's id must not encode its project (projects get
re-parented), its freezer (things move), or its creation order (sequence gaps
and reuse become visible artefacts). Human-facing naming lives in ``name`` /
``label`` / ``gml_id``, all freely mutable.

Alphabet excludes i, l, o and u -- removes the 1/l/I and 0/O transcription
errors that matter when someone reads an id aloud at the bench or copies one
off a printed box sheet.
"""

from __future__ import annotations

import re
import secrets

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
SUFFIX_LEN = 8
_ID_RE = re.compile(
    r"^([a-z]{3})_([" + ALPHABET + r"]{" + str(SUFFIX_LEN) + r"})$"
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{''.join(secrets.choice(ALPHABET) for _ in range(SUFFIX_LEN))}"


def parse_id(value: str) -> tuple[str, str] | None:
    """Return ``(prefix, suffix)`` for a well-formed id, else None."""
    m = _ID_RE.match(value.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def is_valid_id(value: str, prefix: str | None = None) -> bool:
    parsed = parse_id(value)
    if parsed is None:
        return False
    return prefix is None or parsed[0] == prefix


def looks_like_human_label(value: str) -> bool:
    """Heuristic for error messages: 'GML001631' or '-70C Misc', not an id."""
    return bool(value) and parse_id(value) is None
