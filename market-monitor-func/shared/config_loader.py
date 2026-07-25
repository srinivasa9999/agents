"""Loads config.yaml and nse_holidays.json from disk (no caching across cold
starts is fine - these files are tiny and read once per invocation)."""
import json
import logging
import os

import yaml

logger = logging.getLogger("market_monitor.config")

_DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")
_DEFAULT_HOLIDAYS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "nse_holidays.json")


def load_config() -> dict:
    path = os.environ.get("CONFIG_PATH", _DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not config:
        raise ValueError(f"Config file at {path} is empty or invalid")
    return config


def load_holidays() -> set:
    path = os.environ.get("HOLIDAYS_PATH", _DEFAULT_HOLIDAYS_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error("Holiday file not found at %s - treating every weekday as a trading day", path)
        return set()

    holidays = {entry["date"] for entry in data.get("holidays", [])}
    todo = data.get("unconfirmed_variable_holidays_todo")
    if todo:
        logger.warning(
            "nse_holidays.json has %d unconfirmed variable-date holidays not in the skip list: %s. "
            "Verify against the official NSE holiday circular and fill these in.",
            len(todo), ", ".join(todo),
        )
    return holidays
