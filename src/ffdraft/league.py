"""Parse and validate a league YAML against `config/leagues/_schema.yml`.

Every rule lives in the schema file; this module is its interpreter. There are
no defaults anywhere in here — a missing or null setting raises. A league
setting that silently defaults produces a board that is wrong in a way nobody
notices, which is worse than a board that refuses to build.

Validation collects *every* problem and raises once, because fixing a config
one error per run is miserable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LEAGUES_DIR, SCHEMA_PATH, load_yaml, resolve

# The only condition form `scoring.group_required_when` accepts. Deliberately
# not eval() — a config file should not be able to execute arbitrary Python.
_CONDITION_RE = re.compile(r"^roster\.([A-Za-z_]+)\s*(>=|>|==)\s*(\d+)$")

_TYPE_NAMES = {"str": str, "int": int, "float": float, "map": dict, "list": list}


class LeagueValidationError(ValueError):
    """Raised with the full list of problems, not just the first one."""

    def __init__(self, path: Path | str, errors: list[str]):
        self.path = str(path)
        self.errors = list(errors)
        body = "\n".join(f"  {i}. {e}" for i, e in enumerate(self.errors, 1))
        super().__init__(f"{self.path}: {len(self.errors)} validation error(s)\n{body}")


@dataclass(frozen=True)
class Band:
    """One row of a banded scoring table, e.g. `"7-13": 3`."""

    key: str
    lo: int
    hi: int | None  # None means open-ended ("35+")
    points: float

    def contains(self, value: float) -> bool:
        if value < self.lo:
            return False
        return self.hi is None or value <= self.hi


def parse_band_key(key: str) -> tuple[int, int | None]:
    """Parse "0", "1-6", or "35+" into (lo, hi). hi is None when open-ended."""
    text = str(key).strip()
    if text.endswith("+"):
        return int(text[:-1]), None
    if "-" in text:
        lo, hi = text.split("-", 1)
        return int(lo), int(hi)
    value = int(text)
    return value, value


def build_bands(mapping: dict[str, Any]) -> list[Band]:
    bands = [
        Band(key=str(k), lo=parse_band_key(k)[0], hi=parse_band_key(k)[1], points=float(v))
        for k, v in mapping.items()
    ]
    return sorted(bands, key=lambda b: b.lo)


def score_band(bands: list[Band], value: float) -> float:
    for band in bands:
        if band.contains(value):
            return band.points
    # Unreachable for a schema-validated config: bands start at 0 and the last
    # is open-topped. Loud rather than silently zero if that ever changes.
    raise ValueError(f"value {value} fell outside every band: {[b.key for b in bands]}")


@dataclass(frozen=True)
class League:
    """A validated league configuration.

    Derived properties are computed from `teams` and `roster` rather than
    assumed, because every default in public fantasy content assumes 12 teams
    and this league has 10.
    """

    name: str
    teams: int
    format: str
    playoff_teams: int
    playoff_start_week: int
    trade_deadline_week: int
    ir_slots: int
    waivers: str
    roster: dict[str, int]
    scoring: dict[str, Any]
    schema: dict[str, Any]
    source_path: str

    # --- roster shape -----------------------------------------------------

    @property
    def starter_slots(self) -> list[str]:
        return [s for s in self.schema["roster"]["starter_slots"] if self.roster.get(s, 0) > 0]

    @property
    def starters_per_team(self) -> int:
        return sum(self.roster[s] for s in self.schema["roster"]["starter_slots"])

    @property
    def roster_spots(self) -> int:
        """Starters + bench. IR does not count against the roster limit."""
        return self.starters_per_team + self.roster["BENCH"]

    @property
    def rostered_players(self) -> int:
        """Total players off the board leaguewide — the real depth constraint."""
        return self.teams * self.roster_spots

    @property
    def bench_spots(self) -> int:
        return self.roster["BENCH"]

    # --- eligibility ------------------------------------------------------

    @property
    def flex_slots(self) -> dict[str, list[str]]:
        """Flex-type slots this league actually uses, mapped to eligible positions."""
        elig = self.schema["roster"]["eligibility"]
        return {slot: list(pos) for slot, pos in elig.items() if self.roster.get(slot, 0) > 0}

    def flex_eligible_positions(self) -> set[str]:
        return {p for positions in self.flex_slots.values() for p in positions}

    def base_starters(self, position: str) -> int:
        """Leaguewide dedicated starting slots at a position, excluding flex."""
        return self.teams * self.roster.get(position, 0)

    def flex_slots_total(self) -> int:
        return sum(self.teams * self.roster[slot] for slot in self.flex_slots)

    # --- player universe --------------------------------------------------

    @property
    def player_universe(self) -> tuple[str, ...]:
        """Positions worth ranking. Derived, never hardcoded.

        A position with zero dedicated slots that no flex slot accepts is
        dropped entirely. Under main.yml that is exactly K.
        """
        cfg = self.schema["player_universe"]
        positions = list(cfg["positions"])
        if not cfg.get("exclude_zero_slot_positions"):
            return tuple(positions)
        flex_ok = self.flex_eligible_positions()
        return tuple(p for p in positions if self.roster.get(p, 0) > 0 or p in flex_ok)

    @property
    def excluded_positions(self) -> tuple[str, ...]:
        universe = set(self.player_universe)
        return tuple(p for p in self.schema["player_universe"]["positions"] if p not in universe)

    # --- scoring helpers --------------------------------------------------

    def bands(self, group: str, key: str) -> list[Band]:
        return build_bands(self.scoring[group][key])

    def assumptions(self) -> dict[str, Any]:
        """The block every board and every sub-agent prompt has to carry."""
        return {
            "league": self.name,
            "teams": self.teams,
            "format": self.format,
            "scoring": "full PPR" if self.scoring["receiving"]["reception"] == 1 else "custom",
            "roster_slots": dict(self.roster),
            "starters_per_team": self.starters_per_team,
            "roster_spots_per_team": self.roster_spots,
            "rostered_players_leaguewide": self.rostered_players,
            "bench_spots": self.bench_spots,
            "ir_slots": self.ir_slots,
            "player_universe": list(self.player_universe),
            "excluded_positions": list(self.excluded_positions),
        }


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _check_type(value: Any, type_name: str) -> bool:
    expected = _TYPE_NAMES[type_name]
    if expected is int:
        # bool is an int subclass in Python; `teams: true` is not a team count.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _validate_top_level(raw: dict, schema: dict, errors: list[str]) -> None:
    spec = schema["top_level"]
    for key, rules in spec.items():
        if key not in raw:
            if rules.get("required"):
                errors.append(f"missing required key {key!r}")
            continue
        value = raw[key]
        if value is None:
            errors.append(f"{key!r} is null; it must be set explicitly")
            continue
        if not _check_type(value, rules["type"]):
            errors.append(
                f"{key!r} must be {rules['type']}, got {type(value).__name__} ({value!r})"
            )
            continue
        if "min" in rules and value < rules["min"]:
            errors.append(f"{key!r} must be >= {rules['min']}, got {value}")
        if "max" in rules and value > rules["max"]:
            errors.append(f"{key!r} must be <= {rules['max']}, got {value}")
        if "enum" in rules and value not in rules["enum"]:
            errors.append(f"{key!r} must be one of {rules['enum']}, got {value!r}")

    if schema.get("strict"):
        for key in raw:
            if key not in spec:
                errors.append(f"unknown top-level key {key!r} (strict mode); check for a typo")


def _validate_cross_field(raw: dict, schema: dict, errors: list[str]) -> None:
    for rule in schema.get("cross_field", []):
        rid = rule["id"]
        if rid == "playoff_teams_fit":
            if _both_int(raw, "playoff_teams", "teams") and raw["playoff_teams"] > raw["teams"]:
                errors.append(
                    f"playoff_teams ({raw['playoff_teams']}) exceeds teams ({raw['teams']})"
                )
        elif rid == "deadline_before_playoffs":
            if (
                _both_int(raw, "trade_deadline_week", "playoff_start_week")
                and raw["trade_deadline_week"] >= raw["playoff_start_week"]
            ):
                errors.append(
                    f"trade_deadline_week ({raw['trade_deadline_week']}) must be before "
                    f"playoff_start_week ({raw['playoff_start_week']})"
                )
        else:
            errors.append(f"schema declares cross_field rule {rid!r} that league.py cannot enforce")


def _both_int(raw: dict, *keys: str) -> bool:
    return all(isinstance(raw.get(k), int) and not isinstance(raw.get(k), bool) for k in keys)


def _validate_roster(raw: dict, schema: dict, errors: list[str]) -> None:
    roster = raw.get("roster")
    if not isinstance(roster, dict):
        return  # already reported by the top-level type check
    rules = schema["roster"]
    slot_rules = rules["slot_value"]

    for slot in rules["required_slots"]:
        if slot not in roster:
            errors.append(f"roster is missing slot {slot!r}; use 0 if this league does not use it")
            continue
        value = roster[slot]
        if value is None:
            errors.append(
                f"roster.{slot} is null. An unset slot count is a config bug, not a zero — "
                f"write `{slot}: 0` if this league genuinely has no {slot} slot."
            )
            continue
        if not _check_type(value, slot_rules["type"]):
            errors.append(f"roster.{slot} must be an int, got {type(value).__name__} ({value!r})")
            continue
        if value < slot_rules["min"]:
            errors.append(f"roster.{slot} must be >= {slot_rules['min']}, got {value}")

    for slot in roster:
        if slot not in rules["required_slots"]:
            errors.append(f"unknown roster slot {slot!r}")

    for slot, positions in rules["eligibility"].items():
        unknown = set(positions) - set(schema["player_universe"]["positions"])
        if unknown:
            errors.append(
                f"schema eligibility for {slot} names unknown positions {sorted(unknown)}"
            )


def _validate_scoring(raw: dict, schema: dict, errors: list[str]) -> None:
    scoring = raw.get("scoring")
    if not isinstance(scoring, dict):
        return
    rules = schema["scoring"]
    roster = raw.get("roster") if isinstance(raw.get("roster"), dict) else {}

    known_groups = set(rules["required_groups"]) | set(rules["optional_groups"])
    for group in rules["required_groups"]:
        if group not in scoring:
            errors.append(f"scoring is missing required group {group!r}")
    for group in scoring:
        if group not in known_groups:
            errors.append(f"unknown scoring group {group!r}")

    for group, keys in rules["required_keys"].items():
        block = scoring.get(group)
        if not isinstance(block, dict):
            continue
        for key in keys:
            if key not in block:
                errors.append(f"scoring.{group} is missing required key {key!r}")
            elif block[key] is None:
                errors.append(f"scoring.{group}.{key} is null; it must be set explicitly")

    for dotted in rules["must_be_positive"]:
        group, key = dotted.split(".", 1)
        value = (scoring.get(group) or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
            errors.append(
                f"scoring.{dotted} must be > 0 (it is a divisor: points = yards / {key}), "
                f"got {value}"
            )

    for group, condition in rules.get("group_required_when", {}).items():
        match = _CONDITION_RE.match(str(condition))
        if not match:
            errors.append(
                f"schema condition {condition!r} for scoring group {group!r} is not a form "
                f"league.py understands (expected `roster.SLOT <op> N`)"
            )
            continue
        slot, op, threshold = match.group(1), match.group(2), int(match.group(3))
        actual = roster.get(slot)
        if not isinstance(actual, int) or isinstance(actual, bool):
            continue  # roster problems are reported separately
        triggered = {
            ">": actual > threshold,
            ">=": actual >= threshold,
            "==": actual == threshold,
        }[op]
        if triggered and group not in scoring:
            errors.append(
                f"scoring.{group} is required because roster.{slot} is {actual} ({condition})"
            )

    _validate_bands(scoring, rules, errors)


def _validate_bands(scoring: dict, rules: dict, errors: list[str]) -> None:
    band_rules = rules.get("banded", {})
    for dotted in band_rules.get("fields", []):
        group, key = dotted.split(".", 1)
        mapping = (scoring.get(group) or {}).get(key)
        if mapping is None:
            continue  # required-key check already covers absence
        if not isinstance(mapping, dict):
            errors.append(f"scoring.{dotted} must be a mapping of band -> points")
            continue

        try:
            bands = build_bands(mapping)
        except (ValueError, TypeError) as exc:
            errors.append(f"scoring.{dotted} has an unparseable band key: {exc}")
            continue

        if band_rules.get("require_start_at_zero") and bands and bands[0].lo != 0:
            errors.append(f"scoring.{dotted} must start at 0, starts at {bands[0].lo}")
        if band_rules.get("require_open_top"):
            if bands and bands[-1].hi is not None:
                errors.append(
                    f"scoring.{dotted} must end open-topped (e.g. \"{bands[-1].lo}+\") so no "
                    f"real game falls outside every band"
                )
        if band_rules.get("require_contiguous"):
            for prev, nxt in zip(bands, bands[1:], strict=False):
                if prev.hi is None:
                    errors.append(f"scoring.{dotted}: open-ended band {prev.key!r} is not last")
                elif nxt.lo != prev.hi + 1:
                    gap = "overlaps" if nxt.lo <= prev.hi else "leaves a gap after"
                    errors.append(
                        f"scoring.{dotted}: band {nxt.key!r} {gap} band {prev.key!r}"
                    )


def validate(raw: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Return every validation error. Empty list means the config is usable."""
    errors: list[str] = []
    _validate_top_level(raw, schema, errors)
    _validate_cross_field(raw, schema, errors)
    _validate_roster(raw, schema, errors)
    _validate_scoring(raw, schema, errors)
    return errors


def league_path(name_or_path: str | Path) -> Path:
    """Accept either a league name ("main") or a path to a YAML file."""
    candidate = Path(name_or_path)
    if candidate.suffix in {".yml", ".yaml"}:
        return resolve(candidate)
    return LEAGUES_DIR / f"{name_or_path}.yml"


def load_league(name_or_path: str | Path = "main") -> League:
    path = league_path(name_or_path)
    raw = load_yaml(path)
    schema = load_yaml(SCHEMA_PATH)

    errors = validate(raw, schema)
    if errors:
        raise LeagueValidationError(path, errors)

    return League(
        name=raw["name"],
        teams=raw["teams"],
        format=raw["format"],
        playoff_teams=raw["playoff_teams"],
        playoff_start_week=raw["playoff_start_week"],
        trade_deadline_week=raw["trade_deadline_week"],
        ir_slots=raw["ir_slots"],
        waivers=raw["waivers"],
        roster=dict(raw["roster"]),
        scoring=raw["scoring"],
        schema=schema,
        source_path=str(path),
    )
