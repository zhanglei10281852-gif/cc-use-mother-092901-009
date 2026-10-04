"""HTTP API：标准意见协同后端。

运行：python run_api.py  （或 uvicorn 直接加载 standards_collaboration.api:app）
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .contracts import BallotChoice
from .domain import (
    Ballot,
    ClauseVersion,
    Comment,
    Credential,
    Delegation,
    Disposition,
    Proposal,
    Release,
    VoteRound,
)
from .services import (
    CollaborationService,
    DomainError,
    ballot_dict,
    comment_dict,
    disposition_dict,
    version_dict,
)

_STATUS_BY_CODE = {
    "not_found": 404,
    "conflict": 409,
    "forbidden": 403,
    "validation": 422,
}


# ------------------------------------------------------------ 请求模型


class ProposalIn(BaseModel):
    title: str
    proposal_id: str | None = None


class ClauseIn(BaseModel):
    clause_id: str
    title: str
    text: str


class DelegationIn(BaseModel):
    code: str
    name: str


class CredentialIn(BaseModel):
    representative_id: str
    role: str = "head"


class CommentIn(BaseModel):
    clause_id: str
    delegation_code: str
    language: str = "en"
    kind: str = "technical"
    text: str
    depends_on: list[str] = Field(default_factory=list)


class SupersedeIn(BaseModel):
    by_comment_id: str
    reason: str = ""
    decided_by: str = "secretariat"


class MergeIn(BaseModel):
    comment_ids: list[str]
    text: str
    reason: str = ""
    decided_by: str = "secretariat"
    language: str = "en"
    kind: str = "technical"


class SplitPartIn(BaseModel):
    text: str
    language: str | None = None
    kind: str | None = None


class SplitIn(BaseModel):
    parts: list[SplitPartIn]
    reason: str = ""
    decided_by: str = "secretariat"


class AcceptIn(BaseModel):
    applied_text: str
    reason: str = ""
    decided_by: str = "secretariat"


class RejectIn(BaseModel):
    reason: str
    decided_by: str = "secretariat"


class ReopenIn(BaseModel):
    reason: str
    decided_by: str = "secretariat"


class OpenRoundIn(BaseModel):
    version_id: str | None = None


class BallotIn(BaseModel):
    delegation_code: str
    representative_id: str
    choice: BallotChoice
    objection: str | None = None
    reason: str | None = None


class RevisionIn(BaseModel):
    text: str
    note: str = ""


class SealIn(BaseModel):
    note: str = ""


class RevokeIn(BaseModel):
    reason: str


# ------------------------------------------------------------ 序列化


def _proposal_dict(p: Proposal) -> dict:
    return {"id": p.id, "title": p.title, "status": p.status.value}


def _delegation_dict(d: Delegation) -> dict:
    return {"code": d.code, "name": d.name}


def _credential_dict(c: Credential) -> dict:
    return {
        "id": c.id,
        "delegation_code": c.delegation_code,
        "representative_id": c.representative_id,
        "role": c.role,
        "granted_seq": c.granted_seq,
        "withdrawn_seq": c.withdrawn_seq,
    }


def _round_dict(r: VoteRound) -> dict:
    return {
        "id": r.id,
        "clause_id": r.clause_id,
        "clause_version_id": r.clause_version_id,
        "round_no": r.round_no,
        "status": r.status.value,
        "opened_seq": r.opened_seq,
        "closed_seq": r.closed_seq,
        "eligible": r.eligible,
        "result": r.result.value if r.result else None,
    }


def _release_brief(r: Release) -> dict:
    return {
        "id": r.id,
        "proposal_id": r.proposal_id,
        "seq_no": r.seq_no,
        "status": r.status.value,
        "content_hash": r.content_hash,
        "previous_release_id": r.previous_release_id,
    }


# ------------------------------------------------------------ 应用工厂


def create_app(service: CollaborationService | None = None) -> FastAPI:
    svc = service or CollaborationService()
    app = FastAPI(title="标准意见协同后端", version="1.0.0")
    app.state.service = svc

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_BY_CODE.get(exc.code, 400),
            content={"error": exc.code, "message": exc.message},
        )

    # ---------------- 提案与条款

    @app.post("/proposals", status_code=201)
    def create_proposal(body: ProposalIn) -> dict:
        return _proposal_dict(svc.create_proposal(body.title, body.proposal_id))

    @app.get("/proposals/{proposal_id}")
    def get_proposal(proposal_id: str) -> dict:
        return _proposal_dict(svc._proposal(proposal_id))

    @app.post("/proposals/{proposal_id}/clauses", status_code=201)
    def add_clause(proposal_id: str, body: ClauseIn) -> dict:
        return version_dict(
            svc.add_clause(proposal_id, body.clause_id, body.title, body.text)
        )

    # ---------------- 代表团与授权

    @app.post("/delegations", status_code=201)
    def register_delegation(body: DelegationIn) -> dict:
        return _delegation_dict(svc.register_delegation(body.code, body.name))

    @app.post("/delegations/{code}/credentials", status_code=201)
    def grant_credential(code: str, body: CredentialIn) -> dict:
        return _credential_dict(
            svc.grant_credential(code, body.representative_id, body.role)
        )

    @app.post("/credentials/{credential_id}/withdraw")
    def withdraw_credential(credential_id: str) -> dict:
        return _credential_dict(svc.withdraw_credential(credential_id))

    @app.get("/credentials")
    def list_active_credentials() -> list[dict]:
        return [_credential_dict(c) for c in svc.active_credentials()]

    # ---------------- 意见

    @app.post("/proposals/{proposal_id}/comments", status_code=201)
    def submit_comment(proposal_id: str, body: CommentIn) -> dict:
        return comment_dict(
            svc.submit_comment(
                proposal_id,
                body.clause_id,
                body.delegation_code,
                body.language,
                body.kind,
                body.text,
                body.depends_on,
            )
        )

    @app.post("/comments/{comment_id}/supersede")
    def supersede_comment(comment_id: str, body: SupersedeIn) -> dict:
        return disposition_dict(
            svc.supersede_comment(
                comment_id, body.by_comment_id, body.reason, body.decided_by
            )
        )

    @app.post("/comments/merge", status_code=201)
    def merge_comments(body: MergeIn) -> dict:
        return comment_dict(
            svc.merge_comments(
                body.comment_ids,
                body.text,
                body.reason,
                body.decided_by,
                body.language,
                body.kind,
            )
        )

    @app.post("/comments/{comment_id}/split", status_code=201)
    def split_comment(comment_id: str, body: SplitIn) -> list[dict]:
        parts = [p.model_dump(exclude_none=True) for p in body.parts]
        return [
            comment_dict(c)
            for c in svc.split_comment(comment_id, parts, body.reason, body.decided_by)
        ]

    @app.post("/comments/{comment_id}/accept", status_code=201)
    def accept_comment(comment_id: str, body: AcceptIn) -> dict:
        disposition, version = svc.accept_comment(
            comment_id, body.applied_text, body.reason, body.decided_by
        )
        return {
            "disposition": disposition_dict(disposition),
            "clause_version": version_dict(version),
        }

    @app.post("/comments/{comment_id}/reject")
    def reject_comment(comment_id: str, body: RejectIn) -> dict:
        return disposition_dict(
            svc.reject_comment(comment_id, body.reason, body.decided_by)
        )

    @app.post("/comments/{comment_id}/reopen")
    def reopen_comment(comment_id: str, body: ReopenIn) -> dict:
        return disposition_dict(
            svc.reopen_comment(comment_id, body.reason, body.decided_by)
        )

    # ---------------- 表决

    @app.post("/clauses/{clause_id}/rounds", status_code=201)
    def open_round(clause_id: str, body: OpenRoundIn) -> dict:
        return _round_dict(svc.open_round(clause_id, body.version_id))

    @app.post("/rounds/{round_id}/ballots")
    def cast_ballot(round_id: str, body: BallotIn, response: Response) -> dict:
        ballot, replayed = svc.cast_ballot(
            round_id,
            body.delegation_code,
            body.representative_id,
            body.choice,
            body.objection,
            body.reason,
        )
        response.status_code = 200 if replayed else 201
        return {"ballot": ballot_dict(ballot), "idempotent_replay": replayed}

    @app.post("/rounds/{round_id}/close")
    def close_round(round_id: str) -> dict:
        return svc.close_round(round_id)

    @app.get("/rounds/{round_id}/tally")
    def get_tally(round_id: str) -> dict:
        return svc.tally(round_id)

    # ---------------- 修订与差异

    @app.post("/clauses/{clause_id}/revisions", status_code=201)
    def revise_clause(clause_id: str, body: RevisionIn) -> dict:
        return version_dict(svc.revise_clause(clause_id, body.text, body.note))

    @app.get("/clauses/{clause_id}/diff")
    def clause_diff(clause_id: str, from_version: int, to_version: int) -> dict:
        return svc.clause_diff(clause_id, from_version, to_version)

    @app.get("/clauses/{clause_id}/rounds/{round_no}/comment-matrix")
    def comment_matrix(clause_id: str, round_no: int) -> dict:
        return svc.comment_matrix(clause_id, round_no)

    # ---------------- 发布

    @app.post("/proposals/{proposal_id}/releases", status_code=201)
    def seal_release(proposal_id: str, body: SealIn) -> dict:
        return _release_brief(svc.seal_release(proposal_id, body.note))

    @app.post("/releases/{release_id}/revoke")
    def revoke_release(release_id: str, body: RevokeIn) -> dict:
        return _release_brief(svc.revoke_release(release_id, body.reason))

    @app.get("/releases/{release_id}")
    def get_release(release_id: str) -> dict:
        return svc.release_view(release_id)

    @app.get("/releases/{release_id}/objections")
    def get_release_objections(release_id: str) -> list[dict]:
        return svc.release_objections(release_id)

    return app


app = create_app()
