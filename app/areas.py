"""Fast, local delivery-area answers used before full address resolution."""
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AreaRule:
    name: str
    aliases: tuple[str, ...]
    outcome: str  # within | outside | detail
    message: str


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def load_area_rules(path: str) -> list[AreaRule]:
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    return [AreaRule(name=r["name"], aliases=tuple(_key(a) for a in r["aliases"]),
                     outcome=r["outcome"], message=r["message"]) for r in records]


def match_area(text: str, rules: list[AreaRule]) -> AreaRule | None:
    value = _key(text)
    if not value:
        return None
    return next((rule for rule in rules if any(alias in value or value in alias for alias in rule.aliases)), None)
