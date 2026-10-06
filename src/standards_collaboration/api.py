"""标准意见协同后端的 JSON REST API（基于标准库 http.server 的薄适配层）。

所有业务规则都在 CollaborationService 中，本层只负责：
请求解析 -> 调用服务 -> 状态码与 JSON 响应。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .domain import (
    AuthorizationError,
    DomainError,
    LockedError,
    NotFoundError,
    StateError,
    ValidationError,
)
from .service import CollaborationService

_ERROR_STATUS = {
    NotFoundError: 404,
    ValidationError: 400,
    AuthorizationError: 403,
    StateError: 409,
    LockedError: 409,
}


def _error_status(exc: DomainError) -> int:
    for error_type, status in _ERROR_STATUS.items():
        if isinstance(exc, error_type):
            return status
    return 400


Handler = Callable[["ApiHandler", re.Match, dict], tuple[int, dict]]


class ApiHandler(BaseHTTPRequestHandler):
    """每个请求一个 handler 实例；服务实例挂在 server 上。"""

    server: "CollaborationApiServer"

    # 路由表：(方法, 路径模式, 处理函数名)
    ROUTES: list[tuple[str, str, str]] = [
        ("POST", r"^/proposals$", "_create_proposal"),
        ("POST", r"^/proposals/([^/]+)/clauses$", "_add_clause"),
        ("POST", r"^/delegations$", "_register_delegation"),
        ("POST", r"^/delegations/([^/]+)/authorizations$", "_grant_authorization"),
        ("POST", r"^/delegations/([^/]+)/revocations$", "_revoke_authorization"),
        ("POST", r"^/comments$", "_submit_comment"),
        ("POST", r"^/comments/([^/]+)/accept$", "_accept_comment"),
        ("POST", r"^/comments/([^/]+)/reject$", "_reject_comment"),
        ("POST", r"^/comments/([^/]+)/reopen$", "_reopen_comment"),
        ("POST", r"^/comments/merge$", "_merge_comments"),
        ("POST", r"^/comments/split$", "_split_comment"),
        ("POST", r"^/comments/substitute$", "_substitute_comment"),
        ("POST", r"^/rounds$", "_open_round"),
        ("POST", r"^/rounds/([^/]+)/votes$", "_cast_vote"),
        ("POST", r"^/rounds/([^/]+)/close$", "_close_round"),
        ("GET", r"^/rounds/([^/]+)/comment-matrix$", "_comment_matrix"),
        ("GET", r"^/rounds/([^/]+)/tally$", "_tally"),
        ("GET", r"^/clauses/([^/]+)/diff$", "_text_diff"),
        ("POST", r"^/releases$", "_seal_release"),
        ("POST", r"^/releases/([^/]+)/revoke$", "_revoke_release"),
        ("GET", r"^/releases/([^/]+)/reproduction$", "_reproduce_release"),
    ]

    # ------------------------------------------------------------- 基础工具

    @property
    def service(self) -> CollaborationService:
        return self.server.service

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        try:
            for route_method, pattern, handler_name in self.ROUTES:
                if route_method != method:
                    continue
                match = re.match(pattern, path)
                if match:
                    body = self._read_body() if method == "POST" else {}
                    handler: Handler = getattr(self, handler_name)
                    status, payload = handler(match, body)
                    self._send_json(status, payload)
                    return
            self._send_json(404, {"error": f"路由不存在: {method} {path}"})
        except DomainError as exc:
            self._send_json(_error_status(exc), {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802（http.server 约定）
        self._dispatch("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def log_message(self, format: str, *args: object) -> None:
        pass  # 测试环境保持安静

    # ------------------------------------------------------------- 提案与条款

    def _create_proposal(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 201, self.service.create_proposal(body["proposal_id"], body["title"])

    def _add_clause(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 201, self.service.add_clause(
            match.group(1), body["clause_id"], body["text"]
        )

    # ------------------------------------------------------------- 代表授权

    def _register_delegation(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 201, self.service.register_delegation(body["code"], body["name"])

    def _grant_authorization(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 201, self.service.grant_authorization(
            match.group(1), body["representative_id"]
        )

    def _revoke_authorization(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.revoke_authorization(
            match.group(1), body["representative_id"]
        )

    # ------------------------------------------------------------- 意见与处置

    def _submit_comment(self, match: re.Match, body: dict) -> tuple[int, dict]:
        comment = self.service.submit_comment(
            body["comment_id"],
            body["proposal_id"],
            body["clause_id"],
            body["delegation_code"],
            body["language"],
            body["text"],
            supersedes_id=body.get("supersedes_id"),
        )
        return 201, self.service._comment_view(comment)

    def _accept_comment(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.accept_comment(
            match.group(1), body["reason"], new_text=body.get("new_text")
        )

    def _reject_comment(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.reject_comment(match.group(1), body["reason"])

    def _reopen_comment(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.reopen_comment(match.group(1), body["reason"])

    def _merge_comments(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.merge_comments(
            body["comment_ids"], body["new_comment_id"], body["merged_text"],
            body["reason"],
        )

    def _split_comment(self, match: re.Match, body: dict) -> tuple[int, dict]:
        parts = [(part["comment_id"], part["text"]) for part in body["parts"]]
        return 200, self.service.split_comment(
            body["comment_id"], parts, body["reason"]
        )

    def _substitute_comment(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.substitute_comment(
            body["comment_id"], body["new_comment_id"], body["text"], body["reason"]
        )

    # ------------------------------------------------------------- 表决

    def _open_round(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 201, self.service.open_round(body["round_id"], body["clause_id"])

    def _cast_vote(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.cast_vote(
            match.group(1),
            body["delegation_code"],
            body["representative_id"],
            body["choice"],
            reason=body.get("reason", ""),
        )

    def _close_round(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.close_round(match.group(1))

    def _comment_matrix(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.comment_matrix(match.group(1))

    def _tally(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.tally_report(match.group(1))

    def _text_diff(self, match: re.Match, body: dict) -> tuple[int, dict]:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        return 200, self.service.text_diff(
            match.group(1), int(params["from"]), int(params["to"])
        )

    # ------------------------------------------------------------- 发布包

    def _seal_release(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 201, self.service.seal_release(body["release_id"], body["proposal_id"])

    def _revoke_release(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.revoke_release(match.group(1), body["reason"])

    def _reproduce_release(self, match: re.Match, body: dict) -> tuple[int, dict]:
        return 200, self.service.reproduce_release(match.group(1))


class CollaborationApiServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], service: Optional[CollaborationService] = None):
        super().__init__(address, ApiHandler)
        self.service = service or CollaborationService()


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    server = CollaborationApiServer((host, port))
    print(f"标准意见协同 API 监听于 http://{host}:{server.server_address[1]}")
    server.serve_forever()


if __name__ == "__main__":
    serve()
