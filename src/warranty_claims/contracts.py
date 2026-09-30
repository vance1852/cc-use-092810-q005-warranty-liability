"""保修责任与索赔领域的输入契约与校验。

设计要点：
- 条款（WarrantyTermsInput）按版本登记，起止条件、覆盖范围、排除条款全部可结构化比较；
- 配置版本（ConfigurationInput）以装配/维修事件为边界，槽位上记录组件实例与当时挂载的条款版本；
- 索赔受理只接受结构化标识与证据摘要，证据正文由外部保全系统保存，本服务冻结其指纹。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
COMPONENT_KINDS = {"pack_shell", "module", "bms", "cell", "harness", "thermal", "other"}
WARRANTY_BASIS = {"calendar", "throughput", "cycle", "composite"}
CONFIG_EVENTS = {"assembly", "repair", "inspection", "decommission"}
DECISION_KINDS = {
    "investigation",
    "evidence_supplement",
    "liability_allocation",
    "settlement",
    "rejection",
    "reopen",
    "evidence_release",
}
TERMINAL_KINDS = {"settlement", "rejection"}
LIABILITY_STATUS = {"proposed", "disputed", "confirmed"}
HOLDSCOPE = {"all", "partial"}


def _text(value: object, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        from .errors import ValidationFailed

        raise ValidationFailed(f"{name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        from .errors import ValidationFailed

        raise ValidationFailed(f"{name} 不能超过 {maximum} 个字符")
    return result


def _identifier(value: object, name: str) -> str:
    from .errors import ValidationFailed

    result = _text(value, name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{name} 格式不正确")
    return result


def _choice(value: object, name: str, choices: set[str]) -> str:
    from .errors import ValidationFailed

    result = _text(value, name, 32)
    if result not in choices:
        raise ValidationFailed(f"{name} 必须是 {sorted(choices)} 之一")
    return result


def _time(value: object, name: str) -> str:
    from .errors import ValidationFailed

    text = _text(value, name, 40)
    try:
        return parse_utc(text, name).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _amount(value: object, name: str) -> str:
    from .errors import ValidationFailed

    if isinstance(value, bool):
        raise ValidationFailed(f"{name} 必须是非负十进制金额")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{name} 必须是十进制金额") from exc
    if not result.is_finite() or result < 0:
        raise ValidationFailed(f"{name} 必须是非负有限金额")
    return format(result, "f")


def _share(value: object, name: str) -> str:
    """责任分摊份额：0 到 1 之间的小数，序列化为规范文本。"""

    from .errors import ValidationFailed

    if isinstance(value, bool):
        raise ValidationFailed(f"{name} 必须是 0 到 1 之间的小数")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{name} 必须是十进制小数") from exc
    if not result.is_finite() or not Decimal("0") <= result <= Decimal("1"):
        raise ValidationFailed(f"{name} 必须落在 [0, 1]")
    return format(result, "f")


def _string_list(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    from .errors import ValidationFailed

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationFailed(f"{name} 必须是字符串数组")
    items = tuple(item.strip() for item in value if item.strip())
    if not allow_empty and not items:
        raise ValidationFailed(f"{name} 至少包含一项")
    if len(items) != len({item.lower() for item in items}):
        raise ValidationFailed(f"{name} 不能包含重复项")
    return items


@dataclass(frozen=True, slots=True)
class WarrantyTermsInput:
    terms_id: str
    version: int
    component_kind: str
    warrantor_id: str
    title: str
    coverage: tuple[str, ...]
    exclusions: tuple[str, ...]
    start_basis: str
    start_condition: str
    end_basis: str
    end_condition: str
    liability_limit_cny: str
    notes: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WarrantyTermsInput":
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            from .errors import ValidationFailed

            raise ValidationFailed("version 必须是正整数")
        return cls(
            terms_id=_identifier(raw.get("terms_id"), "terms_id"),
            version=version,
            component_kind=_choice(raw.get("component_kind"), "component_kind", set(COMPONENT_KINDS)),
            warrantor_id=_identifier(raw.get("warrantor_id"), "warrantor_id"),
            title=_text(raw.get("title"), "title"),
            coverage=_string_list(raw.get("coverage"), "coverage"),
            exclusions=_string_list(raw.get("exclusions"), "exclusions", allow_empty=True),
            start_basis=_choice(raw.get("start_basis"), "start_basis", set(WARRANTY_BASIS)),
            start_condition=_text(raw.get("start_condition"), "start_condition"),
            end_basis=_choice(raw.get("end_basis"), "end_basis", set(WARRANTY_BASIS)),
            end_condition=_text(raw.get("end_condition"), "end_condition"),
            liability_limit_cny=_amount(raw.get("liability_limit_cny", 0), "liability_limit_cny"),
            notes=_text(raw.get("notes", ""), "notes", 2000) if raw.get("notes") else "",
        )


@dataclass(frozen=True, slots=True)
class SlotInput:
    slot_id: str
    component_serial: str
    component_kind: str
    terms_id: str
    terms_version: int
    fitted_at: str
    action: str


@dataclass(frozen=True, slots=True)
class ConfigurationInput:
    config_id: str
    pack_serial: str
    event_type: str
    event_at: str
    supplier_id: str
    work_order_ref: str
    slots: tuple[SlotInput, ...]
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConfigurationInput":
        raw_slots = raw.get("slots")
        if not isinstance(raw_slots, list) or not raw_slots:
            from .errors import ValidationFailed

            raise ValidationFailed("slots 必须是非空数组")
        slots: list[SlotInput] = []
        seen_slots: set[str] = set()
        seen_serials: set[str] = set()
        for index, item in enumerate(raw_slots):
            if not isinstance(item, Mapping):
                from .errors import ValidationFailed

                raise ValidationFailed(f"slots[{index}] 必须是对象")
            slot_id = _identifier(item.get("slot_id"), f"slots[{index}].slot_id")
            serial = _identifier(item.get("component_serial"), f"slots[{index}].component_serial")
            if slot_id in seen_slots:
                from .errors import ValidationFailed

                raise ValidationFailed(f"槽位编号重复: {slot_id}")
            if serial in seen_serials:
                from .errors import ValidationFailed

                raise ValidationFailed(f"同一配置中组件序列号重复: {serial}")
            seen_slots.add(slot_id)
            seen_serials.add(serial)
            version = item.get("terms_version")
            if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
                from .errors import ValidationFailed

                raise ValidationFailed(f"slots[{index}].terms_version 必须是正整数")
            slots.append(
                SlotInput(
                    slot_id=slot_id,
                    component_serial=serial,
                    component_kind=_choice(
                        item.get("component_kind"), f"slots[{index}].component_kind", set(COMPONENT_KINDS)
                    ),
                    terms_id=_identifier(item.get("terms_id"), f"slots[{index}].terms_id"),
                    terms_version=version,
                    fitted_at=_time(item.get("fitted_at"), f"slots[{index}].fitted_at"),
                    action=_text(item.get("action", "装配"), f"slots[{index}].action", 64),
                )
            )
        return cls(
            config_id=_identifier(raw.get("config_id"), "config_id"),
            pack_serial=_identifier(raw.get("pack_serial"), "pack_serial"),
            event_type=_choice(raw.get("event_type"), "event_type", set(CONFIG_EVENTS)),
            event_at=_time(raw.get("event_at"), "event_at"),
            supplier_id=_identifier(raw.get("supplier_id"), "supplier_id"),
            work_order_ref=_text(raw.get("work_order_ref"), "work_order_ref", 96),
            slots=tuple(slots),
            note=_text(raw.get("note", ""), "note", 2000) if raw.get("note") else "",
        )


@dataclass(frozen=True, slots=True)
class ClaimItemInput:
    slot_id: str
    component_serial: str
    symptom: str


@dataclass(frozen=True, slots=True)
class ClaimInput:
    claim_id: str
    pack_serial: str
    failure_occurred_at: str
    reported_by_owner_id: str
    fault_fingerprint: str
    evidence_items: tuple[Mapping[str, Any], ...]
    claimed_items: tuple[ClaimItemInput, ...]
    summary: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ClaimInput":
        raw_evidence = raw.get("evidence_items", [])
        if not isinstance(raw_evidence, list) or not raw_evidence:
            from .errors import ValidationFailed

            raise ValidationFailed("evidence_items 必须是非空数组")
        evidence: list[Mapping[str, Any]] = []
        seen_refs: set[str] = set()
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, Mapping):
                from .errors import ValidationFailed

                raise ValidationFailed(f"evidence_items[{index}] 必须是对象")
            ref = _identifier(item.get("evidence_ref"), f"evidence_items[{index}].evidence_ref")
            if ref in seen_refs:
                from .errors import ValidationFailed

                raise ValidationFailed(f"证据编号重复: {ref}")
            seen_refs.add(ref)
            digest = item.get("content_sha256")
            if not isinstance(digest, str) or len(digest.strip()) != 64:
                from .errors import ValidationFailed

                raise ValidationFailed(f"evidence_items[{index}].content_sha256 必须是 64 位 SHA-256")
            evidence.append(
                {
                    "evidence_ref": ref,
                    "kind": _text(item.get("kind"), f"evidence_items[{index}].kind", 48),
                    "content_sha256": digest.strip().lower(),
                    "captured_at": _time(item.get("captured_at"), f"evidence_items[{index}].captured_at"),
                    "note": _text(item.get("note", ""), f"evidence_items[{index}].note", 1000)
                    if item.get("note")
                    else "",
                }
            )
        raw_items = raw.get("claimed_items", [])
        if not isinstance(raw_items, list) or not raw_items:
            from .errors import ValidationFailed

            raise ValidationFailed("claimed_items 必须是非空数组")
        items: list[ClaimItemInput] = []
        seen_pairs: set[tuple[str, str]] = set()
        for index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                from .errors import ValidationFailed

                raise ValidationFailed(f"claimed_items[{index}] 必须是对象")
            slot_id = _identifier(item.get("slot_id"), f"claimed_items[{index}].slot_id")
            serial = _identifier(item.get("component_serial"), f"claimed_items[{index}].component_serial")
            if (slot_id, serial) in seen_pairs:
                from .errors import ValidationFailed

                raise ValidationFailed(f"索赔组件重复: {slot_id}/{serial}")
            seen_pairs.add((slot_id, serial))
            items.append(
                ClaimItemInput(
                    slot_id=slot_id,
                    component_serial=serial,
                    symptom=_text(item.get("symptom"), f"claimed_items[{index}].symptom", 1000),
                )
            )
        return cls(
            claim_id=_identifier(raw.get("claim_id"), "claim_id"),
            pack_serial=_identifier(raw.get("pack_serial"), "pack_serial"),
            failure_occurred_at=_time(raw.get("failure_occurred_at"), "failure_occurred_at"),
            reported_by_owner_id=_identifier(raw.get("reported_by_owner_id"), "reported_by_owner_id"),
            fault_fingerprint=_identifier(raw.get("fault_fingerprint"), "fault_fingerprint"),
            evidence_items=tuple(evidence),
            claimed_items=tuple(items),
            summary=_text(raw.get("summary"), "summary", 2000),
        )


@dataclass(frozen=True, slots=True)
class LiabilityShareInput:
    party_id: str
    share: str
    status: str
    rationale: str


@dataclass(frozen=True, slots=True)
class SettlementItemInput:
    component_serial: str
    party_id: str
    amount_cny: str
    remedy: str


def parse_liability_shares(raw: object) -> tuple[LiabilityShareInput, ...]:
    from .errors import ValidationFailed

    if not isinstance(raw, list) or not raw:
        raise ValidationFailed("liability_shares 必须是非空数组")
    result: list[LiabilityShareInput] = []
    seen: set[str] = set()
    total = Decimal("0")
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"liability_shares[{index}] 必须是对象")
        party_id = _identifier(item.get("party_id"), f"liability_shares[{index}].party_id")
        if party_id in seen:
            raise ValidationFailed(f"责任方重复: {party_id}")
        seen.add(party_id)
        share_text = _share(item.get("share"), f"liability_shares[{index}].share")
        total += Decimal(share_text)
        result.append(
            LiabilityShareInput(
                party_id=party_id,
                share=share_text,
                status=_choice(
                    item.get("status", "proposed"),
                    f"liability_shares[{index}].status",
                    set(LIABILITY_STATUS),
                ),
                rationale=_text(item.get("rationale", ""), f"liability_shares[{index}].rationale", 2000)
                if item.get("rationale")
                else "",
            )
        )
    if total != Decimal("1"):
        raise ValidationFailed(f"责任份额之和必须等于 1，当前为 {format(total, 'f')}")
    return tuple(result)


def parse_settlement_items(raw: object) -> tuple[SettlementItemInput, ...]:
    from .errors import ValidationFailed

    if not isinstance(raw, list) or not raw:
        raise ValidationFailed("settlement_items 必须是非空数组")
    result: list[SettlementItemInput] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"settlement_items[{index}] 必须是对象")
        serial = _identifier(item.get("component_serial"), f"settlement_items[{index}].component_serial")
        party_id = _identifier(item.get("party_id"), f"settlement_items[{index}].party_id")
        if (serial, party_id) in seen:
            raise ValidationFailed(f"赔付项重复: {serial}/{party_id}")
        seen.add((serial, party_id))
        result.append(
            SettlementItemInput(
                component_serial=serial,
                party_id=party_id,
                amount_cny=_amount(item.get("amount_cny"), f"settlement_items[{index}].amount_cny"),
                remedy=_choice(item.get("remedy"), f"settlement_items[{index}].remedy",
                               {"repair", "replace", "refund", "credit", "service"}),
            )
        )
    return tuple(result)
