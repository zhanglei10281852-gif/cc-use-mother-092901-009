"""API 端到端测试：意见矩阵、计票结果、文本差异与发布包重现。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fastapi.testclient import TestClient

from standards_collaboration.api import create_app


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())
        self.client.post("/proposals", json={"title": "智能网联汽车协同标准", "proposal_id": "P-1"})
        self.client.post(
            "/proposals/P-1/clauses",
            json={"clause_id": "CL-1", "title": "事件时间戳", "text": "车辆事件消息应包含观测时间"},
        )
        for code, name in [("CN", "中国"), ("DE", "德国"), ("JP", "日本"), ("US", "美国")]:
            self.client.post("/delegations", json={"code": code, "name": name})

    def grant(self, delegation: str, rep: str) -> str:
        resp = self.client.post(
            f"/delegations/{delegation}/credentials", json={"representative_id": rep}
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def open_round(self) -> str:
        resp = self.client.post("/clauses/CL-1/rounds", json={})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def vote(self, round_id: str, delegation: str, rep: str, choice: str, **extra):
        return self.client.post(
            f"/rounds/{round_id}/ballots",
            json={
                "delegation_code": delegation,
                "representative_id": rep,
                "choice": choice,
                **extra,
            },
        )


class FullWorkflowTests(ApiTestBase):
    def test_matrix_tally_diff_and_release(self):
        self.grant("CN", "rep-cn")
        self.grant("DE", "rep-de")
        self.grant("JP", "rep-jp")

        # 多语言意见：中文意见依赖英文意见
        resp = self.client.post(
            "/proposals/P-1/comments",
            json={"clause_id": "CL-1", "delegation_code": "CN", "language": "zh",
                  "text": "时间戳应使用 UTC"},
        )
        self.assertEqual(resp.status_code, 201)
        base_id = resp.json()["id"]
        resp = self.client.post(
            "/proposals/P-1/comments",
            json={"clause_id": "CL-1", "delegation_code": "DE", "language": "en",
                  "text": "Timestamp shall include timezone offset",
                  "depends_on": [base_id]},
        )
        dependent_id = resp.json()["id"]

        # 依赖未满足时接受 → 409
        resp = self.client.post(
            f"/comments/{dependent_id}/accept",
            json={"applied_text": "x", "reason": "r"},
        )
        self.assertEqual(resp.status_code, 409)

        # 先接受基础意见，再接受依赖意见，文本逐步演进
        resp = self.client.post(
            f"/comments/{base_id}/accept",
            json={"applied_text": "车辆事件消息应包含观测时间（UTC）", "reason": "采纳"},
        )
        self.assertEqual(resp.status_code, 201)
        v2 = resp.json()["clause_version"]
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["provenance"]["comment_ids"], [base_id])
        resp = self.client.post(
            f"/comments/{dependent_id}/accept",
            json={"applied_text": "车辆事件消息应包含观测时间（UTC，含时区偏移）", "reason": "采纳"},
        )
        self.assertEqual(resp.status_code, 201)

        # 表决：JP 反对并附异议，CN/DE 赞成
        round_id = self.open_round()
        self.assertEqual(self.vote(round_id, "CN", "rep-cn", "approve").status_code, 201)
        self.assertEqual(self.vote(round_id, "DE", "rep-de", "approve").status_code, 201)
        resp = self.vote(round_id, "JP", "rep-jp", "reject",
                         objection="缺少数据保留期限", reason="隐私合规")
        self.assertEqual(resp.status_code, 201)

        # 幂等：JP 重复提交相同选票 → 200 且不计两次
        resp = self.vote(round_id, "JP", "rep-jp", "reject",
                         objection="缺少数据保留期限", reason="隐私合规")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["idempotent_replay"])
        # 不同内容 → 409
        resp = self.vote(round_id, "JP", "rep-jp", "approve")
        self.assertEqual(resp.status_code, 409)

        resp = self.client.post(f"/rounds/{round_id}/close")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["result"], "approved")

        # 指定轮次的计票结果
        resp = self.client.get(f"/rounds/{round_id}/tally")
        self.assertEqual(resp.json()["approve"], 2)
        self.assertEqual(resp.json()["reject"], 1)
        self.assertEqual(resp.json()["eligible"], ["CN", "DE", "JP"])

        # 指定轮次的意见矩阵
        resp = self.client.get("/clauses/CL-1/rounds/1/comment-matrix")
        self.assertEqual(resp.status_code, 200)
        matrix = resp.json()
        self.assertEqual(matrix["delegations"], ["CN", "DE", "JP"])
        self.assertEqual(len(matrix["comments"]), 2)
        by_id = {c["id"]: c for c in matrix["comments"]}
        self.assertEqual(by_id[base_id]["status"], "accepted")
        self.assertEqual(by_id[base_id]["disposition"]["action"], "accept")
        self.assertEqual(by_id[dependent_id]["depends_on"], [base_id])
        self.assertEqual(matrix["ballots"]["JP"]["choice"], "reject")
        self.assertEqual(matrix["ballots"]["JP"]["objection"], "缺少数据保留期限")
        self.assertEqual(matrix["tally"]["result"], "approved")

        # 文本差异：v1 → v3，来源链完整
        resp = self.client.get("/clauses/CL-1/diff", params={"from_version": 1, "to_version": 3})
        self.assertEqual(resp.status_code, 200)
        diff = resp.json()
        self.assertIn("-车辆事件消息应包含观测时间", diff["unified_diff"])
        self.assertIn("含时区偏移", diff["unified_diff"])
        self.assertEqual(diff["from"]["provenance"]["source"], "initial")
        self.assertEqual(diff["to"]["provenance"]["source"], "disposition")

        # 封存发布包并可完整重现（含异议与理由）
        resp = self.client.post("/proposals/P-1/releases", json={"note": "首版"})
        self.assertEqual(resp.status_code, 201)
        release_id = resp.json()["id"]
        content_hash = resp.json()["content_hash"]

        resp = self.client.get(f"/releases/{release_id}")
        self.assertEqual(resp.status_code, 200)
        view = resp.json()
        self.assertEqual(view["content_hash"], content_hash)
        self.assertEqual(view["payload"]["proposal"]["status"], "published")
        self.assertEqual(len(view["payload"]["comments"]), 2)
        self.assertEqual(view["payload"]["rounds"][0]["tally"]["result"], "approved")

        resp = self.client.get(f"/releases/{release_id}/objections")
        self.assertEqual(resp.status_code, 200)
        objections = resp.json()
        self.assertEqual(len(objections), 1)
        self.assertEqual(objections[0]["delegation_code"], "JP")
        self.assertEqual(objections[0]["reason"], "隐私合规")

        # 撤销发布 → 新版本 → 再封存；旧包仍可重现
        resp = self.client.post(f"/releases/{release_id}/revoke",
                                json={"reason": "发现编辑性错误"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "revoked")

        resp = self.client.post("/clauses/CL-1/revisions",
                                json={"text": "车辆事件消息应包含观测时间（UTC，含时区偏移，精度 10 毫秒）",
                                      "note": "撤销后修订"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["version_no"], 4)

        round2 = self.open_round()
        for delegation, rep in [("CN", "rep-cn"), ("DE", "rep-de"), ("JP", "rep-jp")]:
            self.assertEqual(
                self.vote(round2, delegation, rep, "approve").status_code, 201
            )
        self.client.post(f"/rounds/{round2}/close")
        resp = self.client.post("/proposals/P-1/releases", json={"note": "重发"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["seq_no"], 2)
        self.assertEqual(resp.json()["previous_release_id"], release_id)

        resp = self.client.get(f"/releases/{release_id}")
        self.assertEqual(resp.json()["content_hash"], content_hash)
        self.assertEqual(resp.json()["status"], "revoked")
        self.assertEqual(len(resp.json()["payload"]["rounds"]), 1)


class TieViaApiTests(ApiTestBase):
    def test_tie_result(self):
        for delegation, rep in [("CN", "r1"), ("DE", "r2"), ("JP", "r3"), ("US", "r4")]:
            self.grant(delegation, rep)
        round_id = self.open_round()
        self.vote(round_id, "CN", "r1", "approve")
        self.vote(round_id, "DE", "r2", "approve")
        self.vote(round_id, "JP", "r3", "reject", objection="技术不可行", reason="理由")
        self.vote(round_id, "US", "r4", "reject", objection="成本过高", reason="理由")
        resp = self.client.post(f"/rounds/{round_id}/close")
        self.assertEqual(resp.json()["result"], "tied")
        resp = self.client.get("/clauses/CL-1/rounds/1/comment-matrix")
        self.assertEqual(resp.json()["tally"]["result"], "tied")


class DelegateChangeViaApiTests(ApiTestBase):
    def test_withdrawal_scopes_to_later_rounds(self):
        self.grant("CN", "rep-cn")
        cred = self.grant("DE", "rep-de-1")
        round1 = self.open_round()
        self.assertEqual(self.vote(round1, "DE", "rep-de-1", "approve").status_code, 201)
        self.client.post(f"/rounds/{round1}/close")

        resp = self.client.post(f"/credentials/{cred}/withdraw")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.json()["withdrawn_seq"])
        self.grant("DE", "rep-de-2")

        round2 = self.open_round()
        # 旧代表在新轮次投票 → 403；新代表可以投
        self.assertEqual(self.vote(round2, "DE", "rep-de-1", "approve").status_code, 403)
        self.assertEqual(self.vote(round2, "DE", "rep-de-2", "approve").status_code, 201)
        # 第 1 轮计票保持原样
        resp = self.client.get(f"/rounds/{round1}/tally")
        self.assertEqual(resp.json()["approve"], 1)
        self.assertEqual(resp.json()["result"], "approved")


class ErrorMappingTests(ApiTestBase):
    def test_not_found_and_validation(self):
        self.assertEqual(self.client.get("/proposals/NOPE").status_code, 404)
        resp = self.client.post(
            "/proposals/P-1/comments",
            json={"clause_id": "CL-1", "delegation_code": "CN", "text": "  "},
        )
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post(
            "/proposals/P-1/comments",
            json={"clause_id": "CL-1", "delegation_code": "XX", "text": "hi"},
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
