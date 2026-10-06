"""标准意见协同后端的服务层测试。

覆盖题目要求的关键场景：
- 代表变更与授权撤回（只影响后续轮次）
- 平票处理与重新表决
- 意见依赖（替代 / 合并 / 拆分 / 重新讨论）
- 发布包封存、撤销后的新版本与完整重现
- 重复选票幂等、迟到修订不改变已确认的表决结果
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from standards_collaboration import (
    AuthorizationError,
    BallotChoice,
    CollaborationService,
    LockedError,
    StateError,
    TallyResult,
    ValidationError,
)

DELEGATIONS = [("CN", "中国"), ("DE", "德国"), ("US", "美国"), ("JP", "日本"), ("KR", "韩国")]


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = CollaborationService()
        self.svc.create_proposal("P-1", "智能网联汽车数据交换标准")
        self.svc.add_clause("P-1", "CL-1", "车辆事件消息应包含观测时间")
        for code, name in DELEGATIONS:
            self.svc.register_delegation(code, name)
            self.svc.grant_authorization(code, f"R-{code}-1")

    def open_round_with_votes(self, round_id: str, votes: dict[str, str]) -> None:
        self.svc.open_round(round_id, "CL-1")
        for code, choice in votes.items():
            self.svc.cast_vote(round_id, code, f"R-{code}-1", choice)


class DelegationChangeTests(ServiceTestBase):
    """代表变更：授权撤回只影响后续轮次，计票按关闭时点有效授权。"""

    def test_replacement_representative_vote_counts_original_does_not(self):
        self.svc.open_round("RD-1", "CL-1")
        self.svc.cast_vote("RD-1", "DE", "R-DE-1", BallotChoice.APPROVE)
        # 代表团更换代表：撤回旧授权，授予新授权
        self.svc.revoke_authorization("DE", "R-DE-1")
        self.svc.grant_authorization("DE", "R-DE-2")
        self.svc.cast_vote("RD-1", "DE", "R-DE-2", BallotChoice.REJECT)
        self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)

        tally = self.svc.close_round("RD-1")
        de_ballot = next(b for b in tally["ballots"] if b["delegation_code"] == "DE")
        # 计票取代表团最新一票，且新代表授权有效
        self.assertEqual(de_ballot["representative_id"], "R-DE-2")
        self.assertEqual(de_ballot["choice"], "reject")
        self.assertTrue(de_ballot["counted"])
        self.assertEqual(tally["approve"], 1)
        self.assertEqual(tally["reject"], 1)
        self.assertEqual(tally["result"], TallyResult.TIED.value)

    def test_revoked_representative_cannot_vote_in_later_round(self):
        self.open_round_with_votes("RD-1", {"DE": "approve", "CN": "approve"})
        first_tally = self.svc.close_round("RD-1")
        self.assertEqual(first_tally["approve"], 2)

        # 轮次关闭后撤回授权：历史结果冻结不变
        self.svc.revoke_authorization("DE", "R-DE-1")
        frozen = self.svc.tally_report("RD-1")
        self.assertEqual(frozen["approve"], 2)
        de_ballot = next(b for b in frozen["ballots"] if b["delegation_code"] == "DE")
        self.assertTrue(de_ballot["counted"])

        # 后续轮次中，被撤回的代表无投票资格
        self.svc.open_round("RD-2", "CL-1")
        with self.assertRaises(AuthorizationError):
            self.svc.cast_vote("RD-2", "DE", "R-DE-1", BallotChoice.APPROVE)

    def test_vote_without_valid_authorization_at_close_is_not_counted(self):
        self.svc.open_round("RD-1", "CL-1")
        self.svc.cast_vote("RD-1", "DE", "R-DE-1", BallotChoice.APPROVE)
        self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)
        # 投票后、关闭前撤回授权：该票在计票时点无效
        self.svc.revoke_authorization("DE", "R-DE-1")
        tally = self.svc.close_round("RD-1")
        de_ballot = next(b for b in tally["ballots"] if b["delegation_code"] == "DE")
        self.assertFalse(de_ballot["counted"])
        self.assertEqual(tally["approve"], 1)
        self.assertEqual(tally["result"], TallyResult.APPROVED.value)


class BallotIdempotencyTests(ServiceTestBase):
    def test_duplicate_vote_is_idempotent(self):
        self.svc.open_round("RD-1", "CL-1")
        first = self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)
        events_after_first = len(self.svc.events)
        second = self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(len(self.svc.events), events_after_first)  # 无新事件
        self.svc.cast_vote("RD-1", "DE", "R-DE-1", BallotChoice.APPROVE)
        tally = self.svc.close_round("RD-1")
        self.assertEqual(tally["approve"], 2)  # 重复票只计一次

    def test_changed_vote_counts_latest_choice(self):
        self.svc.open_round("RD-1", "CL-1")
        self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)
        changed = self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.REJECT)
        self.assertFalse(changed["idempotent_replay"])
        tally = self.svc.close_round("RD-1")
        self.assertEqual(tally["approve"], 0)
        self.assertEqual(tally["reject"], 1)
        cn_ballot = next(b for b in tally["ballots"] if b["delegation_code"] == "CN")
        self.assertEqual(cn_ballot["note"], "改票后以最新一票为准")

    def test_close_round_is_idempotent(self):
        self.open_round_with_votes("RD-1", {"CN": "approve"})
        first = self.svc.close_round("RD-1")
        second = self.svc.close_round("RD-1")
        self.assertEqual(first, second)
        with self.assertRaises(StateError):
            self.svc.cast_vote("RD-1", "DE", "R-DE-1", BallotChoice.APPROVE)


class TieAndLateRevisionTests(ServiceTestBase):
    def test_tie_is_not_approved_and_can_be_revoted_in_new_round(self):
        self.open_round_with_votes(
            "RD-1",
            {"CN": "approve", "US": "approve", "DE": "reject", "JP": "reject",
             "KR": "abstain"},
        )
        tally = self.svc.close_round("RD-1")
        self.assertEqual(tally["result"], TallyResult.TIED.value)
        self.assertEqual(tally["approve"], 2)
        self.assertEqual(tally["reject"], 2)
        self.assertEqual(tally["abstain"], 1)

        # 平票后重新讨论、开启新一轮表决
        self.open_round_with_votes(
            "RD-2", {"CN": "approve", "US": "approve", "DE": "approve", "JP": "reject"}
        )
        second = self.svc.close_round("RD-2")
        self.assertEqual(second["result"], TallyResult.APPROVED.value)

    def test_late_revision_does_not_change_confirmed_round(self):
        self.svc.submit_comment("C-1", "P-1", "CL-1", "CN", "zh",
                                "车辆事件消息应包含观测时间与位置")
        self.svc.open_round("RD-1", "CL-1")  # 绑定 v1
        # 轮次进行中条款文本被修订（迟到修订）
        self.svc.accept_comment("C-1", "采纳位置字段建议")
        self.assertEqual(self.svc.clauses["CL-1"].current_version, 2)

        self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)
        self.svc.cast_vote("RD-1", "DE", "R-DE-1", BallotChoice.APPROVE)
        tally = self.svc.close_round("RD-1")
        # 已确认的表决仍针对开启时锁定的 v1
        self.assertEqual(tally["clause_version"], 1)
        self.assertEqual(tally["result"], TallyResult.APPROVED.value)


class CommentDependencyTests(ServiceTestBase):
    def test_supersede_chain_and_disposition_guards(self):
        self.svc.submit_comment("C-1", "P-1", "CL-1", "CN", "zh", "加入时间戳")
        self.svc.submit_comment("C-2", "P-1", "CL-1", "DE", "en",
                                "加入时间戳与位置", supersedes_id="C-1")
        self.assertEqual(self.svc.comments["C-1"].status.value, "superseded")
        self.assertEqual(self.svc.comments["C-2"].status.value, "open")

        # 被替代的意见不能再处置
        with self.assertRaises(StateError):
            self.svc.accept_comment("C-1", "试图接受已替代意见")

        result = self.svc.accept_comment("C-2", "采纳替代意见")
        version = result["version"]
        self.assertEqual(version["version"], 2)
        self.assertEqual(version["source_comment_ids"], ["C-2"])
        self.assertEqual(version["parent_version"], 1)

    def test_merge_split_substitute_and_reopen(self):
        self.svc.submit_comment("C-3", "P-1", "CL-1", "CN", "zh", "增加车速字段")
        self.svc.submit_comment("C-4", "P-1", "CL-1", "JP", "ja", "增加加速度字段")

        merged = self.svc.merge_comments(
            ["C-3", "C-4"], "C-5", "增加车速与加速度字段", "两条意见主题相同"
        )
        self.assertEqual(merged["merged_comment"]["derived_from"], ["C-3", "C-4"])
        self.assertEqual(self.svc.comments["C-3"].status.value, "merged")
        self.assertEqual(self.svc.comments["C-4"].status.value, "merged")

        split = self.svc.split_comment(
            "C-5",
            [("C-6", "增加车速字段与单位"), ("C-7", "增加加速度字段与单位")],
            "两个字段应分别评审",
        )
        self.assertEqual(self.svc.comments["C-5"].status.value, "split")
        self.assertEqual(
            [c["comment_id"] for c in split["split_comments"]], ["C-6", "C-7"]
        )

        substituted = self.svc.substitute_comment(
            "C-6", "C-8", "增加车速字段（km/h）", "明确单位"
        )
        self.assertEqual(self.svc.comments["C-6"].status.value, "superseded")
        self.assertEqual(substituted["replacement_comment"]["supersedes"], "C-6")

        # 拒绝后可重新讨论；已合并的意见不可重开
        self.svc.reject_comment("C-7", "加速度由另一条款覆盖")
        self.svc.reopen_comment("C-7", "复议后恢复讨论")
        self.assertEqual(self.svc.comments["C-7"].status.value, "open")
        with self.assertRaises(StateError):
            self.svc.reopen_comment("C-3", "已合并意见不可重开")

    def test_merge_requires_same_clause_and_two_comments(self):
        self.svc.add_clause("P-1", "CL-2", "车辆事件消息应包含事件类型")
        self.svc.submit_comment("C-9", "P-1", "CL-1", "CN", "zh", "建议甲")
        self.svc.submit_comment("C-10", "P-1", "CL-2", "CN", "zh", "建议乙")
        with self.assertRaises(ValidationError):
            self.svc.merge_comments(["C-9", "C-10"], "C-11", "合并", "跨条款合并")
        with self.assertRaises(ValidationError):
            self.svc.merge_comments(["C-9"], "C-12", "合并", "单条意见")


class ReportApiTests(ServiceTestBase):
    def test_comment_matrix_reflects_state_at_round_close(self):
        self.svc.submit_comment("C-1", "P-1", "CL-1", "CN", "zh", "加入时间戳")
        self.svc.submit_comment("C-2", "P-1", "CL-1", "DE", "en", "加入位置")
        self.svc.open_round("RD-1", "CL-1")
        self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)
        self.svc.close_round("RD-1")
        # 轮次关闭后才处置意见
        self.svc.accept_comment("C-1", "采纳")
        self.svc.reject_comment("C-2", "位置精度未定义")

        matrix = self.svc.comment_matrix("RD-1")
        rows = {row["comment_id"]: row for row in matrix["rows"]}
        # 矩阵按关闭时点重建：两条意见当时均待处置
        self.assertEqual(rows["C-1"]["status"], "open")
        self.assertEqual(rows["C-2"]["status"], "open")
        self.assertEqual(matrix["as_of_seq"], self.svc.rounds["RD-1"].closed_seq)

    def test_text_diff_tracks_provenance(self):
        self.svc.submit_comment("C-1", "P-1", "CL-1", "CN", "zh",
                                "车辆事件消息应包含观测时间与位置")
        self.svc.accept_comment("C-1", "采纳位置字段")
        diff = self.svc.text_diff("CL-1", 1, 2)
        self.assertTrue(diff["changed"])
        self.assertIn("-车辆事件消息应包含观测时间", diff["unified_diff"])
        self.assertIn("+车辆事件消息应包含观测时间与位置", diff["unified_diff"])
        self.assertEqual(diff["provenance"]["source_comment_ids"], ["C-1"])
        self.assertTrue(
            diff["provenance"]["source_disposition_id"].startswith("DISP-")
        )


class ReleasePackageTests(ServiceTestBase):
    def _prepare_release(self) -> None:
        self.svc.submit_comment("C-1", "P-1", "CL-1", "CN", "zh", "加入位置")
        self.svc.submit_comment("C-2", "P-1", "CL-1", "DE", "en", "删除观测时间")
        self.svc.accept_comment("C-1", "采纳位置字段")
        self.svc.reject_comment("C-2", "观测时间为安全审计必需")
        self.svc.open_round("RD-1", "CL-1")
        self.svc.cast_vote("RD-1", "CN", "R-CN-1", BallotChoice.APPROVE)
        self.svc.cast_vote("RD-1", "US", "R-US-1", BallotChoice.APPROVE)
        self.svc.cast_vote("RD-1", "DE", "R-DE-1", BallotChoice.REJECT,
                           reason="位置字段精度待验证")
        self.svc.close_round("RD-1")

    def test_sealed_release_reproduces_objections_and_digest(self):
        self._prepare_release()
        sealed = self.svc.seal_release("REL-1", "P-1")
        self.assertEqual(sealed["status"], "sealed")

        reproduction = self.svc.reproduce_release("REL-1")
        self.assertTrue(reproduction["digest_verified"])
        snapshot = reproduction["snapshot"]
        # 条款版本与来源链完整
        clause = snapshot["clauses"][0]
        self.assertEqual(clause["version"], 2)
        self.assertEqual(clause["provenance"]["source_comment_ids"], ["C-1"])
        # 异议完整：被拒绝的意见 + 反对票，均含理由
        kinds = {obj["type"] for obj in snapshot["objections"]}
        self.assertEqual(kinds, {"rejected_comment", "reject_vote"})
        rejected = next(o for o in snapshot["objections"] if o["type"] == "rejected_comment")
        self.assertEqual(rejected["reason"], "观测时间为安全审计必需")
        reject_vote = next(o for o in snapshot["objections"] if o["type"] == "reject_vote")
        self.assertEqual(reject_vote["reason"], "位置字段精度待验证")
        # 计票快照随包封存
        self.assertEqual(snapshot["rounds"][0]["result"], TallyResult.APPROVED.value)

    def test_revoked_release_allows_new_version_and_keeps_snapshot(self):
        self._prepare_release()
        self.svc.seal_release("REL-1", "P-1")
        original = self.svc.reproduce_release("REL-1")

        # 封存期间条款被锁定，迟到修订被拒绝
        self.svc.submit_comment("C-3", "P-1", "CL-1", "JP", "ja", "补充时间精度")
        with self.assertRaises(LockedError):
            self.svc.accept_comment("C-3", "试图在封存后修订")

        # 撤销发布后可以产生新版本，来源链完整
        self.svc.revoke_release("REL-1", "秘书处发现程序瑕疵，重新开放修订")
        accepted = self.svc.accept_comment("C-3", "采纳时间精度建议")
        self.assertEqual(accepted["version"]["version"], 3)
        self.assertEqual(accepted["version"]["source_comment_ids"], ["C-3"])
        self.assertEqual(accepted["version"]["parent_version"], 2)

        # 已撤销的发布包仍可完整重现，内容不因后续修订而改变
        reproduction = self.svc.reproduce_release("REL-1")
        self.assertEqual(reproduction["status"], "revoked")
        self.assertEqual(reproduction["snapshot"], original["snapshot"])
        self.assertEqual(reproduction["digest"], original["digest"])
        self.assertTrue(reproduction["digest_verified"])

    def test_second_seal_requires_revoking_first(self):
        self._prepare_release()
        self.svc.seal_release("REL-1", "P-1")
        with self.assertRaises(StateError):
            self.svc.seal_release("REL-2", "P-1")
        self.svc.revoke_release("REL-1", "程序性撤销")
        self.svc.seal_release("REL-2", "P-1")
        self.assertEqual(self.svc.releases["REL-2"].status.value, "sealed")


if __name__ == "__main__":
    unittest.main()
