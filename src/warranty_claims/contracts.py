"""保修与索赔领域输入契约。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

COMPONENT_KINDS = {"enclosure", "module", "bms", "cell", "other"}
CHANGE_TYPES = {"assembly", "repair", "replacement"}
TERM_EFFECTS = {"reset", "continue", "new_full"}
PARTY_KINDS = {"oem", "repairer", "supplier", "integrator", "insurer", "customer"}
EVIDENCE_KINDS = {"diagnostic", "report", "photo", "log", "measurement", "other"}


def required_text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, field: str, maximum: int = 1024) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} 必须是文本")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def choice(value: object, field: str, allowed: set[str]) -> str:
    result = required_text(value, field, 32)
    if result not in allowed:
        raise ValidationFailed(f"{field} 必须是 {sorted(allowed)} 之一")
    return result


def digest(value: object, field: str = "content_sha256") -> str:
    result = required_text(value, field, 64).lower()
    if not SHA256.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制 SHA-256")
    return result


def timestamp(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return parse_utc(text, field).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def money(value: object, field: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是十进制金额")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制金额") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if result < 0 or (not allow_zero and result == 0):
        raise ValidationFailed(f"{field} 必须{'大于' if not allow_zero else '不小于'}零")
    return result.quantize(Decimal("0.01"))


def positive_int(value: object, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    if maximum is not None and value > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum}")
    return value


def string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationFailed(f"{field} 必须是非空字符串数组")
    items: list[str] = []
    for index, item in enumerate(value):
        items.append(required_text(item, f"{field}[{index}]", 256))
    return tuple(items)


def optional_string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationFailed(f"{field} 必须是字符串数组")
    items: list[str] = []
    for index, item in enumerate(value):
        items.append(required_text(item, f"{field}[{index}]", 256))
    return tuple(items)


def evidence_item(raw: Mapping[str, Any], index: int | None = None) -> dict[str, str]:
    prefix = "证据" if index is None else f"证据[{index}]"
    if not isinstance(raw, Mapping):
        raise ValidationFailed(f"{prefix} 必须是对象")
    return {
        "label": required_text(raw.get("label"), f"{prefix}.label", 128),
        "kind": choice(raw.get("kind"), f"{prefix}.kind", EVIDENCE_KINDS),
        "content_sha256": digest(raw.get("content_sha256"), f"{prefix}.content_sha256"),
    }


def evidence_list(value: object, field: str = "evidence") -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    return tuple(evidence_item(item, index) for index, item in enumerate(value))
