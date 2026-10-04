"""内存仓储：表、逻辑时钟与锁。

逻辑时钟 seq 为所有事件定序：授权生效/撤回、轮次开关、投票、处置、封存。
计票与快照都以 seq 判定"当时有效"，从而保证迟到变更不改写历史。
"""

from __future__ import annotations

import threading

from .domain import (
    Ballot,
    Clause,
    ClauseVersion,
    Comment,
    Credential,
    Delegation,
    Disposition,
    Proposal,
    Release,
    VoteRound,
)


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.seq = 0
        self._id_counters: dict[str, int] = {}

        self.proposals: dict[str, Proposal] = {}
        self.clauses: dict[str, Clause] = {}
        self.versions: dict[str, ClauseVersion] = {}
        self.delegations: dict[str, Delegation] = {}
        self.credentials: dict[str, Credential] = {}
        self.comments: dict[str, Comment] = {}
        self.dispositions: dict[str, Disposition] = {}
        self.rounds: dict[str, VoteRound] = {}
        # 选票按 (轮次, 代表团) 唯一，天然幂等
        self.ballots: dict[tuple[str, str], Ballot] = {}
        self.releases: dict[str, Release] = {}

    def tick(self) -> int:
        """推进逻辑时钟并返回新时刻。"""
        self.seq += 1
        return self.seq

    def next_id(self, prefix: str) -> str:
        self._id_counters[prefix] = self._id_counters.get(prefix, 0) + 1
        return f"{prefix}-{self._id_counters[prefix]:04d}"
