"""Loader for data/wireless.yaml (schema ``contracts.wireless.WirelessFile``); only this arm reads it."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from contracts.wireless import WirelessFile
from utils.data import DataError, data_dir

WIRELESS_FILE = "wireless.yaml"


def load_wireless(data_root: str | Path | None = None) -> WirelessFile:
    path = data_dir(data_root) / WIRELESS_FILE
    if not path.exists():
        raise DataError(f"wireless data not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return WirelessFile.model_validate(raw)
    except yaml.YAMLError as exc:
        raise DataError(f"{path}: invalid YAML: {exc}") from exc
    except ValidationError as exc:
        raise DataError(f"{path}: does not match contracts.WirelessFile:\n{exc}") from exc
