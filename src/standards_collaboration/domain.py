"""标准意见协同的领域模型。

实体关系：
    Proposal 1─n Clause 1─n ClauseVersion（每次文本变更一条，携带 Provenance 来源）
    Delegation 1─n Credential（代表授权，按逻辑时刻生效/撤回）
    Clause 1─n Comment（意见，可依赖/合并/拆分/替代）
    Comment 1─n Disposition（处置决定：接受/拒绝/合并/拆分/替代/重新讨论）
    Clause 1─n VoteRound（每轮绑定一个 ClauseVersion，开启时快照有效授权）
    VoteRound 1─n Ballot（每个代表团每轮一票，幂等）
    Proposal 1─n Release（封存后不可变，可撤销，撤销后可再封存新版本）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .contracts import BallotChoice


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"


class CommentStatus(StrEnum):
    SUBMITTED = "submitted"      # 已提交，待处置
    ACCEPTED = "accepted"        # 已接受（文本已吸收）
    REJECTED = "rejected"        # 已拒绝
    MERGED = "merged"            # 已并入其他意见
    SPLIT = "split"              # 已拆分为多条意见
    SUPERSEDED = "superseded"    # 已被新意见替代
    REOPENED = "reopened"        # 重新讨论中，可再次处置


class DispositionAction(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    MERGE = "merge"
    SPLIT = "split"
    SUPERSEDE = "supersede"
    REOPEN = "reopen"


class RoundStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class RoundResult(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIED = "tied"  # 平票：赞成与反对相等，需重新讨论


class ReleaseStatus(StrEnum):
    SEALED = "sealed"
    REVOKED = "revoked"


class VersionSource(StrEnum):
    INITIAL = "initial"          # 条款初始文本
    DISPOSITION = "disposition"  # 意见处置（接受）产生
    REVISION = "revision"        # 直接修订（含表决关闭后的迟到修订）


@dataclass(frozen=True)
class Provenance:
    """文本来源：每个条款版本记录自己由谁产生、基于谁。"""

    source: VersionSource
    base_version_id: str | None
    comment_ids: tuple[str, ...] = ()
    disposition_ids: tuple[str, ...] = ()
    note: str = ""
    seq: int = 0


@dataclass
class Proposal:
    id: str
    title: str
    status: ProposalStatus = ProposalStatus.DRAFT
    created_seq: int = 0


@dataclass
class Clause:
    id: str
    proposal_id: str
    title: str
    needs_rediscussion: bool = False  # 平票或被否决后置位，等待重新讨论


@dataclass
class ClauseVersion:
    id: str
    clause_id: str
    version_no: int
    text: str
    provenance: Provenance
    created_seq: int = 0


@dataclass
class Delegation:
    code: str
    name: str


@dataclass
class Credential:
    """代表授权。withdrawn_seq 为 None 表示仍有效；撤回只影响之后的轮次快照。"""

    id: str
    delegation_code: str
    representative_id: str
    role: str = "head"
    granted_seq: int = 0
    withdrawn_seq: int | None = None

    def active_at(self, seq: int) -> bool:
        return self.granted_seq <= seq and (
            self.withdrawn_seq is None or self.withdrawn_seq > seq
        )


@dataclass
class Comment:
    id: str
    proposal_id: str
    clause_id: str
    base_version_id: str  # 针对哪个条款版本提出
    delegation_code: str
    language: str
    kind: str  # technical / editorial
    text: str
    depends_on: list[str] = field(default_factory=list)
    status: CommentStatus = CommentStatus.SUBMITTED
    merged_into: str | None = None
    split_into: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    created_seq: int = 0


@dataclass
class Disposition:
    id: str
    comment_id: str
    action: DispositionAction
    reason: str
    decided_by: str
    resulting_version_id: str | None = None  # accept 产生的新条款版本
    created_seq: int = 0


@dataclass
class VoteRound:
    """表决轮次。开启时把当时有效的代表团授权快照进 eligible，

    之后的授权撤回/代表变更不影响本轮计票。
    """

    id: str
    clause_id: str
    clause_version_id: str
    round_no: int  # 条款内递增
    status: RoundStatus = RoundStatus.OPEN
    opened_seq: int = 0
    closed_seq: int | None = None
    eligible: dict[str, str] = field(default_factory=dict)  # 代表团 -> 开启时有效代表
    result: RoundResult | None = None


@dataclass
class Ballot:
    round_id: str
    delegation_code: str
    representative_id: str
    choice: BallotChoice
    objection: str | None = None  # 反对票的异议内容
    reason: str | None = None     # 异议/弃权理由
    cast_seq: int = 0


@dataclass
class Release:
    """发布包：封存时对提案全部状态做不可变快照，可完整重现。"""

    id: str
    proposal_id: str
    seq_no: int
    status: ReleaseStatus
    payload: dict
    content_hash: str
    sealed_seq: int
    revoked_seq: int | None = None
    revoke_reason: str | None = None
    previous_release_id: str | None = None
