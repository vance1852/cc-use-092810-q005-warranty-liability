"""保修责任与索赔管理的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, add_months, isoformat, parse_utc
from .contracts import (
    CHANGE_TYPES,
    COMPONENT_KINDS,
    PARTY_KINDS,
    TERM_EFFECTS,
    choice,
    digest,
    evidence_list,
    identifier,
    money,
    optional_text,
    optional_string_list,
    positive_int,
    required_text,
    string_list,
    timestamp,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import IN_FLIGHT_STATES, TERMINAL_STATES, initialize, transaction


ROLE_PERMISSIONS = {
    "service_agent": {
        "catalog.write", "claim.intake", "evidence.supplement", "claim.read",
    },
    "warranty_admin": {
        "catalog.write", "claim.investigate", "claim.allocate", "claim.reopen",
        "hold.release", "claim.read",
    },
    "investigator": {"claim.investigate", "evidence.supplement", "claim.read"},
    "approver": {"claim.conclude", "claim.read"},
    "auditor": {"report.read", "audit.read", "claim.read"},
}

EFFECT_TEXT = {"reset": "维修件重新起算", "continue": "沿用原保修剩余期限", "new_full": "全新完整保修"}
ALLOCATION_TEXT = {"proposed": "拟定", "confirmed": "已确认", "disputed": "有争议"}


class WarrantyClaimService:
    """在单个 SQLite 连接上提供全部保修与索赔操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ---- 基础辅助 -------------------------------------------------------

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
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
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def _party(self, party_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM parties WHERE party_id=?", (party_id,)).fetchone()
        if row is None:
            raise NotFound(f"责任主体不存在: {party_id}")
        if not row["active"]:
            raise InvalidState(f"责任主体已停用: {party_id}")
        return row

    def _get_claim(self, claim_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound(f"索赔单不存在: {claim_id}")
        return row

    def _latest_decision(self, claim_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM claim_decisions WHERE claim_id=? ORDER BY revision DESC LIMIT 1", (claim_id,)
        ).fetchone()

    def _append_decision(
        self,
        claim_id: str,
        kind: str,
        content: Mapping[str, Any],
        actor_id: str,
        *,
        status: str = "draft",
        notified_at: str | None = None,
        notification_ref: str | None = None,
    ) -> int:
        """追加一个新的决定修订；已有修订永不被改写。"""

        claim = self._get_claim(claim_id)
        revision = claim["current_revision"] + 1
        text = canonical_json(content)
        self.connection.execute(
            "INSERT INTO claim_decisions(claim_id,revision,kind,status,content_json,basis_sha256,"
            "decided_by,decided_at,notified_at,notification_ref) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                claim_id, revision, kind, status, text, content_digest([content]),
                actor_id, self._now(), notified_at, notification_ref,
            ),
        )
        self.connection.execute(
            "UPDATE claims SET current_revision=? WHERE claim_id=?", (revision, claim_id)
        )
        return revision

    # ---- 用户与主数据 ---------------------------------------------------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        identifier(user_id, "user_id")
        required_text(display_name, "display_name")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_party(
        self, actor_id: str, party_id: str, display_name: str, kind: str, contact: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        party_id = identifier(party_id, "party_id")
        display_name = required_text(display_name, "display_name")
        kind = choice(kind, "kind", PARTY_KINDS)
        contact = optional_text(contact, "contact")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO parties(party_id,display_name,kind,contact,created_at) VALUES(?,?,?,?,?)",
                    (party_id, display_name, kind, contact, self._now()),
                )
                self._audit("party", party_id, "party.registered", actor_id, {"kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"责任主体已存在: {party_id}") from exc
        return {"party_id": party_id, "display_name": display_name, "kind": kind}

    def register_pack(
        self, actor_id: str, pack_id: str, model_name: str, owner_party_id: str, delivered_at: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        pack_id = identifier(pack_id, "pack_id")
        model_name = required_text(model_name, "model_name")
        owner_party_id = identifier(owner_party_id, "owner_party_id")
        delivered_at = timestamp(delivered_at, "delivered_at")
        self._party(owner_party_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO packs(pack_id,model_name,owner_party_id,delivered_at,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (pack_id, model_name, owner_party_id, delivered_at, self._now()),
                )
                self._audit("pack", pack_id, "pack.registered", actor_id, {"owner_party_id": owner_party_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"整包已存在: {pack_id}") from exc
        return {"pack_id": pack_id, "owner_party_id": owner_party_id, "delivered_at": delivered_at}

    def record_ownership_transfer(
        self, actor_id: str, pack_id: str, to_owner_party_id: str, transferred_at: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        to_owner_party_id = identifier(to_owner_party_id, "to_owner_party_id")
        transferred_at = timestamp(transferred_at, "transferred_at")
        self._get_pack(pack_id)
        self._party(to_owner_party_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO ownership_transfers(pack_id,to_owner_party_id,transferred_at,recorded_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (pack_id, to_owner_party_id, transferred_at, actor_id, self._now()),
            )
            transfer_id = cursor.lastrowid
            self._audit("pack", pack_id, "ownership.transferred", actor_id,
                        {"transfer_id": transfer_id, "to_owner_party_id": to_owner_party_id})
        return {"transfer_id": transfer_id, "owner_party_id": to_owner_party_id, "transferred_at": transferred_at}

    def _get_pack(self, pack_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM packs WHERE pack_id=?", (pack_id,)).fetchone()
        if row is None:
            raise NotFound(f"整包不存在: {pack_id}")
        return row

    def register_component(self, actor_id: str, component_id: str, kind: str, serial: str) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        component_id = identifier(component_id, "component_id")
        kind = choice(kind, "kind", COMPONENT_KINDS)
        serial = required_text(serial, "serial", 128)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO components(component_id,kind,serial,created_at) VALUES(?,?,?,?)",
                    (component_id, kind, serial, self._now()),
                )
                self._audit("component", component_id, "component.registered", actor_id, {"kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"组件已存在: {component_id}") from exc
        return {"component_id": component_id, "kind": kind, "serial": serial}

    # ---- 保修条款版本 ---------------------------------------------------

    def attach_term(
        self,
        actor_id: str,
        component_id: str,
        change_type: str,
        warrantor_party_id: str,
        coverage: list[str],
        exclusions: list[str],
        start_condition: str,
        end_condition: str,
        start_at: str,
        *,
        duration_months: int | None = None,
        end_at: str | None = None,
        term_effect: str = "",
        supersedes_version: int | None = None,
        predecessor_component_id: str | None = None,
    ) -> dict[str, Any]:
        """在组件的装配或维修/更换版本上登记保修条款。"""

        self._require(actor_id, "catalog.write")
        component_id = identifier(component_id, "component_id")
        change_type = choice(change_type, "change_type", CHANGE_TYPES)
        warrantor_party_id = identifier(warrantor_party_id, "warrantor_party_id")
        coverage_items = string_list(coverage, "coverage")
        exclusion_items = optional_string_list(exclusions, "exclusions")
        start_condition = required_text(start_condition, "start_condition")
        end_condition = required_text(end_condition, "end_condition")
        start_at = timestamp(start_at, "start_at")
        if duration_months is not None:
            duration_months = positive_int(duration_months, "duration_months", maximum=600)
        if end_at is not None:
            end_at = timestamp(end_at, "end_at")
        if term_effect:
            term_effect = choice(term_effect, "term_effect", TERM_EFFECTS)
        if predecessor_component_id is not None:
            predecessor_component_id = identifier(predecessor_component_id, "predecessor_component_id")

        component = self.connection.execute(
            "SELECT component_id FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if component is None:
            raise NotFound(f"组件不存在: {component_id}")
        self._party(warrantor_party_id)
        latest = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM component_terms WHERE component_id=?",
            (component_id,),
        ).fetchone()["version"]

        if change_type == "assembly":
            if latest != 0:
                raise InvalidState("装配条款只能是组件的第一个版本")
            if supersedes_version is not None or predecessor_component_id is not None or term_effect:
                raise ValidationFailed("装配版本不能取代其他条款或声明顺延效果")
        elif change_type == "repair":
            if latest == 0:
                raise InvalidState("维修条款必须建立在装配版本之上")
            if supersedes_version is None:
                raise ValidationFailed("维修条款必须指明被取代的条款版本 supersedes_version")
            if supersedes_version != latest:
                raise ValidationFailed(f"维修条款只能取代当前最新版本 {latest}")
            if not term_effect:
                raise ValidationFailed("维修条款必须声明 term_effect：reset、continue 或 new_full")
            if predecessor_component_id is not None:
                raise ValidationFailed("同组件维修不能填写 predecessor_component_id")
        else:  # replacement
            if latest != 0:
                raise InvalidState("更换件应登记为新组件的装配起点，该组件已有条款")
            if predecessor_component_id is None:
                raise ValidationFailed("更换条款必须指明被更换组件 predecessor_component_id")
            if not term_effect:
                raise ValidationFailed("更换条款必须声明 term_effect：reset、continue 或 new_full")
            if supersedes_version is not None:
                raise ValidationFailed("跨组件更换不能填写 supersedes_version")
            old = self.connection.execute(
                "SELECT component_id FROM components WHERE component_id=?", (predecessor_component_id,)
            ).fetchone()
            if old is None:
                raise NotFound(f"被更换组件不存在: {predecessor_component_id}")

        # 计算名义到期日。
        if duration_months is not None:
            nominal_end = isoformat(add_months(parse_utc(start_at), duration_months))
            if end_at is not None and end_at != nominal_end:
                raise ValidationFailed("duration_months 与 end_at 推算结果不一致")
            end_at = nominal_end
        elif end_at is None:
            raise ValidationFailed("必须提供 duration_months 或 end_at")
        if not end_at > start_at:
            raise ValidationFailed("条款结束时间必须晚于开始时间")

        # continue：维修不重新起算，到期日不得超过被取代条款的剩余期限。
        predecessor_end: str | None = None
        if change_type == "repair":
            predecessor_end = self.connection.execute(
                "SELECT end_at FROM component_terms WHERE component_id=? AND version=?",
                (component_id, supersedes_version),
            ).fetchone()["end_at"]
        elif change_type == "replacement":
            predecessor_end = self.connection.execute(
                "SELECT end_at FROM component_terms WHERE component_id=? ORDER BY version DESC LIMIT 1",
                (predecessor_component_id,),
            ).fetchone()["end_at"]
        if term_effect == "continue" and predecessor_end is not None and end_at > predecessor_end:
            end_at = predecessor_end

        version = latest + 1
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO component_terms(component_id,version,change_type,warrantor_party_id,"
                "coverage_json,exclusions_json,start_condition,end_condition,start_at,end_at,"
                "duration_months,term_effect,supersedes_version,predecessor_component_id,"
                "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    component_id, version, change_type, warrantor_party_id,
                    canonical_json(coverage_items), canonical_json(exclusion_items),
                    start_condition, end_condition, start_at, end_at,
                    duration_months, term_effect, supersedes_version, predecessor_component_id,
                    actor_id, self._now(),
                ),
            )
            self._audit("component", component_id, "term.attached", actor_id, {
                "version": version, "change_type": change_type, "warrantor_party_id": warrantor_party_id,
                "term_effect": term_effect, "start_at": start_at, "end_at": end_at,
            })
        return {
            "component_id": component_id, "version": version, "change_type": change_type,
            "warrantor_party_id": warrantor_party_id, "start_at": start_at, "end_at": end_at,
            "term_effect": term_effect,
        }

    def assemble_slot(
        self, actor_id: str, pack_id: str, position: str, component_id: str, installed_at: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        position = required_text(position, "position", 64)
        component_id = identifier(component_id, "component_id")
        installed_at = timestamp(installed_at, "installed_at")
        self._get_pack(pack_id)
        term = self.connection.execute(
            "SELECT version,change_type FROM component_terms WHERE component_id=? ORDER BY version LIMIT 1",
            (component_id,),
        ).fetchone()
        if term is None:
            raise ValidationFailed("组件尚无装配保修条款，不能入装")
        if term["change_type"] != "assembly":
            raise ValidationFailed("只有装配起点的组件可以直接入装整包")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO pack_slots(pack_id,position,component_id,installed_version,change_type,"
                    "installed_at,installed_by) VALUES(?,?,?,?, 'assembly', ?,?)",
                    (pack_id, position, component_id, term["version"], installed_at, actor_id),
                )
                slot_id = cursor.lastrowid
                self._audit("pack", pack_id, "slot.assembled", actor_id,
                            {"slot_id": slot_id, "position": position, "component_id": component_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("槽位或组件已被占用") from exc
        return {"slot_id": slot_id, "position": position, "component_id": component_id}

    def replace_component(
        self, actor_id: str, pack_id: str, position: str, new_component_id: str, installed_at: str
    ) -> dict[str, Any]:
        """更换槽位组件；旧行关闭留痕，新组件的更换条款必须指向旧组件。"""

        self._require(actor_id, "catalog.write")
        position = required_text(position, "position", 64)
        new_component_id = identifier(new_component_id, "new_component_id")
        installed_at = timestamp(installed_at, "installed_at")
        self._get_pack(pack_id)
        with transaction(self.connection, immediate=True):
            active = self.connection.execute(
                "SELECT * FROM pack_slots WHERE pack_id=? AND position=? AND removed_at IS NULL",
                (pack_id, position),
            ).fetchone()
            if active is None:
                raise NotFound(f"槽位不存在或已无在装组件: {position}")
            new_term = self.connection.execute(
                "SELECT * FROM component_terms WHERE component_id=? ORDER BY version DESC LIMIT 1",
                (new_component_id,),
            ).fetchone()
            if new_term is None:
                raise ValidationFailed("新组件没有保修条款")
            if new_term["change_type"] != "replacement":
                raise ValidationFailed("新组件最新条款必须是 replacement 版本")
            if new_term["predecessor_component_id"] != active["component_id"]:
                raise ValidationFailed("更换条款的被更换组件与槽位旧组件不一致")
            self.connection.execute(
                "UPDATE pack_slots SET removed_at=? WHERE slot_id=?", (installed_at, active["slot_id"])
            )
            cursor = self.connection.execute(
                "INSERT INTO pack_slots(pack_id,position,component_id,installed_version,change_type,"
                "installed_at,installed_by) VALUES(?,?,?,?, 'replacement', ?,?)",
                (pack_id, position, new_component_id, new_term["version"], installed_at, actor_id),
            )
            slot_id = cursor.lastrowid
            self._audit("pack", pack_id, "component.replaced", actor_id, {
                "slot_id": slot_id, "position": position,
                "old_component_id": active["component_id"], "new_component_id": new_component_id,
            })
        return {"slot_id": slot_id, "position": position, "component_id": new_component_id}

    # ---- 索赔受理与冻结 -------------------------------------------------

    def _owner_at(self, pack_id: str, when: str) -> str:
        row = self.connection.execute(
            "SELECT to_owner_party_id FROM ownership_transfers "
            "WHERE pack_id=? AND transferred_at<=? ORDER BY transferred_at DESC, transfer_id DESC LIMIT 1",
            (pack_id, when),
        ).fetchone()
        if row is not None:
            return row["to_owner_party_id"]
        return self._get_pack(pack_id)["owner_party_id"]

    def _effective_term(self, component_id: str, when: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM component_terms WHERE component_id=? AND start_at<=? AND end_at>? "
            "ORDER BY version DESC LIMIT 1",
            (component_id, when, when),
        ).fetchone()

    def intake_claim(
        self,
        actor_id: str,
        claim_id: str,
        pack_id: str,
        customer_party_id: str,
        fault_key: str,
        symptom: str,
        failure_at: str,
        evidence: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """受理索赔：冻结故障证据、当时配置、所有权和有效条款，并建立证据保全。"""

        self._require(actor_id, "claim.intake")
        claim_id = identifier(claim_id, "claim_id")
        pack_id = identifier(pack_id, "pack_id")
        customer_party_id = identifier(customer_party_id, "customer_party_id")
        fault_key = required_text(fault_key, "fault_key", 128)
        symptom = required_text(symptom, "symptom")
        failure_at = timestamp(failure_at, "failure_at")
        evidence_items = evidence_list(evidence)
        pack = self._get_pack(pack_id)
        self._party(customer_party_id)
        if failure_at < pack["delivered_at"]:
            raise ValidationFailed("故障时间早于整包交付时间")

        paid = self.connection.execute(
            "SELECT claim_id FROM paid_faults WHERE fault_key=?", (fault_key,)
        ).fetchone()
        if paid is not None:
            raise Conflict(f"同一故障已经赔付（索赔单 {paid['claim_id']}），不得重复索赔")

        owner_party_id = self._owner_at(pack_id, failure_at)
        slot_rows = self.connection.execute(
            "SELECT s.*, c.kind, c.serial FROM pack_slots s JOIN components c ON c.component_id=s.component_id "
            "WHERE s.pack_id=? AND s.installed_at<=? AND (s.removed_at IS NULL OR s.removed_at>?) "
            "ORDER BY s.position, s.slot_id",
            (pack_id, failure_at, failure_at),
        ).fetchall()
        if not slot_rows:
            raise ValidationFailed("故障发生时整包没有任何在装组件，无法冻结配置")

        configuration: list[dict[str, Any]] = []
        terms_index: dict[str, dict[str, Any]] = {}
        warrantors: set[str] = set()
        for slot in slot_rows:
            term = self._effective_term(slot["component_id"], failure_at)
            term_body = None
            in_warranty = False
            if term is not None:
                in_warranty = True
                warrantors.add(term["warrantor_party_id"])
                term_body = {
                    "component_id": slot["component_id"],
                    "version": term["version"],
                    "change_type": term["change_type"],
                    "warrantor_party_id": term["warrantor_party_id"],
                    "coverage": json.loads(term["coverage_json"]),
                    "exclusions": json.loads(term["exclusions_json"]),
                    "start_condition": term["start_condition"],
                    "end_condition": term["end_condition"],
                    "start_at": term["start_at"],
                    "end_at": term["end_at"],
                    "term_effect": term["term_effect"],
                }
                terms_index[f"{slot['component_id']}@{term['version']}"] = term_body
            configuration.append({
                "position": slot["position"],
                "component_id": slot["component_id"],
                "kind": slot["kind"],
                "serial": slot["serial"],
                "installed_version": slot["installed_version"],
                "installed_at": slot["installed_at"],
                "in_warranty": in_warranty,
                "term": term_body,
            })

        now = self._now()
        freeze_body = {
            "frozen_at": now,
            "failure_at": failure_at,
            "pack_id": pack_id,
            "owner_party_id": owner_party_id,
            "customer_party_id": customer_party_id,
            "configuration": configuration,
        }
        freeze_digest = content_digest([freeze_body])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO claims(claim_id,pack_id,customer_party_id,fault_key,symptom,failure_at,"
                    "reported_at,state,current_revision,frozen_at,freeze_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?, 'investigating', 1, ?, ?, ?, ?)",
                    (claim_id, pack_id, customer_party_id, fault_key, symptom, failure_at,
                     now, now, freeze_digest, actor_id, now),
                )
                self.connection.execute(
                    "INSERT INTO claim_freeze(claim_id,frozen_at,failure_at,owner_party_id,"
                    "configuration_json,terms_json,evidence_json,content_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        claim_id, now, failure_at, owner_party_id,
                        canonical_json(configuration), canonical_json(list(terms_index.values())),
                        canonical_json(evidence_items), freeze_digest,
                    ),
                )
                for index, item in enumerate(evidence_items):
                    self.connection.execute(
                        "INSERT INTO claim_evidence(claim_id,label,kind,content_sha256,source,"
                        "submitted_by,submitted_at) VALUES(?,?,?,?, 'intake', ?,?)",
                        (claim_id, item["label"], item["kind"], item["content_sha256"], actor_id, now),
                    )
                # 每位在保责任方独立持有证据保全，任何一方都不能被提前释放。
                for party_id in sorted(warrantors):
                    self.connection.execute(
                        "INSERT INTO evidence_holds(claim_id,party_id,status,held_reason) "
                        "VALUES(?,?,'held',?)",
                        (claim_id, party_id, f"索赔受理时在保责任方，故障时间 {failure_at}"),
                    )
                intake_content = {"freeze_sha256": freeze_digest, "warrantors": sorted(warrantors)}
                self.connection.execute(
                    "INSERT INTO claim_decisions(claim_id,revision,kind,status,content_json,basis_sha256,"
                    "decided_by,decided_at,notified_at,notification_ref) "
                    "VALUES(?,1,'intake','notified',?,?,?,?,?,?)",
                    (
                        claim_id, canonical_json(intake_content), content_digest([intake_content]),
                        actor_id, now, now, "intake-acknowledged",
                    ),
                )
                self._audit("claim", claim_id, "claim.intake_frozen", actor_id, {
                    "fault_key": fault_key, "failure_at": failure_at, "owner_party_id": owner_party_id,
                    "warrantors": sorted(warrantors), "freeze_sha256": freeze_digest,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("索赔编号或故障编号冲突，或同一故障已有在途索赔") from exc
        return {
            "claim_id": claim_id, "state": "investigating", "fault_key": fault_key,
            "failure_at": failure_at, "owner_party_id": owner_party_id,
            "warrantor_party_ids": sorted(warrantors), "freeze_sha256": freeze_digest,
        }

    def add_evidence(self, actor_id: str, claim_id: str, evidence: list[Mapping[str, Any]]) -> dict[str, Any]:
        self._require(actor_id, "evidence.supplement")
        items = evidence_list(evidence)
        claim = self._get_claim(claim_id)
        if claim["state"] in TERMINAL_STATES:
            raise InvalidState("索赔结论已通知客户；新检测结果只能登记为迟到发现并申请复开")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                for item in items:
                    self.connection.execute(
                        "INSERT INTO claim_evidence(claim_id,label,kind,content_sha256,source,"
                        "submitted_by,submitted_at) VALUES(?,?,?,?, 'supplement', ?,?)",
                        (claim_id, item["label"], item["kind"], item["content_sha256"], actor_id, now),
                    )
                self._audit("claim", claim_id, "evidence.supplemented", actor_id, {"count": len(items)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据摘要与已有证据重复") from exc
        return {"claim_id": claim_id, "added": len(items)}

    def register_late_finding(
        self, actor_id: str, claim_id: str, label: str, content_sha256: str, summary: str
    ) -> dict[str, Any]:
        """结论通知后到达的检测只能登记挂起，等待复开，不触碰既有结论。"""

        self._require(actor_id, "claim.investigate")
        label = required_text(label, "label", 128)
        content_sha256 = digest(content_sha256)
        summary = required_text(summary, "summary")
        claim = self._get_claim(claim_id)
        if claim["state"] not in TERMINAL_STATES or not claim["notified_conclusion_at"]:
            raise InvalidState("只有已通知客户结论的索赔才需要登记迟到检测；在途索赔请走补证")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO late_findings(claim_id,label,content_sha256,summary,registered_by,registered_at) "
                "VALUES(?,?,?,?,?,?)",
                (claim_id, label, content_sha256, summary, actor_id, self._now()),
            )
            finding_id = cursor.lastrowid
            self._audit("claim", claim_id, "finding.registered_late", actor_id,
                        {"finding_id": finding_id, "label": label})
        return {"finding_id": finding_id, "claim_id": claim_id, "status": "pending_reopen"}

    # ---- 调查、补证与争议 ----------------------------------------------

    def _require_in_flight(self, claim: sqlite3.Row) -> None:
        if claim["state"] not in IN_FLIGHT_STATES:
            raise InvalidState(f"索赔当前状态 {claim['state']} 不能追加决定")

    def record_investigation(self, actor_id: str, claim_id: str, findings: str) -> dict[str, Any]:
        self._require(actor_id, "claim.investigate")
        findings = required_text(findings, "findings", 4000)
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            self._require_in_flight(claim)
            revision = self._append_decision(
                claim_id, "investigation", {"findings": findings}, actor_id
            )
            self.connection.execute(
                "UPDATE claims SET state='investigating' WHERE claim_id=?", (claim_id,)
            )
            self._audit("claim", claim_id, "decision.investigation", actor_id, {"revision": revision})
        return {"claim_id": claim_id, "revision": revision, "kind": "investigation"}

    def request_supplement(
        self, actor_id: str, claim_id: str, party_id: str, requirements: list[str], reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "claim.investigate")
        party_id = identifier(party_id, "party_id")
        requirements_items = string_list(requirements, "requirements")
        reason = required_text(reason, "reason")
        self._party(party_id)
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            self._require_in_flight(claim)
            revision = self._append_decision(claim_id, "supplement_request", {
                "party_id": party_id, "requirements": list(requirements_items), "reason": reason,
            }, actor_id)
            self.connection.execute(
                "UPDATE claims SET state='awaiting_evidence' WHERE claim_id=?", (claim_id,)
            )
            self._audit("claim", claim_id, "decision.supplement_requested", actor_id, {"revision": revision})
        return {"claim_id": claim_id, "revision": revision, "state": "awaiting_evidence"}

    def open_dispute(
        self, actor_id: str, claim_id: str, party_id: str, subject: str, detail: str
    ) -> dict[str, Any]:
        self._require(actor_id, "claim.allocate")
        party_id = identifier(party_id, "party_id")
        subject = required_text(subject, "subject", 128)
        detail = required_text(detail, "detail", 2000)
        self._party(party_id)
        claim = self._get_claim(claim_id)
        self._require_in_flight(claim)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO claim_disputes(claim_id,party_id,subject,detail,opened_by,opened_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (claim_id, party_id, subject, detail, actor_id, self._now()),
                )
                dispute_id = cursor.lastrowid
                self._audit("claim", claim_id, "dispute.opened", actor_id,
                            {"dispute_id": dispute_id, "party_id": party_id, "subject": subject})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该主体就同一事项已有未解决争议") from exc
        return {"dispute_id": dispute_id, "status": "open"}

    def resolve_dispute(self, actor_id: str, claim_id: str, dispute_id: int, resolution: str) -> dict[str, Any]:
        self._require(actor_id, "claim.allocate")
        resolution = required_text(resolution, "resolution", 2000)
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM claim_disputes WHERE dispute_id=? AND claim_id=?", (dispute_id, claim_id)
            ).fetchone()
            if row is None:
                raise NotFound("争议不存在")
            if row["status"] != "open":
                raise InvalidState("争议已经解决")
            self.connection.execute(
                "UPDATE claim_disputes SET status='resolved',resolved_by=?,resolved_at=?,resolution=? "
                "WHERE dispute_id=?",
                (actor_id, self._now(), resolution, dispute_id),
            )
            self._audit("claim", claim_id, "dispute.resolved", actor_id, {"dispute_id": dispute_id})
        return {"dispute_id": dispute_id, "status": "resolved"}

    # ---- 责任分摊（有版本） --------------------------------------------

    def _freeze_warrantors(self, claim_id: str) -> set[str]:
        freeze = self.connection.execute(
            "SELECT configuration_json FROM claim_freeze WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if freeze is None:
            raise InvalidState("索赔尚未受理")
        configuration = json.loads(freeze["configuration_json"])
        return {
            entry["term"]["warrantor_party_id"]
            for entry in configuration
            if entry.get("in_warranty") and entry.get("term")
        }

    def propose_allocation(
        self, actor_id: str, claim_id: str, lines: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """形成新一版责任分摊；旧版本保留，确认/争议只作用于最新版本。"""

        self._require(actor_id, "claim.allocate")
        if not isinstance(lines, list) or not lines:
            raise ValidationFailed("分摊明细不能为空")
        claim = self._get_claim(claim_id)
        self._require_in_flight(claim)
        allowed_parties = self._freeze_warrantors(claim_id) | {claim["customer_party_id"]}
        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        for index, line in enumerate(lines):
            party_id = identifier(line.get("party_id"), f"lines[{index}].party_id")
            if party_id in seen:
                raise ValidationFailed(f"分摊主体重复: {party_id}")
            seen.add(party_id)
            if party_id not in allowed_parties:
                raise ValidationFailed(f"{party_id} 不是本次冻结配置中的在保责任方或客户")
            share = positive_int(line.get("share_basis_points"), f"lines[{index}].share_basis_points", maximum=10000)
            amount = money(line.get("amount_cny"), f"lines[{index}].amount_cny")
            rationale = required_text(line.get("rationale"), f"lines[{index}].rationale", 1000)
            total += share
            parsed.append({"party_id": party_id, "share": share, "amount": amount, "rationale": rationale})
        if total != 10000:
            raise ValidationFailed(f"分摊比例合计必须为 10000 个基点，当前为 {total}")

        with transaction(self.connection, immediate=True):
            revision = self._append_decision(claim_id, "allocation", {
                "lines": [
                    {"party_id": item["party_id"], "share_basis_points": item["share"],
                     "amount_cny": format(item["amount"], "f"), "rationale": item["rationale"]}
                    for item in parsed
                ],
            }, actor_id)
            for item in parsed:
                self.connection.execute(
                    "INSERT INTO claim_allocations(claim_id,revision,party_id,share_basis_points,"
                    "amount_cny,rationale,status) VALUES(?,?,?,?,?,?,'proposed')",
                    (claim_id, revision, item["party_id"], item["share"],
                     format(item["amount"], "f"), item["rationale"]),
                )
            self.connection.execute(
                "UPDATE claims SET state='allocating' WHERE claim_id=?", (claim_id,)
            )
            self._audit("claim", claim_id, "decision.allocation_proposed", actor_id, {"revision": revision})
        return {"claim_id": claim_id, "revision": revision, "lines": len(parsed)}

    def _latest_allocation_revision(self, claim_id: str) -> int:
        row = self.connection.execute(
            "SELECT revision FROM claim_decisions WHERE claim_id=? AND kind='allocation' "
            "ORDER BY revision DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise InvalidState("索赔尚未形成责任分摊版本")
        return row["revision"]

    def confirm_allocation(
        self, actor_id: str, claim_id: str, party_id: str, confirmation_ref: str
    ) -> dict[str, Any]:
        self._require(actor_id, "claim.allocate")
        party_id = identifier(party_id, "party_id")
        confirmation_ref = required_text(confirmation_ref, "confirmation_ref", 128)
        claim = self._get_claim(claim_id)
        if claim["state"] in TERMINAL_STATES:
            raise InvalidState("结论已通知客户；确认只能在复开后的新版本上进行")
        revision = self._latest_allocation_revision(claim_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE claim_allocations SET status='confirmed',confirmation_ref=?,confirmed_at=? "
                "WHERE claim_id=? AND revision=? AND party_id=? AND status IN ('proposed','disputed')",
                (confirmation_ref, self._now(), claim_id, revision, party_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("最新分摊版本中没有该主体的待确认明细")
            self._audit("claim", claim_id, "allocation.confirmed", actor_id,
                        {"revision": revision, "party_id": party_id})
        return {"claim_id": claim_id, "revision": revision, "party_id": party_id, "status": "confirmed"}

    def dispute_allocation(
        self, actor_id: str, claim_id: str, party_id: str, subject: str, detail: str
    ) -> dict[str, Any]:
        self._require(actor_id, "claim.allocate")
        party_id = identifier(party_id, "party_id")
        subject = required_text(subject, "subject", 128)
        detail = required_text(detail, "detail", 2000)
        claim = self._get_claim(claim_id)
        if claim["state"] in TERMINAL_STATES:
            raise InvalidState("结论已通知客户；争议只能在复开后的新版本上提出")
        revision = self._latest_allocation_revision(claim_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE claim_allocations SET status='disputed' "
                "WHERE claim_id=? AND revision=? AND party_id=? AND status IN ('proposed','confirmed')",
                (claim_id, revision, party_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("最新分摊版本中没有该主体的明细")
            try:
                self.connection.execute(
                    "INSERT INTO claim_disputes(claim_id,party_id,subject,detail,opened_by,opened_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (claim_id, party_id, subject, detail, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该主体就同一事项已有未解决争议") from exc
            self._audit("claim", claim_id, "allocation.disputed", actor_id,
                        {"revision": revision, "party_id": party_id, "subject": subject})
        return {"claim_id": claim_id, "revision": revision, "party_id": party_id, "status": "disputed"}

    # ---- 和解 / 驳回：拟定与通知分离，通知后不可变 ----------------------

    def propose_settlement(self, actor_id: str, claim_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "claim.allocate")
        note = required_text(note, "note", 2000)
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            self._require_in_flight(claim)
            revision = self._latest_allocation_revision(claim_id)
            pending = self.connection.execute(
                "SELECT count(*) FROM claim_allocations WHERE claim_id=? AND revision=? AND status='proposed'",
                (claim_id, revision),
            ).fetchone()[0]
            if pending:
                raise InvalidState("仍有责任方既未确认也未提出争议，不能拟定和解")
            new_revision = self._append_decision(claim_id, "settlement", {
                "allocation_revision": revision, "note": note,
            }, actor_id)
            self.connection.execute(
                "UPDATE claims SET state='settlement_pending' WHERE claim_id=?", (claim_id,)
            )
            self._audit("claim", claim_id, "decision.settlement_proposed", actor_id,
                        {"revision": new_revision})
        return {"claim_id": claim_id, "revision": new_revision, "state": "settlement_pending"}

    def notify_settlement(self, actor_id: str, claim_id: str, notification_ref: str) -> dict[str, Any]:
        """通知客户和解结论：仅对已确认方登记赔付；争议方的保全继续保留。"""

        self._require(actor_id, "claim.conclude")
        notification_ref = required_text(notification_ref, "notification_ref", 128)
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            if claim["state"] != "settlement_pending":
                raise InvalidState("索赔没有待通知的和解决定")
            decision = self._latest_decision(claim_id)
            if decision["kind"] != "settlement" or decision["status"] == "notified":
                raise InvalidState("最新决定不是可通知的和解")
            allocation_revision = json.loads(decision["content_json"])["allocation_revision"]
            now = self._now()
            rows = self.connection.execute(
                "SELECT * FROM claim_allocations WHERE claim_id=? AND revision=? ORDER BY party_id",
                (claim_id, allocation_revision),
            ).fetchall()
            paid_parties = {
                row["party_id"]
                for row in self.connection.execute(
                    "SELECT party_id FROM claim_payments WHERE claim_id=?", (claim_id,)
                ).fetchall()
            }
            payments: list[dict[str, Any]] = []
            for row in rows:
                if row["status"] != "confirmed" or row["party_id"] in paid_parties:
                    continue  # 争议方不赔付且证据保全不释放；复开后已赔付方不重复赔付。
                amount = Decimal(row["amount_cny"])
                if amount <= 0:
                    continue
                self.connection.execute(
                    "INSERT INTO claim_payments(claim_id,fault_key,party_id,decision_revision,"
                    "amount_cny,paid_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, claim["fault_key"], row["party_id"], decision["revision"],
                     format(amount, "f"), now),
                )
                payments.append({"party_id": row["party_id"], "amount_cny": format(amount, "f")})
            # 同一故障全局只允许赔付一次；复开导致的追加责任方在原记录上挂账。
            self.connection.execute(
                "INSERT INTO paid_faults(fault_key,claim_id,settled_revision,paid_at) VALUES(?,?,?,?) "
                "ON CONFLICT(fault_key) DO NOTHING",
                (claim["fault_key"], claim_id, decision["revision"], now),
            )
            self.connection.execute(
                "UPDATE claim_decisions SET status='notified',notified_at=?,notification_ref=? "
                "WHERE decision_id=?",
                (now, notification_ref, decision["decision_id"]),
            )
            self.connection.execute(
                "UPDATE claims SET state='settled',notified_conclusion_at=? WHERE claim_id=?",
                (now, claim_id),
            )
            self._audit("claim", claim_id, "decision.settlement_notified", actor_id, {
                "revision": decision["revision"], "payments": payments,
            })
        return {"claim_id": claim_id, "state": "settled", "payments": payments}

    def propose_rejection(self, actor_id: str, claim_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "claim.allocate")
        reason = required_text(reason, "reason", 2000)
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            self._require_in_flight(claim)
            revision = self._append_decision(claim_id, "rejection", {"reason": reason}, actor_id)
            self.connection.execute(
                "UPDATE claims SET state='rejection_pending' WHERE claim_id=?", (claim_id,)
            )
            self._audit("claim", claim_id, "decision.rejection_proposed", actor_id, {"revision": revision})
        return {"claim_id": claim_id, "revision": revision, "state": "rejection_pending"}

    def notify_rejection(self, actor_id: str, claim_id: str, notification_ref: str) -> dict[str, Any]:
        self._require(actor_id, "claim.conclude")
        notification_ref = required_text(notification_ref, "notification_ref", 128)
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            if claim["state"] != "rejection_pending":
                raise InvalidState("索赔没有待通知的驳回决定")
            decision = self._latest_decision(claim_id)
            if decision["kind"] != "rejection" or decision["status"] == "notified":
                raise InvalidState("最新决定不是可通知的驳回")
            now = self._now()
            self.connection.execute(
                "UPDATE claim_decisions SET status='notified',notified_at=?,notification_ref=? "
                "WHERE decision_id=?",
                (now, notification_ref, decision["decision_id"]),
            )
            # 驳回不放行任何证据保全：客户可能在之后补交迟到检测。
            self.connection.execute(
                "UPDATE claims SET state='rejected',notified_conclusion_at=? WHERE claim_id=?",
                (now, claim_id),
            )
            self._audit("claim", claim_id, "decision.rejection_notified", actor_id,
                        {"revision": decision["revision"]})
        return {"claim_id": claim_id, "state": "rejected"}

    # ---- 复开与证据保全释放 --------------------------------------------

    def reopen_claim(
        self, actor_id: str, claim_id: str, reason: str, finding_id: int | None = None
    ) -> dict[str, Any]:
        """复开已通知结论的索赔：迟到检测或未决争议只能追加新修订，原结论原样保留。"""

        self._require(actor_id, "claim.reopen")
        reason = required_text(reason, "reason", 2000)
        if finding_id is not None:
            finding_id = positive_int(finding_id, "finding_id")
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            if claim["state"] not in TERMINAL_STATES or not claim["notified_conclusion_at"]:
                raise InvalidState("只有已通知结论的索赔可以复开")
            finding = None
            if finding_id is not None:
                finding = self.connection.execute(
                    "SELECT * FROM late_findings WHERE finding_id=? AND claim_id=?",
                    (finding_id, claim_id),
                ).fetchone()
                if finding is None:
                    raise NotFound("迟到检测发现不存在")
                if finding["carried_into_revision"] is not None:
                    raise InvalidState("该迟到发现已经用于复开")
            else:
                open_dispute = self.connection.execute(
                    "SELECT 1 FROM claim_disputes WHERE claim_id=? AND status='open' LIMIT 1",
                    (claim_id,),
                ).fetchone()
                if open_dispute is None:
                    raise ValidationFailed("复开必须基于一份迟到检测，或针对至少一项未决争议")
            revision = self._append_decision(claim_id, "reopen", {
                "finding_id": None if finding is None else finding_id,
                "summary": None if finding is None else finding["summary"],
                "reason": reason,
            }, actor_id)
            if finding is not None:
                self.connection.execute(
                    "UPDATE late_findings SET carried_into_revision=? WHERE finding_id=?",
                    (revision, finding_id),
                )
                self.connection.execute(
                    "INSERT INTO claim_evidence(claim_id,label,kind,content_sha256,source,"
                    "submitted_by,submitted_at) VALUES(?,?,'other',?, 'late', ?,?)",
                    (claim_id, finding["label"], finding["content_sha256"], actor_id, self._now()),
                )
            self.connection.execute(
                "UPDATE claims SET state='reopened' WHERE claim_id=?", (claim_id,)
            )
            self._audit("claim", claim_id, "decision.reopened", actor_id, {
                "revision": revision, "finding_id": finding_id,
            })
        return {"claim_id": claim_id, "revision": revision, "state": "reopened"}

    def release_hold(self, actor_id: str, claim_id: str, party_id: str, note: str) -> dict[str, Any]:
        """证据保全逐方释放：结论已通知且该方已确认赔付后才可释放，其他方不受影响。"""

        self._require(actor_id, "hold.release")
        party_id = identifier(party_id, "party_id")
        note = required_text(note, "note", 1000)
        with transaction(self.connection, immediate=True):
            claim = self._get_claim(claim_id)
            hold = self.connection.execute(
                "SELECT * FROM evidence_holds WHERE claim_id=? AND party_id=?", (claim_id, party_id)
            ).fetchone()
            if hold is None:
                raise NotFound("该主体没有证据保全记录")
            if hold["status"] == "released":
                raise InvalidState("证据保全已经释放")
            if not claim["notified_conclusion_at"]:
                raise InvalidState("结论尚未通知客户，不能提前释放任何证据保全")
            paid = self.connection.execute(
                "SELECT 1 FROM claim_payments WHERE claim_id=? AND party_id=?",
                (claim_id, party_id),
            ).fetchone()
            if paid is None:
                raise InvalidState("该责任方尚未确认并完成赔付，证据保全必须继续")
            self.connection.execute(
                "UPDATE evidence_holds SET status='released',released_by=?,released_at=?,release_note=? "
                "WHERE hold_id=?",
                (actor_id, self._now(), note, hold["hold_id"]),
            )
            self._audit("claim", claim_id, "hold.released", actor_id, {"party_id": party_id})
        return {"claim_id": claim_id, "party_id": party_id, "hold": "released"}

    # ---- 客服说明接口 ---------------------------------------------------

    def _party_name(self, party_id: str) -> str:
        row = self.connection.execute(
            "SELECT display_name FROM parties WHERE party_id=?", (party_id,)
        ).fetchone()
        return party_id if row is None else row["display_name"]

    def warranty_continuation(self, pack_id: str) -> list[dict[str, Any]]:
        """更换/维修后各组件剩余保修如何延续。"""

        self._get_pack(pack_id)
        now = self._now()
        slots = self.connection.execute(
            "SELECT s.*, c.kind, c.serial FROM pack_slots s JOIN components c ON c.component_id=s.component_id "
            "WHERE s.pack_id=? AND s.removed_at IS NULL ORDER BY s.position",
            (pack_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for slot in slots:
            term = self.connection.execute(
                "SELECT * FROM component_terms WHERE component_id=? ORDER BY version DESC LIMIT 1",
                (slot["component_id"],),
            ).fetchone()
            chain: list[str] = []
            cursor_row = term
            while cursor_row is not None and cursor_row["term_effect"] == "continue":
                pred = cursor_row["predecessor_component_id"]
                super_version = cursor_row["supersedes_version"]
                if pred is not None:
                    chain.append(f"{pred} 截止 {cursor_row['end_at']}")
                    cursor_row = self.connection.execute(
                        "SELECT * FROM component_terms WHERE component_id=? ORDER BY version DESC LIMIT 1",
                        (pred,),
                    ).fetchone()
                elif super_version is not None:
                    cursor_row = self.connection.execute(
                        "SELECT * FROM component_terms WHERE component_id=? AND version=?",
                        (cursor_row["component_id"], super_version),
                    ).fetchone()
                else:
                    break
            remaining_seconds = (parse_utc(term["end_at"]) - parse_utc(now)).total_seconds()
            result.append({
                "position": slot["position"],
                "component_id": slot["component_id"],
                "kind": slot["kind"],
                "latest_version": term["version"],
                "change_type": term["change_type"],
                "term_effect": term["term_effect"],
                "warrantor_party_id": term["warrantor_party_id"],
                "warranty_end_at": term["end_at"],
                "remaining_days": max(0, int(remaining_seconds // 86400)),
                "active": parse_utc(term["start_at"]) <= parse_utc(now) < parse_utc(term["end_at"]),
                "continuation_basis": EFFECT_TEXT.get(term["term_effect"], "装配原始保修"),
                "predecessor_chain": chain,
            })
        return result

    def explain_claim(self, actor_id: str, claim_id: str) -> dict[str, Any]:
        """一次索赔为何由哪些主体承担、争议是否未决、剩余保修如何延续。"""

        self._require(actor_id, "claim.read")
        claim = self._get_claim(claim_id)
        freeze_row = self.connection.execute(
            "SELECT * FROM claim_freeze WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if freeze_row is None:
            raise InvalidState("索赔尚未受理")
        configuration = json.loads(freeze_row["configuration_json"])
        intake_evidence = json.loads(freeze_row["evidence_json"])

        decisions = [
            dict(row) | {"content": json.loads(row["content_json"])}
            for row in self.connection.execute(
                "SELECT * FROM claim_decisions WHERE claim_id=? ORDER BY revision", (claim_id,)
            ).fetchall()
        ]
        notified = [d for d in decisions if d["status"] == "notified" and d["kind"] in ("settlement", "rejection")]
        latest_notified = notified[-1] if notified else None

        allocations = [dict(row) for row in self.connection.execute(
            "SELECT * FROM claim_allocations WHERE claim_id=? ORDER BY revision,party_id", (claim_id,)
        ).fetchall()]
        latest_revision = max((a["revision"] for a in allocations), default=0)
        holds = {
            row["party_id"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM evidence_holds WHERE claim_id=?", (claim_id,)
            ).fetchall()
        }
        disputes_open = [dict(row) for row in self.connection.execute(
            "SELECT * FROM claim_disputes WHERE claim_id=? AND status='open' ORDER BY dispute_id",
            (claim_id,),
        ).fetchall()]
        payments = [dict(row) for row in self.connection.execute(
            "SELECT * FROM claim_payments WHERE claim_id=? ORDER BY payment_id", (claim_id,)
        ).fetchall()]
        late_pending = [dict(row) for row in self.connection.execute(
            "SELECT * FROM late_findings WHERE claim_id=? AND carried_into_revision IS NULL ORDER BY finding_id",
            (claim_id,),
        ).fetchall()]
        continuation = self.warranty_continuation(claim["pack_id"])

        # 按责任主体归并：哪些组件位置、哪版条款、当前分摊与保全状态。
        parties: dict[str, dict[str, Any]] = {}
        for entry in configuration:
            term = entry.get("term")
            if not term:
                continue
            party_id = term["warrantor_party_id"]
            bucket = parties.setdefault(party_id, {
                "party_id": party_id,
                "display_name": self._party_name(party_id),
                "positions": [],
                "term_versions": [],
                "coverage": set(),
                "exclusions": set(),
                "term_window": None,
                "in_warranty": True,
            })
            bucket["positions"].append(entry["position"])
            bucket["term_versions"].append(f"{entry['component_id']}@v{term['version']}")
            bucket["coverage"].update(term["coverage"])
            bucket["exclusions"].update(term["exclusions"])
            window = f"{term['start_at']} ~ {term['end_at']}"
            if bucket["term_window"] is None:
                bucket["term_window"] = window
        for line in allocations:
            if line["revision"] != latest_revision:
                continue
            bucket = parties.setdefault(line["party_id"], {
                "party_id": line["party_id"],
                "display_name": self._party_name(line["party_id"]),
                "positions": [], "term_versions": [], "coverage": set(),
                "exclusions": set(), "term_window": None, "in_warranty": False,
            })
            bucket["share_basis_points"] = line["share_basis_points"]
            bucket["amount_cny"] = line["amount_cny"]
            bucket["allocation_status"] = line["status"]
            bucket["rationale"] = line["rationale"]
        for party_id, bucket in parties.items():
            hold = holds.get(party_id)
            bucket["evidence_hold"] = None if hold is None else hold["status"]
            bucket["coverage"] = sorted(bucket["coverage"])
            bucket["exclusions"] = sorted(bucket["exclusions"])

        # 客服可读的说明文字。
        narrative: list[str] = []
        narrative.append(
            f"索赔 {claim_id}（故障 {claim['fault_key']}）发生于 {freeze_row['failure_at']}，"
            f"受理时所有权人：{self._party_name(freeze_row['owner_party_id'])}；"
            f"当前状态：{claim['state']}。"
        )
        for bucket in sorted(parties.values(), key=lambda item: item["party_id"]):
            bits = [f"{bucket['display_name']}（{bucket['party_id']}）"]
            if bucket["positions"]:
                bits.append(f"对槽位 {', '.join(bucket['positions'])} 在故障时处于保修期内")
                bits.append(f"适用条款 {', '.join(bucket['term_versions'])}，有效期 {bucket['term_window']}")
            else:
                bits.append("承担保修范围外的客户自担份额")
            if bucket.get("share_basis_points") is not None:
                bits.append(
                    f"最新第 {latest_revision} 版分摊 {bucket['share_basis_points'] / 100:.2f}%、"
                    f"{bucket['amount_cny']} 元，状态：{ALLOCATION_TEXT.get(bucket.get('allocation_status'), '未知')}"
                )
            else:
                bits.append("尚未形成分摊")
            if bucket["coverage"]:
                bits.append(f"覆盖范围：{'; '.join(bucket['coverage'])}")
            if bucket["exclusions"]:
                bits.append(f"排除条款：{'; '.join(bucket['exclusions'])}")
            if bucket.get("evidence_hold"):
                hold_text = "证据保全中" if bucket["evidence_hold"] == "held" else "证据保全已释放"
                bits.append(hold_text)
            narrative.append("；".join(bits) + "。")
        out_of_warranty = [entry for entry in configuration if not entry.get("in_warranty")]
        for entry in out_of_warranty:
            narrative.append(
                f"槽位 {entry['position']} 的 {entry['component_id']}（{entry['kind']}）"
                f"在故障发生时没有处于有效期内的保修条款，不由任何责任方按保修承担。"
            )
        if disputes_open:
            narrative.append("未决争议：" + "；".join(
                f"{self._party_name(d['party_id'])} 就 {d['subject']} 提出异议" for d in disputes_open
            ) + "。")
        else:
            narrative.append("目前没有未决争议。")
        if latest_notified is not None:
            kind_text = "和解" if latest_notified["kind"] == "settlement" else "驳回"
            narrative.append(
                f"已通知客户的结论：第 {latest_notified['revision']} 版{kind_text}"
                f"（通知时间 {latest_notified['notified_at']}），该结论不可改写；"
                "新的检测结果只能通过复开产生新版本决定。"
            )
        if late_pending:
            narrative.append(
                f"有 {len(late_pending)} 份迟到检测待处理，可触发复开："
                + "、".join(f"#{f['finding_id']} {f['label']}" for f in late_pending) + "。"
            )
        if continuation:
            narrative.append("更换组件后的剩余保修延续：" + "；".join(
                f"槽位 {item['position']} {item['component_id']} 按“{item['continuation_basis']}”"
                f"保修至 {item['warranty_end_at']}（剩余约 {item['remaining_days']} 天）"
                for item in continuation
            ) + "。")

        return {
            "claim": dict(claim),
            "freeze": {
                "frozen_at": freeze_row["frozen_at"],
                "failure_at": freeze_row["failure_at"],
                "owner_party_id": freeze_row["owner_party_id"],
                "configuration": configuration,
                "intake_evidence": intake_evidence,
                "content_sha256": freeze_row["content_sha256"],
            },
            "responsibility": sorted(parties.values(), key=lambda item: item["party_id"]),
            "decisions": decisions,
            "latest_allocation_revision": latest_revision,
            "open_disputes": disputes_open,
            "payments": payments,
            "late_findings_pending": late_pending,
            "notified_conclusion": None if latest_notified is None else {
                "revision": latest_notified["revision"],
                "kind": latest_notified["kind"],
                "notified_at": latest_notified["notified_at"],
                "notification_ref": latest_notified["notification_ref"],
            },
            "warranty_continuation": continuation,
            "narrative": "\n".join(narrative),
        }
