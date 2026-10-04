"""领域服务测试：代表变更、平票处理、意见依赖、发布撤销与迟到修订。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from standards_collaboration.contracts import BallotChoice
from standards_collaboration.domain import (
    CommentStatus,
    ProposalStatus,
    ReleaseStatus,
    RoundResult,
    VersionSource,
)
from standards_collaboration.services import CollaborationService, DomainError


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = CollaborationService()
        self.svc.create_proposal("智能网联汽车数据记录标准", proposal_id="P-1")
        self.svc.add_clause("P-1", "CL-1", "事件时间戳", "车辆事件消息应包含观测时间")
        for code, name in [("CN", "中国"), ("DE", "德国"), ("JP", "日本"), ("US", "美国")]:
            self.svc.register_delegation(code, name)

    def grant(self, delegation: str, rep: str) -> str:
        return self.svc.grant_credential(delegation, rep).id

    def comment(self, delegation: str, text: str, depends_on=None, language="en"):
        return self.svc.submit_comment(
            "P-1", "CL-1", delegation, language, "technical", text, depends_on or []
        )


class DelegationChangeTests(ServiceTestBase):
    def test_delegate_change_affects_only_later_rounds(self):
        """授权撤回只影响后续轮次：已开启轮次按开启时快照计票。"""
        self.grant("CN", "rep-cn")
        cred_de1 = self.grant("DE", "rep-de-1")
        self.grant("JP", "rep-jp")

        round1 = self.svc.open_round("CL-1")
        self.assertEqual(round1.eligible["DE"], "rep-de-1")

        # DE 原代表在第 1 轮投票，随后授权被撤回、改派新代表
        self.svc.cast_ballot(round1.id, "DE", "rep-de-1", BallotChoice.APPROVE)
        self.svc.close_round(round1.id)
        self.svc.withdraw_credential(cred_de1)
        self.grant("DE", "rep-de-2")

        # 第 1 轮计票不受撤回影响
        tally1 = self.svc.tally(round1.id)
        self.assertEqual(tally1["approve"], 1)
        self.assertEqual(tally1["result"], RoundResult.APPROVED.value)
        self.assertEqual(tally1["eligible"], ["CN", "DE", "JP"])

        # 第 2 轮快照为新代表；旧代表投票被拒绝
        round2 = self.svc.open_round("CL-1")
        self.assertEqual(round2.eligible["DE"], "rep-de-2")
        with self.assertRaises(DomainError) as ctx:
            self.svc.cast_ballot(round2.id, "DE", "rep-de-1", BallotChoice.APPROVE)
        self.assertEqual(ctx.exception.code, "forbidden")
        ballot, replayed = self.svc.cast_ballot(
            round2.id, "DE", "rep-de-2", BallotChoice.APPROVE
        )
        self.assertFalse(replayed)
        self.assertEqual(ballot.representative_id, "rep-de-2")

    def test_delegation_without_credential_cannot_vote(self):
        self.grant("CN", "rep-cn")
        self.svc.register_delegation("FR", "法国")
        vote_round = self.svc.open_round("CL-1")
        with self.assertRaises(DomainError) as ctx:
            self.svc.cast_ballot(vote_round.id, "FR", "rep-fr", BallotChoice.APPROVE)
        self.assertEqual(ctx.exception.code, "forbidden")


class BallotIdempotencyTests(ServiceTestBase):
    def test_duplicate_ballot_is_idempotent(self):
        """重复选票幂等：相同内容返回首次记录，不同内容冲突。"""
        self.grant("CN", "rep-cn")
        self.grant("DE", "rep-de")
        vote_round = self.svc.open_round("CL-1")

        first, replayed = self.svc.cast_ballot(
            vote_round.id, "CN", "rep-cn", BallotChoice.APPROVE
        )
        self.assertFalse(replayed)
        again, replayed = self.svc.cast_ballot(
            vote_round.id, "CN", "rep-cn", BallotChoice.APPROVE
        )
        self.assertTrue(replayed)
        self.assertEqual(again.cast_seq, first.cast_seq)

        with self.assertRaises(DomainError) as ctx:
            self.svc.cast_ballot(
                vote_round.id,
                "CN",
                "rep-cn",
                BallotChoice.REJECT,
                objection="不同内容",
                reason="改票",
            )
        self.assertEqual(ctx.exception.code, "conflict")

        self.svc.close_round(vote_round.id)
        tally = self.svc.tally(vote_round.id)
        self.assertEqual(tally["approve"], 1)  # 重复投票未被双计
        with self.assertRaises(DomainError):
            self.svc.cast_ballot(vote_round.id, "DE", "rep-de", BallotChoice.APPROVE)

    def test_reject_ballot_requires_objection(self):
        self.grant("CN", "rep-cn")
        vote_round = self.svc.open_round("CL-1")
        with self.assertRaises(DomainError) as ctx:
            self.svc.cast_ballot(vote_round.id, "CN", "rep-cn", BallotChoice.REJECT)
        self.assertEqual(ctx.exception.code, "validation")


class TieHandlingTests(ServiceTestBase):
    def test_tie_triggers_rediscussion_then_new_round(self):
        """平票 → 条款进入重新讨论；重新处置后开新轮可通过。"""
        for delegation, rep in [("CN", "r1"), ("DE", "r2"), ("JP", "r3"), ("US", "r4")]:
            self.grant(delegation, rep)
        comment = self.comment("CN", "时间戳应精确到毫秒")
        self.svc.reject_comment(comment.id, "第 1 轮讨论未达成共识", "secretariat")

        round1 = self.svc.open_round("CL-1")
        self.svc.cast_ballot(round1.id, "CN", "r1", BallotChoice.APPROVE)
        self.svc.cast_ballot(round1.id, "DE", "r2", BallotChoice.APPROVE)
        self.svc.cast_ballot(
            round1.id, "JP", "r3", BallotChoice.REJECT,
            objection="毫秒精度超出车载时钟能力", reason="技术不可行",
        )
        self.svc.cast_ballot(
            round1.id, "US", "r4", BallotChoice.REJECT,
            objection="成本过高", reason="经济理由",
        )
        tally = self.svc.close_round(round1.id)
        self.assertEqual(tally["result"], RoundResult.TIED.value)
        self.assertEqual(tally["approve"], 2)
        self.assertEqual(tally["reject"], 2)
        self.assertTrue(self.svc.store.clauses["CL-1"].needs_rediscussion)

        # 重新讨论：重开被拒意见并接受，产生新条款版本
        self.svc.reopen_comment(comment.id, "平票，重新讨论", "chair")
        _, version = self.svc.accept_comment(
            comment.id, "车辆事件消息应包含观测时间（精度 10 毫秒）", "折中方案", "chair"
        )
        self.assertEqual(version.version_no, 2)

        round2 = self.svc.open_round("CL-1")
        for delegation, rep in [("CN", "r1"), ("DE", "r2"), ("JP", "r3"), ("US", "r4")]:
            self.svc.cast_ballot(round2.id, delegation, rep, BallotChoice.APPROVE)
        tally2 = self.svc.close_round(round2.id)
        self.assertEqual(tally2["result"], RoundResult.APPROVED.value)
        self.assertFalse(self.svc.store.clauses["CL-1"].needs_rediscussion)

    def test_abstention_does_not_break_tie_rule(self):
        """弃权不计入多数：2 赞成 1 反对 1 弃权 → 通过。"""
        for delegation, rep in [("CN", "r1"), ("DE", "r2"), ("JP", "r3"), ("US", "r4")]:
            self.grant(delegation, rep)
        vote_round = self.svc.open_round("CL-1")
        self.svc.cast_ballot(vote_round.id, "CN", "r1", BallotChoice.APPROVE)
        self.svc.cast_ballot(vote_round.id, "DE", "r2", BallotChoice.APPROVE)
        self.svc.cast_ballot(
            vote_round.id, "JP", "r3", BallotChoice.REJECT,
            objection="表述不清", reason="编辑性",
        )
        self.svc.cast_ballot(vote_round.id, "US", "r4", BallotChoice.ABSTAIN)
        tally = self.svc.close_round(vote_round.id)
        self.assertEqual(tally["result"], RoundResult.APPROVED.value)
        self.assertEqual(tally["abstain"], 1)


class CommentDependencyTests(ServiceTestBase):
    def test_accept_requires_dependencies_accepted(self):
        base = self.comment("CN", "应使用 UTC 时间")
        dependent = self.comment("DE", "时间戳应带时区偏移", depends_on=[base.id])

        with self.assertRaises(DomainError) as ctx:
            self.svc.accept_comment(dependent.id, "文本", "理由", "chair")
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertIn(base.id, ctx.exception.message)

        _, v2 = self.svc.accept_comment(base.id, "采用 UTC 时间", "接受", "chair")
        _, v3 = self.svc.accept_comment(dependent.id, "采用 UTC 并标注偏移", "接受", "chair")
        # 每个版本都能回溯到产生它的意见
        self.assertEqual(v2.provenance.comment_ids, (base.id,))
        self.assertEqual(v3.provenance.comment_ids, (dependent.id,))
        self.assertEqual(v3.provenance.base_version_id, v2.id)

    def test_rejected_dependency_blocks_acceptance(self):
        base = self.comment("CN", "应使用 UTC 时间")
        dependent = self.comment("DE", "时间戳应带时区偏移", depends_on=[base.id])
        self.svc.reject_comment(base.id, "与现有条款冲突", "chair")
        with self.assertRaises(DomainError) as ctx:
            self.svc.accept_comment(dependent.id, "文本", "理由", "chair")
        self.assertEqual(ctx.exception.code, "conflict")

    def test_supersede_chain_resolves_dependency(self):
        """A 依赖 B，C 替代 B：接受 C 后 A 的依赖即满足。"""
        old = self.comment("CN", "时间戳精度 1 秒")
        newer = self.comment("CN", "时间戳精度 100 毫秒")
        dependent = self.comment("DE", "应记录时间源", depends_on=[old.id])

        self.svc.supersede_comment(old.id, newer.id, "新版替代旧版", "chair")
        self.assertEqual(
            self.svc.store.comments[old.id].status, CommentStatus.SUPERSEDED
        )
        with self.assertRaises(DomainError):
            self.svc.accept_comment(old.id, "文本", "理由", "chair")

        self.svc.accept_comment(newer.id, "精度 100 毫秒", "接受", "chair")
        _, version = self.svc.accept_comment(dependent.id, "记录时间源", "接受", "chair")
        self.assertEqual(version.provenance.comment_ids, (dependent.id,))

    def test_merge_and_split(self):
        m1 = self.comment("CN", "增加事件严重度字段")
        m2 = self.comment("JP", "增加事件优先级字段")
        merged = self.svc.merge_comments(
            [m1.id, m2.id], "增加事件严重度/优先级字段", "内容重叠", "chair"
        )
        self.assertEqual(self.svc.store.comments[m1.id].status, CommentStatus.MERGED)
        self.assertEqual(
            self.svc.store.comments[m1.id].merged_into, merged.id
        )
        with self.assertRaises(DomainError):
            self.svc.accept_comment(m1.id, "文本", "理由", "chair")

        # 依赖被合并意见的，解析到合并产物
        follower = self.comment("DE", "严重度取值 0-3", depends_on=[m1.id])
        self.svc.accept_comment(merged.id, "增加严重度字段", "接受", "chair")
        self.svc.accept_comment(follower.id, "严重度取值 0-3", "接受", "chair")

        # 拆分：依赖原意见 ⇒ 两个拆分部分都接受后才可处置
        big = self.comment("US", "增加时间戳与位置字段")
        parts = self.svc.split_comment(
            big.id,
            [{"text": "增加时间戳字段"}, {"text": "增加位置字段"}],
            "两个独立主题",
            "chair",
        )
        self.assertEqual(self.svc.store.comments[big.id].status, CommentStatus.SPLIT)
        after_split = self.comment("DE", "字段应可空", depends_on=[big.id])
        self.svc.accept_comment(parts[0].id, "加时间戳", "接受", "chair")
        with self.assertRaises(DomainError):
            self.svc.accept_comment(after_split.id, "文本", "理由", "chair")
        self.svc.accept_comment(parts[1].id, "加位置", "接受", "chair")
        self.svc.accept_comment(after_split.id, "字段可空", "接受", "chair")


class LateRevisionTests(ServiceTestBase):
    def test_late_revision_does_not_change_confirmed_tally(self):
        """迟到修订产生新版本，但已关闭轮次的计票结果不变。"""
        self.grant("CN", "rep-cn")
        self.grant("DE", "rep-de")
        vote_round = self.svc.open_round("CL-1")
        self.svc.cast_ballot(vote_round.id, "CN", "rep-cn", BallotChoice.APPROVE)
        self.svc.cast_ballot(vote_round.id, "DE", "rep-de", BallotChoice.APPROVE)
        confirmed = self.svc.close_round(vote_round.id)

        revision = self.svc.revise_clause(
            "CL-1", "车辆事件消息应包含观测时间与定位信息", "迟到修订"
        )
        self.assertEqual(revision.version_no, 2)
        self.assertEqual(revision.provenance.source, VersionSource.REVISION)

        after = self.svc.tally(vote_round.id)
        self.assertEqual(after, confirmed)
        self.assertEqual(after["result"], RoundResult.APPROVED.value)
        self.assertEqual(after["clause_version_id"], vote_round.clause_version_id)

        diff = self.svc.clause_diff("CL-1", 1, 2)
        self.assertIn("+车辆事件消息应包含观测时间与定位信息", diff["unified_diff"])
        self.assertEqual(diff["to"]["provenance"]["source"], "revision")

    def test_cannot_revise_or_dispose_while_round_open(self):
        self.grant("CN", "rep-cn")
        comment = self.comment("CN", "调整措辞")
        self.svc.open_round("CL-1")
        with self.assertRaises(DomainError):
            self.svc.revise_clause("CL-1", "新文本", "表决中修订")
        with self.assertRaises(DomainError):
            self.svc.accept_comment(comment.id, "新文本", "理由", "chair")


class ReleaseTests(ServiceTestBase):
    def _confirmed_round(self) -> None:
        self.grant("CN", "rep-cn")
        self.grant("DE", "rep-de")
        self.grant("JP", "rep-jp")
        vote_round = self.svc.open_round("CL-1")
        self.svc.cast_ballot(vote_round.id, "CN", "rep-cn", BallotChoice.APPROVE)
        self.svc.cast_ballot(vote_round.id, "DE", "rep-de", BallotChoice.APPROVE)
        self.svc.cast_ballot(
            vote_round.id, "JP", "rep-jp", BallotChoice.REJECT,
            objection="缺少数据保留期限", reason="隐私合规",
        )
        self.svc.close_round(vote_round.id)

    def test_sealed_release_is_reproducible_with_objections(self):
        comment = self.comment("CN", "应明确保留期限", language="zh")
        self.svc.accept_comment(comment.id, "车辆事件消息应包含观测时间", "采纳", "chair")
        self._confirmed_round()

        release = self.svc.seal_release("P-1", "首版发布")
        self.assertEqual(release.seq_no, 1)
        self.assertEqual(
            self.svc.store.proposals["P-1"].status, ProposalStatus.PUBLISHED
        )

        view = self.svc.release_view(release.id)
        self.assertEqual(view["content_hash"], release.content_hash)
        self.assertEqual(view["payload"]["proposal"]["id"], "P-1")
        self.assertEqual(len(view["payload"]["rounds"]), 1)
        self.assertEqual(view["payload"]["rounds"][0]["tally"]["approve"], 2)

        objections = self.svc.release_objections(release.id)
        self.assertEqual(len(objections), 1)
        self.assertEqual(objections[0]["delegation_code"], "JP")
        self.assertEqual(objections[0]["objection"], "缺少数据保留期限")
        self.assertEqual(objections[0]["reason"], "隐私合规")

    def test_revoke_then_new_version_and_reseal(self):
        """发布撤销后可产生新版本并再次封存；旧发布包仍可完整重现。"""
        self._confirmed_round()
        release1 = self.svc.seal_release("P-1", "首版发布")
        hash1 = release1.content_hash

        revoked = self.svc.revoke_release(release1.id, "发现编辑性错误，撤回重发")
        self.assertEqual(revoked.status, ReleaseStatus.REVOKED)
        self.assertEqual(
            self.svc.store.proposals["P-1"].status, ProposalStatus.IN_REVIEW
        )
        with self.assertRaises(DomainError):
            self.svc.revoke_release(release1.id, "重复撤销")

        # 撤销后的新版本：修订文本、再表决、再封存
        revision = self.svc.revise_clause(
            "CL-1", "车辆事件消息应包含观测时间（UTC）", "撤销后修订"
        )
        self.assertEqual(revision.version_no, 2)
        round2 = self.svc.open_round("CL-1")
        self.assertEqual(round2.round_no, 2)
        self.svc.cast_ballot(round2.id, "CN", "rep-cn", BallotChoice.APPROVE)
        self.svc.cast_ballot(round2.id, "DE", "rep-de", BallotChoice.APPROVE)
        self.svc.cast_ballot(round2.id, "JP", "rep-jp", BallotChoice.APPROVE)
        self.svc.close_round(round2.id)

        release2 = self.svc.seal_release("P-1", "修订后重发")
        self.assertEqual(release2.seq_no, 2)
        self.assertEqual(release2.previous_release_id, release1.id)
        self.assertNotEqual(release2.content_hash, hash1)

        # 旧发布包内容未被撤销或后续修订改变
        view1 = self.svc.release_view(release1.id)
        self.assertEqual(view1["status"], ReleaseStatus.REVOKED.value)
        self.assertEqual(view1["content_hash"], hash1)
        self.assertEqual(view1["revoke_reason"], "发现编辑性错误，撤回重发")
        self.assertEqual(len(view1["payload"]["rounds"]), 1)
        self.assertEqual(
            view1["payload"]["clauses"][0]["versions"][-1]["version_no"], 1
        )
        view2 = self.svc.release_view(release2.id)
        self.assertEqual(len(view2["payload"]["rounds"]), 2)
        self.assertEqual(
            view2["payload"]["clauses"][0]["versions"][-1]["version_no"], 2
        )

    def test_cannot_seal_with_open_round(self):
        self.grant("CN", "rep-cn")
        self.svc.open_round("CL-1")
        with self.assertRaises(DomainError) as ctx:
            self.svc.seal_release("P-1")
        self.assertEqual(ctx.exception.code, "conflict")


if __name__ == "__main__":
    unittest.main()
