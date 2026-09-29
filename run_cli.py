import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from standards_collaboration.contracts import BallotChoice, ClauseVersion, DelegationBallot


clause = ClauseVersion("CL-7", 3, "车辆事件消息应包含观测时间")
ballot = DelegationBallot("CN", "rep-2", clause, BallotChoice.APPROVE)
print(json.dumps({"clause": ballot.clause.clause_id, "version": ballot.clause.version, "choice": ballot.choice.value}, ensure_ascii=False))
