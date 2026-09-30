from __future__ import annotations

import unittest

from warranty_claims.contracts import (
    ClaimInput,
    ConfigurationInput,
    WarrantyTermsInput,
    parse_liability_shares,
)
from warranty_claims.errors import ValidationFailed


def terms_raw(**overrides) -> dict:
    raw = {
        "terms_id": "t1",
        "version": 1,
        "component_kind": "bms",
        "warrantor_id": "bms-co",
        "title": "BMS 保修",
        "coverage": ["容量异常"],
        "exclusions": ["私拆"],
        "start_basis": "calendar",
        "start_condition": "fitted",
        "end_basis": "calendar",
        "end_condition": "months:24",
        "liability_limit_cny": "10000.00",
        "notes": "",
    }
    raw.update(overrides)
    return raw


class ContractTests(unittest.TestCase):
    def test_terms_ok(self) -> None:
        terms = WarrantyTermsInput.from_dict(terms_raw())
        self.assertEqual(terms.terms_id, "t1")
        self.assertEqual(terms.coverage, ("容量异常",))

    def test_terms_version_must_be_positive_int(self) -> None:
        with self.assertRaises(ValidationFailed):
            WarrantyTermsInput.from_dict(terms_raw(version=0))
        with self.assertRaises(ValidationFailed):
            WarrantyTermsInput.from_dict(terms_raw(version="1"))

    def test_terms_unknown_basis(self) -> None:
        with self.assertRaises(ValidationFailed):
            WarrantyTermsInput.from_dict(terms_raw(end_basis="forever"))

    def test_terms_duplicate_coverage_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            WarrantyTermsInput.from_dict(terms_raw(coverage=["a", "a"]))

    def test_configuration_requires_slots(self) -> None:
        raw = {
            "config_id": "c1", "pack_serial": "p1", "event_type": "assembly",
            "event_at": "2026-06-01T00:00:00Z", "supplier_id": "s1",
            "work_order_ref": "WO-1", "slots": [],
        }
        with self.assertRaises(ValidationFailed):
            ConfigurationInput.from_dict(raw)

    def test_configuration_rejects_duplicate_slot_and_serial(self) -> None:
        base_slot = {
            "slot_id": "s1", "component_serial": "m1", "component_kind": "module",
            "terms_id": "t1", "terms_version": 1, "fitted_at": "2026-06-01T00:00:00Z",
        }
        raw = {
            "config_id": "c1", "pack_serial": "p1", "event_type": "repair",
            "event_at": "2026-06-01T00:00:00Z", "supplier_id": "s1",
            "work_order_ref": "WO-1",
            "slots": [base_slot, dict(base_slot, slot_id="s2")],
        }
        with self.assertRaisesRegex(ValidationFailed, "序列号重复"):
            ConfigurationInput.from_dict(raw)

    def test_configuration_rejects_naive_time(self) -> None:
        raw = {
            "config_id": "c1", "pack_serial": "p1", "event_type": "assembly",
            "event_at": "2026-06-01 00:00:00", "supplier_id": "s1",
            "work_order_ref": "WO-1",
            "slots": [{
                "slot_id": "s1", "component_serial": "m1", "component_kind": "module",
                "terms_id": "t1", "terms_version": 1, "fitted_at": "2026-06-01T00:00:00Z",
            }],
        }
        with self.assertRaises(ValidationFailed):
            ConfigurationInput.from_dict(raw)

    def test_claim_requires_64_hex_evidence(self) -> None:
        raw = {
            "claim_id": "clm1", "pack_serial": "p1",
            "failure_occurred_at": "2026-09-01T00:00:00Z",
            "reported_by_owner_id": "owner1", "fault_fingerprint": "fp1",
            "evidence_items": [{
                "evidence_ref": "e1", "kind": "log", "content_sha256": "abc",
                "captured_at": "2026-09-01T00:00:00Z",
            }],
            "claimed_items": [{
                "slot_id": "s1", "component_serial": "m1", "symptom": "容量降",
            }],
            "summary": "索赔",
        }
        with self.assertRaisesRegex(ValidationFailed, "64 位"):
            ClaimInput.from_dict(raw)

    def test_liability_shares_must_sum_to_one(self) -> None:
        with self.assertRaisesRegex(ValidationFailed, "等于 1"):
            parse_liability_shares([
                {"party_id": "a", "share": "0.6", "status": "confirmed"},
                {"party_id": "b", "share": "0.3", "status": "disputed"},
            ])

    def test_liability_shares_exact_decimal(self) -> None:
        shares = parse_liability_shares([
            {"party_id": "a", "share": "0.7"},
            {"party_id": "b", "share": "0.3"},
        ])
        self.assertEqual(len(shares), 2)
        self.assertEqual(shares[0].status, "proposed")


if __name__ == "__main__":
    unittest.main()
