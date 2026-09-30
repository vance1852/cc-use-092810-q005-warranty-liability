from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from warranty_claims.clock import FrozenClock
from warranty_claims.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from warranty_claims.service import WarrantyClaimService


def evidence(label: str, seed: str) -> dict[str, str]:
    return {"label": label, "kind": "diagnostic", "content_sha256": seed * 64}


class WarrantyScenarioMixin:
    """构造“原厂壳体 + 第三方维修模组 + 更换 BMS”的标准场景。"""

    def seed_catalog(self, service: WarrantyClaimService) -> None:
        for user_id, role in (
            ("agent", "service_agent"),
            ("admin", "warranty_admin"),
            ("invest", "investigator"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            service.create_user(user_id, user_id, role)
        for party_id, name, kind in (
            ("oem", "原厂", "oem"),
            ("repairer", "维修商", "repairer"),
            ("bms-vendor", "BMS 供应商", "supplier"),
            ("customer", "客户", "customer"),
            ("customer-2", "二手客户", "customer"),
        ):
            service.register_party("admin", party_id, name, kind)
        service.register_pack("admin", "pack-1", "翻新整包", "customer", "2026-06-01T00:00:00Z")

        service.register_component("admin", "enclosure-1", "enclosure", "ENC")
        service.attach_term(
            "admin", "enclosure-1", "assembly", "oem",
            ["结构缺陷"], ["撞击"], "交付", "60 个月", "2026-06-01T00:00:00Z", duration_months=60,
        )
        service.register_component("admin", "module-1", "module", "MOD")
        service.attach_term(
            "admin", "module-1", "assembly", "oem",
            ["容量衰减"], ["进水"], "出厂", "24 个月", "2025-01-01T00:00:00Z", duration_months=24,
        )
        service.attach_term(
            "admin", "module-1", "repair", "repairer",
            ["维修失效"], ["再拆"], "维修交付", "12 个月", "2026-06-01T00:00:00Z",
            duration_months=12, term_effect="reset", supersedes_version=1,
        )
        service.register_component("admin", "bms-1", "bms", "BMS-OLD")
        service.attach_term(
            "admin", "bms-1", "assembly", "bms-vendor",
            ["采样失效"], ["改写固件"], "出厂", "24 个月", "2025-06-01T00:00:00Z", duration_months=24,
        )
        service.assemble_slot("admin", "pack-1", "enclosure", "enclosure-1", "2026-06-01T00:00:00Z")
        service.assemble_slot("admin", "pack-1", "module-bank", "module-1", "2026-06-01T00:00:00Z")
        service.assemble_slot("admin", "pack-1", "bms", "bms-1", "2026-06-01T00:00:00Z")

        service.register_component("admin", "bms-2", "bms", "BMS-NEW")
        service.attach_term(
            "admin", "bms-2", "replacement", "bms-vendor",
            ["采样失效", "通讯中断"], ["改写固件"], "更换上电", "24 个月",
            "2026-07-15T00:00:00Z", duration_months=24,
            term_effect="new_full", predecessor_component_id="bms-1",
        )
        service.replace_component("admin", "pack-1", "bms", "bms-2", "2026-07-15T00:00:00Z")


class ServiceTests(WarrantyScenarioMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        self.service = WarrantyClaimService(self.connection, self.clock)
        self.seed_catalog(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def _intake(self, claim_id: str = "claim-1", failure_at: str = "2026-09-15T08:30:00Z") -> dict:
        return self.service.intake_claim(
            "agent", claim_id, "pack-1", "customer", f"fault-{claim_id}",
            "容量骤降", failure_at, [evidence("充放电曲线", "a"), evidence("告警日志", "b")],
        )

    # ---- 条款版本与保修延续 ----

    def test_repair_must_supersede_latest_and_declare_effect(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.attach_term(
                "admin", "module-1", "repair", "repairer", ["x"], ["y"],
                "维修", "12 个月", "2026-09-01T00:00:00Z", duration_months=12, supersedes_version=1,
            )
        with self.assertRaises(ValidationFailed):
            self.service.attach_term(
                "admin", "module-1", "repair", "repairer", ["x"], ["y"],
                "维修", "12 个月", "2026-09-01T00:00:00Z", duration_months=12,
                term_effect="reset", supersedes_version=1,
            )

    def test_continue_effect_caps_end_at_predecessor_window(self) -> None:
        self.service.register_component("admin", "fan-1", "other", "FAN")
        self.service.attach_term(
            "admin", "fan-1", "assembly", "oem", ["电机"], [], "交付", "24 个月",
            "2026-06-01T00:00:00Z", duration_months=24,
        )
        result = self.service.attach_term(
            "admin", "fan-1", "repair", "oem", ["电机"], [], "维修交付", "24 个月",
            "2027-06-01T00:00:00Z", duration_months=24,
            term_effect="continue", supersedes_version=1,
        )
        self.assertEqual(result["end_at"], "2028-06-01T00:00:00Z")

    def test_replacement_term_must_reference_slot_predecessor(self) -> None:
        self.service.register_component("admin", "bms-3", "bms", "BMS-X")
        self.service.attach_term(
            "admin", "bms-3", "replacement", "bms-vendor", ["x"], ["y"], "更换", "24 个月",
            "2026-09-01T00:00:00Z", duration_months=24,
            term_effect="new_full", predecessor_component_id="bms-1",
        )
        with self.assertRaises(ValidationFailed):
            self.service.replace_component(
                "admin", "pack-1", "bms", "bms-3", "2026-09-01T00:00:00Z"
            )

    def test_warranty_continuation_lists_each_slot_basis(self) -> None:
        items = {item["component_id"]: item for item in self.service.warranty_continuation("pack-1")}
        self.assertEqual(set(items), {"enclosure-1", "module-1", "bms-2"})
        self.assertEqual(items["module-1"]["continuation_basis"], "维修件重新起算")
        self.assertEqual(items["module-1"]["warranty_end_at"], "2027-06-01T00:00:00Z")
        self.assertTrue(items["bms-2"]["active"])

    # ---- 受理冻结 ----

    def test_intake_freezes_configuration_terms_and_holds(self) -> None:
        intake = self._intake()
        self.assertEqual(set(intake["warrantor_party_ids"]), {"oem", "repairer", "bms-vendor"})
        freeze = json.loads(
            self.connection.execute(
                "SELECT configuration_json FROM claim_freeze WHERE claim_id='claim-1'"
            ).fetchone()["configuration_json"]
        )
        by_position = {entry["position"]: entry for entry in freeze}
        self.assertEqual(by_position["bms"]["component_id"], "bms-2")
        self.assertEqual(by_position["module-bank"]["term"]["version"], 2)
        self.assertEqual(by_position["module-bank"]["term"]["warrantor_party_id"], "repairer")
        holds = {
            row["party_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT party_id,status FROM evidence_holds WHERE claim_id='claim-1'"
            )
        }
        self.assertEqual(holds, {"oem": "held", "repairer": "held", "bms-vendor": "held"})

    def test_freeze_uses_owner_at_failure_time(self) -> None:
        self.service.record_ownership_transfer(
            "admin", "pack-1", "customer-2", "2026-08-01T00:00:00Z"
        )
        self._intake()
        owner = self.connection.execute(
            "SELECT owner_party_id FROM claim_freeze WHERE claim_id='claim-1'"
        ).fetchone()[0]
        self.assertEqual(owner, "customer-2")

    def test_freeze_reflects_past_configuration(self) -> None:
        # 故障发生在 BMS 更换之前：冻结旧 BMS 及其原厂装配条款。
        intake = self.service.intake_claim(
            "agent", "claim-old", "pack-1", "customer", "fault-old",
            "早期告警", "2026-07-01T00:00:00Z", [evidence("旧日志", "e")],
        )
        self.assertNotIn("bms-2", intake["warrantor_party_ids"])
        freeze = json.loads(self.connection.execute(
            "SELECT configuration_json FROM claim_freeze WHERE claim_id='claim-old'"
        ).fetchone()["configuration_json"])
        self.assertEqual(
            [entry["component_id"] for entry in freeze if entry["position"] == "bms"], ["bms-1"]
        )

    def test_same_fault_cannot_open_twice_or_reclaim_after_payment(self) -> None:
        self._intake()
        with self.assertRaises(Conflict):
            self.service.intake_claim(
                "agent", "claim-2", "pack-1", "customer", "fault-claim-1",
                "重复", "2026-09-16T00:00:00Z", [evidence("x", "f")],
            )

    def test_intake_rejects_failure_before_delivery(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.intake_claim(
                "agent", "claim-x", "pack-1", "customer", "fault-x",
                "太早", "2026-01-01T00:00:00Z", [evidence("x", "f")],
            )

    # ---- 决定推进与不可变性 ----

    def _settle_first_round(self, claim_id: str = "claim-1") -> None:
        fault_key = f"fault-{claim_id}"
        self.service.intake_claim(
            "agent", claim_id, "pack-1", "customer", fault_key,
            "容量骤降", "2026-09-15T08:30:00Z",
            [evidence("充放电曲线", "a"), evidence("告警日志", "b")],
        )
        self.service.propose_allocation("admin", claim_id, [
            {"party_id": "oem", "share_basis_points": 2000, "amount_cny": "2000.00", "rationale": "r"},
            {"party_id": "repairer", "share_basis_points": 3000, "amount_cny": "3000.00", "rationale": "r"},
            {"party_id": "bms-vendor", "share_basis_points": 5000, "amount_cny": "5000.00", "rationale": "r"},
        ])
        self.service.confirm_allocation("admin", claim_id, "oem", "OEM-1")
        self.service.confirm_allocation("admin", claim_id, "repairer", "REP-1")
        self.service.dispute_allocation("admin", claim_id, "bms-vendor", "比例", "不认可")
        self.service.propose_settlement("admin", claim_id, "部分先行")
        self.service.notify_settlement("approver", claim_id, "NOTICE-1")

    def test_allocation_shares_must_total_100_percent_and_match_freeze(self) -> None:
        self._intake()
        with self.assertRaises(ValidationFailed):
            self.service.propose_allocation("admin", "claim-1", [
                {"party_id": "oem", "share_basis_points": 9000, "amount_cny": "9.00", "rationale": "r"},
            ])
        with self.assertRaises(ValidationFailed):
            self.service.propose_allocation("admin", "claim-1", [
                {"party_id": "oem", "share_basis_points": 5000, "amount_cny": "5.00", "rationale": "r"},
                {"party_id": "customer-2", "share_basis_points": 5000, "amount_cny": "5.00", "rationale": "r"},
            ])

    def test_partial_confirmation_cannot_release_other_holds(self) -> None:
        self._settle_first_round()
        # 通知前任何释放都被拒绝。
        with self.assertRaises(InvalidState):
            self.service.release_hold("admin", "claim-1", "bms-vendor", "试图提前释放")
        # 未赔付的争议方即使在通知后也不能释放。
        with self.assertRaises(InvalidState):
            self.service.release_hold("admin", "claim-1", "bms-vendor", "争议未决")
        # 已确认赔付方逐方释放，不影响争议方。
        self.service.release_hold("admin", "claim-1", "oem", "已赔付")
        statuses = {
            row["party_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT party_id,status FROM evidence_holds WHERE claim_id='claim-1'"
            )
        }
        self.assertEqual(statuses["bms-vendor"], "held")
        self.assertEqual(statuses["repairer"], "held")
        self.assertEqual(statuses["oem"], "released")

    def test_notified_decision_is_immutable_in_database(self) -> None:
        self._settle_first_round()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE claim_decisions SET content_json='{}' WHERE claim_id='claim-1' AND notified_at IS NOT NULL"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM claim_freeze WHERE claim_id='claim-1'")

    def test_late_finding_only_reopens_and_keeps_old_conclusion(self) -> None:
        self._intake("claim-open")
        with self.assertRaises(InvalidState):
            self.service.register_late_finding(
                "invest", "claim-open", "在途检测", "c" * 64, "在途索赔应走补证而非迟到登记"
            )
        self._settle_first_round()
        late = self.service.register_late_finding(
            "invest", "claim-1", "迟到拆解", "d" * 64, "BMS 批次缺陷"
        )
        # 旧结论不可被新决定覆盖：复开前不能直接追加新分摊。
        with self.assertRaises(InvalidState):
            self.service.propose_allocation("admin", "claim-1", [
                {"party_id": "bms-vendor", "share_basis_points": 10000, "amount_cny": "1.00", "rationale": "r"},
            ])
        reopened = self.service.reopen_claim("admin", "claim-1", "迟到证据", late["finding_id"])
        self.assertEqual(reopened["revision"], 4)
        old = self.connection.execute(
            "SELECT revision,status,kind FROM claim_decisions WHERE claim_id='claim-1' "
            "AND kind='settlement' ORDER BY revision"
        ).fetchall()
        self.assertEqual([(row["revision"], row["status"]) for row in old], [(3, "notified")])
        # 同一份迟到发现不能重复复开。
        with self.assertRaises(InvalidState):
            self.service.reopen_claim("admin", "claim-1", "再次复开", late["finding_id"])

    def test_reopen_without_finding_requires_open_dispute(self) -> None:
        # 驳回结案且无未决争议、无迟到检测时，不能没有依据就复开。
        self._intake("claim-r")
        self.service.propose_rejection("admin", "claim-r", "属于排除条款")
        self.service.notify_rejection("approver", "claim-r", "NOTICE-R")
        with self.assertRaises(ValidationFailed):
            self.service.reopen_claim("admin", "claim-r", "没有依据")
        # 通知时仍有未决争议的和解单，可以据此复开。
        self._settle_first_round("claim-d")
        reopened = self.service.reopen_claim("admin", "claim-d", "按 BMS 供应商争议复开")
        self.assertEqual(reopened["state"], "reopened")

    def test_reopen_settlement_pays_only_unpaid_party_once_per_fault(self) -> None:
        self._settle_first_round()
        self.service.release_hold("admin", "claim-1", "oem", "ok")
        self.service.release_hold("admin", "claim-1", "repairer", "ok")
        late = self.service.register_late_finding(
            "invest", "claim-1", "迟到拆解", "d" * 64, "BMS 批次缺陷"
        )
        self.service.reopen_claim("admin", "claim-1", "确认为 BMS 责任", late["finding_id"])
        self.service.propose_allocation("admin", "claim-1", [
            {"party_id": "oem", "share_basis_points": 2000, "amount_cny": "2000.00", "rationale": "r"},
            {"party_id": "repairer", "share_basis_points": 3000, "amount_cny": "3000.00", "rationale": "r"},
            {"party_id": "bms-vendor", "share_basis_points": 5000, "amount_cny": "5000.00", "rationale": "r"},
        ])
        for party, ref in (("oem", "OEM-2"), ("repairer", "REP-2"), ("bms-vendor", "BMS-2")):
            self.service.confirm_allocation("admin", "claim-1", party, ref)
        self.resolve_open_disputes()
        self.service.propose_settlement("admin", "claim-1", "二轮和解")
        self.service.notify_settlement("approver", "claim-1", "NOTICE-2")

        paid = [
            (row["party_id"], row["amount_cny"])
            for row in self.connection.execute(
                "SELECT party_id,amount_cny FROM claim_payments ORDER BY payment_id"
            )
        ]
        self.assertEqual(paid, [("oem", "2000.00"), ("repairer", "3000.00"), ("bms-vendor", "5000.00")])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM paid_faults").fetchone()[0], 1
        )
        # 全局：同一故障不能再开新索赔。
        with self.assertRaises(Conflict):
            self.service.intake_claim(
                "agent", "claim-again", "pack-1", "customer", "fault-claim-1",
                "重复赔付尝试", "2026-09-20T00:00:00Z", [evidence("x", "f")],
            )

    def resolve_open_disputes(self) -> None:
        rows = self.connection.execute(
            "SELECT dispute_id FROM claim_disputes WHERE claim_id='claim-1' AND status='open'"
        ).fetchall()
        for row in rows:
            self.service.resolve_dispute("admin", "claim-1", row["dispute_id"], "迟到证据解决")

    def test_rejection_keeps_holds_and_late_finding_can_reopen(self) -> None:
        self._intake()
        self.service.propose_rejection("admin", "claim-1", "故障属于排除条款：固件被改写")
        self.service.notify_rejection("approver", "claim-1", "NOTICE-R")
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM evidence_holds WHERE claim_id='claim-1' AND status='held'"
            ).fetchone()[0],
            3,
        )
        with self.assertRaises(InvalidState):
            self.service.release_hold("admin", "claim-1", "oem", "驳回也不能释放")
        late = self.service.register_late_finding(
            "invest", "claim-1", "新检测", "e" * 64, "排除条款不成立"
        )
        self.service.reopen_claim("admin", "claim-1", "新检测推翻排除认定", late["finding_id"])
        claim = self.connection.execute(
            "SELECT state FROM claims WHERE claim_id='claim-1'"
        ).fetchone()
        self.assertEqual(claim["state"], "reopened")

    def test_settlement_requires_no_pending_lines(self) -> None:
        self._intake()
        self.service.propose_allocation("admin", "claim-1", [
            {"party_id": "oem", "share_basis_points": 10000, "amount_cny": "1.00", "rationale": "r"},
        ])
        with self.assertRaises(InvalidState):
            self.service.propose_settlement("admin", "claim-1", "尚未确认")

    # ---- 证据与角色 ----

    def test_evidence_is_append_only(self) -> None:
        self._intake()
        added = self.service.add_evidence("invest", "claim-1", [evidence("补证", "c")])
        self.assertEqual(added["added"], 1)
        with self.assertRaises(Conflict):
            self.service.add_evidence("invest", "claim-1", [evidence("重复摘要", "c")])
        sources = [
            row[0] for row in self.connection.execute(
                "SELECT source FROM claim_evidence WHERE claim_id='claim-1' ORDER BY evidence_id"
            )
        ]
        self.assertEqual(sources, ["intake", "intake", "supplement"])

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.intake_claim(
                "invest", "claim-z", "pack-1", "customer", "fault-z",
                "x", "2026-09-15T00:00:00Z", [evidence("x", "f")],
            )
        self._intake()
        with self.assertRaises(Forbidden):
            self.service.record_investigation("agent", "claim-1", "越权调查")
        with self.assertRaises(Forbidden):
            self.service.notify_rejection("admin", "claim-1", "管理员不能自行通知")

    def test_explanation_covers_parties_disputes_and_continuation(self) -> None:
        self._settle_first_round()
        self.service.release_hold("admin", "claim-1", "oem", "原厂已赔付")
        explanation = self.service.explain_claim("agent", "claim-1")
        narrative = explanation["narrative"]
        self.assertIn("原厂", narrative)
        self.assertIn("维修商", narrative)
        self.assertIn("BMS 供应商", narrative)
        self.assertIn("未决争议", narrative)
        self.assertIn("不可改写", narrative)
        self.assertIn("剩余保修延续", narrative)
        self.assertEqual(
            explanation["notified_conclusion"]["kind"], "settlement"
        )
        parties = {item["party_id"]: item for item in explanation["responsibility"]}
        self.assertEqual(parties["bms-vendor"]["evidence_hold"], "held")
        self.assertEqual(parties["oem"]["evidence_hold"], "released")

    def test_explanation_notes_out_of_warranty_component(self) -> None:
        self.service.register_component("admin", "fan-1", "other", "FAN")
        self.service.attach_term(
            "admin", "fan-1", "assembly", "oem", ["电机"], [], "交付", "6 个月",
            "2025-01-01T00:00:00Z", duration_months=6,
        )
        self.service.assemble_slot("admin", "pack-1", "fan", "fan-1", "2026-06-01T00:00:00Z")
        self._intake("claim-fan")
        explanation = self.service.explain_claim("agent", "claim-fan")
        self.assertIn("fan-1", explanation["narrative"])
        self.assertIn("没有处于有效期内的保修条款", explanation["narrative"])
        positions = {
            entry["position"]: entry["in_warranty"]
            for entry in explanation["freeze"]["configuration"]
        }
        self.assertFalse(positions["fan"])

    def test_missing_claim_and_pack_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.explain_claim("auditor", "missing")
        with self.assertRaises(NotFound):
            self.service.warranty_continuation("missing-pack")


if __name__ == "__main__":
    unittest.main()
