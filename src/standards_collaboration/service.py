"""标准意见协同的核心服务：提案、条款版本、代表授权、意见处置、表决与发布。

设计要点：
- 逻辑时钟（seq）为所有状态变更定序，"当时有效"的判定均基于 seq 区间；
- 表决轮次开启时锁定条款版本，关闭时冻结计票快照——迟到修订与授权撤回
  都不会改变已经确认的表决结果；
- 投票以 (轮次, 代表团) 为幂等键：相同选择重复提交是幂等重放，
  不同选择视为改票（轮次关闭前），计票取每代表团最新一票；
- 每次文本变更通过处置决定记录来源意见，发布包封存后可完整重现。
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
from typing import Optional

from .contracts import BallotChoice
from .domain import (
    CLOSED_COMMENT_STATES,
    REOPENABLE_STATES,
    Authorization,
    AuthorizationError,
    BallotRound,
    Clause,
    ClauseVersionRecord,
    Comment,
    CommentStatus,
    Delegation,
    Disposition,
    DispositionAction,
    DomainError,
    Event,
    LockedError,
    NotFoundError,
    Proposal,
    ReleasePackage,
    ReleaseStatus,
    RoundStatus,
    StateError,
    TallyResult,
    ValidationError,
    Vote,
)


class CollaborationService:
    """标准意见协同后端的服务入口（方法即 API）。"""

    def __init__(self) -> None:
        self._seq = 0
        self.events: list[Event] = []
        self.proposals: dict[str, Proposal] = {}
        self.clauses: dict[str, Clause] = {}
        self.versions: dict[str, ClauseVersionRecord] = {}  # version_id -> 版本
        self.delegations: dict[str, Delegation] = {}
        self.authorizations: dict[str, Authorization] = {}
        self.comments: dict[str, Comment] = {}
        self.dispositions: dict[str, Disposition] = {}
        self.rounds: dict[str, BallotRound] = {}
        self.votes: list[Vote] = []
        self.releases: dict[str, ReleasePackage] = {}

    # ------------------------------------------------------------------
    # 基础设施：逻辑时钟、事件、校验
    # ------------------------------------------------------------------

    def _tick(self, kind: str, payload: dict) -> int:
        self._seq += 1
        self.events.append(Event(self._seq, kind, payload))
        return self._seq

    @staticmethod
    def _require_text(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field_name}不能为空")
        return value.strip()

    def _get_proposal(self, proposal_id: str) -> Proposal:
        try:
            return self.proposals[proposal_id]
        except KeyError:
            raise NotFoundError(f"提案不存在: {proposal_id}") from None

    def _get_clause(self, clause_id: str) -> Clause:
        try:
            return self.clauses[clause_id]
        except KeyError:
            raise NotFoundError(f"条款不存在: {clause_id}") from None

    def _get_comment(self, comment_id: str) -> Comment:
        try:
            return self.comments[comment_id]
        except KeyError:
            raise NotFoundError(f"意见不存在: {comment_id}") from None

    def _get_round(self, round_id: str) -> BallotRound:
        try:
            return self.rounds[round_id]
        except KeyError:
            raise NotFoundError(f"表决轮次不存在: {round_id}") from None

    def _get_release(self, release_id: str) -> ReleasePackage:
        try:
            return self.releases[release_id]
        except KeyError:
            raise NotFoundError(f"发布包不存在: {release_id}") from None

    def _version_of(self, clause_id: str, version: int) -> ClauseVersionRecord:
        version_id = f"{clause_id}@{version}"
        try:
            return self.versions[version_id]
        except KeyError:
            raise NotFoundError(f"条款版本不存在: {version_id}") from None

    def _authorization_at(
        self, delegation_code: str, seq: int
    ) -> Optional[Authorization]:
        """指定时点该代表团的有效授权（后授予的优先）。"""
        effective = [
            a
            for a in self.authorizations.values()
            if a.delegation_code == delegation_code and a.effective_at(seq)
        ]
        if not effective:
            return None
        return max(effective, key=lambda a: a.granted_seq)

    def _active_authorization(
        self, delegation_code: str, representative_id: str
    ) -> Authorization:
        """当前时点有效且属于该代表的授权，否则拒绝。"""
        for auth in self.authorizations.values():
            if (
                auth.delegation_code == delegation_code
                and auth.representative_id == representative_id
                and auth.effective_at(self._seq)
            ):
                return auth
        raise AuthorizationError(
            f"代表 {representative_id} 不持有代表团 {delegation_code} 的有效授权"
        )

    def _ensure_clause_unlocked(self, clause_id: str) -> None:
        """条款的当前版本若被未撤销的发布包封存，则禁止产生新版本。"""
        clause = self._get_clause(clause_id)
        for release in self.releases.values():
            if release.status is not ReleaseStatus.SEALED:
                continue
            for entry in release.snapshot["clauses"]:
                if (
                    entry["clause_id"] == clause_id
                    and entry["version"] == clause.current_version
                ):
                    raise LockedError(
                        f"条款 {clause_id} 的当前版本已被发布包 {release.release_id} "
                        "封存，请先撤销发布再产生新版本"
                    )

    # ------------------------------------------------------------------
    # 提案与条款
    # ------------------------------------------------------------------

    def create_proposal(self, proposal_id: str, title: str) -> dict:
        proposal_id = self._require_text(proposal_id, "提案编号")
        title = self._require_text(title, "提案标题")
        if proposal_id in self.proposals:
            raise StateError(f"提案已存在: {proposal_id}")
        seq = self._tick("proposal_created", {"proposal_id": proposal_id})
        self.proposals[proposal_id] = Proposal(proposal_id, title, seq)
        return {"proposal_id": proposal_id, "title": title, "created_seq": seq}

    def add_clause(self, proposal_id: str, clause_id: str, text: str) -> dict:
        """为提案新增条款并建立初始版本（v1）。"""
        self._get_proposal(proposal_id)
        clause_id = self._require_text(clause_id, "条款编号")
        text = self._require_text(text, "条款文本")
        if clause_id in self.clauses:
            raise StateError(f"条款已存在: {clause_id}")
        seq = self._tick(
            "clause_added", {"proposal_id": proposal_id, "clause_id": clause_id}
        )
        self.clauses[clause_id] = Clause(clause_id, proposal_id, current_version=1)
        record = ClauseVersionRecord(clause_id, 1, text, seq)
        self.versions[record.version_id] = record
        return self._version_view(record)

    # ------------------------------------------------------------------
    # 代表团与授权
    # ------------------------------------------------------------------

    def register_delegation(self, code: str, name: str) -> dict:
        code = self._require_text(code, "代表团代码")
        name = self._require_text(name, "代表团名称")
        if code in self.delegations:
            raise StateError(f"代表团已存在: {code}")
        seq = self._tick("delegation_registered", {"delegation_code": code})
        self.delegations[code] = Delegation(code, name, seq)
        return {"code": code, "name": name, "created_seq": seq}

    def grant_authorization(
        self, delegation_code: str, representative_id: str
    ) -> dict:
        """授予代表资格；同一代表重复授予是幂等的。"""
        if delegation_code not in self.delegations:
            raise NotFoundError(f"代表团不存在: {delegation_code}")
        representative_id = self._require_text(representative_id, "代表编号")
        for auth in self.authorizations.values():
            if (
                auth.delegation_code == delegation_code
                and auth.representative_id == representative_id
                and auth.revoked_seq is None
            ):
                return self._authorization_view(auth)  # 幂等：返回现有授权
        seq = self._tick(
            "authorization_granted",
            {
                "delegation_code": delegation_code,
                "representative_id": representative_id,
            },
        )
        auth = Authorization(
            f"AUTH-{seq}", delegation_code, representative_id, granted_seq=seq
        )
        self.authorizations[auth.authorization_id] = auth
        return self._authorization_view(auth)

    def revoke_authorization(
        self, delegation_code: str, representative_id: str
    ) -> dict:
        """撤回授权；只影响撤回时点之后的判定，历史轮次结果不变。"""
        representative_id = self._require_text(representative_id, "代表编号")
        for auth in self.authorizations.values():
            if (
                auth.delegation_code == delegation_code
                and auth.representative_id == representative_id
                and auth.revoked_seq is None
            ):
                seq = self._tick(
                    "authorization_revoked",
                    {
                        "delegation_code": delegation_code,
                        "representative_id": representative_id,
                    },
                )
                auth.revoked_seq = seq
                return self._authorization_view(auth)
        raise NotFoundError(
            f"代表 {representative_id} 在代表团 {delegation_code} 无有效授权"
        )

    # ------------------------------------------------------------------
    # 意见
    # ------------------------------------------------------------------

    def submit_comment(
        self,
        comment_id: str,
        proposal_id: str,
        clause_id: str,
        delegation_code: str,
        language: str,
        text: str,
        supersedes_id: Optional[str] = None,
        derived_from: tuple[str, ...] = (),
        _seq: Optional[int] = None,
    ) -> Comment:
        self._get_proposal(proposal_id)
        clause = self._get_clause(clause_id)
        if clause.proposal_id != proposal_id:
            raise ValidationError(f"条款 {clause_id} 不属于提案 {proposal_id}")
        if delegation_code not in self.delegations:
            raise NotFoundError(f"代表团不存在: {delegation_code}")
        comment_id = self._require_text(comment_id, "意见编号")
        language = self._require_text(language, "语言代码")
        text = self._require_text(text, "意见文本")
        if comment_id in self.comments:
            raise StateError(f"意见已存在: {comment_id}")
        target = None
        if supersedes_id is not None:
            # 先校验再落事件，避免意见已创建而替代未生效的部分失败
            target = self._get_comment(supersedes_id)
            if supersedes_id == comment_id:
                raise ValidationError("意见不能替代自身")
            if target.status is not CommentStatus.OPEN:
                raise StateError(
                    f"意见 {supersedes_id} 当前状态为 {target.status}，不能被替代"
                )
        seq = _seq if _seq is not None else self._tick(
            "comment_submitted", {"comment_id": comment_id}
        )
        comment = Comment(
            comment_id,
            proposal_id,
            clause_id,
            delegation_code,
            language,
            text,
            created_seq=seq,
            derived_from=tuple(derived_from),
            status_history=[(seq, CommentStatus.OPEN)],
        )
        self.comments[comment_id] = comment
        if target is not None:
            target.set_status(CommentStatus.SUPERSEDED, seq)
            comment.supersedes_id = supersedes_id
        return comment

    # ------------------------------------------------------------------
    # 处置决定：接受 / 拒绝 / 合并 / 拆分 / 替代 / 重新讨论
    # ------------------------------------------------------------------

    def _record_disposition(
        self,
        action: DispositionAction,
        input_ids: tuple[str, ...],
        reason: str,
        seq: int,
        output_ids: tuple[str, ...] = (),
    ) -> Disposition:
        disposition = Disposition(
            f"DISP-{seq}",
            action,
            input_ids,
            reason,
            created_seq=seq,
            output_comment_ids=output_ids,
        )
        self.dispositions[disposition.disposition_id] = disposition
        return disposition

    def _require_open_comments(self, comment_ids: list[str]) -> list[Comment]:
        comments = [self._get_comment(cid) for cid in comment_ids]
        for comment in comments:
            if comment.status is not CommentStatus.OPEN:
                raise StateError(
                    f"意见 {comment.comment_id} 当前状态为 {comment.status}，"
                    "不能处置；如需再议请先重新讨论"
                )
        return comments

    def accept_comment(
        self, comment_id: str, reason: str, new_text: Optional[str] = None
    ) -> dict:
        """接受意见：以意见文本（或指定文本）产生条款新版本，并记录来源。"""
        (comment,) = self._require_open_comments([comment_id])
        reason = self._require_text(reason, "处置理由")
        text = new_text if new_text is not None else comment.text
        text = self._require_text(text, "条款文本")
        self._ensure_clause_unlocked(comment.clause_id)
        clause = self._get_clause(comment.clause_id)
        seq = self._tick("comment_accepted", {"comment_id": comment_id})
        disposition = self._record_disposition(
            DispositionAction.ACCEPT, (comment_id,), reason, seq
        )
        comment.set_status(CommentStatus.ACCEPTED, seq)
        parent = clause.current_version
        record = ClauseVersionRecord(
            clause.clause_id,
            parent + 1,
            text,
            created_seq=seq,
            parent_version=parent,
            source_disposition_id=disposition.disposition_id,
            source_comment_ids=(comment_id,),
        )
        self.versions[record.version_id] = record
        clause.current_version = record.version
        disposition.resulting_clause_id = clause.clause_id
        disposition.resulting_version = record.version
        return {
            "disposition": self._disposition_view(disposition),
            "version": self._version_view(record),
        }

    def reject_comment(self, comment_id: str, reason: str) -> dict:
        """拒绝意见：必须给出理由，理由将进入意见矩阵与发布包异议。"""
        (comment,) = self._require_open_comments([comment_id])
        reason = self._require_text(reason, "拒绝理由")
        seq = self._tick("comment_rejected", {"comment_id": comment_id})
        disposition = self._record_disposition(
            DispositionAction.REJECT, (comment_id,), reason, seq
        )
        comment.set_status(CommentStatus.REJECTED, seq)
        return {"disposition": self._disposition_view(disposition)}

    def merge_comments(
        self,
        comment_ids: list[str],
        new_comment_id: str,
        merged_text: str,
        reason: str,
    ) -> dict:
        """合并多条意见为一条新意见；原意见标记为已合并。"""
        if len(comment_ids) < 2:
            raise ValidationError("合并至少需要两条意见")
        comments = self._require_open_comments(comment_ids)
        reason = self._require_text(reason, "合并理由")
        merged_text = self._require_text(merged_text, "合并后文本")
        clause_ids = {c.clause_id for c in comments}
        if len(clause_ids) != 1:
            raise ValidationError("只能合并同一条款下的意见")
        seq = self._tick(
            "comments_merged",
            {"comment_ids": list(comment_ids), "new_comment_id": new_comment_id},
        )
        disposition = self._record_disposition(
            DispositionAction.MERGE, tuple(comment_ids), reason, seq
        )
        first = comments[0]
        merged = self.submit_comment(
            new_comment_id,
            first.proposal_id,
            first.clause_id,
            first.delegation_code,
            first.language,
            merged_text,
            derived_from=tuple(comment_ids),
            _seq=seq,
        )
        for comment in comments:
            comment.set_status(CommentStatus.MERGED, seq)
        disposition.output_comment_ids = (merged.comment_id,)
        return {
            "disposition": self._disposition_view(disposition),
            "merged_comment": self._comment_view(merged),
        }

    def split_comment(
        self, comment_id: str, parts: list[tuple[str, str]], reason: str
    ) -> dict:
        """把一条意见拆分为多条；原意见标记为已拆分。"""
        if len(parts) < 2:
            raise ValidationError("拆分至少需要两个部分")
        (comment,) = self._require_open_comments([comment_id])
        reason = self._require_text(reason, "拆分理由")
        seq = self._tick(
            "comment_split",
            {"comment_id": comment_id, "parts": [p[0] for p in parts]},
        )
        disposition = self._record_disposition(
            DispositionAction.SPLIT, (comment_id,), reason, seq
        )
        new_comments = [
            self.submit_comment(
                part_id,
                comment.proposal_id,
                comment.clause_id,
                comment.delegation_code,
                comment.language,
                part_text,
                derived_from=(comment_id,),
                _seq=seq,
            )
            for part_id, part_text in parts
        ]
        comment.set_status(CommentStatus.SPLIT, seq)
        disposition.output_comment_ids = tuple(c.comment_id for c in new_comments)
        return {
            "disposition": self._disposition_view(disposition),
            "split_comments": [self._comment_view(c) for c in new_comments],
        }

    def substitute_comment(
        self, comment_id: str, new_comment_id: str, text: str, reason: str
    ) -> dict:
        """以新意见替代旧意见（处置形式的替代）。"""
        (comment,) = self._require_open_comments([comment_id])
        reason = self._require_text(reason, "替代理由")
        text = self._require_text(text, "替代文本")
        seq = self._tick(
            "comment_substituted",
            {"comment_id": comment_id, "new_comment_id": new_comment_id},
        )
        disposition = self._record_disposition(
            DispositionAction.SUBSTITUTE, (comment_id,), reason, seq
        )
        replacement = self.submit_comment(
            new_comment_id,
            comment.proposal_id,
            comment.clause_id,
            comment.delegation_code,
            comment.language,
            text,
            derived_from=(comment_id,),
            _seq=seq,
        )
        comment.set_status(CommentStatus.SUPERSEDED, seq)
        replacement.supersedes_id = comment_id
        disposition.output_comment_ids = (replacement.comment_id,)
        return {
            "disposition": self._disposition_view(disposition),
            "replacement_comment": self._comment_view(replacement),
        }

    def reopen_comment(self, comment_id: str, reason: str) -> dict:
        """重新讨论：被拒绝或被替代的意见可以重开为待处置状态。"""
        comment = self._get_comment(comment_id)
        reason = self._require_text(reason, "重开理由")
        if comment.status not in REOPENABLE_STATES:
            raise StateError(
                f"意见 {comment_id} 当前状态为 {comment.status}，不允许重新讨论"
            )
        seq = self._tick("comment_reopened", {"comment_id": comment_id})
        disposition = self._record_disposition(
            DispositionAction.REOPEN, (comment_id,), reason, seq
        )
        comment.set_status(CommentStatus.OPEN, seq)
        return {"disposition": self._disposition_view(disposition)}

    # ------------------------------------------------------------------
    # 表决
    # ------------------------------------------------------------------

    def open_round(self, round_id: str, clause_id: str) -> dict:
        """开启表决轮次；轮次绑定开启时的条款版本，迟到修订不影响本轮。"""
        clause = self._get_clause(clause_id)
        round_id = self._require_text(round_id, "轮次编号")
        if round_id in self.rounds:
            raise StateError(f"轮次已存在: {round_id}")
        seq = self._tick(
            "round_opened", {"round_id": round_id, "clause_id": clause_id}
        )
        ballot_round = BallotRound(
            round_id, clause_id, clause.current_version, opened_seq=seq
        )
        self.rounds[round_id] = ballot_round
        return self._round_view(ballot_round)

    def cast_vote(
        self,
        round_id: str,
        delegation_code: str,
        representative_id: str,
        choice: BallotChoice | str,
        reason: str = "",
    ) -> dict:
        """投票。幂等规则：

        - 同一代表团在同一轮次只有一票；
        - 相同代表重复提交相同选择是幂等重放，返回原票且不计新事件；
        - 关闭前提交不同选择（或更换代表后投票）视为改票，计票取最新一票。
        """
        ballot_round = self._get_round(round_id)
        if ballot_round.status is not RoundStatus.OPEN:
            raise StateError(f"轮次 {round_id} 已关闭，不能投票")
        if delegation_code not in self.delegations:
            raise NotFoundError(f"代表团不存在: {delegation_code}")
        choice = BallotChoice(choice)
        self._active_authorization(delegation_code, representative_id)
        existing = self._latest_vote(round_id, delegation_code)
        if (
            existing is not None
            and existing.representative_id == representative_id
            and existing.choice is choice
        ):
            return {"vote": self._vote_view(existing), "idempotent_replay": True}
        seq = self._tick(
            "vote_cast",
            {
                "round_id": round_id,
                "delegation_code": delegation_code,
                "choice": choice.value,
            },
        )
        vote = Vote(round_id, delegation_code, representative_id, choice, reason, seq)
        self.votes.append(vote)
        return {"vote": self._vote_view(vote), "idempotent_replay": False}

    def _latest_vote(self, round_id: str, delegation_code: str) -> Optional[Vote]:
        votes = [
            v
            for v in self.votes
            if v.round_id == round_id and v.delegation_code == delegation_code
        ]
        return max(votes, key=lambda v: v.cast_seq) if votes else None

    def close_round(self, round_id: str) -> dict:
        """关闭轮次并冻结计票结果。

        计票按关闭时点有效的代表授权：每代表团取最新一票，
        该票代表在关闭时点仍持有效授权才计入；平票记为 TIED（未通过）。
        """
        ballot_round = self._get_round(round_id)
        if ballot_round.status is RoundStatus.CLOSED:
            return copy.deepcopy(ballot_round.tally)  # 幂等：已关闭则返回冻结结果
        seq = self._tick("round_closed", {"round_id": round_id})
        ballot_round.status = RoundStatus.CLOSED
        ballot_round.closed_seq = seq
        ballot_round.tally = self._compute_tally(ballot_round, as_of=seq)
        return copy.deepcopy(ballot_round.tally)

    def _compute_tally(self, ballot_round: BallotRound, as_of: int) -> dict:
        delegations = sorted(
            {v.delegation_code for v in self.votes if v.round_id == ballot_round.round_id}
        )
        ballots = []
        approve = reject = abstain = 0
        for code in delegations:
            vote = self._latest_vote(ballot_round.round_id, code)
            assert vote is not None
            auth = self._authorization_at(code, as_of)
            counted = (
                auth is not None and auth.representative_id == vote.representative_id
            )
            note = ""
            if not counted:
                note = "计票时点无有效授权，选票不计入"
            elif any(
                v.cast_seq < vote.cast_seq
                for v in self.votes
                if v.round_id == ballot_round.round_id and v.delegation_code == code
            ):
                note = "改票后以最新一票为准"
            ballots.append(
                {
                    "delegation_code": code,
                    "representative_id": vote.representative_id,
                    "choice": vote.choice.value,
                    "reason": vote.reason,
                    "cast_seq": vote.cast_seq,
                    "counted": counted,
                    "note": note,
                }
            )
            if counted:
                if vote.choice is BallotChoice.APPROVE:
                    approve += 1
                elif vote.choice is BallotChoice.REJECT:
                    reject += 1
                else:
                    abstain += 1
        if approve > reject:
            result = TallyResult.APPROVED
        elif reject > approve:
            result = TallyResult.REJECTED
        else:
            result = TallyResult.TIED
        return {
            "round_id": ballot_round.round_id,
            "clause_id": ballot_round.clause_id,
            "clause_version": ballot_round.clause_version,
            "status": RoundStatus.CLOSED.value,
            "result": result.value,
            "approve": approve,
            "reject": reject,
            "abstain": abstain,
            "ballots": ballots,
            "opened_seq": ballot_round.opened_seq,
            "closed_seq": as_of,
        }

    # ------------------------------------------------------------------
    # 发布包
    # ------------------------------------------------------------------

    def seal_release(self, release_id: str, proposal_id: str) -> dict:
        """封存发布包：对提案当前状态做不可变快照，含异议与理由。"""
        proposal = self._get_proposal(proposal_id)
        release_id = self._require_text(release_id, "发布包编号")
        if release_id in self.releases:
            raise StateError(f"发布包已存在: {release_id}")
        for release in self.releases.values():
            if (
                release.proposal_id == proposal_id
                and release.status is ReleaseStatus.SEALED
            ):
                raise StateError(
                    f"提案 {proposal_id} 已存在未撤销的发布包 "
                    f"{release.release_id}，请先撤销再封存新包"
                )
        seq = self._tick(
            "release_sealed", {"release_id": release_id, "proposal_id": proposal_id}
        )
        snapshot = self._build_release_snapshot(proposal, as_of=seq)
        digest = self._snapshot_digest(snapshot)
        self.releases[release_id] = ReleasePackage(
            release_id, proposal_id, seq, snapshot, digest
        )
        return self._release_view(self.releases[release_id])

    def _build_release_snapshot(self, proposal: Proposal, as_of: int) -> dict:
        clause_ids = [
            cid
            for cid, clause in self.clauses.items()
            if clause.proposal_id == proposal.proposal_id
        ]
        clauses = []
        for clause_id in clause_ids:
            record = self._version_of(
                clause_id, self.clauses[clause_id].current_version
            )
            clauses.append(
                {
                    "clause_id": clause_id,
                    "version": record.version,
                    "text": record.text,
                    "provenance": {
                        "parent_version": record.parent_version,
                        "source_disposition_id": record.source_disposition_id,
                        "source_comment_ids": list(record.source_comment_ids),
                    },
                }
            )
        round_ids = [
            rid
            for rid, r in self.rounds.items()
            if r.clause_id in clause_ids and r.status is RoundStatus.CLOSED
        ]
        rounds = [copy.deepcopy(self.rounds[rid].tally) for rid in round_ids]
        matrix = self._comment_matrix_rows(proposal.proposal_id, as_of)
        objections = self._collect_objections(proposal.proposal_id, clause_ids, as_of)
        return {
            "proposal_id": proposal.proposal_id,
            "title": proposal.title,
            "sealed_seq": as_of,
            "clauses": clauses,
            "rounds": rounds,
            "comment_matrix": matrix,
            "objections": objections,
        }

    def _collect_objections(
        self, proposal_id: str, clause_ids: list[str], as_of: int
    ) -> list[dict]:
        """异议 = 截至封存时被拒绝的意见（含理由）+ 各轮次中的反对票（含理由）。"""
        objections = []
        for comment in self.comments.values():
            if comment.proposal_id != proposal_id:
                continue
            if comment.created_seq > as_of:
                continue
            if comment.status_at(as_of) is CommentStatus.REJECTED:
                disposition = self._disposition_for(comment.comment_id, as_of)
                objections.append(
                    {
                        "type": "rejected_comment",
                        "comment_id": comment.comment_id,
                        "clause_id": comment.clause_id,
                        "delegation_code": comment.delegation_code,
                        "language": comment.language,
                        "text": comment.text,
                        "reason": disposition.reason if disposition else "",
                    }
                )
        clause_set = set(clause_ids)
        for ballot_round in self.rounds.values():
            if ballot_round.clause_id not in clause_set or ballot_round.tally is None:
                continue
            for ballot in ballot_round.tally["ballots"]:
                if ballot["choice"] == BallotChoice.REJECT.value and ballot["counted"]:
                    objections.append(
                        {
                            "type": "reject_vote",
                            "round_id": ballot_round.round_id,
                            "clause_id": ballot_round.clause_id,
                            "clause_version": ballot_round.clause_version,
                            "delegation_code": ballot["delegation_code"],
                            "reason": ballot["reason"],
                        }
                    )
        return objections

    def _disposition_for(
        self, comment_id: str, as_of: int
    ) -> Optional[Disposition]:
        candidates = [
            d
            for d in self.dispositions.values()
            if comment_id in d.input_comment_ids and d.created_seq <= as_of
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.created_seq)

    @staticmethod
    def _snapshot_digest(snapshot: dict) -> str:
        canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def revoke_release(self, release_id: str, reason: str) -> dict:
        """撤销发布：解除条款锁定，允许产生新版本；快照本身保留可查。"""
        release = self._get_release(release_id)
        reason = self._require_text(reason, "撤销理由")
        if release.status is ReleaseStatus.REVOKED:
            raise StateError(f"发布包 {release_id} 已撤销")
        seq = self._tick("release_revoked", {"release_id": release_id})
        release.status = ReleaseStatus.REVOKED
        release.revoked_seq = seq
        release.revoke_reason = reason
        return self._release_view(release)

    def reproduce_release(self, release_id: str) -> dict:
        """完整重现发布包：返回封存快照并校验内容摘要，含异议与理由。"""
        release = self._get_release(release_id)
        snapshot = copy.deepcopy(release.snapshot)
        digest = self._snapshot_digest(snapshot)
        if digest != release.digest:
            raise StateError(f"发布包 {release_id} 内容摘要不一致，封存数据已损坏")
        return {
            "release_id": release.release_id,
            "proposal_id": release.proposal_id,
            "status": release.status.value,
            "sealed_seq": release.sealed_seq,
            "revoked_seq": release.revoked_seq,
            "revoke_reason": release.revoke_reason,
            "digest": release.digest,
            "digest_verified": True,
            "snapshot": snapshot,
        }

    # ------------------------------------------------------------------
    # 报告 API：意见矩阵 / 计票结果 / 文本差异
    # ------------------------------------------------------------------

    def comment_matrix(self, round_id: str) -> dict:
        """生成指定轮次的意见矩阵。

        已关闭的轮次按关闭时点重建意见状态（历史可重现）；
        进行中的轮次按当前状态生成。
        """
        ballot_round = self._get_round(round_id)
        as_of = (
            ballot_round.closed_seq
            if ballot_round.closed_seq is not None
            else self._seq
        )
        clause = self._get_clause(ballot_round.clause_id)
        return {
            "round_id": round_id,
            "proposal_id": clause.proposal_id,
            "as_of_seq": as_of,
            "rows": self._comment_matrix_rows(clause.proposal_id, as_of),
        }

    def _comment_matrix_rows(self, proposal_id: str, as_of: int) -> list[dict]:
        rows = []
        for comment in sorted(
            (c for c in self.comments.values() if c.proposal_id == proposal_id),
            key=lambda c: c.created_seq,
        ):
            if comment.created_seq > as_of:
                continue
            disposition = self._disposition_for(comment.comment_id, as_of)
            rows.append(
                {
                    "comment_id": comment.comment_id,
                    "clause_id": comment.clause_id,
                    "delegation_code": comment.delegation_code,
                    "language": comment.language,
                    "text": comment.text,
                    "status": comment.status_at(as_of).value,
                    "supersedes": comment.supersedes_id,
                    "derived_from": list(comment.derived_from),
                    "disposition": (
                        {
                            "disposition_id": disposition.disposition_id,
                            "action": disposition.action.value,
                            "reason": disposition.reason,
                        }
                        if disposition
                        else None
                    ),
                }
            )
        return rows

    def tally_report(self, round_id: str) -> dict:
        """指定轮次的计票结果；已关闭轮次返回冻结快照。"""
        ballot_round = self._get_round(round_id)
        if ballot_round.status is RoundStatus.CLOSED:
            return copy.deepcopy(ballot_round.tally)
        return self._compute_tally(ballot_round, as_of=self._seq) | {
            "status": RoundStatus.OPEN.value,
            "result": None,
        }

    def text_diff(self, clause_id: str, from_version: int, to_version: int) -> dict:
        """两个条款版本之间的文本差异（unified diff）及来源链。"""
        self._get_clause(clause_id)
        source = self._version_of(clause_id, from_version)
        target = self._version_of(clause_id, to_version)
        diff_lines = list(
            difflib.unified_diff(
                source.text.splitlines(),
                target.text.splitlines(),
                fromfile=source.version_id,
                tofile=target.version_id,
                lineterm="",
            )
        )
        return {
            "clause_id": clause_id,
            "from_version": from_version,
            "to_version": to_version,
            "changed": source.text != target.text,
            "unified_diff": "\n".join(diff_lines),
            "provenance": {
                "source_disposition_id": target.source_disposition_id,
                "source_comment_ids": list(target.source_comment_ids),
                "parent_version": target.parent_version,
            },
        }

    # ------------------------------------------------------------------
    # 视图序列化
    # ------------------------------------------------------------------

    @staticmethod
    def _authorization_view(auth: Authorization) -> dict:
        return {
            "authorization_id": auth.authorization_id,
            "delegation_code": auth.delegation_code,
            "representative_id": auth.representative_id,
            "granted_seq": auth.granted_seq,
            "revoked_seq": auth.revoked_seq,
        }

    def _version_view(self, record: ClauseVersionRecord) -> dict:
        return {
            "version_id": record.version_id,
            "clause_id": record.clause_id,
            "version": record.version,
            "text": record.text,
            "parent_version": record.parent_version,
            "source_disposition_id": record.source_disposition_id,
            "source_comment_ids": list(record.source_comment_ids),
            "created_seq": record.created_seq,
        }

    def _comment_view(self, comment: Comment) -> dict:
        return {
            "comment_id": comment.comment_id,
            "proposal_id": comment.proposal_id,
            "clause_id": comment.clause_id,
            "delegation_code": comment.delegation_code,
            "language": comment.language,
            "text": comment.text,
            "status": comment.status.value,
            "supersedes": comment.supersedes_id,
            "derived_from": list(comment.derived_from),
            "created_seq": comment.created_seq,
        }

    @staticmethod
    def _disposition_view(disposition: Disposition) -> dict:
        return {
            "disposition_id": disposition.disposition_id,
            "action": disposition.action.value,
            "input_comment_ids": list(disposition.input_comment_ids),
            "output_comment_ids": list(disposition.output_comment_ids),
            "reason": disposition.reason,
            "resulting_clause_id": disposition.resulting_clause_id,
            "resulting_version": disposition.resulting_version,
            "created_seq": disposition.created_seq,
        }

    @staticmethod
    def _vote_view(vote: Vote) -> dict:
        return {
            "round_id": vote.round_id,
            "delegation_code": vote.delegation_code,
            "representative_id": vote.representative_id,
            "choice": vote.choice.value,
            "reason": vote.reason,
            "cast_seq": vote.cast_seq,
        }

    def _round_view(self, ballot_round: BallotRound) -> dict:
        return {
            "round_id": ballot_round.round_id,
            "clause_id": ballot_round.clause_id,
            "clause_version": ballot_round.clause_version,
            "status": ballot_round.status.value,
            "opened_seq": ballot_round.opened_seq,
            "closed_seq": ballot_round.closed_seq,
        }

    @staticmethod
    def _release_view(release: ReleasePackage) -> dict:
        return {
            "release_id": release.release_id,
            "proposal_id": release.proposal_id,
            "status": release.status.value,
            "sealed_seq": release.sealed_seq,
            "revoked_seq": release.revoked_seq,
            "revoke_reason": release.revoke_reason,
            "digest": release.digest,
        }
