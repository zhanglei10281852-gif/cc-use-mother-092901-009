"""业务规则层：意见协同、表决计票与发布封存。

核心不变量：
1. 计票按轮次开启时刻的有效授权快照进行；授权撤回只影响之后开启的轮次。
2. 选票按 (轮次, 代表团) 唯一：相同内容重复提交幂等返回，不同内容冲突。
3. 每次文本变更都产生新的 ClauseVersion 并记录来源（意见/处置/修订）。
4. 发布包封存后不可变；撤销只影响后续流程，不改写已封存内容。
"""

from __future__ import annotations

import copy
import hashlib
import json

from .contracts import BallotChoice
from .diffing import change_blocks, unified_diff
from .domain import (
    Ballot,
    Clause,
    ClauseVersion,
    Comment,
    CommentStatus,
    Credential,
    Delegation,
    Disposition,
    DispositionAction,
    Proposal,
    ProposalStatus,
    Provenance,
    Release,
    ReleaseStatus,
    RoundResult,
    RoundStatus,
    VersionSource,
    VoteRound,
)
from .store import Store


class DomainError(Exception):
    """业务规则冲突。code 供 API 层映射 HTTP 状态。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _not_found(what: str) -> DomainError:
    return DomainError("not_found", what)


def _conflict(what: str) -> DomainError:
    return DomainError("conflict", what)


def _forbidden(what: str) -> DomainError:
    return DomainError("forbidden", what)


def _validation(what: str) -> DomainError:
    return DomainError("validation", what)


# ---------------------------------------------------------------- 序列化


def provenance_dict(p: Provenance) -> dict:
    return {
        "source": p.source.value,
        "base_version_id": p.base_version_id,
        "comment_ids": list(p.comment_ids),
        "disposition_ids": list(p.disposition_ids),
        "note": p.note,
        "seq": p.seq,
    }


def version_dict(v: ClauseVersion) -> dict:
    return {
        "id": v.id,
        "clause_id": v.clause_id,
        "version_no": v.version_no,
        "text": v.text,
        "provenance": provenance_dict(v.provenance),
        "created_seq": v.created_seq,
    }


def comment_dict(c: Comment) -> dict:
    return {
        "id": c.id,
        "proposal_id": c.proposal_id,
        "clause_id": c.clause_id,
        "base_version_id": c.base_version_id,
        "delegation_code": c.delegation_code,
        "language": c.language,
        "kind": c.kind,
        "text": c.text,
        "depends_on": list(c.depends_on),
        "status": c.status.value,
        "merged_into": c.merged_into,
        "split_into": list(c.split_into),
        "superseded_by": c.superseded_by,
        "created_seq": c.created_seq,
    }


def disposition_dict(d: Disposition) -> dict:
    return {
        "id": d.id,
        "comment_id": d.comment_id,
        "action": d.action.value,
        "reason": d.reason,
        "decided_by": d.decided_by,
        "resulting_version_id": d.resulting_version_id,
        "created_seq": d.created_seq,
    }


def ballot_dict(b: Ballot) -> dict:
    return {
        "round_id": b.round_id,
        "delegation_code": b.delegation_code,
        "representative_id": b.representative_id,
        "choice": b.choice.value,
        "objection": b.objection,
        "reason": b.reason,
        "cast_seq": b.cast_seq,
    }


# ---------------------------------------------------------------- 服务


class CollaborationService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ------------------------------------------------------------ 提案与条款

    def create_proposal(self, title: str, proposal_id: str | None = None) -> Proposal:
        with self.store.lock:
            pid = proposal_id or self.store.next_id("P")
            if pid in self.store.proposals:
                raise _conflict(f"提案已存在: {pid}")
            proposal = Proposal(id=pid, title=title, created_seq=self.store.tick())
            self.store.proposals[pid] = proposal
            return proposal

    def add_clause(self, proposal_id: str, clause_id: str, title: str, text: str) -> ClauseVersion:
        with self.store.lock:
            proposal = self._proposal(proposal_id)
            if clause_id in self.store.clauses:
                raise _conflict(f"条款已存在: {clause_id}")
            if not text.strip():
                raise _validation("条款文本不能为空")
            clause = Clause(id=clause_id, proposal_id=proposal.id, title=title)
            self.store.clauses[clause_id] = clause
            version = self._new_version(
                clause,
                text,
                Provenance(
                    source=VersionSource.INITIAL,
                    base_version_id=None,
                    note="初始文本",
                    seq=self.store.tick(),
                ),
            )
            return version

    # ------------------------------------------------------------ 代表资格

    def register_delegation(self, code: str, name: str) -> Delegation:
        with self.store.lock:
            if code in self.store.delegations:
                raise _conflict(f"代表团已存在: {code}")
            delegation = Delegation(code=code, name=name)
            self.store.delegations[code] = delegation
            return delegation

    def grant_credential(
        self, delegation_code: str, representative_id: str, role: str = "head"
    ) -> Credential:
        with self.store.lock:
            self._delegation(delegation_code)
            for cred in self.store.credentials.values():
                if (
                    cred.delegation_code == delegation_code
                    and cred.representative_id == representative_id
                    and cred.withdrawn_seq is None
                ):
                    raise _conflict(
                        f"代表 {representative_id} 已持有 {delegation_code} 的有效授权"
                    )
            cred = Credential(
                id=self.store.next_id("CR"),
                delegation_code=delegation_code,
                representative_id=representative_id,
                role=role,
                granted_seq=self.store.tick(),
            )
            self.store.credentials[cred.id] = cred
            return cred

    def withdraw_credential(self, credential_id: str) -> Credential:
        """撤回授权。撤回时刻之后开启的轮次不再计入该代表；已开启轮次不受影响。"""
        with self.store.lock:
            cred = self.store.credentials.get(credential_id)
            if cred is None:
                raise _not_found(f"授权不存在: {credential_id}")
            if cred.withdrawn_seq is not None:
                raise _conflict(f"授权已撤回: {credential_id}")
            cred.withdrawn_seq = self.store.tick()
            return cred

    def active_credentials(self, seq: int | None = None) -> list[Credential]:
        at = self.store.seq if seq is None else seq
        return [c for c in self.store.credentials.values() if c.active_at(at)]

    # ------------------------------------------------------------ 意见

    def submit_comment(
        self,
        proposal_id: str,
        clause_id: str,
        delegation_code: str,
        language: str,
        kind: str,
        text: str,
        depends_on: list[str] | None = None,
    ) -> Comment:
        with self.store.lock:
            proposal = self._proposal(proposal_id)
            clause = self._clause(clause_id)
            if clause.proposal_id != proposal.id:
                raise _validation(f"条款 {clause_id} 不属于提案 {proposal_id}")
            self._delegation(delegation_code)
            if not text.strip():
                raise _validation("意见文本不能为空")
            deps = list(depends_on or [])
            for dep_id in deps:
                dep = self._comment(dep_id)
                if dep.proposal_id != proposal_id:
                    raise _validation(f"依赖的意见 {dep_id} 不属于同一提案")
                if dep_id in deps[: deps.index(dep_id)]:
                    raise _validation("依赖列表重复")
            comment = Comment(
                id=self.store.next_id("C"),
                proposal_id=proposal_id,
                clause_id=clause_id,
                base_version_id=self._current_version(clause_id).id,
                delegation_code=delegation_code,
                language=language,
                kind=kind,
                text=text,
                depends_on=deps,
                created_seq=self.store.tick(),
            )
            self.store.comments[comment.id] = comment
            if proposal.status == ProposalStatus.DRAFT:
                proposal.status = ProposalStatus.IN_REVIEW
            return comment

    def supersede_comment(self, comment_id: str, by_comment_id: str, reason: str, decided_by: str) -> Disposition:
        """by_comment 替代 comment；被替代意见退出处置流程，依赖跟随替代链。"""
        with self.store.lock:
            target = self._comment(comment_id)
            source = self._comment(by_comment_id)
            if target.id == source.id:
                raise _validation("意见不能替代自身")
            if target.clause_id != source.clause_id:
                raise _validation("互为替代的意见必须针对同一条款")
            self._require_disposable(target)
            self._require_disposable(source)
            target.status = CommentStatus.SUPERSEDED
            target.superseded_by = source.id
            return self._record_disposition(
                target, DispositionAction.SUPERSEDE, reason or f"由 {source.id} 替代", decided_by
            )

    def merge_comments(
        self,
        comment_ids: list[str],
        text: str,
        reason: str,
        decided_by: str,
        language: str = "en",
        kind: str = "technical",
    ) -> Comment:
        """把多条意见合并为一条新意见；原意见状态置为 merged。"""
        with self.store.lock:
            if len(comment_ids) < 2:
                raise _validation("合并至少需要两条意见")
            sources = [self._comment(cid) for cid in comment_ids]
            if len({c.clause_id for c in sources}) != 1:
                raise _validation("只能合并同一条款下的意见")
            for c in sources:
                self._require_disposable(c)
            if not text.strip():
                raise _validation("合并后的文本不能为空")
            first = sources[0]
            merged = Comment(
                id=self.store.next_id("C"),
                proposal_id=first.proposal_id,
                clause_id=first.clause_id,
                base_version_id=self._current_version(first.clause_id).id,
                delegation_code=first.delegation_code,
                language=language,
                kind=kind,
                text=text,
                depends_on=sorted(
                    {d for c in sources for d in c.depends_on} - set(comment_ids)
                ),
                created_seq=self.store.tick(),
            )
            self.store.comments[merged.id] = merged
            for c in sources:
                c.status = CommentStatus.MERGED
                c.merged_into = merged.id
                self._record_disposition(
                    c, DispositionAction.MERGE, reason or f"并入 {merged.id}", decided_by
                )
            return merged

    def split_comment(
        self, comment_id: str, parts: list[dict], reason: str, decided_by: str
    ) -> list[Comment]:
        """把一条意见拆分为多条；原意见状态置为 split。"""
        with self.store.lock:
            original = self._comment(comment_id)
            self._require_disposable(original)
            if len(parts) < 2:
                raise _validation("拆分至少需要两个部分")
            created: list[Comment] = []
            for part in parts:
                text = (part.get("text") or "").strip()
                if not text:
                    raise _validation("拆分部分的文本不能为空")
                child = Comment(
                    id=self.store.next_id("C"),
                    proposal_id=original.proposal_id,
                    clause_id=original.clause_id,
                    base_version_id=original.base_version_id,
                    delegation_code=original.delegation_code,
                    language=part.get("language") or original.language,
                    kind=part.get("kind") or original.kind,
                    text=text,
                    depends_on=list(original.depends_on),
                    created_seq=self.store.tick(),
                )
                self.store.comments[child.id] = child
                created.append(child)
            original.status = CommentStatus.SPLIT
            original.split_into = [c.id for c in created]
            self._record_disposition(
                original,
                DispositionAction.SPLIT,
                reason or f"拆分为 {', '.join(original.split_into)}",
                decided_by,
            )
            return created

    # ------------------------------------------------------------ 处置

    def accept_comment(
        self, comment_id: str, applied_text: str, reason: str, decided_by: str
    ) -> tuple[Disposition, ClauseVersion]:
        """接受意见：按所给文本产生条款新版本，来源记录该意见与本次处置。

        依赖未全部接受（含沿替代/合并/拆分链解析后的终态意见）时拒绝处置。
        """
        with self.store.lock:
            comment = self._comment(comment_id)
            self._require_disposable(comment)
            unresolved = self._unresolved_dependencies(comment)
            if unresolved:
                raise _conflict(
                    "存在未接受的依赖意见: " + ", ".join(unresolved)
                )
            if not applied_text.strip():
                raise _validation("接受意见时必须给出吸收后的条款文本")
            clause = self._clause(comment.clause_id)
            if self._open_round_of(clause.id) is not None:
                raise _conflict(f"条款 {clause.id} 正在表决，不能变更文本")
            disposition = self._record_disposition(
                comment, DispositionAction.ACCEPT, reason, decided_by
            )
            version = self._new_version(
                clause,
                applied_text,
                Provenance(
                    source=VersionSource.DISPOSITION,
                    base_version_id=self._current_version(clause.id).id,
                    comment_ids=(comment.id,),
                    disposition_ids=(disposition.id,),
                    note=reason,
                    seq=self.store.tick(),
                ),
            )
            disposition.resulting_version_id = version.id
            comment.status = CommentStatus.ACCEPTED
            return disposition, version

    def reject_comment(self, comment_id: str, reason: str, decided_by: str) -> Disposition:
        with self.store.lock:
            comment = self._comment(comment_id)
            self._require_disposable(comment)
            if not reason.strip():
                raise _validation("拒绝意见必须说明理由")
            comment.status = CommentStatus.REJECTED
            return self._record_disposition(
                comment, DispositionAction.REJECT, reason, decided_by
            )

    def reopen_comment(self, comment_id: str, reason: str, decided_by: str) -> Disposition:
        """重新讨论：仅适用于已被拒绝的意见，使其回到可处置状态。"""
        with self.store.lock:
            comment = self._comment(comment_id)
            if comment.status != CommentStatus.REJECTED:
                raise _conflict(f"只有被拒绝的意见可以重新讨论: {comment_id}")
            comment.status = CommentStatus.REOPENED
            return self._record_disposition(
                comment, DispositionAction.REOPEN, reason, decided_by
            )

    # ------------------------------------------------------------ 表决

    def open_round(self, clause_id: str, version_id: str | None = None) -> VoteRound:
        """开启表决轮次：快照当时有效的代表团授权，之后撤回不影响本轮。"""
        with self.store.lock:
            clause = self._clause(clause_id)
            if self._open_round_of(clause_id) is not None:
                raise _conflict(f"条款 {clause_id} 已有进行中的表决轮次")
            version = (
                self._version(version_id)
                if version_id
                else self._current_version(clause_id)
            )
            if version.clause_id != clause_id:
                raise _validation(f"版本 {version_id} 不属于条款 {clause_id}")
            opened_seq = self.store.tick()
            eligible = self._eligible_snapshot(opened_seq)
            if not eligible:
                raise _conflict("当前没有任何有效授权，无法开启表决")
            round_no = 1 + sum(
                1 for r in self.store.rounds.values() if r.clause_id == clause_id
            )
            vote_round = VoteRound(
                id=self.store.next_id("VR"),
                clause_id=clause_id,
                clause_version_id=version.id,
                round_no=round_no,
                opened_seq=opened_seq,
                eligible=eligible,
            )
            self.store.rounds[vote_round.id] = vote_round
            proposal = self._proposal(clause.proposal_id)
            if proposal.status == ProposalStatus.DRAFT:
                proposal.status = ProposalStatus.IN_REVIEW
            return vote_round

    def cast_ballot(
        self,
        round_id: str,
        delegation_code: str,
        representative_id: str,
        choice: BallotChoice,
        objection: str | None = None,
        reason: str | None = None,
    ) -> tuple[Ballot, bool]:
        """投票。返回 (选票, 是否幂等重放)。

        幂等规则：同一代表团在同一轮次重复提交完全相同的选票，返回首次记录；
        内容不同则冲突。反对票必须附异议与理由。
        """
        with self.store.lock:
            vote_round = self._round(round_id)
            if vote_round.status != RoundStatus.OPEN:
                raise _conflict(f"轮次 {round_id} 已关闭，不再接受选票")
            if delegation_code not in vote_round.eligible:
                raise _forbidden(
                    f"代表团 {delegation_code} 不在本轮有效授权快照内"
                )
            if vote_round.eligible[delegation_code] != representative_id:
                raise _forbidden(
                    f"代表 {representative_id} 与轮次开启时 {delegation_code} 的授权不符"
                )
            if choice == BallotChoice.REJECT and not (objection and objection.strip()):
                raise _validation("反对票必须附异议内容")
            key = (round_id, delegation_code)
            existing = self.store.ballots.get(key)
            if existing is not None:
                same = (
                    existing.choice == choice
                    and existing.representative_id == representative_id
                    and (existing.objection or None) == (objection or None)
                    and (existing.reason or None) == (reason or None)
                )
                if same:
                    return existing, True
                raise _conflict(
                    f"代表团 {delegation_code} 已在本轮投过不同的选票"
                )
            ballot = Ballot(
                round_id=round_id,
                delegation_code=delegation_code,
                representative_id=representative_id,
                choice=choice,
                objection=objection,
                reason=reason,
                cast_seq=self.store.tick(),
            )
            self.store.ballots[key] = ballot
            return ballot, False

    def close_round(self, round_id: str) -> dict:
        """关闭轮次并计票。重复关闭幂等返回首次计票结果。"""
        with self.store.lock:
            vote_round = self._round(round_id)
            if vote_round.status == RoundStatus.CLOSED:
                return self.tally(round_id)
            vote_round.closed_seq = self.store.tick()
            vote_round.status = RoundStatus.CLOSED
            counts = self._counts(vote_round)
            if counts["approve"] > counts["reject"]:
                vote_round.result = RoundResult.APPROVED
            elif counts["reject"] > counts["approve"]:
                vote_round.result = RoundResult.REJECTED
            else:
                vote_round.result = RoundResult.TIED
            clause = self._clause(vote_round.clause_id)
            clause.needs_rediscussion = vote_round.result != RoundResult.APPROVED
            return self.tally(round_id)

    def tally(self, round_id: str) -> dict:
        with self.store.lock:
            vote_round = self._round(round_id)
            counts = self._counts(vote_round)
            cast = {
                delegation
                for (rid, delegation) in self.store.ballots
                if rid == round_id
            }
            return {
                "round_id": vote_round.id,
                "clause_id": vote_round.clause_id,
                "clause_version_id": vote_round.clause_version_id,
                "round_no": vote_round.round_no,
                "status": vote_round.status.value,
                "eligible": sorted(vote_round.eligible),
                "cast": len(cast),
                "missing": sorted(set(vote_round.eligible) - cast),
                **counts,
                "result": vote_round.result.value if vote_round.result else None,
            }

    # ------------------------------------------------------------ 修订

    def revise_clause(self, clause_id: str, text: str, note: str) -> ClauseVersion:
        """直接修订条款（含表决关闭后的迟到修订）。

        修订只产生新版本，不回写任何已关闭轮次的计票结果。
        """
        with self.store.lock:
            clause = self._clause(clause_id)
            if self._open_round_of(clause_id) is not None:
                raise _conflict(f"条款 {clause_id} 正在表决，不能修订文本")
            if not text.strip():
                raise _validation("修订文本不能为空")
            return self._new_version(
                clause,
                text,
                Provenance(
                    source=VersionSource.REVISION,
                    base_version_id=self._current_version(clause_id).id,
                    note=note,
                    seq=self.store.tick(),
                ),
            )

    # ------------------------------------------------------------ 发布

    def seal_release(self, proposal_id: str, note: str = "") -> Release:
        """封存发布包：对提案全部状态做不可变快照，含异议与理由。"""
        with self.store.lock:
            proposal = self._proposal(proposal_id)
            clauses = [
                c for c in self.store.clauses.values() if c.proposal_id == proposal_id
            ]
            if not clauses:
                raise _validation("提案尚无任何条款，不能封存")
            for clause in clauses:
                if self._open_round_of(clause.id) is not None:
                    raise _conflict(f"条款 {clause.id} 仍有进行中的表决，不能封存")
            proposal.status = ProposalStatus.PUBLISHED
            payload = self._build_payload(proposal, clauses, note)
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            previous = [
                r for r in self.store.releases.values() if r.proposal_id == proposal_id
            ]
            release = Release(
                id=self.store.next_id("REL"),
                proposal_id=proposal_id,
                seq_no=len(previous) + 1,
                status=ReleaseStatus.SEALED,
                payload=payload,
                content_hash=content_hash,
                sealed_seq=self.store.tick(),
                previous_release_id=previous[-1].id if previous else None,
            )
            self.store.releases[release.id] = release
            return release

    def revoke_release(self, release_id: str, reason: str) -> Release:
        """撤销发布包。已封存内容保持可重现；撤销后提案可继续演进并再次封存。"""
        with self.store.lock:
            release = self.store.releases.get(release_id)
            if release is None:
                raise _not_found(f"发布包不存在: {release_id}")
            if release.status != ReleaseStatus.SEALED:
                raise _conflict(f"发布包 {release_id} 已撤销")
            if not reason.strip():
                raise _validation("撤销发布包必须说明理由")
            release.status = ReleaseStatus.REVOKED
            release.revoked_seq = self.store.tick()
            release.revoke_reason = reason
            proposal = self._proposal(release.proposal_id)
            proposal.status = ProposalStatus.IN_REVIEW
            return release

    def release_view(self, release_id: str) -> dict:
        with self.store.lock:
            release = self.store.releases.get(release_id)
            if release is None:
                raise _not_found(f"发布包不存在: {release_id}")
            return {
                "id": release.id,
                "proposal_id": release.proposal_id,
                "seq_no": release.seq_no,
                "status": release.status.value,
                "content_hash": release.content_hash,
                "sealed_seq": release.sealed_seq,
                "revoked_seq": release.revoked_seq,
                "revoke_reason": release.revoke_reason,
                "previous_release_id": release.previous_release_id,
                "payload": copy.deepcopy(release.payload),
            }

    def release_objections(self, release_id: str) -> list[dict]:
        """从封存快照中提取全部异议与理由，保证发布包可完整重现。"""
        view = self.release_view(release_id)
        objections: list[dict] = []
        for round_info in view["payload"]["rounds"]:
            for ballot in round_info["ballots"]:
                if ballot["objection"]:
                    objections.append(
                        {
                            "round_id": round_info["id"],
                            "clause_id": round_info["clause_id"],
                            "delegation_code": ballot["delegation_code"],
                            "objection": ballot["objection"],
                            "reason": ballot["reason"],
                        }
                    )
        return objections

    # ------------------------------------------------------------ 查询

    def comment_matrix(self, clause_id: str, round_no: int) -> dict:
        """指定轮次的意见矩阵：意见×处置 × 代表团×选票 × 计票。"""
        with self.store.lock:
            clause = self._clause(clause_id)
            vote_round = self._round_by_no(clause_id, round_no)
            version = self._version(vote_round.clause_version_id)
            comments = [
                c
                for c in self.store.comments.values()
                if c.clause_id == clause_id
            ]
            comments.sort(key=lambda c: c.created_seq)
            latest_dispositions: dict[str, Disposition] = {}
            for d in sorted(
                self.store.dispositions.values(), key=lambda d: d.created_seq
            ):
                latest_dispositions[d.comment_id] = d
            rows = []
            for c in comments:
                row = comment_dict(c)
                disp = latest_dispositions.get(c.id)
                row["disposition"] = disposition_dict(disp) if disp else None
                rows.append(row)
            ballots: dict[str, dict | None] = {}
            for delegation in sorted(vote_round.eligible):
                ballot = self.store.ballots.get((vote_round.id, delegation))
                ballots[delegation] = ballot_dict(ballot) if ballot else None
            return {
                "proposal_id": clause.proposal_id,
                "clause_id": clause_id,
                "round_no": round_no,
                "clause_version": version_dict(version),
                "delegations": sorted(vote_round.eligible),
                "comments": rows,
                "ballots": ballots,
                "tally": self.tally(vote_round.id),
            }

    def clause_diff(self, clause_id: str, from_version: int, to_version: int) -> dict:
        """两个条款版本之间的文本差异，连同各自的来源链。"""
        with self.store.lock:
            self._clause(clause_id)
            older = self._version_by_no(clause_id, from_version)
            newer = self._version_by_no(clause_id, to_version)
            from_label = f"{clause_id}@v{from_version}"
            to_label = f"{clause_id}@v{to_version}"
            return {
                "clause_id": clause_id,
                "from": version_dict(older),
                "to": version_dict(newer),
                "unified_diff": unified_diff(
                    older.text, newer.text, from_label, to_label
                ),
                "changes": change_blocks(older.text, newer.text),
            }

    # ------------------------------------------------------------ 内部

    def _proposal(self, proposal_id: str) -> Proposal:
        proposal = self.store.proposals.get(proposal_id)
        if proposal is None:
            raise _not_found(f"提案不存在: {proposal_id}")
        return proposal

    def _clause(self, clause_id: str) -> Clause:
        clause = self.store.clauses.get(clause_id)
        if clause is None:
            raise _not_found(f"条款不存在: {clause_id}")
        return clause

    def _comment(self, comment_id: str) -> Comment:
        comment = self.store.comments.get(comment_id)
        if comment is None:
            raise _not_found(f"意见不存在: {comment_id}")
        return comment

    def _round(self, round_id: str) -> VoteRound:
        vote_round = self.store.rounds.get(round_id)
        if vote_round is None:
            raise _not_found(f"表决轮次不存在: {round_id}")
        return vote_round

    def _version(self, version_id: str) -> ClauseVersion:
        version = self.store.versions.get(version_id)
        if version is None:
            raise _not_found(f"条款版本不存在: {version_id}")
        return version

    def _delegation(self, code: str) -> Delegation:
        delegation = self.store.delegations.get(code)
        if delegation is None:
            raise _not_found(f"代表团不存在: {code}")
        return delegation

    def _current_version(self, clause_id: str) -> ClauseVersion:
        versions = [
            v for v in self.store.versions.values() if v.clause_id == clause_id
        ]
        if not versions:
            raise _not_found(f"条款 {clause_id} 没有任何版本")
        return max(versions, key=lambda v: v.version_no)

    def _version_by_no(self, clause_id: str, version_no: int) -> ClauseVersion:
        for v in self.store.versions.values():
            if v.clause_id == clause_id and v.version_no == version_no:
                return v
        raise _not_found(f"条款 {clause_id} 没有版本 v{version_no}")

    def _round_by_no(self, clause_id: str, round_no: int) -> VoteRound:
        for r in self.store.rounds.values():
            if r.clause_id == clause_id and r.round_no == round_no:
                return r
        raise _not_found(f"条款 {clause_id} 没有第 {round_no} 轮表决")

    def _open_round_of(self, clause_id: str) -> VoteRound | None:
        for r in self.store.rounds.values():
            if r.clause_id == clause_id and r.status == RoundStatus.OPEN:
                return r
        return None

    def _new_version(self, clause: Clause, text: str, provenance: Provenance) -> ClauseVersion:
        existing = [
            v for v in self.store.versions.values() if v.clause_id == clause.id
        ]
        version = ClauseVersion(
            id=self.store.next_id("CV"),
            clause_id=clause.id,
            version_no=len(existing) + 1,
            text=text,
            provenance=provenance,
            created_seq=self.store.tick(),
        )
        self.store.versions[version.id] = version
        return version

    def _record_disposition(
        self,
        comment: Comment,
        action: DispositionAction,
        reason: str,
        decided_by: str,
    ) -> Disposition:
        disposition = Disposition(
            id=self.store.next_id("D"),
            comment_id=comment.id,
            action=action,
            reason=reason,
            decided_by=decided_by,
            created_seq=self.store.tick(),
        )
        self.store.dispositions[disposition.id] = disposition
        return disposition

    def _require_disposable(self, comment: Comment) -> None:
        if comment.status not in (CommentStatus.SUBMITTED, CommentStatus.REOPENED):
            raise _conflict(
                f"意见 {comment.id} 当前状态为 {comment.status.value}，不能处置"
            )

    def _terminal_comments(self, comment: Comment) -> list[Comment]:
        """沿 替代/合并/拆分 链解析依赖的终态意见。"""
        seen: set[str] = set()
        stack = [comment]
        terminals: list[Comment] = []
        while stack:
            current = stack.pop()
            if current.id in seen:
                continue
            seen.add(current.id)
            if current.status == CommentStatus.SUPERSEDED and current.superseded_by:
                stack.append(self.store.comments[current.superseded_by])
            elif current.status == CommentStatus.MERGED and current.merged_into:
                stack.append(self.store.comments[current.merged_into])
            elif current.status == CommentStatus.SPLIT and current.split_into:
                stack.extend(self.store.comments[cid] for cid in current.split_into)
            else:
                terminals.append(current)
        return terminals

    def _unresolved_dependencies(self, comment: Comment) -> list[str]:
        unresolved: list[str] = []
        for dep_id in comment.depends_on:
            dep = self._comment(dep_id)
            for terminal in self._terminal_comments(dep):
                if terminal.status != CommentStatus.ACCEPTED:
                    unresolved.append(terminal.id)
        return sorted(set(unresolved))

    def _eligible_snapshot(self, seq: int) -> dict[str, str]:
        """seq 时刻各代表团的投票代表：优先 head，否则任一有效授权。"""
        snapshot: dict[str, str] = {}
        for cred in sorted(
            self.store.credentials.values(), key=lambda c: c.granted_seq
        ):
            if not cred.active_at(seq):
                continue
            current = snapshot.get(cred.delegation_code)
            if current is None or cred.role == "head":
                snapshot[cred.delegation_code] = cred.representative_id
        return snapshot

    def _counts(self, vote_round: VoteRound) -> dict:
        counts = {"approve": 0, "reject": 0, "abstain": 0}
        for (round_id, delegation), ballot in self.store.ballots.items():
            if round_id != vote_round.id or delegation not in vote_round.eligible:
                continue
            counts[ballot.choice.value] += 1
        return counts

    def _build_payload(self, proposal: Proposal, clauses: list[Clause], note: str) -> dict:
        clause_ids = {c.id for c in clauses}
        versions = [
            v for v in self.store.versions.values() if v.clause_id in clause_ids
        ]
        comments = [
            c for c in self.store.comments.values() if c.clause_id in clause_ids
        ]
        dispositions = [
            d
            for d in self.store.dispositions.values()
            if self.store.comments[d.comment_id].clause_id in clause_ids
        ]
        rounds = [
            r for r in self.store.rounds.values() if r.clause_id in clause_ids
        ]
        return {
            "note": note,
            "proposal": {
                "id": proposal.id,
                "title": proposal.title,
                "status": proposal.status.value,
            },
            "clauses": [
                {
                    "id": c.id,
                    "title": c.title,
                    "needs_rediscussion": c.needs_rediscussion,
                    "versions": [
                        version_dict(v)
                        for v in sorted(
                            (v for v in versions if v.clause_id == c.id),
                            key=lambda v: v.version_no,
                        )
                    ],
                }
                for c in sorted(clauses, key=lambda c: c.id)
            ],
            "comments": [
                comment_dict(c)
                for c in sorted(comments, key=lambda c: c.created_seq)
            ],
            "dispositions": [
                disposition_dict(d)
                for d in sorted(dispositions, key=lambda d: d.created_seq)
            ],
            "rounds": [
                {
                    "id": r.id,
                    "clause_id": r.clause_id,
                    "clause_version_id": r.clause_version_id,
                    "round_no": r.round_no,
                    "opened_seq": r.opened_seq,
                    "closed_seq": r.closed_seq,
                    "eligible": dict(sorted(r.eligible.items())),
                    "result": r.result.value if r.result else None,
                    "ballots": [
                        ballot_dict(self.store.ballots[(r.id, delegation)])
                        for delegation in sorted(r.eligible)
                        if (r.id, delegation) in self.store.ballots
                    ],
                    "tally": self.tally(r.id),
                }
                for r in sorted(rounds, key=lambda r: (r.clause_id, r.round_no))
            ],
        }
