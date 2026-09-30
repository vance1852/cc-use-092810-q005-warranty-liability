"""翻新储能电池保修责任与索赔流程的离线验收入口。

场景：一套翻新储能电池整包由原厂壳体、第三方维修的模组和本次更换的 BMS
组成，交付三个月后容量骤降。脚本验证：
1. 各组件条款按装配/维修版本关联责任方、覆盖范围、起止条件、排除条款；
2. 受理时冻结故障证据、当时配置、所有权与有效条款；
3. 调查、补证、责任分摊、和解作为有版本决定推进，部分确认不释放保全；
4. 已通知结论不可改写；迟到检测只能复开，同一故障不重复赔付；
5. 更换组件后剩余保修延续可登记，客服解释视图可读。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import WarrantyClaimsService
from .storage import connect, inspect_schema


def _terms(terms_id: str, version: int, kind: str, warrantor: str, months: int,
           exclusions: list[str] | None = None) -> dict[str, object]:
    return {
        "terms_id": terms_id,
        "version": version,
        "component_kind": kind,
        "warrantor_id": warrantor,
        "title": f"{terms_id} 保修条款 v{version}",
        "coverage": ["容量保持率低于约定阈值", "材料与工艺缺陷"],
        "exclusions": exclusions or ["私自拆解", "超出充放电边界使用"],
        "start_basis": "calendar",
        "start_condition": "fitted",
        "end_basis": "calendar",
        "end_condition": f"months:{months}",
        "liability_limit_cny": "50000",
        "notes": "翻新件条款，起止以装配记录为准",
    }


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="warranty-claims-") as temporary:
        database = Path(temporary) / "warranty.sqlite3"
        connection = connect(database)
        try:
            svc = WarrantyClaimsService(connection)
            for user_id, name, role in (
                ("agent-1", "客服", "service_agent"),
                ("mgr-1", "保修经理", "warranty_manager"),
                ("adj-1", "理赔理算师", "claims_adjuster"),
                ("fin-1", "财务", "finance"),
                ("aud-1", "审计", "auditor"),
            ):
                svc.create_user(user_id, name, role)

            # 责任方：原厂壳体厂、第三方维修商、BMS 供应商。
            svc.register_warrantor("mgr-1", "oem-shell", "原厂壳体公司", "oem")
            svc.register_warrantor("mgr-1", "fix-mod", "第三方模组维修商", "repair_vendor")
            svc.register_warrantor("mgr-1", "bms-co", "BMS 供应商", "component_supplier")

            # 组件实例。
            for serial, kind, party in (
                ("shell-001", "pack_shell", "oem-shell"),
                ("mod-r-007", "module", "fix-mod"),
                ("bms-new-209", "bms", "bms-co"),
                ("bms-rpl-210", "bms", "bms-co"),
            ):
                svc.register_component("mgr-1", serial, kind, party)

            # 条款：壳体原厂 36 个月；维修模组 12 个月且排除"二次翻新损坏"；BMS 24 个月。
            svc.publish_terms("mgr-1", _terms("t-shell", 1, "pack_shell", "oem-shell", 36))
            svc.publish_terms("mgr-1", _terms("t-mod", 1, "module", "fix-mod", 12,
                                              exclusions=["私自拆解", "二次翻新损坏"]))
            svc.publish_terms("mgr-1", _terms("t-bms", 1, "bms", "bms-co", 24))

            # 所有权：交付给客户 site-alpha。
            svc.record_ownership("mgr-1", "pack-reuse-9", "site-alpha",
                                 "2026-06-01T00:00:00Z")

            # 初始装配（2026-06-01）：壳体 + 维修模组 + 初装 BMS。
            svc.record_configuration("mgr-1", {
                "config_id": "cfg-assembly-1",
                "pack_serial": "pack-reuse-9",
                "event_type": "assembly",
                "event_at": "2026-06-01T02:00:00Z",
                "supplier_id": "fix-mod",
                "work_order_ref": "WO-ASSEMBLY-77",
                "slots": [
                    {"slot_id": "shell", "component_serial": "shell-001", "component_kind": "pack_shell",
                     "terms_id": "t-shell", "terms_version": 1,
                     "fitted_at": "2026-06-01T02:00:00Z", "action": "原厂壳体装入"},
                    {"slot_id": "module-1", "component_serial": "mod-r-007", "component_kind": "module",
                     "terms_id": "t-mod", "terms_version": 1,
                     "fitted_at": "2026-06-01T02:00:00Z", "action": "维修模组装入"},
                    {"slot_id": "bms", "component_serial": "bms-new-209", "component_kind": "bms",
                     "terms_id": "t-bms", "terms_version": 1,
                     "fitted_at": "2026-06-01T02:00:00Z", "action": "初装 BMS"},
                ],
                "note": "翻新整包出厂装配",
            })

            # 交付后维修（2026-07-15）：本次更换 BMS，新 BMS 按其自身条款起算。
            svc.record_configuration("mgr-1", {
                "config_id": "cfg-repair-2",
                "pack_serial": "pack-reuse-9",
                "event_type": "repair",
                "event_at": "2026-07-15T06:00:00Z",
                "supplier_id": "fix-mod",
                "work_order_ref": "WO-REPAIR-132",
                "slots": [
                    {"slot_id": "shell", "component_serial": "shell-001", "component_kind": "pack_shell",
                     "terms_id": "t-shell", "terms_version": 1,
                     "fitted_at": "2026-06-01T02:00:00Z", "action": "沿用"},
                    {"slot_id": "module-1", "component_serial": "mod-r-007", "component_kind": "module",
                     "terms_id": "t-mod", "terms_version": 1,
                     "fitted_at": "2026-06-01T02:00:00Z", "action": "沿用"},
                    {"slot_id": "bms", "component_serial": "bms-rpl-210", "component_kind": "bms",
                     "terms_id": "t-bms", "terms_version": 1,
                     "fitted_at": "2026-07-15T06:00:00Z", "action": "本次更换 BMS"},
                ],
                "note": "保修期内更换 BMS，其余组件沿用",
            })

            # 三个月后（2026-09-28）容量骤降，客户索赔。
            claim = svc.open_claim("agent-1", {
                "claim_id": "clm-2026-0001",
                "pack_serial": "pack-reuse-9",
                "failure_occurred_at": "2026-09-28T03:00:00Z",
                "reported_by_owner_id": "site-alpha",
                "fault_fingerprint": "fp-capacity-drop-2026-09",
                "evidence_items": [
                    {"evidence_ref": "ev-log", "kind": "bms_log",
                     "content_sha256": "1" * 64,
                     "captured_at": "2026-09-28T03:05:00Z", "note": "容量骤降时刻 BMS 日志"},
                    {"evidence_ref": "ev-snapshot", "kind": "capacity_report",
                     "content_sha256": "2" * 64,
                     "captured_at": "2026-09-29T01:00:00Z", "note": "第三方容量复测报告"},
                ],
                "claimed_items": [
                    {"slot_id": "module-1", "component_serial": "mod-r-007",
                     "symptom": "可用容量较交付值下降 31%"},
                    {"slot_id": "bms", "component_serial": "bms-rpl-210",
                     "symptom": "SOC 估算异常，疑似加速老化"},
                ],
                "summary": "交付三个月后容量骤降，客户要求赔偿",
            })
            assert claim["frozen_owner_id"] == "site-alpha"
            assert claim["frozen_config_id"] == "cfg-repair-2"
            assert len(claim["evidence_holds"]) == 3
            states = {slot["component_serial"]: slot["effectiveness_state"]
                      for slot in claim["frozen_slots"]}
            assert states == {"shell-001": "active", "mod-r-007": "active", "bms-rpl-210": "active"}

            # 调查与补证。
            inv = svc.record_investigation("adj-1", "clm-2026-0001",
                                           "初步判断与模组一致性及 BMS 估算均相关")
            svc.supplement_evidence("adj-1", "clm-2026-0001", [
                {"evidence_ref": "ev-teardown", "kind": "teardown_photo",
                 "content_sha256": "3" * 64,
                 "captured_at": "2026-10-02T08:00:00Z", "note": "拆检照片"},
            ], "拆检补充证据")

            # 终局前释放保全必须被拒绝（争议未决）。
            released_early = False
            try:
                svc.release_evidence("mgr-1", "clm-2026-0001", "提前放行")
            except Exception:
                released_early = True
            assert released_early

            # 责任分摊：维修商与 BMS 方各半，BMS 方暂有争议。
            alloc = svc.allocate_liability("mgr-1", "clm-2026-0001", [
                {"party_id": "fix-mod", "share": "0.5", "status": "confirmed",
                 "rationale": "维修模组容量衰减异常"},
                {"party_id": "bms-co", "share": "0.5", "status": "disputed",
                 "rationale": "BMS 厂主张 SOC 偏差不构成容量损失"},
            ], "按拆检与日志初判分摊")
            assert alloc["supersedes_revision"] is None

            # 部分责任方确认后，任何一方的证据保全都不得释放：
            held_after_partial = {
                row["party_id"] for row in claim["evidence_holds"]
            }
            detail = svc.get_claim("clm-2026-0001")
            assert all(row["status"] == "held" for row in detail["evidence_holds"])
            assert held_after_partial == {"fix-mod", "bms-co", "oem-shell"}

            # 有争议时和解必须被拒绝。
            settled_with_dispute = False
            try:
                svc.record_settlement("adj-1", "clm-2026-0001", [
                    {"component_serial": "mod-r-007", "party_id": "fix-mod",
                     "amount_cny": "9000", "remedy": "repair"},
                ], "争议未决尝试和解")
            except Exception:
                settled_with_dispute = True
            assert settled_with_dispute

            # BMS 方确认后形成新版本分摊（新版本不覆盖旧版本）。
            alloc2 = svc.allocate_liability("mgr-1", "clm-2026-0001", [
                {"party_id": "fix-mod", "share": "0.5", "status": "confirmed",
                 "rationale": "维修模组容量衰减异常"},
                {"party_id": "bms-co", "share": "0.5", "status": "confirmed",
                 "rationale": "复测确认 BMS 参与容量异常"},
            ], "复测后双方确认")
            assert alloc2["supersedes_revision"] is not None

            settlement = svc.record_settlement("adj-1", "clm-2026-0001", [
                {"component_serial": "mod-r-007", "party_id": "fix-mod",
                 "amount_cny": "9000", "remedy": "repair"},
                {"component_serial": "bms-rpl-210", "party_id": "bms-co",
                 "amount_cny": "9000", "remedy": "replace"},
            ], "双方按确认份额各承担 9000 元")

            # 重复赔付必须被数据库与服务双重拒绝。
            duplicate_blocked = False
            try:
                svc.record_settlement("adj-1", "clm-2026-0001", [
                    {"component_serial": "mod-r-007", "party_id": "fix-mod",
                     "amount_cny": "1", "remedy": "refund"},
                ], "重复赔付尝试")
            except Exception:
                duplicate_blocked = True
            assert duplicate_blocked

            notified = svc.notify_customer("adj-1", "clm-2026-0001",
                                           settlement["decision_id"], "site-alpha", "email")

            # 已通知结论不可改写。
            rewrite_blocked = False
            try:
                svc.notify_customer("adj-1", "clm-2026-0001",
                                    settlement["decision_id"], "site-alpha", "sms")
            except Exception:
                rewrite_blocked = True
            assert rewrite_blocked

            # 终局后登记保修延续：更换 BMS 按剩余期限延续；维修模组期限不变。
            svc.register_continuation("mgr-1", "clm-2026-0001", {
                "original_serial": "bms-rpl-210",
                "replacement_serial": "bms-rpl-210",
                "warrantor_id": "bms-co",
                "terms_id": "t-bms",
                "terms_version": 1,
                "continuation_rule": "remaining",
                "remaining_end_condition": "months:24 自 2026-07-15 起算的剩余期限延续",
                "starts_at": "2026-10-05T00:00:00Z",
                "ends_at": "2028-07-14T23:59:59Z",
                "note": "更换不重置，仅延续原 BMS 条款剩余期限",
            })

            # 终局后统一放行保全。
            release = svc.release_evidence("mgr-1", "clm-2026-0001",
                                           "和解已履行，统一放行全部责任方证据")
            assert set(release["released_parties"]) == {"oem-shell", "fix-mod", "bms-co"}

            # 迟到检测（和解通知后才到的检测报告）只能复开，不能改写已通知结论。
            reopen = svc.reopen_claim("mgr-1", "clm-2026-0001",
                                      "厂商迟到检测显示模组为主因，申请复核内部追偿",
                                      [{"evidence_ref": "ev-late", "kind": "late_test",
                                        "content_sha256": "4" * 64,
                                        "captured_at": "2026-10-20T00:00:00Z",
                                        "note": "迟到的第三方检测"}])
            after = svc.get_claim("clm-2026-0001")
            notified_decision = next(
                d for d in after["decisions"] if d["decision_id"] == notified["decision_id"]
            )
            assert notified_decision["immutable"] is True
            assert after["notified_decision_id"] == notified["decision_id"]
            # 复开后：旧放行记录保留，同批责任方新增 held 行（重新持有）。
            assert any(row["status"] == "released" for row in after["evidence_holds"])
            reheld = [row for row in after["evidence_holds"] if row["status"] == "held"]
            assert {row["party_id"] for row in reheld} == {"oem-shell", "fix-mod", "bms-co"}
            # 对客户而言只赔付一次：维持原结论，调整的仅是内部追偿份额。
            affirm = svc.record_affirmation("mgr-1", "clm-2026-0001",
                                            "迟到证据不改变对客户结论，内部追偿调整为模组方七成",
                                            revised_shares=[
                                                {"party_id": "fix-mod", "share": "0.7",
                                                 "status": "confirmed", "rationale": "迟到检测主因"},
                                                {"party_id": "bms-co", "share": "0.3",
                                                 "status": "confirmed", "rationale": "次要因素"},
                                            ])
            assert affirm["additional_payout"] is False
            payouts = connection.execute("SELECT count(*) FROM fault_payouts").fetchone()[0]
            assert payouts == 1
            svc.notify_customer("adj-1", "clm-2026-0001",
                                affirm["decision_id"], "site-alpha", "email")

            explanation = svc.claim_explanation("agent-1", "clm-2026-0001")
            audit = svc.audit_trail("aud-1", "clm-2026-0001")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    return {
        "status": "ok",
        "investigation_id": inv["decision_id"],
        "first_allocation_id": alloc["decision_id"],
        "second_allocation_id": alloc2["decision_id"],
        "settlement_id": settlement["decision_id"],
        "reopen_id": reopen["decision_id"],
        "affirmation_id": affirm["decision_id"],
        "frozen_config_id": claim["frozen_config_id"],
        "frozen_owner_id": claim["frozen_owner_id"],
        "payout_count": payouts,
        "explanation_status": explanation["status"],
        "liable_parties": sorted({
            item["warrantor_id"] for item in explanation["why_parties_liable"]
        }),
        "continuations": len(explanation["warranty_continuations"]),
        "audit_event_count": len(audit),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行保修责任与索赔管理的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
