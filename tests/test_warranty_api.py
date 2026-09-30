from __future__ import annotations

import json
import sqlite3
import unittest

from warranty_claims.api import JsonApplication
from warranty_claims.service import WarrantyClaimService


HEADERS = {"x-actor-id": "agent"}


def post(path: str, payload: dict, headers: dict | None = None) -> tuple:
    body = json.dumps(payload).encode("utf-8")
    merged = dict(HEADERS)
    if headers:
        merged.update(headers)
    return APP.handle("POST", path, merged, body)


def get(path: str, headers: dict | None = None) -> tuple:
    return APP.handle("GET", path, headers or HEADERS)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        global APP
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        APP = JsonApplication(WarrantyClaimService(connection))
        post("/users", {"user_id": "agent", "display_name": "客服", "role": "service_agent"}, {})
        post("/users", {"user_id": "admin", "display_name": "管理员", "role": "warranty_admin"}, {})
        post("/users", {"user_id": "approver", "display_name": "审批", "role": "approver"}, {})
        post("/parties", {"party_id": "oem", "display_name": "原厂", "kind": "oem"},
             {"x-actor-id": "admin"})
        post("/parties", {"party_id": "customer", "display_name": "客户", "kind": "customer"},
             {"x-actor-id": "admin"})

    def test_health(self) -> None:
        response = get("/health", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = APP.handle("POST", "/claims", {"x-actor-id": "agent"}, b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_missing_actor(self) -> None:
        response = APP.handle(
            "POST", "/parties", {},
            json.dumps({"party_id": "x", "display_name": "x", "kind": "oem"}).encode(),
        )
        self.assertEqual(response.status, 422)

    def test_claim_lifecycle_routes(self) -> None:
        admin = {"x-actor-id": "admin"}
        approver = {"x-actor-id": "approver"}
        response = post("/packs", {
            "pack_id": "pack-a", "model_name": "整包", "owner_party_id": "customer",
            "delivered_at": "2026-06-01T00:00:00Z",
        }, admin)
        self.assertEqual(response.status, 201)
        self.assertEqual(
            post("/components", {"component_id": "c1", "kind": "enclosure", "serial": "S1"}, admin).status,
            201,
        )
        self.assertEqual(post("/component_terms", {
            "component_id": "c1", "change_type": "assembly", "warrantor_party_id": "oem",
            "coverage": ["结构"], "exclusions": ["撞击"],
            "start_condition": "交付", "end_condition": "60 个月",
            "start_at": "2026-06-01T00:00:00Z", "duration_months": 60,
        }, admin).status, 201)
        self.assertEqual(post("/packs/pack-a/slots", {
            "position": "enclosure", "component_id": "c1", "installed_at": "2026-06-01T00:00:00Z",
        }, admin).status, 201)
        response = post("/claims", {
            "claim_id": "claim-a", "pack_id": "pack-a", "customer_party_id": "customer",
            "fault_key": "fault-a", "symptom": "异响", "failure_at": "2026-09-01T00:00:00Z",
            "evidence": [{"label": "日志", "kind": "log", "content_sha256": "a" * 64}],
        })
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["warrantor_party_ids"], ["oem"])

        self.assertEqual(post("/claims/claim-a/rejection", {"reason": "属于排除条款"}, admin).status, 201)
        response = post("/claims/claim-a/rejection/notify", {"notification_ref": "N-1"}, approver)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "rejected")

        response = get("/claims/claim-a/explanation", admin)
        self.assertEqual(response.status, 200)
        self.assertIn("驳回", response.body["narrative"])
        self.assertEqual(response.body["warranty_continuation"][0]["component_id"], "c1")

    def test_unknown_route(self) -> None:
        response = APP.handle("POST", "/nope", {"x-actor-id": "agent"}, b"{}")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")


if __name__ == "__main__":
    unittest.main()
