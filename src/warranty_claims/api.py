"""保修责任与索赔管理的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ServiceError, ValidationFailed
from .service import WarrantyClaimService
from .storage import connect


class ThreadedApplication:
    """线程服务器入口：每个工作线程持有独立 SQLite 连接与服务实例。"""

    def __init__(self, database: Path) -> None:
        self._database = database
        self._local = threading.local()

    def _application(self) -> "JsonApplication":
        application = getattr(self._local, "application", None)
        if application is None:
            application = JsonApplication(WarrantyClaimService(connect(self._database)))
            self._local.application = application
        return application

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> "Response":
        return self._application().handle(method, target, headers, body)


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: WarrantyClaimService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        actor = lambda: self._actor(normalized_headers)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)
            if method == "POST" and path == "/parties":
                return Response(201, self.service.register_party(
                    actor(), payload["party_id"], payload["display_name"], payload["kind"],
                    payload.get("contact", ""),
                ))
            if method == "POST" and path == "/packs":
                return Response(201, self.service.register_pack(
                    actor(), payload["pack_id"], payload["model_name"],
                    payload["owner_party_id"], payload["delivered_at"],
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "packs" and parts[2] == "ownership_transfers":
                return Response(201, self.service.record_ownership_transfer(
                    actor(), parts[1], payload["to_owner_party_id"], payload["transferred_at"],
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "packs" and parts[2] == "warranty":
                return Response(200, {
                    "pack_id": parts[1],
                    "components": self.service.warranty_continuation(parts[1]),
                })
            if method == "POST" and path == "/components":
                return Response(201, self.service.register_component(
                    actor(), payload["component_id"], payload["kind"], payload["serial"],
                ))
            if method == "POST" and path == "/component_terms":
                return Response(201, self.service.attach_term(
                    actor(),
                    payload["component_id"], payload["change_type"], payload["warrantor_party_id"],
                    payload["coverage"], payload["exclusions"],
                    payload["start_condition"], payload["end_condition"], payload["start_at"],
                    duration_months=payload.get("duration_months"),
                    end_at=payload.get("end_at"),
                    term_effect=payload.get("term_effect", ""),
                    supersedes_version=payload.get("supersedes_version"),
                    predecessor_component_id=payload.get("predecessor_component_id"),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "packs" and parts[2] == "slots":
                return Response(201, self.service.assemble_slot(
                    actor(), parts[1], payload["position"], payload["component_id"],
                    payload["installed_at"],
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "packs" and parts[2] == "replace":
                return Response(201, self.service.replace_component(
                    actor(), parts[1], payload["position"], payload["new_component_id"],
                    payload["installed_at"],
                ))

            if method == "POST" and path == "/claims":
                return Response(201, self.service.intake_claim(
                    actor(), payload["claim_id"], payload["pack_id"], payload["customer_party_id"],
                    payload["fault_key"], payload["symptom"], payload["failure_at"],
                    payload["evidence"],
                ))
            if len(parts) >= 2 and parts[0] == "claims":
                claim_id = parts[1]
                tail = parts[2:]
                if method == "GET" and tail == ["explanation"]:
                    return Response(200, self.service.explain_claim(actor(), claim_id))
                if method == "POST" and tail == ["evidence"]:
                    return Response(200, self.service.add_evidence(actor(), claim_id, payload["evidence"]))
                if method == "POST" and tail == ["late_findings"]:
                    return Response(201, self.service.register_late_finding(
                        actor(), claim_id, payload["label"], payload["content_sha256"], payload["summary"],
                    ))
                if method == "POST" and tail == ["investigations"]:
                    return Response(201, self.service.record_investigation(
                        actor(), claim_id, payload["findings"],
                    ))
                if method == "POST" and tail == ["supplement_requests"]:
                    return Response(201, self.service.request_supplement(
                        actor(), claim_id, payload["party_id"], payload["requirements"], payload["reason"],
                    ))
                if method == "POST" and tail == ["disputes"]:
                    return Response(201, self.service.open_dispute(
                        actor(), claim_id, payload["party_id"], payload["subject"], payload["detail"],
                    ))
                if method == "POST" and len(tail) == 3 and tail[0] == "disputes" and tail[2] == "resolve":
                    return Response(200, self.service.resolve_dispute(
                        actor(), claim_id, int(tail[1]), payload["resolution"],
                    ))
                if method == "POST" and tail == ["allocations"]:
                    return Response(201, self.service.propose_allocation(
                        actor(), claim_id, payload["lines"],
                    ))
                if method == "POST" and tail == ["allocations", "confirm"]:
                    return Response(200, self.service.confirm_allocation(
                        actor(), claim_id, payload["party_id"], payload["confirmation_ref"],
                    ))
                if method == "POST" and tail == ["allocations", "dispute"]:
                    return Response(200, self.service.dispute_allocation(
                        actor(), claim_id, payload["party_id"], payload["subject"], payload["detail"],
                    ))
                if method == "POST" and tail == ["settlement"]:
                    return Response(201, self.service.propose_settlement(
                        actor(), claim_id, payload["note"],
                    ))
                if method == "POST" and tail == ["settlement", "notify"]:
                    return Response(200, self.service.notify_settlement(
                        actor(), claim_id, payload["notification_ref"],
                    ))
                if method == "POST" and tail == ["rejection"]:
                    return Response(201, self.service.propose_rejection(
                        actor(), claim_id, payload["reason"],
                    ))
                if method == "POST" and tail == ["rejection", "notify"]:
                    return Response(200, self.service.notify_rejection(
                        actor(), claim_id, payload["notification_ref"],
                    ))
                if method == "POST" and tail == ["reopen"]:
                    return Response(201, self.service.reopen_claim(
                        actor(), claim_id, payload["reason"], payload.get("finding_id"),
                    ))
                if method == "POST" and tail == ["holds", "release"]:
                    return Response(200, self.service.release_hold(
                        actor(), claim_id, payload["party_id"], payload["note"],
                    ))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WarrantyClaims/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动保修责任与索赔管理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("warranty-claims.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = ThreadedApplication(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
