"""Deterministic numeric-constraint extraction from brief text.

Runs independently of the LLM interpreter. Numbers in a brief are high-stakes
(a misread "CNY 12" silently changes the cost gate for thousands of variants),
so the resolver cross-checks the model's extraction against this pass and turns
any disagreement into an open question. The result also guards the semantic cache.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import constants as C

_MONEY = re.compile(r"(?:¥|CNY|RMB)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:CNY|RMB|yuan|元)", re.I)
_THICK = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*(?:thick|thin|deep|profile|depth)", re.I)
_MASS = re.compile(r"(?:under|below|max(?:imum)?|at most|<=?)\s*(\d+(?:\.\d+)?)\s*g\b", re.I)
_IP = re.compile(r"\bIP\s?(54|65|67)\b", re.I)


# things the platform can't make: it only does injection-moulded ABS / PC-ABS / rABS with 1.5-3.0 mm walls
# A material or process word only counts as a request when it isn't describing a look ("steel grey",
# "machined-look", "wood-grain"), a filler ("glass-fibre reinforced PC-ABS") or a bought-in part ("glass cover").
_LOOK = (r"(?![- ]?(?:look|like|style|effect|finish|colou?r(?:ed)?|tone|toned|grain|grey|gray|white|black|blue|"
         r"silver|print|pattern|texture|fib(?:re|er)|filled|reinforced|cover|lens|window|display|screen|panel))")
_NON_PLASTIC = re.compile(
    r"\b(alumin(?:i)?um|stainless steel|steel|magnesium|zinc|titanium|die[- ]?cast(?:ing)?|glass|wood(?:en)?|"
    r"bamboo|ceramic|carbon[- ]fib(?:re|er)|sheet metal|metal (?:housing|case|shell|body|enclosure))\b" + _LOOK, re.I)
_PROCESS = re.compile(r"\b(cnc(?:[- ]machined)?|machined|3d[- ]print(?:ed|ing)?|vacuum[- ]formed)\b" + _LOOK, re.I)
# "10 mm wall-mount bracket" is about mounting, not wall thickness
_WALL = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*(?:thick\s+)?walls?\b(?![- ]?(?:mount|mounted|bracket|plug|hook))"
                   r"|\bwalls?\s+(?:thickness\s+)?(?:of\s+|at\s+)?(\d+(?:\.\d+)?)\s*mm", re.I)
# "no metal housing", "avoid CNC", "rather than aluminium": the brief is ruling it out, not asking for it
_NEGATION = re.compile(r"\b(?:no|not|non|avoid|without|never|instead of|rather than|don't|do not|nor)\b[^.;,]*$", re.I)


def _negated(text: str, start: int) -> bool:
    """True when a negation word sits earlier in the same clause, e.g. 'no metal housing'."""
    clause = re.split(r"[.;,\n]", text[:start])[-1]
    return bool(_NEGATION.search(clause))


def out_of_platform(text: str) -> list[str]:
    """Requests in the brief that no variant on this platform can meet. Checked by code so they can't slip by.

    Deliberately narrow: a false alarm costs a full fan-out (the brief gets only example variants and a human
    review), so look-alike wording and negated requests are ignored.
    """
    found = [f"a non-plastic material ('{m.group(0)}')" for m in _NON_PLASTIC.finditer(text)
             if not _negated(text, m.start())]
    found += [f"a process other than injection moulding ('{m.group(0)}')" for m in _PROCESS.finditer(text)
              if not _negated(text, m.start())]
    for m in _WALL.finditer(text):
        wall = float(m.group(1) or m.group(2))
        if not C.WALL_MIN_MM <= wall <= C.WALL_MAX_MM and not _negated(text, m.start()):
            found.append(f"{wall:g} mm walls (platform range {C.WALL_MIN_MM}-{C.WALL_MAX_MM} mm)")
    return list(dict.fromkeys(found))  # de-duplicate, keep order


@dataclass(frozen=True)
class NumericConstraints:
    cost_cny: Optional[float]
    thickness_mm: Optional[float]
    mass_g: Optional[float]
    ip: Optional[str]

    # used as the semantic-cache key: similar briefs only share a result if these numbers match
    def guard(self) -> str:
        return f"cost={self.cost_cny}|thick={self.thickness_mm}|mass={self.mass_g}|ip={self.ip}"


def _first(rx: re.Pattern, text: str) -> Optional[float]:
    m = rx.search(text)
    if not m:
        return None
    # the regexes have two alternative groups; take whichever one matched
    return float(next(g for g in m.groups() if g is not None))


def extract(text: str) -> NumericConstraints:
    m = _IP.search(text)
    return NumericConstraints(cost_cny=_first(_MONEY, text), thickness_mm=_first(_THICK, text),
                              mass_g=_first(_MASS, text), ip=f"IP{m.group(1)}" if m else None)
