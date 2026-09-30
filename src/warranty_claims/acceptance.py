"""保修责任与索赔管理完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import WarrantyClaimService
from .storage import connect, inspect_schema


def _evidence(label: str, seed: str) -> dict[str, str]:
    return {"label": label, "kind": "diagnostic", "content_sha256": seed * 64}


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="warranty-claims-") as temporary:
        database = Path(temporary) / "warranty.sqlite3"
        connection = connect(database)
        try:
            service = WarrantyClaimService(connection)
            service.create_user("agent-1", "客服受理", "service_agent")
            service.create_user("admin-1", "保修管理员", "warranty_admin")
            service.create_user("invest-1", "调查工程师", "investigator")
            service.create_user("approver-1", "索赔审批人", "approver")
            service.create_user("auditor-1", "审计人员", "auditor")

            # 责任主体：原厂、第三方维修商、BMS 供应商、客户。
            service.register_party("admin-1", "oem", "原厂动力", "oem")
            service.register_party("admin-1", "repairer", "第三方翻新维修商", "repairer")
            service.register_party("admin-1", "bms-vendor", "BMS 供应商", "supplier")
            service.register_party("admin-1", "customer", "场站客户", "customer")

            # 翻新整包 2026-06-01 交付：原厂壳体 + 第三方维修模组 + 旧 BMS。
            service.register_pack(
                "admin-1", "pack-1", "翻新 LFP 储能整包", "customer", "2026-06-01T00:00:00Z"
            )
            service.register_component("admin-1", "enclosure-1", "enclosure", "ENC-SN-001")
            service.attach_term(
                "admin-1", "enclosure-1", "assembly", "oem",
                ["壳体结构缺陷", "密封失效"], ["人为撞击", "私自拆解"],
                "整包交付", "交付后 60 个月", "2026-06-01T00:00:00Z",
                duration_months=60,
            )
            service.register_component("admin-1", "module-1", "module", "MOD-SN-077")
            service.attach_term(
                "admin-1", "module-1", "assembly", "oem",
                ["容量衰减", "内阻异常"], ["进水", "超倍率使用"],
                "模组出厂", "出厂后 24 个月", "2025-01-01T00:00:00Z",
                duration_months=24,
            )
            # 第三方维修版本：维修件短保修，自维修日重新起算。
            service.attach_term(
                "admin-1", "module-1", "repair", "repairer",
                ["维修焊点失效", "容量不达标"], ["再次拆解", "外部短路"],
                "维修交付", "维修后 12 个月", "2026-06-01T00:00:00Z",
                duration_months=12, term_effect="reset", supersedes_version=1,
            )
            service.register_component("admin-1", "bms-1", "bms", "BMS-SN-OLD")
            service.attach_term(
                "admin-1", "bms-1", "assembly", "bms-vendor",
                ["采样失效", "均衡故障"], ["固件被改写"],
                "BMS 出厂", "出厂后 24 个月", "2025-06-01T00:00:00Z",
                duration_months=24,
            )
            service.assemble_slot("admin-1", "pack-1", "enclosure", "enclosure-1", "2026-06-01T00:00:00Z")
            service.assemble_slot("admin-1", "pack-1", "module-bank", "module-1", "2026-06-01T00:00:00Z")
            service.assemble_slot("admin-1", "pack-1", "bms", "bms-1", "2026-06-01T00:00:00Z")

            # 2026-07-15 本次更换 BMS：全新完整 24 个月保修。
            service.register_component("admin-1", "bms-2", "bms", "BMS-SN-NEW")
            service.attach_term(
                "admin-1", "bms-2", "replacement", "bms-vendor",
                ["采样失效", "均衡故障", "通讯中断"], ["固件被改写"],
                "更换上电", "更换后 24 个月", "2026-07-15T00:00:00Z",
                duration_months=24, term_effect="new_full", predecessor_component_id="bms-1",
            )
            service.replace_component("admin-1", "pack-1", "bms", "bms-2", "2026-07-15T00:00:00Z")

            # 交付三个月后容量骤降，客户索赔。
            intake = service.intake_claim(
                "agent-1", "claim-1", "pack-1", "customer", "fault-capdrop-20260915",
                "充放电容量骤降至额定 62%", "2026-09-15T08:30:00Z",
                [_evidence("首次充放电曲线", "a"), _evidence("现场告警日志", "b")],
            )
            warrantors = intake["warrantor_party_ids"]

            service.record_investigation("invest-1", "claim-1", "三方组件均在保，需进一步定责")
            service.request_supplement(
                "invest-1", "claim-1", "repairer", ["维修批次内阻复测"], "核实维修焊点"
            )
            service.add_evidence("invest-1", "claim-1", [_evidence("维修商复测报告", "c")])

            # 第一版责任分摊：原厂 20%、维修商 30%、BMS 供应商 50%。
            service.propose_allocation("admin-1", "claim-1", [
                {"party_id": "oem", "share_basis_points": 2000, "amount_cny": "2000.00",
                 "rationale": "壳体密封条款与本次故障弱相关"},
                {"party_id": "repairer", "share_basis_points": 3000, "amount_cny": "3000.00",
                 "rationale": "维修版本容量不达标"},
                {"party_id": "bms-vendor", "share_basis_points": 5000, "amount_cny": "5000.00",
                 "rationale": "均衡故障导致容量骤降"},
            ])
            service.confirm_allocation("admin-1", "claim-1", "oem", "OEM-CONF-1")
            service.confirm_allocation("admin-1", "claim-1", "repairer", "REP-CONF-1")
            service.dispute_allocation(
                "admin-1", "claim-1", "bms-vendor", "BMS 责任比例", "供应商主张固件日志证明 BMS 正常"
            )

            # 部分确认不能提前释放任何证据保全。
            service.propose_settlement("admin-1", "claim-1", "先就已确认部分和解，BMS 争议继续")
            hold_before = connection.execute(
                "SELECT status FROM evidence_holds WHERE claim_id='claim-1' AND party_id='oem'"
            ).fetchone()[0]
            notified = service.notify_settlement("approver-1", "claim-1", "NOTICE-S-1")
            # 已确认并赔付的两方可以逐方释放，争议方继续保全。
            service.release_hold("admin-1", "claim-1", "oem", "原厂部分已赔付")
            service.release_hold("admin-1", "claim-1", "repairer", "维修商部分已赔付")

            # 迟到检测：结论通知客户后才到达，不能改写结论，只能复开。
            late = service.register_late_finding(
                "invest-1", "claim-1", "第三方实验室迟到拆解报告", "d" * 64,
                "拆解确认新 BMS 均衡 MOS 批次性失效",
            )
            service.reopen_claim("admin-1", "claim-1", "迟到证据指向 BMS 批次缺陷", late["finding_id"])
            service.propose_allocation("admin-1", "claim-1", [
                {"party_id": "oem", "share_basis_points": 2000, "amount_cny": "2000.00",
                 "rationale": "壳体密封条款与本次故障弱相关"},
                {"party_id": "repairer", "share_basis_points": 3000, "amount_cny": "3000.00",
                 "rationale": "维修版本容量不达标"},
                {"party_id": "bms-vendor", "share_basis_points": 5000, "amount_cny": "5000.00",
                 "rationale": "批次性 MOS 失效确认为主因"},
            ])
            for party, ref in (
                ("oem", "OEM-CONF-2"), ("repairer", "REP-CONF-2"), ("bms-vendor", "BMS-CONF-2")
            ):
                service.confirm_allocation("admin-1", "claim-1", party, ref)
            # 复开后迟到拆解报告化解了 BMS 供应商的旧争议。
            disputes = connection.execute(
                "SELECT dispute_id FROM claim_disputes WHERE claim_id='claim-1' AND status='open'"
            ).fetchall()
            for row in disputes:
                service.resolve_dispute("admin-1", "claim-1", row["dispute_id"], "迟到拆解报告确认批次缺陷")
            service.propose_settlement("admin-1", "claim-1", "复开后 BMS 供应商确认承担")
            service.notify_settlement("approver-1", "claim-1", "NOTICE-S-2")
            service.release_hold("admin-1", "claim-1", "bms-vendor", "BMS 部分已赔付，保全关闭")

            payment_rows = connection.execute(
                "SELECT party_id, amount_cny FROM claim_payments ORDER BY payment_id"
            ).fetchall()
            payments = [dict(row) for row in payment_rows]
            paid_fault_count = connection.execute("SELECT count(*) FROM paid_faults").fetchone()[0]
            decisions = connection.execute(
                "SELECT revision,kind,status FROM claim_decisions ORDER BY revision"
            ).fetchall()
            explanation = service.explain_claim("auditor-1", "claim-1")
            continuation = service.warranty_continuation("pack-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    if hold_before != "held":
        raise RuntimeError("结论通知前证据保全被提前释放")
    if [row["party_id"] for row in payment_rows] != ["oem", "repairer", "bms-vendor"]:
        raise RuntimeError("赔付主体顺序与复开后只补赔争议方的预期不符")
    if paid_fault_count != 1:
        raise RuntimeError("同一故障出现了多条赔付台账索引")
    if [row["status"] for row in decisions if row["kind"] == "settlement"] != ["notified", "notified"]:
        raise RuntimeError("已通知的和解结论应原样保留并追加新版本")
    return {
        "status": "ok",
        "warrantors": warrantors,
        "frozen_at": intake["freeze_sha256"][:12],
        "payments": payments,
        "paid_fault_records": paid_fault_count,
        "decision_revisions": [dict(row) for row in decisions],
        "holds_released": 3,
        "continuation": [
            {"position": item["position"], "component_id": item["component_id"],
             "basis": item["continuation_basis"], "warranty_end_at": item["warranty_end_at"]}
            for item in continuation
        ],
        "narrative": explanation["narrative"],
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
