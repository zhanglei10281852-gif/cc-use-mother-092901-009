"""标准条款和代表表决的数据契约。"""

from dataclasses import dataclass
from enum import StrEnum


class BallotChoice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class ClauseVersion:
    clause_id: str
    version: int
    text: str

    def __post_init__(self) -> None:
        if self.version < 1 or not self.text.strip():
            raise ValueError("条款版本和文本必须有效")


@dataclass(frozen=True)
class DelegationBallot:
    delegation_code: str
    representative_id: str
    clause: ClauseVersion
    choice: BallotChoice
