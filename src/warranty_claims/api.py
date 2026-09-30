"""保修责任与索赔服务的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import WarrantyClaimsService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: WarrantyClaimsService) -> None:
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
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)  # noqa: E731

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)
            if method == "POST" and path == "/warrantors":
                result = self.service.register_warrantor(
                    actor(), payload["party_id"], payload["display_name"], payload["party_type"],
                    payload.get("contact", ""),
                )
                return Response(201, result)
            if method == "POST" and path == "/components":
                result = self.service.register_component(
                    actor(), payload["component_serial"], payload["component_kind"],
                    payload.get("source_party_id"),
                )
                return Response(201, result)
            if method == "POST" and path == "/warranty_terms":
                return Response(201, self.service.publish_terms(actor(), payload))
            if method == "POST" and path == "/pack_ownership":
                result = self.service.record_ownership(
                    actor(), payload["pack_serial"], payload["owner_id"], payload["valid_from"]
                )
                return Response(201, result)
            if method == "POST" and path == "/configurations":
                return Response(201, self.service.record_configuration(actor(), payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "configurations":
                return Response(200, {"configurations": self.service.configuration_history(parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "components" and parts[2] == "warranty":
                at_time = query["at"][0] if "at" in query else None
                return Response(200, self.service.component_warranty_status(parts[1], at_time))
            if method == "POST" and path == "/claims":
                return Response(201, self.service.open_claim(actor(), payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "claims":
                return Response(200, self.service.get_claim(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "claims" and parts[2] == "explanation":
                return Response(200, self.service.claim_explanation(actor(), parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "claims" and parts[2] == "audit":
                return Response(200, {"events": self.service.audit_trail(actor(), parts[1])})

            subroutes = {
                "investigation": ("POST", self.service.record_investigation,
                                  lambda p: (actor(), parts[1], p["finding"])),
                "evidence": ("POST", self.service.supplement_evidence,
                             lambda p: (actor(), parts[1], p["evidence_items"], p["reason"])),
                "allocation": ("POST", self.service.allocate_liability,
                               lambda p: (actor(), parts[1], p["liability_shares"], p["basis"])),
                "settlement": ("POST", self.service.record_settlement,
                               lambda p: (actor(), parts[1], p["settlement_items"], p["basis"])),
                "rejection": ("POST", self.service.record_rejection,
                              lambda p: (actor(), parts[1], p["basis"])),
                "affirmation": ("POST", self.service.record_affirmation,
                                lambda p: (actor(), parts[1], p["basis"], p.get("revised_shares"))),
                "release_evidence": ("POST", self.service.release_evidence,
                                     lambda p: (actor(), parts[1], p["note"])),
                "continuations": ("POST", self.service.register_continuation,
                                  lambda p: (actor(), parts[1], p)),
            }
            if method == "POST" and len(parts) == 3 and parts[0] == "claims":
                if parts[2] == "notify":
                    result = self.service.notify_customer(
                        actor(), parts[1], int(payload["decision_id"]),
                        payload["notified_to"], payload.get("channel", "email"),
                    )
                    return Response(200, result)
                if parts[2] == "reopen":
                    result = self.service.reopen_claim(
                        actor(), parts[1], payload["reason"], payload.get("late_evidence")
                    )
                    return Response(200, result)
                route = subroutes.get(parts[2])
                if route is not None:
                    _, func, build_args = route
                    result = func(*build_args(payload))
                    return Response(200, result)
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
    parser = argparse.ArgumentParser(description="启动翻新储能电池保修责任与索赔管理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("warranty-claims.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(WarrantyClaimsService(connection))
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
