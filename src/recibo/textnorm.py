"""Normalização de texto para casar rótulos das fontes com a classificação funcional."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_LEAD_CODE = re.compile(r"^(\d{2,3})\s*[-–.:]\s*(.+)$")


def norm(s: object) -> str:
    """Sem acento, maiúsculas, espaços colapsados. None vira ''."""
    if s is None:
        return ""
    t = unicodedata.normalize("NFKD", str(s))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return _WS.sub(" ", t).strip().upper()


def split_leading_code(s: str) -> tuple[str | None, str]:
    """'10 - SAUDE' -> ('10', 'SAUDE'); 'SAUDE' -> (None, 'SAUDE')."""
    m = _LEAD_CODE.match(s)
    if m:
        return m.group(1), m.group(2).strip()
    return None, s
