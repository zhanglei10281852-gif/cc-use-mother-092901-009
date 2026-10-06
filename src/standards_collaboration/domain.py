"""标准意见协同的领域模型：实体、枚举、事件与异常。

所有可变状态都绑定逻辑时钟序号（seq），从而支持"当时有效"的时态判定：
授权撤回只影响撤回之后的判定，已关闭轮次的计票结果永久冻结。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

from .contracts import BallotChoice


class CommentStatus(StrEnum):
    OPEN = "open"  # 待处置
    ACCEPTED = "accepted"  # 已接受（可能产生了新条款版本）
    REJECTED = "rejected"  # 已拒绝（附理由）
    SUPERSEDED = "superseded"  # 被另一条意见替代
    MERGED = "merged"  # 已并入合并意见
    SPLIT = "split"  # 已拆分为多条意见


class DispositionAction(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    MERGE = "merge"
    SPLIT = "split"
    SUBSTITUTE = "substitute"
    REOPEN = "reopen"  # 重新讨论


class RoundStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TallyResult(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIED = "tied"  # 平票：未获通过，需重新讨论或再次表决


class ReleaseStatus(StrEnum):
    SEALED = "sealed"
    REVOKED = "revoked"


# 已关闭（不可再处置）的意见状态；REOPEN 只能作用于其中一部分
CLOSED_COMMENT_STATES = frozenset(
    {
        CommentStatus.ACCEPTED,
        CommentStatus.REJECTED,
        CommentStatus.SUPERSEDED,
        CommentStatus.MERGED,
        CommentStatus.SPLIT,
    }
)
# 允许重新讨论的状态：被拒绝或被替代的意见可以重开；
# 已接受（文本已生效）、已合并/已拆分（派生意见已存在）的不允许回退。
REOPENABLE_STATES = frozenset({CommentStatus.REJECTED, CommentStatus.SUPERSEDED})


class DomainError(Exception):
    """领域规则违例的基类。"""


class NotFoundError(DomainError):
    """引用的实体不存在。"""


class ValidationError(DomainError):
    """输入参数不合法。"""


class StateError(DomainError):
    """实体当前状态不允许该操作。"""


class AuthorizationError(DomainError):
    """代表授权缺失或已撤回。"""


class LockedError(DomainError):
    """条款被已封存发布包锁定，禁止产生新版本。"""


@dataclass
class Event:
    """追加式审计日志条目：每次状态变更一条。"""

    seq: int
    kind: str
    payload: dict


@dataclass
class Proposal:
    proposal_id: str
    title: str
    created_seq: int


@dataclass
class Clause:
    clause_id: str
    proposal_id: str
    current_version: int = 0  # 当前版本号；版本实体存于 versions


@dataclass
class ClauseVersionRecord:
    """条款版本及其来源链：每一次文本变更都能回答"吸收了哪些建议"。"""

    clause_id: str
    version: int
    text: str
    created_seq: int
    parent_version: Optional[int] = None
    source_disposition_id: Optional[str] = None  # 触发本次变更的处置决定
    source_comment_ids: tuple[str, ...] = ()  # 该处置吸收的意见

    @property
    def version_id(self) -> str:
        return f"{self.clause_id}@{self.version}"


@dataclass
class Delegation:
    code: str
    name: str
    created_seq: int


@dataclass
class Authorization:
    """代表团对代表的授权；revoked_seq 为空表示仍然有效。"""

    authorization_id: str
    delegation_code: str
    representative_id: str
    granted_seq: int
    revoked_seq: Optional[int] = None

    def effective_at(self, seq: int) -> bool:
        """在指定时点是否有效：授予不晚于该时点，且撤回晚于该时点。"""
        return self.granted_seq <= seq and (
            self.revoked_seq is None or self.revoked_seq > seq
        )


@dataclass
class Comment:
    """一条意见；status_history 支持按任意时点重建状态。"""

    comment_id: str
    proposal_id: str
    clause_id: str
    delegation_code: str
    language: str
    text: str  # 建议的条款文本（接受时作为新版本的默认文本）
    created_seq: int
    supersedes_id: Optional[str] = None  # 提交时声明替代的意见
    derived_from: tuple[str, ...] = ()  # 由哪些意见派生（合并/拆分/替代的产物）
    status_history: list[tuple[int, CommentStatus]] = field(default_factory=list)

    @property
    def status(self) -> CommentStatus:
        return self.status_history[-1][1]

    def status_at(self, seq: int) -> CommentStatus:
        """指定时点的状态（用于按轮次重现意见矩阵）。"""
        current = self.status_history[0][1]
        for changed_seq, status in self.status_history:
            if changed_seq > seq:
                break
            current = status
        return current

    def set_status(self, status: CommentStatus, seq: int) -> None:
        self.status_history.append((seq, status))


@dataclass
class Disposition:
    """处置决定：连接意见与条款版本变更的核心枢纽。"""

    disposition_id: str
    action: DispositionAction
    input_comment_ids: tuple[str, ...]  # 被处置的意见
    reason: str
    created_seq: int
    output_comment_ids: tuple[str, ...] = ()  # 合并/拆分/替代产生的新意见
    resulting_clause_id: Optional[str] = None  # 接受时产生新版本的条款
    resulting_version: Optional[int] = None


@dataclass
class Vote:
    """一次投票记录；同一代表团在同一轮次的多次投票构成改票历史。"""

    round_id: str
    delegation_code: str
    representative_id: str
    choice: BallotChoice
    reason: str
    cast_seq: int


@dataclass
class BallotRound:
    """表决轮次；开启时锁定条款版本，迟到修订不影响本轮计票基础。"""

    round_id: str
    clause_id: str
    clause_version: int  # 开启时锁定的版本
    opened_seq: int
    status: RoundStatus = RoundStatus.OPEN
    closed_seq: Optional[int] = None
    tally: Optional[dict] = None  # 关闭时冻结的计票快照


@dataclass
class ReleasePackage:
    """发布包：封存后内容不可变，可连同异议与理由完整重现。"""

    release_id: str
    proposal_id: str
    sealed_seq: int
    snapshot: dict  # 条款版本、计票、意见矩阵、异议的完整快照
    digest: str  # 快照内容的 SHA-256，用于重现时校验完整性
    status: ReleaseStatus = ReleaseStatus.SEALED
    revoked_seq: Optional[int] = None
    revoke_reason: Optional[str] = None
