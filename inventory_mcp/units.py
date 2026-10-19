"""Unit handling: parse, convert to canonical, reject nonsense.

Conversion is deliberately table-driven rather than pulled in from a library --
the set of dimensions a sample inventory needs is small and closed, and an
unknown unit must fail loudly rather than silently pass through.
"""

from __future__ import annotations

from dataclasses import dataclass


class UnitError(ValueError):
    """Raised when a unit is unknown or incompatible with a quantity."""


@dataclass(frozen=True)
class Dimension:
    name: str
    canonical: str
    #: unit (lowercased) -> multiplier to reach the canonical unit
    factors: dict[str, float]


def _prefixed(base: str, scale: float, prefixes: tuple[str, ...]) -> dict[str, float]:
    out = {}
    for i, p in enumerate(prefixes):
        out[f"{p}{base}"] = scale * (1000.0 ** i)
    return out


DIMENSIONS: dict[str, Dimension] = {
    "volume": Dimension(
        name="volume",
        canonical="ul",
        factors={
            **_prefixed("l", 1e6, ("", "m", "n", "p", "u")),
            "ml": 1000.0,
            "ul": 1.0,
            "µl": 1.0,
            "usl": 1.0,
            "microliter": 1.0,
            "microlitre": 1.0,
            "l": 1e6,
            # ``_prefixed`` assigns its factors by position in the prefix tuple, so
            # it treats n and p as steps *up* from a litre. Every key it gets wrong
            # is corrected here; these two were missing until 2026-10-19, which made
            # 1 nl convert as 1e12 ul -- fifteen orders of magnitude out, silently,
            # because an unknown unit fails loudly and a wrong one does not.
            "nl": 1e-3,
            "pl": 1e-6,
        },
    ),
    "mass": Dimension(
        name="mass",
        canonical="ng",
        factors={
            **_prefixed("g", 1e9, ("", "m", "u", "n", "p")),
            "ug": 1000.0,
            "µg": 1000.0,
            "ng": 1.0,
            "pg": 1e-3,
            "mg": 1e6,
            "g": 1e9,
        },
    ),
    "concentration_mass": Dimension(
        name="concentration_mass",
        canonical="ng/ul",
        factors={
            "ng/ul": 1.0,
            "ng/µl": 1.0,
            "ug/ml": 1.0,
            "µg/ml": 1.0,
            "ug/µl": 1.0,
            "µg/ul": 1.0,
            "mg/ml": 1000.0,
            "g/l": 1000.0,
            "ng/ml": 1e-3,
            "ng/ul ": 1.0,
        },
    ),
    "concentration_molar": Dimension(
        name="concentration_molar",
        canonical="nM",
        factors={
            "nm": 1.0,
            "nmol/l": 1.0,
            "um": 1000.0,
            "µm": 1000.0,
            "umol/l": 1000.0,
            "mm": 1e6,
            "mol/l": 1e9,
            "pm": 1e-3,
        },
    ),
    "amount": Dimension(
        name="amount",
        canonical="mol",
        factors={"mol": 1.0, "umol": 1e-6, "µmol": 1e-6, "nmol": 1e-9, "pmol": 1e-12},
    ),
    # Fragment sizing is quoted in base pairs because that is how a TapeGuard
    # reports it; there is no attempt to convert to physical length.
    "length": Dimension(
        name="length",
        canonical="bp",
        factors={"bp": 1.0, "kb": 1000.0, "mb": 1e6},
    ),
    "temperature": Dimension(
        name="temperature",
        canonical="C",
        # Offsets are handled specially; see to_canonical().
        factors={"c": 1.0, "°c": 1.0, "celsius": 1.0},
    ),
}

#: Quantities that carry no unit arithmetic.
UNITLESS = {
    "cell_count": "cells",
    "viability": "%",
    "count": "count",
    "ratio": "",
    "other": "",
}


def dimension_for(quantity: str) -> Dimension | None:
    return DIMENSIONS.get(quantity)


def normalise(unit: str) -> str:
    u = unit.strip().replace("**", "").lower()
    return u


def is_known_unit(quantity: str, unit: str) -> bool:
    if not unit or not unit.strip():
        return quantity in UNITLESS
    if quantity in UNITLESS:
        return normalise(unit) in {"", "cells", "%", "count", "n"}
    dim = dimension_for(quantity)
    if dim is None:
        return False
    return normalise(unit) in dim.factors


def to_canonical(quantity: str, value: float, unit: str) -> tuple[float, str]:
    """Convert ``value`` recorded in ``unit`` into the quantity's canonical unit.

    Raises UnitError for unknown or incompatible units -- never passes them
    through unconverted, because a silently wrong concentration is worse than a
    rejected one.
    """
    if quantity in UNITLESS:
        canonical = UNITLESS[quantity]
        if unit and unit.strip() and not is_known_unit(quantity, unit):
            raise UnitError(f"unit {unit!r} is not valid for a unitless quantity {quantity!r}")
        return value, canonical

    dim = dimension_for(quantity)
    if dim is None:
        raise UnitError(f"no unit arithmetic defined for quantity {quantity!r}")

    key = normalise(unit)
    if key not in dim.factors:
        known = ", ".join(sorted(dim.factors))
        raise UnitError(
            f"unit {unit!r} is not a recognised {quantity} unit. Known: {known}"
        )
    return value * dim.factors[key], dim.canonical


def compatible_quantity(unit: str) -> set[str]:
    """Which quantities could plausibly accept this unit (used in error hints)."""
    key = normalise(unit)
    hits = {d.name for d in DIMENSIONS.values() if key in d.factors}
    return hits
