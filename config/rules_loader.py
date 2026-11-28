"""Version-controlled statutory tax rules loader."""
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
import yaml

RULES_FILE = Path(__file__).parent / "tax_rules.yaml"


def _parse_date(d: Union[date, str]) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def load_all_rules() -> List[Dict[str, Any]]:
    """Load the complete raw list of version-controlled tax rules."""
    if not RULES_FILE.exists():
        return []
    with open(RULES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("rules", [])


def get_rules_for_date(target_date: Optional[Union[date, str]] = None) -> List[Dict[str, Any]]:
    """Return all statutory rules in effect on a specific transaction or filing date.
    
    If target_date is omitted, defaults to today.
    """
    check_date = _parse_date(target_date) if target_date else date.today()
    all_rules = load_all_rules()
    active_rules: List[Dict[str, Any]] = []

    for rule in all_rules:
        eff_from = _parse_date(rule["effective_from"])
        eff_to = _parse_date(rule["effective_to"]) if rule.get("effective_to") else None

        if eff_from <= check_date:
            if eff_to is None or check_date <= eff_to:
                active_rules.append(rule)

    return active_rules


def get_rule_by_id(rule_id: str, target_date: Optional[Union[date, str]] = None) -> Optional[Dict[str, Any]]:
    """Retrieve a specific rule active on a given date."""
    active = get_rules_for_date(target_date)
    for r in active:
        if r.get("id") == rule_id:
            return r
    return None
