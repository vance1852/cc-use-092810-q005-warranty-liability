from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from warranty_claims.clock import FrozenClock
from warranty_claims.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from warranty_claims.service import WarrantyClaimsService


def terms(terms_id: str, version: int, kind: str, party: str, months: int,
          limit: str = "50000") -> dict:
    return {
        "terms_id": terms_id, "version": version, "component_kind": kind,
        "warrantor_id": party, "title": f"{terms_id} v{version}",
        "coverage": ["容量缺陷"], "exclusions": ["私拆"],
        "start_basis": "calendar", "start_condition": "fitted",
        "end_basis": "calendar", "end_condition": f"months:{months}",
        "liability_limit_cny": limit, "notes": "",
    }


class ClaimScenario:
    """整包 = 原厂壳体 + 第三方维修模组 + 更换过的 BMS。"""

    def __init__(self, service: WarrantyClaimsService) -> None:
        self.svc = service
        for uid, role in (
            ("agent", "service_agent"), ("mgr", "warranty_manager"),
            ("adj", "claims_adjuster"), ("fin", "finance"), ("aud", "auditor"),
        ):
            service.create_user(uid, uid, role)
        service.register_warrantor("mgr", "oem-shell", "原厂壳体公司", "oem")
        service.register_warrantor("mgr", "fix-mod", "第三方模组维修商", "repair_vendor")
        service.register_warrantor("mgr", "bms-co", "BMS 供应商", "component_supplier")
        for serial, kind, party in (
            ("shell-1", "pack_shell", "oem-shell"),
            ("mod-7", "module", "fix-mod"),
            ("bms-9", "bms", "bms-co"),
        ):
            service.register_component("mgr", serial, kind, party)
        service.publish_terms("mgr", terms("t-shell", 1, "pack_shell", "oem-shell", 36))
        service.publish_terms("mgr", terms("t-mod", 1, "module", "fix-mod", 12))
        service.publish_terms("mgr", terms("t-bms", 1, "bms", "bms-co", 24))
        service.record_ownership("mgr", "pack-9", "owner-a", "2026-06-01T00:00:00Z")
        service.record_configuration("mgr", {
            "config_id": "cfg-1", "pack_serial": "pack-9", "event_type": "assembly",
            "event_at": "2026-06-01T02:00:00Z", "supplier_id": "fix-mod",
            "work_order_ref": "WO-1",
            "slots": [
                {"slot_id": "shell", "component_serial": "shell-1", "component_kind": "pack_shell",
                 "terms_id": "t-shell", "terms_version": 1,
                 "fitted_at": "2026-06-01T02:00:00Z", "action": "装入"},
                {"slot_id": "module", "component_serial": "mod-7", "component_kind": "module",
                 "terms_id": "t-mod", "terms_version": 1,
                 "fitted_at": "2026-06-01T02:00:00Z", "action": "装入"},
                {"slot_id": "bms", "component_serial": "bms-9", "component_kind": "bms",
                 "terms_id": "t-bms", "terms_version": 1,
                 "fitted_at": "2026-06-01T02:00:00Z", "action": "初装"},
            ],
        })

    def repair_bms(self, serial: str = "bms-9") -> None:
        """2026-07-15 维修事件：记录一个新版本配置。"""

        self.svc.record_configuration("mgr", {
            "config_id": "cfg-2", "pack_serial": "pack-9", "event_type": "repair",
            "event_at": "2026-07-15T06:00:00Z", "supplier_id": "fix-mod",
            "work_order_ref": "WO-2",
            "slots": [
                {"slot_id": "shell", "component_serial": "shell-1", "component_kind": "pack_shell",
                 "terms_id": "t-shell", "terms_version": 1,
                 "fitted_at": "2026-06-01T02:00:00Z", "action": "沿用"},
                {"slot_id": "module", "component_serial": "mod-7", "component_kind": "module",
                 "terms_id": "t-mod", "terms_version": 1,
                 "fitted_at": "2026-06-01T02:00:00Z", "action": "沿用"},
                {"slot_id": "bms", "component_serial": serial, "component_kind": "bms",
                 "terms_id": "t-bms", "terms_version": 1,
                 "fitted_at": "2026-07-15T06:00:00Z", "action": "更换 BMS"},
            ],
        })

    def open(self, claim_id: str = "clm-1", *, occurred: str = "2026-09-28T03:00:00Z",
             fingerprint: str = "fp-1") -> dict:
        return self.svc.open_claim("agent", {
            "claim_id": claim_id, "pack_serial": "pack-9",
            "failure_occurred_at": occurred, "reported_by_owner_id": "owner-a",
            "fault_fingerprint": fingerprint,
            "evidence_items": [{
                "evidence_ref": "ev1", "kind": "log", "content_sha256": "a" * 64,
                "captured_at": occurred,
            }],
            "claimed_items": [{
                "slot_id": "module", "component_serial": "mod-7", "symptom": "容量骤降 31%",
            }],
            "summary": "容量骤降",
        })


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.svc = WarrantyClaimsService(self.connection, self.clock)
        self.scenario = ClaimScenario(self.svc)

    def tearDown(self) -> None:
        self.connection.close()

    def _confirmed_allocation(self) -> dict:
        return self.svc.allocate_liability("mgr", "clm-1", [
            {"party_id": "fix-mod", "share": "1", "status": "confirmed",
             "rationale": "维修模组衰减"},
        ], "模组方全责")

    def _settle(self) -> dict:
        return self.svc.record_settlement("adj", "clm-1", [
            {"component_serial": "mod-7", "party_id": "fix-mod",
             "amount_cny": "9000", "remedy": "repair"},
        ], "和解 9000 元")

    # ------------------------------------------------------------- 冻结

    def test_open_freezes_config_owner_terms_and_holds(self) -> None:
        self.scenario.repair_bms()
        claim = self.scenario.open()
        self.assertEqual(claim["frozen_config_id"], "cfg-2")
        self.assertEqual(claim["frozen_owner_id"], "owner-a")
        module = next(s for s in claim["frozen_slots"] if s["component_serial"] == "mod-7")
        self.assertEqual(module["warrantor_id"], "fix-mod")
        self.assertEqual(module["terms_version"], 1)
        self.assertEqual(module["effectiveness_state"], "active")
        self.assertEqual(module["coverage"], ["容量缺陷"])
        self.assertEqual(module["exclusions"], ["私拆"])
        # 冻结副本在后续条款修订后不改变。
        self.svc.publish_terms("mgr", terms("t-mod", 2, "module", "fix-mod", 3))
        again = self.svc.get_claim("clm-1")
        frozen = next(s for s in again["frozen_slots"] if s["component_serial"] == "mod-7")
        self.assertEqual(frozen["terms_version"], 1)
        held = {row["party_id"] for row in claim["evidence_holds"]}
        self.assertEqual(held, {"oem-shell", "fix-mod", "bms-co"})

    def test_configuration_at_failure_is_the_latest_before_it(self) -> None:
        # 故障发生在维修之前 -> 冻结的是初装配置。
        claim = self.scenario.open("clm-early", occurred="2026-07-01T00:00:00Z",
                                   fingerprint="fp-early")
        self.assertEqual(claim["frozen_config_id"], "cfg-1")

    def test_repair_action_changes_warranty_clock_for_new_bms(self) -> None:
        # 新装入 BMS 在 2027-07-14 仍按其 24 个月条款有效（自 2026-07-15 起算）。
        self.scenario.repair_bms()
        status = self.svc.component_warranty_status("bms-9", "2027-07-14T00:00:00Z")
        self.assertEqual(status["state"], "active")
        self.assertEqual(status["terms"]["warrantor_id"], "bms-co")
        expired = self.svc.component_warranty_status("bms-9", "2028-07-16T00:00:00Z")
        self.assertEqual(expired["state"], "expired")

    def test_open_without_owner_rejected(self) -> None:
        self.scenario.repair_bms()
        with self.assertRaises(InvalidState):
            self.svc.open_claim("agent", {
                "claim_id": "clm-x", "pack_serial": "pack-9",
                "failure_occurred_at": "2026-05-01T00:00:00Z",
                "reported_by_owner_id": "owner-a", "fault_fingerprint": "fp-x",
                "evidence_items": [{
                    "evidence_ref": "e", "kind": "log", "content_sha256": "b" * 64,
                    "captured_at": "2026-05-01T00:00:00Z"}],
                "claimed_items": [{
                    "slot_id": "module", "component_serial": "mod-7", "symptom": "x"}],
                "summary": "x",
            })

    def test_claimed_component_must_be_in_frozen_config(self) -> None:
        self.scenario.repair_bms()
        with self.assertRaises(ValidationFailed):
            self.svc.open_claim("agent", {
                "claim_id": "clm-bad", "pack_serial": "pack-9",
                "failure_occurred_at": "2026-09-28T03:00:00Z",
                "reported_by_owner_id": "owner-a", "fault_fingerprint": "fp-bad",
                "evidence_items": [{
                    "evidence_ref": "e", "kind": "log", "content_sha256": "c" * 64,
                    "captured_at": "2026-09-28T03:00:00Z"}],
                "claimed_items": [{
                    "slot_id": "bms", "component_serial": "ghost", "symptom": "x"}],
                "summary": "x",
            })

    # ----------------------------------------------------- 重复赔付与指纹

    def test_same_fault_fingerprint_cannot_open_twice(self) -> None:
        self.scenario.open()
        with self.assertRaises(Conflict):
            self.scenario.open("clm-2", fingerprint="fp-1")

    def test_no_double_payout_after_settlement(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self._confirmed_allocation()
        self._settle()
        with self.assertRaises(InvalidState):
            self._settle()
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM fault_payouts").fetchone()[0], 1
        )

    # ------------------------------------------------- 保全与部分确认

    def test_evidence_cannot_release_before_terminal(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        with self.assertRaises(InvalidState):
            self.svc.release_evidence("mgr", "clm-1", "提前放行")
        self._confirmed_allocation()
        # 部分责任方（此处仅模组方）确认后，未和解仍不得放行任何人的保全。
        with self.assertRaises(InvalidState):
            self.svc.release_evidence("mgr", "clm-1", "确认后放行")
        holds = self.connection.execute(
            "SELECT status, count(*) FROM evidence_holds GROUP BY status"
        ).fetchall()
        self.assertEqual({row[0]: row[1] for row in holds}, {"held": 3})

    def test_settlement_requires_all_confirmed(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self.svc.allocate_liability("mgr", "clm-1", [
            {"party_id": "fix-mod", "share": "0.5", "status": "confirmed"},
            {"party_id": "bms-co", "share": "0.5", "status": "disputed"},
        ], "争议分摊")
        with self.assertRaises(InvalidState):
            self._settle()

    def test_allocation_party_must_belong_to_frozen_config(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self.svc.register_warrantor("mgr", "outsider", "无关厂商", "other")
        with self.assertRaises(ValidationFailed):
            self.svc.allocate_liability("mgr", "clm-1", [
                {"party_id": "outsider", "share": "1", "status": "confirmed"}],
                "错误主体")

    def test_settlement_amount_over_limit_rejected(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self._confirmed_allocation()
        with self.assertRaises(InvalidState):
            self.svc.record_settlement("adj", "clm-1", [
                {"component_serial": "mod-7", "party_id": "fix-mod",
                 "amount_cny": "999999", "remedy": "refund"},
            ], "超上限")

    # ----------------------------------------------- 决定版本与通知不可变

    def test_decisions_are_versioned_and_supersede(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        first = self.svc.allocate_liability("mgr", "clm-1", [
            {"party_id": "fix-mod", "share": "0.5", "status": "proposed"},
            {"party_id": "bms-co", "share": "0.5", "status": "proposed"},
        ], "初判")
        second = self.svc.allocate_liability("mgr", "clm-1", [
            {"party_id": "fix-mod", "share": "1", "status": "confirmed",
             "rationale": "复核后全责"},
        ], "复核")
        self.assertIsNone(first["supersedes_revision"])
        self.assertEqual(second["supersedes_revision"], first["decision_id"])
        claim = self.svc.get_claim("clm-1")
        revisions = [d["revision"] for d in claim["decisions"]]
        self.assertEqual(revisions, sorted(revisions))
        # 旧版本仍然存在，没有被改写。
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM claim_decisions WHERE kind='liability_allocation'"
            ).fetchone()[0],
            2,
        )

    def test_notified_conclusion_is_immutable_and_reopen_only_appends(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self._confirmed_allocation()
        settlement = self._settle()
        self.svc.notify_customer("adj", "clm-1", settlement["decision_id"], "owner-a", "email")
        with self.assertRaises(InvalidState):
            self.svc.notify_customer("adj", "clm-1", settlement["decision_id"], "owner-a", "sms")
        # 终局后不能直接调查，必须复开。
        with self.assertRaises(InvalidState):
            self.svc.record_investigation("adj", "clm-1", "迟到结论尝试改写")
        reopen = self.svc.reopen_claim("mgr", "clm-1", "迟到检测", [{
            "evidence_ref": "late1", "kind": "late_test",
            "content_sha256": "d" * 64, "captured_at": "2026-10-20T00:00:00Z",
        }])
        claim = self.svc.get_claim("clm-1")
        notified = next(d for d in claim["decisions"]
                        if d["decision_id"] == settlement["decision_id"])
        self.assertTrue(notified["immutable"])
        self.assertEqual(claim["status"], "reopened")
        self.assertGreater(reopen["decision_id"], settlement["decision_id"])
        self.assertEqual(claim["notified_decision_id"], settlement["decision_id"])

    def test_reopen_then_affirm_pays_nothing_more(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self._confirmed_allocation()
        settlement = self._settle()
        self.svc.notify_customer("adj", "clm-1", settlement["decision_id"], "owner-a", "email")
        self.svc.release_evidence("mgr", "clm-1", "履行完毕放行")
        self.svc.reopen_claim("mgr", "clm-1", "迟到检测")
        # 复开后不能再就同一故障和解付款。
        with self.assertRaises(InvalidState):
            self._settle()
        # 复开后未结案也不能放行。
        with self.assertRaises(InvalidState):
            self.svc.release_evidence("mgr", "clm-1", "复开中放行")
        affirm = self.svc.record_affirmation("mgr", "clm-1", "维持原结论")
        self.assertFalse(affirm["additional_payout"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM fault_payouts").fetchone()[0], 1
        )
        self.svc.notify_customer("adj", "clm-1", affirm["decision_id"], "owner-a", "email")
        claim = self.svc.get_claim("clm-1")
        self.assertEqual(claim["prior_notified_decision_id"], settlement["decision_id"])
        self.assertEqual(claim["notified_decision_id"], affirm["decision_id"])

    def test_reopen_reholds_released_evidence_without_losing_history(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self._confirmed_allocation()
        settlement = self._settle()
        self.svc.notify_customer("adj", "clm-1", settlement["decision_id"], "owner-a", "email")
        released = self.svc.release_evidence("mgr", "clm-1", "放行")
        self.svc.reopen_claim("mgr", "clm-1", "迟到检测")
        rows = self.connection.execute(
            "SELECT party_id,status FROM evidence_holds ORDER BY hold_id"
        ).fetchall()
        # 旧放行行保留，且每方新增一行 held。
        self.assertEqual(
            sum(1 for r in rows if r["status"] == "released"), 3
        )
        held = {r["party_id"] for r in rows if r["status"] == "held"}
        self.assertEqual(held, {"oem-shell", "fix-mod", "bms-co"})
        self.assertEqual(len(released["released_parties"]), 3)

    def test_cannot_reopen_before_notification(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        with self.assertRaises(InvalidState):
            self.svc.reopen_claim("mgr", "clm-1", "尚未通知")

    # ------------------------------------------------------------- 延续

    def test_continuation_requires_settlement_and_known_terms(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        with self.assertRaises(InvalidState):
            self.svc.register_continuation("mgr", "clm-1", {
                "original_serial": "mod-7", "warrantor_id": "fix-mod",
                "terms_id": "t-mod", "terms_version": 1,
                "continuation_rule": "remaining",
                "remaining_end_condition": "剩余 8 个月",
                "starts_at": "2026-10-05T00:00:00Z",
            })
        self._confirmed_allocation()
        settlement = self._settle()
        self.svc.notify_customer("adj", "clm-1", settlement["decision_id"], "owner-a", "email")
        result = self.svc.register_continuation("mgr", "clm-1", {
            "original_serial": "mod-7", "warrantor_id": "fix-mod",
            "terms_id": "t-mod", "terms_version": 1,
            "continuation_rule": "remaining",
            "remaining_end_condition": "months:12 自 2026-06-01 的剩余期限",
            "starts_at": "2026-10-05T00:00:00Z",
            "ends_at": "2027-05-31T23:59:59Z",
        })
        self.assertEqual(result["continuation_rule"], "remaining")

    # ------------------------------------------------------------- 解释

    def test_explanation_reports_basis_disputes_and_continuations(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        self.svc.allocate_liability("mgr", "clm-1", [
            {"party_id": "fix-mod", "share": "1", "status": "disputed",
             "rationale": "维修商否认"},
        ], "争议中")
        view = self.svc.claim_explanation("agent", "clm-1")
        self.assertEqual(view["frozen_config_id"], "cfg-2")
        self.assertEqual(view["frozen_owner_id"], "owner-a")
        liable = {item["warrantor_id"] for item in view["why_parties_liable"]}
        self.assertEqual(liable, {"fix-mod"})
        self.assertEqual(view["open_disputes"]["unconfirmed_parties"], ["fix-mod"])
        self.assertIn("fix-mod", view["open_disputes"]["evidence_held_for_parties"])
        self.assertFalse(view["open_disputes"]["claim_terminal"])

    # ------------------------------------------------------------- 权限

    def test_role_permissions(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        with self.assertRaises(Forbidden):
            self.svc.record_settlement("agent", "clm-1", [], "客服无权和解")
        with self.assertRaises(Forbidden):
            self.svc.allocate_liability("agent", "clm-1", [], "客服无权分摊")
        with self.assertRaises(Forbidden):
            self.svc.audit_trail("agent", "clm-1")
        # 财务可读索赔但不能形成决定。
        self.assertEqual(self.svc.get_claim("clm-1")["status"], "open")
        with self.assertRaises(Forbidden):
            self.svc.record_rejection("fin", "clm-1", "财务驳回")

    def test_finance_can_read_payout_view(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        view = self.svc.get_claim("clm-1")
        self.assertIsNone(view["payout"])

    def test_missing_entities(self) -> None:
        with self.assertRaises(NotFound):
            self.svc.get_claim("nope")
        with self.assertRaises(NotFound):
            self.svc.component_warranty_status("ghost", "2026-09-01T00:00:00Z")

    def test_audit_trail_records_lifecycle(self) -> None:
        self.scenario.repair_bms()
        self.scenario.open()
        events = self.svc.audit_trail("aud", "clm-1")
        kinds = [event["event_type"] for event in events]
        self.assertIn("claim.opened", kinds)


if __name__ == "__main__":
    unittest.main()
