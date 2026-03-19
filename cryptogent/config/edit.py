from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BinanceCredentialUpdate:
    api_key: str | None = None
    api_secret: str | None = None
    base_url: str | None = None
    testnet: bool | None = None
    testnet_api_key: str | None = None
    testnet_api_secret: str | None = None
    spot_bnb_burn: bool | str | None = None
    testnet_spot_bnb_burn: bool | str | None = None


def _is_section(line: str) -> bool:
    s = line.strip()
    return s.startswith("[") and s.endswith("]") and len(s) >= 3


def _section_name(line: str) -> str:
    return line.strip()[1:-1].strip()


def _set_kv_in_section(lines: list[str], *, section: str, key: str, value_repr: str) -> list[str]:
    """
    Minimal TOML line editor:
    - Finds `[section]` and updates `key = ...` inside it.
    - Preserves comments/unknown keys/other sections as-is.
    - If section or key is missing, it is added.
    """
    out = lines[:]

    sec_start = None
    for i, line in enumerate(out):
        if _is_section(line) and _section_name(line) == section:
            sec_start = i
            break

    if sec_start is None:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"[{section}]")
        out.append(f"{key} = {value_repr}")
        out.append("")
        return out

    # Find section end (next section header or EOF)
    sec_end = len(out)
    for j in range(sec_start + 1, len(out)):
        if _is_section(out[j]):
            sec_end = j
            break

    # Update existing key, if present
    needle = key.strip()
    for k in range(sec_start + 1, sec_end):
        raw = out[k]
        stripped = raw.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        left, _right = stripped.split("=", 1)
        if left.strip() == needle:
            indent = raw[: len(raw) - len(stripped)]
            out[k] = f"{indent}{needle} = {value_repr}"
            return out

    # Insert key near the top of the section (right after section header / any blank lines/comments).
    insert_at = sec_start + 1
    while insert_at < sec_end and out[insert_at].strip().startswith("#"):
        insert_at += 1
    out.insert(insert_at, f"{needle} = {value_repr}")
    return out


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"

def _toml_value(value: bool | str) -> str:
    if isinstance(value, bool):
        return _toml_bool(value)
    return _toml_str(value)


def update_binance_config(config_path: Path, update: BinanceCredentialUpdate) -> None:
    config_path = config_path.expanduser()
    raw = config_path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    if update.base_url is not None:
        lines = _set_kv_in_section(lines, section="binance", key="base_url", value_repr=_toml_str(update.base_url))
    if update.testnet is not None:
        lines = _set_kv_in_section(lines, section="binance", key="testnet", value_repr=_toml_bool(update.testnet))
    if update.api_key is not None:
        lines = _set_kv_in_section(lines, section="binance", key="api_key", value_repr=_toml_str(update.api_key))
    if update.api_secret is not None:
        lines = _set_kv_in_section(lines, section="binance", key="api_secret", value_repr=_toml_str(update.api_secret))
    if update.testnet_api_key is not None:
        lines = _set_kv_in_section(lines, section="binance_testnet", key="api_key", value_repr=_toml_str(update.testnet_api_key))
    if update.testnet_api_secret is not None:
        lines = _set_kv_in_section(
            lines, section="binance_testnet", key="api_secret", value_repr=_toml_str(update.testnet_api_secret)
        )
    if update.spot_bnb_burn is not None:
        lines = _set_kv_in_section(lines, section="binance", key="spot_bnb_burn", value_repr=_toml_value(update.spot_bnb_burn))
    if update.testnet_spot_bnb_burn is not None:
        lines = _set_kv_in_section(
            lines, section="binance_testnet", key="spot_bnb_burn", value_repr=_toml_value(update.testnet_spot_bnb_burn)
        )

    # Always end with a newline.
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_binance_credentials(config_path: Path, update: BinanceCredentialUpdate) -> None:
    # Backward-compatible alias.
    update_binance_config(config_path, update)
