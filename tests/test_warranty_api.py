from __future__ import annotations

import json
import sqlite3
import unittest

from warranty_claims.api import JsonApplication
from warranty_claims.service import WarrantyClaimsService


def call(app: JsonApplication, method: str, path: str, payload: dict | None = None,
         actor: str | None = "mgr"):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
    headers = {"X-Actor-Id": actor} if actor else {}
    return app.handle(method, path, headers, body)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(WarrantyClaimsService(self.connection))
        for uid, role in (
            ("agent", "service_agent"), ("mgr", "warranty_manager"),
            ("adj", "claims_adjuster"), ("aud", "auditor"),
        ):
            call(self.app, "POST", "/users",
                 {"user_id": uid, "display_name": uid, "role": role}, actor=None)
        call(self.app, "POST", "/warrantors",
             {"party_id": "fix-mod", "display_name": "维修商", "party_type": "repair_vendor"})
        call(self.app, "POST", "/components",
             {"component_serial": "mod-7", "component_kind": "module",
              "source_party_id": "fix-mod"})
        terms_response = call(self.app, "POST", "/warranty_terms", {
            "terms_id": "t-mod", "version": 1, "component_kind": "module",
            "warrantor_id": "fix-mod", "title": "模组保修",
            "coverage": ["容量缺陷"], "exclusions": [],
            "start_basis": "calendar", "start_condition": "fitted",
            "end_basis": "calendar", "end_condition": "months:12",
            "liability_limit_cny": "50000",
        })
        self.assertEqual(terms_response.status, 201)
        call(self.app, "POST", "/pack_ownership",
             {"pack_serial": "pack-9", "owner_id": "owner-a",
              "valid_from": "2026-06-01T00:00:00Z"})
        config_response = call(self.app, "POST", "/configurations", {
            "config_id": "cfg-1", "pack_serial": "pack-9", "event_type": "assembly",
            "event_at": "2026-06-01T02:00:00Z", "supplier_id": "fix-mod",
            "work_order_ref": "WO-1",
            "slots": [{
                "slot_id": "module", "component_serial": "mod-7", "component_kind": "module",
                "terms_id": "t-mod", "terms_version": 1,
                "fitted_at": "2026-06-01T02:00:00Z", "action": "装入",
            }],
        })
        self.assertEqual(config_response.status, 201)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor(self) -> None:
        response = call(self.app, "POST", "/claims", {}, actor=None)
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_forbidden_role(self) -> None:
        response = call(self.app, "POST", "/warranty_terms",
                        {"terms_id": "x", "version": 1, "component_kind": "bms",
                         "warrantor_id": "fix-mod", "title": "t",
                         "coverage": ["a"], "exclusions": [],
                         "start_basis": "calendar", "start_condition": "fitted",
                         "end_basis": "calendar", "end_condition": "months:1",
                         "liability_limit_cny": "1"}, actor="agent")
        self.assertEqual(response.status, 403)

    def test_full_claim_lifecycle_over_http(self) -> None:
        opened = call(self.app, "POST", "/claims", {
            "claim_id": "clm-9", "pack_serial": "pack-9",
            "failure_occurred_at": "2026-09-28T03:00:00Z",
            "reported_by_owner_id": "owner-a", "fault_fingerprint": "fp-9",
            "evidence_items": [{
                "evidence_ref": "ev1", "kind": "log",
                "content_sha256": "a" * 64,
                "captured_at": "2026-09-28T03:00:00Z",
            }],
            "claimed_items": [{
                "slot_id": "module", "component_serial": "mod-7", "symptom": "容量骤降",
            }],
            "summary": "索赔",
        }, actor="agent")
        self.assertEqual(opened.status, 201)
        self.assertEqual(opened.body["frozen_config_id"], "cfg-1")

        history = call(self.app, "GET", "/configurations/pack-9", actor="aud")
        self.assertEqual(history.status, 200)
        self.assertEqual(len(history.body["configurations"]), 1)

        warranty = call(self.app, "GET",
                        "/components/mod-7/warranty?at=2026-09-28T03:00:00Z", actor="agent")
        self.assertEqual(warranty.status, 200)
        self.assertEqual(warranty.body["state"], "active")

        call(self.app, "POST", "/claims/clm-9/investigation",
             {"finding": "模组异常"}, actor="adj")
        allocation = call(self.app, "POST", "/claims/clm-9/allocation", {
            "basis": "全责",
            "liability_shares": [
                {"party_id": "fix-mod", "share": "1", "status": "confirmed",
                 "rationale": "模组容量衰减"},
            ],
        })
        self.assertEqual(allocation.status, 200)
        settlement = call(self.app, "POST", "/claims/clm-9/settlement", {
            "basis": "和解",
            "settlement_items": [{
                "component_serial": "mod-7", "party_id": "fix-mod",
                "amount_cny": "8000", "remedy": "repair",
            }],
        }, actor="adj")
        self.assertEqual(settlement.status, 200)
        notified = call(self.app, "POST", "/claims/clm-9/notify", {
            "decision_id": settlement.body["decision_id"],
            "notified_to": "owner-a", "channel": "email",
        }, actor="adj")
        self.assertEqual(notified.status, 200)
        self.assertTrue(notified.body["immutable"])

        explanation = call(self.app, "GET", "/claims/clm-9/explanation", actor="agent")
        self.assertEqual(explanation.status, 200)
        self.assertEqual(explanation.body["payout"]["fault_fingerprint"], "fp-9")

        audit = call(self.app, "GET", "/claims/clm-9/audit", actor="aud")
        self.assertEqual(audit.status, 200)
        self.assertGreaterEqual(len(audit.body["events"]), 5)

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")


if __name__ == "__main__":
    unittest.main()
