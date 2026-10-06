"""命令行冒烟：演示一次完整的标准意见协同流程。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from standards_collaboration import BallotChoice, CollaborationService

svc = CollaborationService()
svc.create_proposal("P-1", "智能网联汽车数据交换标准")
svc.add_clause("P-1", "CL-7", "车辆事件消息应包含观测时间")
for code, name in [("CN", "中国"), ("DE", "德国"), ("US", "美国")]:
    svc.register_delegation(code, name)
    svc.grant_authorization(code, f"rep-{code.lower()}")

# 多语言意见，互为替代；秘书处处置并保留来源
svc.submit_comment("C-1", "P-1", "CL-7", "CN", "zh", "车辆事件消息应包含观测时间与位置")
svc.submit_comment("C-2", "P-1", "CL-7", "DE", "en",
                   "车辆事件消息应包含观测时间、位置与置信度", supersedes_id="C-1")
svc.accept_comment("C-2", "采纳最完整的替代意见")

# 表决：按当时有效授权与锁定版本计票，重复投票幂等
svc.open_round("RD-1", "CL-7")
svc.cast_vote("RD-1", "CN", "rep-cn", BallotChoice.APPROVE)
svc.cast_vote("RD-1", "CN", "rep-cn", BallotChoice.APPROVE)  # 幂等重放
svc.cast_vote("RD-1", "DE", "rep-de", BallotChoice.APPROVE)
svc.cast_vote("RD-1", "US", "rep-us", BallotChoice.REJECT, reason="置信度定义待完善")
tally = svc.close_round("RD-1")

# 封存发布包并可完整重现（含异议与理由）
svc.seal_release("REL-1", "P-1")
reproduction = svc.reproduce_release("REL-1")

print(json.dumps({
    "计票": {"结果": tally["result"], "赞成": tally["approve"], "反对": tally["reject"]},
    "文本差异": svc.text_diff("CL-7", 1, 2)["unified_diff"],
    "发布包摘要": reproduction["digest"][:16] + "...",
    "异议数": len(reproduction["snapshot"]["objections"]),
}, ensure_ascii=False, indent=2))
