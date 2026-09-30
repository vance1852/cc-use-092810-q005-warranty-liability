"""保修责任与索赔管理领域用例。

责任链：
    责任方/组件目录 -> 版本化条款 -> 装配/维修配置版本 -> 索赔受理冻结
    -> 有版本的决定（调查/补证/责任分摊/和解/驳回/复开/证据放行）
    -> 客户通知（结论冻结）-> 剩余保修延续登记。

不可变保证：
- 条款、配置、决定只追加，不提供更新或删除接口；
- 已通知客户的终局决定永久保留 notified_at，复开只产生新 revision；
- 故障指纹在 fault_payouts 中全局唯一，杜绝重复赔付；
- 证据保全按责任方持有，只有索赔整体终局后才能统一放行；
  部分责任方确认份额不会释放任何一方的保全。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, isoformat
from .contracts import (
    ClaimInput,
    ConfigurationInput,
    LiabilityShareInput,
    SettlementItemInput,
    WarrantyTermsInput,
    parse_liability_shares,
    parse_settlement_items,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .evaluation import evaluate
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS: Mapping[str, set[str]] = {
    "service_agent": {
        "party.read", "component.read", "warranty.read",
        "claim.open", "claim.read",
        "decision.investigate", "decision.supplement",
    },
    "warranty_manager": {
        "party.read", "party.write", "component.write",
        "terms.write", "ownership.write", "configuration.write",
        "claim.read", "decision.allocate", "decision.reopen",
        "evidence.release", "warranty.continue", "decision.notify",
    },
    "claims_adjuster": {
        "claim.read", "decision.investigate", "decision.supplement",
        "decision.allocate", "decision.settle", "decision.reject", "decision.notify",
    },
    "finance": {"claim.read", "payout.read"},
    "auditor": {"claim.read", "audit.read", "warranty.read"},
}

TERMINAL_STATUSES = {"settled", "rejected", "closed"}
OPEN_STATUSES = {"open", "investigating", "allocation", "reopened"}


class WarrantyClaimsService:
    """在单个 SQLite 连接上提供全部保修与索赔操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM wc_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO wc_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO wc_users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_warrantor(
        self, actor_id: str, party_id: str, display_name: str, party_type: str, contact: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "party.write")
        if party_type not in {"oem", "repair_vendor", "component_supplier", "insurer", "internal", "other"}:
            raise ValidationFailed("未知责任方类型")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO warrantors(party_id,display_name,party_type,contact,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (party_id, display_name, party_type, contact, self._now()),
                )
                self._audit("warrantor", party_id, "warrantor.registered", actor_id, {"party_type": party_type})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"责任方已存在: {party_id}") from exc
        return {"party_id": party_id, "party_type": party_type}

    def register_component(
        self, actor_id: str, component_serial: str, component_kind: str, source_party_id: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "component.write")
        if component_kind not in {
            "pack_shell", "module", "bms", "cell", "harness", "thermal", "other"
        }:
            raise ValidationFailed("未知组件类别")
        if source_party_id is not None and self.connection.execute(
            "SELECT 1 FROM warrantors WHERE party_id=?", (source_party_id,)
        ).fetchone() is None:
            raise NotFound(f"来源责任方不存在: {source_party_id}")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO components(component_serial,component_kind,source_party_id,created_at) "
                    "VALUES(?,?,?,?)",
                    (component_serial, component_kind, source_party_id, self._now()),
                )
                self._audit("component", component_serial, "component.registered", actor_id,
                            {"component_kind": component_kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"组件序列号已存在: {component_serial}") from exc
        return {"component_serial": component_serial, "component_kind": component_kind}

    # ------------------------------------------------------------- 条款版本

    def publish_terms(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "terms.write")
        terms = WarrantyTermsInput.from_dict(raw)
        if self.connection.execute(
            "SELECT 1 FROM warrantors WHERE party_id=?", (terms.warrantor_id,)
        ).fetchone() is None:
            raise NotFound(f"责任方不存在: {terms.warrantor_id}")
        digest = content_digest([{
            "terms_id": terms.terms_id,
            "version": terms.version,
            "component_kind": terms.component_kind,
            "warrantor_id": terms.warrantor_id,
            "title": terms.title,
            "coverage": list(terms.coverage),
            "exclusions": list(terms.exclusions),
            "start_basis": terms.start_basis,
            "start_condition": terms.start_condition,
            "end_basis": terms.end_basis,
            "end_condition": terms.end_condition,
            "liability_limit_cny": terms.liability_limit_cny,
            "notes": terms.notes,
        }])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE warranty_terms SET superseded_at=? "
                    "WHERE terms_id=? AND version<? AND superseded_at IS NULL",
                    (self._now(), terms.terms_id, terms.version),
                )
                self.connection.execute(
                    "INSERT INTO warranty_terms(terms_id,version,component_kind,warrantor_id,title,"
                    "coverage_json,exclusions_json,start_basis,start_condition,end_basis,end_condition,"
                    "liability_limit_cny,notes,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        terms.terms_id, terms.version, terms.component_kind, terms.warrantor_id,
                        terms.title, canonical_json(list(terms.coverage)),
                        canonical_json(list(terms.exclusions)),
                        terms.start_basis, terms.start_condition, terms.end_basis, terms.end_condition,
                        terms.liability_limit_cny, terms.notes, digest, actor_id, self._now(),
                    ),
                )
                self._audit("warranty_terms", f"{terms.terms_id}@v{terms.version}",
                            "terms.published", actor_id, {"sha256": digest})
        except sqlite3.IntegrityError as exc:
            raise Conflict("条款编号版本冲突或责任方不存在") from exc
        return {"terms_id": terms.terms_id, "version": terms.version, "content_sha256": digest}

    def get_terms(self, terms_id: str, version: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM warranty_terms WHERE terms_id=? AND version=?", (terms_id, version)
        ).fetchone()
        if row is None:
            raise NotFound("条款版本不存在")
        result = dict(row)
        result["coverage"] = json.loads(result.pop("coverage_json"))
        result["exclusions"] = json.loads(result.pop("exclusions_json"))
        return result

    # --------------------------------------------------------- 所有权与配置

    def record_ownership(
        self, actor_id: str, pack_serial: str, owner_id: str, valid_from: str
    ) -> dict[str, Any]:
        self._require(actor_id, "ownership.write")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE pack_ownership SET valid_to=? "
                "WHERE pack_serial=? AND valid_to IS NULL AND valid_from<?",
                (valid_from, pack_serial, valid_from),
            )
            cursor = self.connection.execute(
                "INSERT INTO pack_ownership(pack_serial,owner_id,valid_from,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?)",
                (pack_serial, owner_id, valid_from, actor_id, self._now()),
            )
            ownership_id = cursor.lastrowid
            self._audit("pack", pack_serial, "ownership.recorded", actor_id,
                        {"ownership_id": ownership_id, "owner_id": owner_id, "valid_from": valid_from})
        return {"ownership_id": ownership_id, "pack_serial": pack_serial,
                "owner_id": owner_id, "valid_from": valid_from}

    def record_configuration(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "configuration.write")
        config = ConfigurationInput.from_dict(raw)
        with transaction(self.connection, immediate=True):
            last = self.connection.execute(
                "SELECT sequence_no, event_at FROM configurations WHERE pack_serial=? "
                "ORDER BY sequence_no DESC LIMIT 1",
                (config.pack_serial,),
            ).fetchone()
            sequence_no = 1 if last is None else last["sequence_no"] + 1
            slot_payload = []
            for slot in config.slots:
                terms = self.connection.execute(
                    "SELECT component_kind FROM warranty_terms WHERE terms_id=? AND version=?",
                    (slot.terms_id, slot.terms_version),
                ).fetchone()
                if terms is None:
                    raise NotFound(f"槽位 {slot.slot_id} 引用的条款版本不存在: {slot.terms_id}@v{slot.terms_version}")
                if terms["component_kind"] != slot.component_kind:
                    raise ValidationFailed(
                        f"槽位 {slot.slot_id} 组件类别与条款适用类别不一致: "
                        f"{slot.component_kind} != {terms['component_kind']}"
                    )
                component = self.connection.execute(
                    "SELECT 1 FROM components WHERE component_serial=?", (slot.component_serial,)
                ).fetchone()
                if component is None:
                    raise NotFound(f"组件未登记: {slot.component_serial}")
                if slot.fitted_at > config.event_at:
                    raise ValidationFailed(f"槽位 {slot.slot_id} 的装入时间晚于配置事件时间")
                slot_payload.append({
                    "slot_id": slot.slot_id,
                    "component_serial": slot.component_serial,
                    "component_kind": slot.component_kind,
                    "terms_id": slot.terms_id,
                    "terms_version": slot.terms_version,
                    "fitted_at": slot.fitted_at,
                    "action": slot.action,
                })
            digest = content_digest([{
                "pack_serial": config.pack_serial,
                "sequence_no": sequence_no,
                "event_type": config.event_type,
                "event_at": config.event_at,
                "supplier_id": config.supplier_id,
                "work_order_ref": config.work_order_ref,
                "slots": slot_payload,
            }])
            try:
                self.connection.execute(
                    "INSERT INTO configurations(config_id,pack_serial,sequence_no,event_type,event_at,"
                    "supplier_id,work_order_ref,note,content_sha256,recorded_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (config.config_id, config.pack_serial, sequence_no, config.event_type, config.event_at,
                     config.supplier_id, config.work_order_ref, config.note, digest, actor_id, self._now()),
                )
                for item in slot_payload:
                    self.connection.execute(
                        "INSERT INTO config_slots(config_id,slot_id,component_serial,component_kind,"
                        "terms_id,terms_version,fitted_at,action) VALUES(?,?,?,?,?,?,?,?)",
                        (config.config_id, item["slot_id"], item["component_serial"], item["component_kind"],
                         item["terms_id"], item["terms_version"], item["fitted_at"], item["action"]),
                    )
            except sqlite3.IntegrityError as exc:
                raise Conflict("配置编号冲突或槽位/条款引用不完整") from exc
            self._audit("configuration", config.config_id, "configuration.recorded", actor_id,
                        {"pack_serial": config.pack_serial, "sequence_no": sequence_no, "sha256": digest})
        return {"config_id": config.config_id, "pack_serial": config.pack_serial,
                "sequence_no": sequence_no, "content_sha256": digest}

    def configuration_history(self, pack_serial: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM configurations WHERE pack_serial=? ORDER BY sequence_no", (pack_serial,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["slots"] = [
                dict(slot) for slot in self.connection.execute(
                    "SELECT slot_id,component_serial,component_kind,terms_id,terms_version,fitted_at,action "
                    "FROM config_slots WHERE config_id=? ORDER BY slot_id",
                    (row["config_id"],),
                ).fetchall()
            ]
            result.append(item)
        return result

    def component_warranty_status(self, component_serial: str, at_time: str | None = None) -> dict[str, Any]:
        """供客服回答：某组件当前挂载条款与在指定时点的剩余保修状态。"""

        moment = at_time or self._now()
        row = self.connection.execute(
            "SELECT c.config_id,c.event_at,s.slot_id,s.terms_id,s.terms_version,s.fitted_at "
            "FROM config_slots s JOIN configurations c ON c.config_id=s.config_id "
            "WHERE s.component_serial=? AND c.event_at<=? "
            "ORDER BY c.sequence_no DESC LIMIT 1",
            (component_serial, moment),
        ).fetchone()
        if row is None:
            raise NotFound("该组件在指定时点没有挂载配置")
        terms = self.get_terms(row["terms_id"], row["terms_version"])
        effectiveness = evaluate(
            terms["start_condition"], terms["end_condition"], row["fitted_at"], moment
        )
        return {
            "component_serial": component_serial,
            "config_id": row["config_id"],
            "slot_id": row["slot_id"],
            "terms": {key: terms[key] for key in (
                "terms_id", "version", "warrantor_id", "title", "coverage", "exclusions",
                "start_basis", "start_condition", "end_basis", "end_condition",
                "liability_limit_cny",
            )},
            "effective_start": effectiveness.effective_start,
            "effective_end": effectiveness.effective_end,
            "state": effectiveness.state,
            "note": effectiveness.note,
        }

    # ------------------------------------------------------------- 索赔受理

    def _configuration_at(self, pack_serial: str, moment: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM configurations WHERE pack_serial=? AND event_at<=? "
            "ORDER BY sequence_no DESC LIMIT 1",
            (pack_serial, moment),
        ).fetchone()
        if row is None:
            raise InvalidState(f"故障时点 {moment} 之前整包 {pack_serial} 没有已登记配置")
        return row

    def _owner_at(self, pack_serial: str, moment: str) -> str:
        row = self.connection.execute(
            "SELECT owner_id FROM pack_ownership WHERE pack_serial=? AND valid_from<=? "
            "AND (valid_to IS NULL OR valid_to>?) ORDER BY ownership_id DESC LIMIT 1",
            (pack_serial, moment, moment),
        ).fetchone()
        if row is None:
            raise InvalidState(f"故障时点 {moment} 整包 {pack_serial} 没有登记所有权")
        return row["owner_id"]

    def open_claim(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "claim.open")
        claim = ClaimInput.from_dict(raw)
        if self.connection.execute(
            "SELECT 1 FROM claims WHERE fault_fingerprint=?", (claim.fault_fingerprint,)
        ).fetchone() is not None:
            raise Conflict(f"故障指纹已存在索赔，不得重复受理: {claim.fault_fingerprint}")
        with transaction(self.connection, immediate=True):
            config = self._configuration_at(claim.pack_serial, claim.failure_occurred_at)
            owner_id = self._owner_at(claim.pack_serial, claim.failure_occurred_at)
            slots = self.connection.execute(
                "SELECT * FROM config_slots WHERE config_id=? ORDER BY slot_id", (config["config_id"],)
            ).fetchall()
            slot_map = {(row["slot_id"], row["component_serial"]): row for row in slots}
            claimed_pairs = {(item.slot_id, item.component_serial) for item in claim.claimed_items}
            missing = claimed_pairs - set(slot_map)
            if missing:
                raise ValidationFailed(f"索赔组件不在故障时点配置中: {sorted(missing)}")
            symptom_map = {
                (item.slot_id, item.component_serial): item.symptom for item in claim.claimed_items
            }
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO claims(claim_id,pack_serial,failure_occurred_at,opened_at,"
                    "reported_by_owner_id,fault_fingerprint,frozen_config_id,frozen_owner_id,status,"
                    "current_revision,summary) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                    (claim.claim_id, claim.pack_serial, claim.failure_occurred_at, now,
                     claim.reported_by_owner_id, claim.fault_fingerprint,
                     config["config_id"], owner_id, "open", claim.summary),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("索赔编号或故障指纹冲突") from exc

            frozen_rows = []
            parties: set[str] = set()
            for slot in slots:
                terms = self.connection.execute(
                    "SELECT * FROM warranty_terms WHERE terms_id=? AND version=?",
                    (slot["terms_id"], slot["terms_version"]),
                ).fetchone()
                if terms is None:
                    raise NotFound(f"条款版本缺失: {slot['terms_id']}@v{slot['terms_version']}")
                parties.add(terms["warrantor_id"])
                effectiveness = evaluate(
                    terms["start_condition"], terms["end_condition"],
                    slot["fitted_at"], claim.failure_occurred_at,
                )
                pair = (slot["slot_id"], slot["component_serial"])
                frozen_rows.append((
                    claim.claim_id, slot["slot_id"], slot["component_serial"], slot["component_kind"],
                    slot["fitted_at"], 1 if pair in claimed_pairs else 0, symptom_map.get(pair, ""),
                    slot["terms_id"], slot["terms_version"], terms["warrantor_id"], terms["title"],
                    terms["coverage_json"], terms["exclusions_json"],
                    terms["start_basis"], terms["start_condition"], terms["end_basis"],
                    terms["end_condition"], terms["liability_limit_cny"], terms["content_sha256"],
                    effectiveness.effective_start, effectiveness.effective_end,
                    effectiveness.state, effectiveness.note,
                ))
            self.connection.executemany(
                "INSERT INTO claim_frozen_slots(claim_id,slot_id,component_serial,component_kind,"
                "fitted_at,claimed,symptom,terms_id,terms_version,warrantor_id,title,coverage_json,"
                "exclusions_json,start_basis,start_condition,end_basis,end_condition,"
                "liability_limit_cny,terms_sha256,effective_start,effective_end,"
                "effectiveness_state,effectiveness_note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                frozen_rows,
            )
            for item in claim.evidence_items:
                self.connection.execute(
                    "INSERT INTO claim_evidence(claim_id,evidence_ref,kind,content_sha256,"
                    "captured_at,note,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (claim.claim_id, item["evidence_ref"], item["kind"], item["content_sha256"],
                     item["captured_at"], item["note"], now),
                )
            # 维修动作的执行方也是潜在责任方，即使其不是任何槽位条款的担保人。
            supplier = self.connection.execute(
                "SELECT 1 FROM warrantors WHERE party_id=?", (config["supplier_id"],)
            ).fetchone()
            if supplier is not None:
                parties.add(config["supplier_id"])
            for party_id in sorted(parties):
                self.connection.execute(
                    "INSERT INTO evidence_holds(claim_id,party_id,status,scope,held_at) "
                    "VALUES(?,?,'held','all',?)",
                    (claim.claim_id, party_id, now),
                )
            self._audit("claim", claim.claim_id, "claim.opened", actor_id, {
                "pack_serial": claim.pack_serial,
                "fault_fingerprint": claim.fault_fingerprint,
                "frozen_config_id": config["config_id"],
                "frozen_owner_id": owner_id,
                "evidence_count": len(claim.evidence_items),
                "hold_parties": sorted(parties),
            })
        return self.get_claim(claim.claim_id)

    # ------------------------------------------------------------- 决定推进

    def _claim(self, claim_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound("索赔不存在")
        return row

    def _next_revision(self, claim_id: str) -> int:
        row = self.connection.execute(
            "SELECT coalesce(max(revision),0)+1 FROM claim_decisions WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return int(row[0])

    def _append_decision(
        self,
        claim_id: str,
        kind: str,
        status: str,
        basis: str,
        payload: Mapping[str, Any],
        actor_id: str,
        supersedes_revision: int | None = None,
    ) -> int:
        revision = self._next_revision(claim_id)
        cursor = self.connection.execute(
            "INSERT INTO claim_decisions(claim_id,revision,kind,status,basis,payload_json,"
            "supersedes_revision,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (claim_id, revision, kind, status, basis, canonical_json(payload),
             supersedes_revision, actor_id, self._now()),
        )
        decision_id = int(cursor.lastrowid)
        self.connection.execute(
            "UPDATE claims SET current_revision=?, status=? WHERE claim_id=?",
            (revision, status, claim_id),
        )
        self._audit("claim", claim_id, f"decision.{kind}", actor_id,
                    {"revision": revision, "decision_id": decision_id})
        return decision_id

    def record_investigation(self, actor_id: str, claim_id: str, finding: str) -> dict[str, Any]:
        self._require(actor_id, "decision.investigate")
        claim = self._claim(claim_id)
        if claim["status"] not in OPEN_STATUSES:
            raise InvalidState("索赔已终局，调查结论请先复开")
        with transaction(self.connection, immediate=True):
            decision_id = self._append_decision(
                claim_id, "investigation", "investigating", finding,
                {"finding": finding}, actor_id,
            )
        return {"claim_id": claim_id, "decision_id": decision_id, "revision": self._claim(claim_id)["current_revision"]}

    def supplement_evidence(
        self, actor_id: str, claim_id: str, evidence_items: list[Mapping[str, Any]], reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.supplement")
        claim = self._claim(claim_id)
        if claim["status"] not in OPEN_STATUSES:
            raise InvalidState("索赔已终局，补证前必须先复开")
        if not evidence_items:
            raise ValidationFailed("补证清单不能为空")
        now = self._now()
        parsed = []
        seen: set[str] = set()
        for index, item in enumerate(evidence_items):
            ref = str(item.get("evidence_ref", "")).strip()
            digest_value = str(item.get("content_sha256", "")).strip()
            if not ref or len(digest_value) != 64:
                raise ValidationFailed(f"补证项 {index} 缺少编号或 64 位摘要")
            if ref in seen:
                raise ValidationFailed(f"补证编号重复: {ref}")
            seen.add(ref)
            parsed.append({
                "evidence_ref": ref,
                "kind": str(item.get("kind", "")),
                "content_sha256": digest_value.lower(),
                "captured_at": str(item.get("captured_at", now)),
                "note": str(item.get("note", "")),
            })
        with transaction(self.connection, immediate=True):
            for item in parsed:
                try:
                    self.connection.execute(
                        "INSERT INTO claim_evidence(claim_id,evidence_ref,kind,content_sha256,"
                        "captured_at,note,frozen_at) VALUES(?,?,?,?,?,?,?)",
                        (claim_id, item["evidence_ref"], item["kind"], item["content_sha256"],
                         item["captured_at"], item["note"], now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(f"证据编号已存在: {item['evidence_ref']}") from exc
            # 补证同样进入保全：保全范围保持 held，不允许借补证释放任何一方。
            decision_id = self._append_decision(
                claim_id, "evidence_supplement", claim["status"] if claim["status"] != "open" else "investigating",
                reason, {"evidence_refs": [item["evidence_ref"] for item in parsed]}, actor_id,
            )
        return {"claim_id": claim_id, "decision_id": decision_id,
                "frozen_evidence": [item["evidence_ref"] for item in parsed]}

    def _latest_allocation(self, claim_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM claim_decisions WHERE claim_id=? AND kind='liability_allocation' "
            "ORDER BY revision DESC LIMIT 1",
            (claim_id,),
        ).fetchone()

    def allocate_liability(
        self, actor_id: str, claim_id: str, raw_shares: list[Mapping[str, Any]], basis: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.allocate")
        claim = self._claim(claim_id)
        if claim["status"] not in OPEN_STATUSES:
            raise InvalidState("索赔已终局，调整责任前必须先复开")
        shares = parse_liability_shares(raw_shares)
        frozen = self.connection.execute(
            "SELECT DISTINCT warrantor_id FROM claim_frozen_slots WHERE claim_id=?", (claim_id,)
        ).fetchall()
        allowed = {row["warrantor_id"] for row in frozen}
        config = self.connection.execute(
            "SELECT supplier_id FROM configurations WHERE config_id=?", (claim["frozen_config_id"],)
        ).fetchone()
        allowed.add(config["supplier_id"])
        for share in shares:
            if share.party_id not in allowed:
                raise ValidationFailed(
                    f"责任方 {share.party_id} 既不是冻结配置中的条款担保人，也不是维修执行方"
                )
        previous = self._latest_allocation(claim_id)
        payload = [
            {"party_id": s.party_id, "share": s.share, "status": s.status, "rationale": s.rationale}
            for s in shares
        ]
        with transaction(self.connection, immediate=True):
            decision_id = self._append_decision(
                claim_id, "liability_allocation", "allocation", basis,
                {"shares": payload}, actor_id,
                supersedes_revision=None if previous is None else previous["revision"],
            )
        return {"claim_id": claim_id, "decision_id": decision_id,
                "supersedes_revision": None if previous is None else previous["revision"],
                "shares": payload}

    def _validate_settlement(
        self, claim: sqlite3.Row, items: tuple[SettlementItemInput, ...]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        allocation_row = self._latest_allocation(claim["claim_id"])
        if allocation_row is None:
            raise InvalidState("和解前必须先形成责任分摊决定")
        allocation = json.loads(allocation_row["payload_json"])
        shares = {item["party_id"]: item for item in allocation["shares"]}
        if any(item["status"] != "confirmed" for item in allocation["shares"]):
            raise InvalidState("仍有责任方未确认份额，不能和解；争议未决时应继续调查或重开分摊")
        slots = {
            row["component_serial"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM claim_frozen_slots WHERE claim_id=? AND claimed=1", (claim["claim_id"],)
            ).fetchall()
        }
        mapped: dict[str, Any] = {}
        for item in items:
            if item.component_serial not in slots:
                raise ValidationFailed(f"赔付组件不在索赔范围内: {item.component_serial}")
            if item.party_id not in shares:
                raise ValidationFailed(f"赔付责任方不在最新分摊中: {item.party_id}")
            slot = slots[item.component_serial]
            if item.party_id != slot["warrantor_id"]:
                config = self.connection.execute(
                    "SELECT supplier_id FROM configurations WHERE config_id=?", (claim["frozen_config_id"],)
                ).fetchone()
                if item.party_id != config["supplier_id"]:
                    raise ValidationFailed(
                        f"{item.party_id} 对组件 {item.component_serial} 既非条款担保人也非维修执行方"
                    )
            amount = Decimal(item.amount_cny)
            if item.party_id == slot["warrantor_id"] and amount > Decimal(slot["liability_limit_cny"]):
                raise InvalidState(
                    f"组件 {item.component_serial} 赔付金额超出条款责任上限 "
                    f"{slot['liability_limit_cny']}"
                )
            mapped[item.component_serial] = {
                "party_id": item.party_id,
                "amount_cny": item.amount_cny,
                "remedy": item.remedy,
            }
        return dict(allocation_row), mapped

    def record_settlement(
        self, actor_id: str, claim_id: str, raw_items: list[Mapping[str, Any]], basis: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.settle")
        claim = self._claim(claim_id)
        if claim["status"] not in OPEN_STATUSES:
            raise InvalidState("索赔已经终局")
        items = parse_settlement_items(raw_items)
        allocation_row, mapped = self._validate_settlement(claim, items)
        if self.connection.execute(
            "SELECT 1 FROM fault_payouts WHERE fault_fingerprint=?", (claim["fault_fingerprint"],)
        ).fetchone() is not None:
            raise InvalidState("该故障已完成赔付，同一故障不得重复赔付；迟到结论只能复开登记")
        with transaction(self.connection, immediate=True):
            decision_id = self._append_decision(
                claim_id, "settlement", "settled", basis,
                {"allocation_revision": allocation_row["revision"], "items": list(mapped.values())},
                actor_id,
            )
            self.connection.execute(
                "INSERT INTO fault_payouts(fault_fingerprint,claim_id,decision_id,paid_at) "
                "VALUES(?,?,?,?)",
                (claim["fault_fingerprint"], claim_id, decision_id, self._now()),
            )
        return {"claim_id": claim_id, "decision_id": decision_id, "items": list(mapped.values())}

    def record_rejection(self, actor_id: str, claim_id: str, basis: str) -> dict[str, Any]:
        self._require(actor_id, "decision.reject")
        claim = self._claim(claim_id)
        if claim["status"] not in OPEN_STATUSES:
            raise InvalidState("索赔已经终局")
        with transaction(self.connection, immediate=True):
            decision_id = self._append_decision(
                claim_id, "rejection", "rejected", basis, {"basis": basis}, actor_id,
            )
        return {"claim_id": claim_id, "decision_id": decision_id}

    def record_affirmation(
        self,
        actor_id: str,
        claim_id: str,
        basis: str,
        revised_shares: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """复开调查后的结案决定：维持或修正原结论，但绝不产生第二笔赔付。

        - 原和解维持：不新增 fault_payouts（故障指纹已赔付，禁止重复）；
        - 份额可重新确认/调整并随本决定版本化（仅影响追偿口径，不再对客户付款）；
        - 原驳回维持：直接结案为 closed。
        """

        self._require(actor_id, "decision.allocate")
        claim = self._claim(claim_id)
        if claim["status"] not in OPEN_STATUSES:
            raise InvalidState("只有复开中的索赔可以维持/修正结案")
        notified = self.connection.execute(
            "SELECT kind FROM claim_decisions WHERE decision_id=?",
            (claim["notified_decision_id"],),
        ).fetchone()
        if notified is None:
            raise InvalidState("没有已通知客户的原结论可维持")
        payload: dict[str, Any] = {"affirmed_kind": notified["kind"], "basis": basis}
        if revised_shares is not None:
            shares = parse_liability_shares(revised_shares)
            payload["revised_shares"] = [
                {"party_id": s.party_id, "share": s.share, "status": s.status, "rationale": s.rationale}
                for s in shares
            ]
        with transaction(self.connection, immediate=True):
            # 硬性防线：无论决定内容如何，赔付表对同一故障指纹的唯一约束都在。
            duplicate = self.connection.execute(
                "SELECT 1 FROM fault_payouts WHERE fault_fingerprint=?", (claim["fault_fingerprint"],)
            ).fetchone()
            if notified["kind"] == "settlement" and duplicate is None:
                raise InvalidState("原和解缺少赔付记录，数据异常，拒绝结案")
            decision_id = self._append_decision(
                claim_id, "affirmation", "closed", basis, payload, actor_id,
            )
        return {"claim_id": claim_id, "decision_id": decision_id,
                "affirmed_kind": notified["kind"], "additional_payout": False}

    def notify_customer(
        self, actor_id: str, claim_id: str, decision_id: int, notified_to: str, channel: str
    ) -> dict[str, Any]:
        """把终局决定通知客户；通知后该决定即为冻结结论，不可再改写。"""

        self._require(actor_id, "decision.notify")
        claim = self._claim(claim_id)
        row = self.connection.execute(
            "SELECT * FROM claim_decisions WHERE decision_id=? AND claim_id=?",
            (decision_id, claim_id),
        ).fetchone()
        if row is None:
            raise NotFound("决定不存在")
        if row["kind"] not in {"settlement", "rejection", "affirmation"}:
            raise InvalidState("只有和解、驳回或复开后的维持/修正结论可以通知客户")
        if row["notified_at"] is not None:
            raise InvalidState("该结论已经通知客户，内容不可改写")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE claim_decisions SET notified_at=?, notified_to=? WHERE decision_id=?",
                (now, notified_to, decision_id),
            )
            # 旧结论行原样保留，只把指针向前滚动到新结论。
            self.connection.execute(
                "UPDATE claims SET prior_notified_decision_id=notified_decision_id,"
                "notified_decision_id=?, notified_at=? WHERE claim_id=?",
                (decision_id, now, claim_id),
            )
            self._audit("claim", claim_id, "decision.notified", actor_id,
                        {"decision_id": decision_id, "kind": row["kind"],
                         "notified_to": notified_to, "channel": channel})
        return {"claim_id": claim_id, "decision_id": decision_id,
                "notified_at": now, "immutable": True}

    def reopen_claim(
        self,
        actor_id: str,
        claim_id: str,
        reason: str,
        late_evidence: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """迟到检测只能复开：产生新的 reopen 决定，不动已通知客户的结论。"""

        self._require(actor_id, "decision.reopen")
        claim = self._claim(claim_id)
        if claim["notified_decision_id"] is None:
            raise InvalidState("只有已通知客户的终局结论才需要复开")
        notified = self.connection.execute(
            "SELECT revision,kind FROM claim_decisions WHERE decision_id=?",
            (claim["notified_decision_id"],),
        ).fetchone()
        now = self._now()
        with transaction(self.connection, immediate=True):
            evidence_refs: list[str] = []
            for index, item in enumerate(late_evidence or []):
                ref = str(item.get("evidence_ref", "")).strip()
                digest_value = str(item.get("content_sha256", "")).strip()
                if not ref or len(digest_value) != 64:
                    raise ValidationFailed(f"迟到证据项 {index} 缺少编号或 64 位摘要")
                self.connection.execute(
                    "INSERT INTO claim_evidence(claim_id,evidence_ref,kind,content_sha256,"
                    "captured_at,note,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (claim_id, ref, str(item.get("kind", "late_detection")),
                     digest_value.lower(), str(item.get("captured_at", now)),
                     str(item.get("note", "")), now),
                )
                evidence_refs.append(ref)
            # 重新持有此前已放行的保全（新增 held 行，旧放行行永久保留）；
            # 仍在持有的责任方不重复建行——部分责任方确认份额也从未释放过其保全。
            ever_held = {
                row["party_id"] for row in self.connection.execute(
                    "SELECT DISTINCT party_id FROM evidence_holds WHERE claim_id=?", (claim_id,)
                ).fetchall()
            }
            currently_held = {
                row["party_id"] for row in self.connection.execute(
                    "SELECT DISTINCT party_id FROM evidence_holds WHERE claim_id=? AND status='held'",
                    (claim_id,),
                ).fetchall()
            }
            parties = ever_held - currently_held
            for party_id in sorted(parties):
                self.connection.execute(
                    "INSERT INTO evidence_holds(claim_id,party_id,status,scope,held_at) "
                    "VALUES(?,?,'held','all',?)",
                    (claim_id, party_id, now),
                )
            decision_id = self._append_decision(
                claim_id, "reopen", "reopened", reason,
                {"reopened_notified_revision": notified["revision"],
                 "reopened_kind": notified["kind"],
                 "late_evidence_refs": evidence_refs},
                actor_id,
            )
            self._audit("claim", claim_id, "claim.reopened", actor_id,
                        {"decision_id": decision_id,
                         "preserved_notified_decision_id": claim["notified_decision_id"]})
        return {"claim_id": claim_id, "decision_id": decision_id,
                "preserved_notified_decision_id": claim["notified_decision_id"],
                "reheld_parties": sorted(parties)}

    def release_evidence(self, actor_id: str, claim_id: str, note: str) -> dict[str, Any]:
        """终局后统一放行全部责任方的证据保全；不允许按方提前放行。"""

        self._require(actor_id, "evidence.release")
        claim = self._claim(claim_id)
        if claim["status"] not in TERMINAL_STATUSES:
            raise InvalidState("索赔未整体终局（调查/分摊/争议未决），不得释放证据保全")
        held = self.connection.execute(
            "SELECT hold_id,party_id FROM evidence_holds WHERE claim_id=? AND status='held'",
            (claim_id,),
        ).fetchall()
        if not held:
            raise InvalidState("当前没有持有的证据保全")
        with transaction(self.connection, immediate=True):
            decision_id = self._append_decision(
                claim_id, "evidence_release", claim["status"], note,
                {"released_holds": [row["hold_id"] for row in held],
                 "released_parties": [row["party_id"] for row in held]},
                actor_id,
            )
            self.connection.execute(
                "UPDATE evidence_holds SET status='released', released_at=?, release_decision_id=? "
                "WHERE claim_id=? AND status='held'",
                (self._now(), decision_id, claim_id),
            )
        return {"claim_id": claim_id, "decision_id": decision_id,
                "released_parties": [row["party_id"] for row in held]}

    # ------------------------------------------------------------- 保修延续

    def register_continuation(
        self, actor_id: str, claim_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        """登记更换组件后剩余保修如何延续（remaining/reset/extended/excluded）。"""

        self._require(actor_id, "warranty.continue")
        claim = self._claim(claim_id)
        settlement = self.connection.execute(
            "SELECT 1 FROM claim_decisions WHERE claim_id=? AND kind='settlement'", (claim_id,)
        ).fetchone()
        if settlement is None:
            raise InvalidState("只有形成和解（含更换/维修）的索赔才能登记保修延续")
        required = ("original_serial", "warrantor_id", "terms_id", "terms_version",
                    "continuation_rule", "remaining_end_condition", "starts_at")
        for key in required:
            if not str(raw.get(key, "")).strip():
                raise ValidationFailed(f"缺少字段: {key}")
        original_serial = str(raw["original_serial"]).strip()
        warrantor_id = str(raw["warrantor_id"]).strip()
        terms_id = str(raw["terms_id"]).strip()
        rule = str(raw["continuation_rule"]).strip()
        if rule not in {"remaining", "reset", "extended", "excluded"}:
            raise ValidationFailed("continuation_rule 必须是 remaining/reset/extended/excluded")
        version = raw["terms_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationFailed("terms_version 必须是正整数")
        slot = self.connection.execute(
            "SELECT * FROM claim_frozen_slots WHERE claim_id=? AND component_serial=?",
            (claim_id, original_serial),
        ).fetchone()
        if slot is None:
            raise NotFound(f"原组件不在索赔冻结配置中: {original_serial}")
        terms = self.connection.execute(
            "SELECT * FROM warranty_terms WHERE terms_id=? AND version=?", (terms_id, version)
        ).fetchone()
        if terms is None:
            raise NotFound("延续条款版本不存在")
        if terms["warrantor_id"] != warrantor_id:
            raise ValidationFailed("延续条款的担保人与声明责任方不一致")
        replacement = raw.get("replacement_serial")
        replacement_serial = str(replacement).strip() if replacement else None
        if replacement_serial is not None:
            if self.connection.execute(
                "SELECT 1 FROM components WHERE component_serial=?", (replacement_serial,)
            ).fetchone() is None:
                raise NotFound(f"替换组件未登记: {replacement_serial}")
        settlement_id = self.connection.execute(
            "SELECT decision_id FROM claim_decisions WHERE claim_id=? AND kind='settlement' "
            "ORDER BY revision DESC LIMIT 1",
            (claim_id,),
        ).fetchone()["decision_id"]
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO warranty_continuations(claim_id,decision_id,original_serial,"
                "replacement_serial,warrantor_id,terms_id,terms_version,continuation_rule,"
                "remaining_end_condition,starts_at,ends_at,note,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, settlement_id, original_serial, replacement_serial, warrantor_id,
                 terms_id, version, rule, str(raw["remaining_end_condition"]).strip(),
                 str(raw["starts_at"]).strip(),
                 str(raw["ends_at"]).strip() if raw.get("ends_at") else None,
                 str(raw.get("note", "")).strip(), self._now()),
            )
            continuation_id = cursor.lastrowid
            self._audit("claim", claim_id, "warranty.continued", actor_id,
                        {"continuation_id": continuation_id, "original_serial": original_serial,
                         "replacement_serial": replacement_serial, "rule": rule})
        return {"continuation_id": continuation_id, "claim_id": claim_id,
                "continuation_rule": rule}

    # ------------------------------------------------------------------ 查询

    @staticmethod
    def _slot_json(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["coverage"] = json.loads(item.pop("coverage_json"))
        item["exclusions"] = json.loads(item.pop("exclusions_json"))
        return item

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        claim = self._claim(claim_id)
        result = dict(claim)
        result["frozen_slots"] = [
            self._slot_json(row) for row in self.connection.execute(
                "SELECT * FROM claim_frozen_slots WHERE claim_id=? ORDER BY slot_id", (claim_id,)
            ).fetchall()
        ]
        result["evidence"] = [
            dict(row) for row in self.connection.execute(
                "SELECT evidence_id,evidence_ref,kind,content_sha256,captured_at,note,frozen_at "
                "FROM claim_evidence WHERE claim_id=? ORDER BY evidence_id", (claim_id,)
            ).fetchall()
        ]
        result["evidence_holds"] = [
            dict(row) for row in self.connection.execute(
                "SELECT hold_id,party_id,status,scope,held_at,released_at,release_decision_id "
                "FROM evidence_holds WHERE claim_id=? ORDER BY hold_id", (claim_id,)
            ).fetchall()
        ]
        decisions = []
        for row in self.connection.execute(
            "SELECT decision_id,revision,kind,status,basis,payload_json,supersedes_revision,"
            "decided_by,decided_at,notified_at,notified_to "
            "FROM claim_decisions WHERE claim_id=? ORDER BY revision", (claim_id,)
        ).fetchall():
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["immutable"] = item["notified_at"] is not None
            decisions.append(item)
        result["decisions"] = decisions
        payout = self.connection.execute(
            "SELECT fault_fingerprint,decision_id,paid_at FROM fault_payouts WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        result["payout"] = None if payout is None else dict(payout)
        result["continuations"] = [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM warranty_continuations WHERE claim_id=? ORDER BY continuation_id",
                (claim_id,),
            ).fetchall()
        ]
        return result

    def claim_explanation(self, actor_id: str, claim_id: str) -> dict[str, Any]:
        """客服说明视图：谁承担、争议未决项、剩余保修延续。"""

        self._require(actor_id, "claim.read")
        claim = self.get_claim(claim_id)
        allocation_row = self._latest_allocation(claim_id)
        shares = []
        if allocation_row is not None:
            shares = json.loads(allocation_row["payload_json"])["shares"]
        settled_items: list[dict[str, Any]] = []
        for decision in claim["decisions"]:
            if decision["kind"] == "settlement":
                settled_items = decision["payload"]["items"]
        liability_basis = []
        for slot in claim["frozen_slots"]:
            if not slot["claimed"]:
                continue
            liability_basis.append({
                "component_serial": slot["component_serial"],
                "component_kind": slot["component_kind"],
                "symptom": slot["symptom"],
                "warrantor_id": slot["warrantor_id"],
                "terms": f"{slot['terms_id']}@v{slot['terms_version']}",
                "coverage": slot["coverage"],
                "exclusions": slot["exclusions"],
                "start_condition": slot["start_condition"],
                "end_condition": slot["end_condition"],
                "effectiveness_at_failure": {
                    "state": slot["effectiveness_state"],
                    "effective_start": slot["effective_start"],
                    "effective_end": slot["effective_end"],
                    "note": slot["effectiveness_note"],
                },
            })
        disputed = [
            {"party_id": item["party_id"], "share": item["share"], "rationale": item["rationale"]}
            for item in shares if item["status"] == "disputed"
        ]
        unconfirmed = [item["party_id"] for item in shares if item["status"] != "confirmed"]
        held_parties = sorted({
            hold["party_id"] for hold in claim["evidence_holds"] if hold["status"] == "held"
        })
        return {
            "claim_id": claim_id,
            "status": claim["status"],
            "fault_fingerprint": claim["fault_fingerprint"],
            "frozen_at": claim["opened_at"],
            "frozen_config_id": claim["frozen_config_id"],
            "frozen_owner_id": claim["frozen_owner_id"],
            "why_parties_liable": liability_basis,
            "current_allocation": None if allocation_row is None else {
                "revision": allocation_row["revision"],
                "shares": shares,
            },
            "settled_items": settled_items,
            "open_disputes": {
                "disputed_shares": disputed,
                "unconfirmed_parties": unconfirmed,
                "claim_terminal": claim["status"] in TERMINAL_STATUSES,
                "evidence_held_for_parties": held_parties,
                "notified_conclusion_immutable": claim["notified_decision_id"] is not None,
            },
            "warranty_continuations": claim["continuations"],
            "payout": claim["payout"],
        }

    def audit_trail(self, actor_id: str, claim_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        if self._claim(claim_id) is None:
            raise NotFound("索赔不存在")
        return [
            dict(row) | {"payload": json.loads(row["payload_json"])}
            for row in self.connection.execute(
                "SELECT event_id,event_type,actor_id,payload_json,created_at "
                "FROM wc_audit_events WHERE entity_type='claim' AND entity_id=? ORDER BY event_id",
                (claim_id,),
            ).fetchall()
        ]
