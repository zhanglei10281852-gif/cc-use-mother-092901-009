"""HTTP API 端到端冒烟测试：通过真实 HTTP 请求走通完整协同流程。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from standards_collaboration.api import CollaborationApiServer


class ApiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = CollaborationApiServer(("127.0.0.1", 0))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_collaboration_flow_over_http(self):
        status, _ = self.call("POST", "/proposals", {
            "proposal_id": "P-HTTP", "title": "车路协同消息集"})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/proposals/P-HTTP/clauses", {
            "clause_id": "CL-H1", "text": "路侧单元应广播信号相位"})
        self.assertEqual(status, 201)

        for code, name in [("CN", "中国"), ("DE", "德国")]:
            status, _ = self.call("POST", "/delegations", {"code": code, "name": name})
            self.assertEqual(status, 201)
            status, _ = self.call("POST", f"/delegations/{code}/authorizations", {
                "representative_id": f"R-{code}-1"})
            self.assertEqual(status, 201)

        status, comment = self.call("POST", "/comments", {
            "comment_id": "C-H1", "proposal_id": "P-HTTP", "clause_id": "CL-H1",
            "delegation_code": "CN", "language": "zh",
            "text": "路侧单元应广播信号相位与配时方案"})
        self.assertEqual(status, 201)
        self.assertEqual(comment["status"], "open")

        status, accepted = self.call("POST", "/comments/C-H1/accept", {
            "reason": "采纳配时方案建议"})
        self.assertEqual(status, 200)
        self.assertEqual(accepted["version"]["version"], 2)

        status, _ = self.call("POST", "/rounds", {
            "round_id": "RD-H1", "clause_id": "CL-H1"})
        self.assertEqual(status, 201)
        for code in ("CN", "DE"):
            status, vote = self.call("POST", "/rounds/RD-H1/votes", {
                "delegation_code": code, "representative_id": f"R-{code}-1",
                "choice": "approve"})
            self.assertEqual(status, 200)
            self.assertFalse(vote["idempotent_replay"])
        # 重复投票：幂等
        status, replay = self.call("POST", "/rounds/RD-H1/votes", {
            "delegation_code": "CN", "representative_id": "R-CN-1",
            "choice": "approve"})
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])

        status, tally = self.call("POST", "/rounds/RD-H1/close")
        self.assertEqual(status, 200)
        self.assertEqual(tally["result"], "approved")
        self.assertEqual(tally["approve"], 2)

        status, matrix = self.call("GET", "/rounds/RD-H1/comment-matrix")
        self.assertEqual(status, 200)
        self.assertEqual(matrix["rows"][0]["status"], "accepted")

        status, tally_again = self.call("GET", "/rounds/RD-H1/tally")
        self.assertEqual(status, 200)
        self.assertEqual(tally_again, tally)

        status, diff = self.call("GET", "/clauses/CL-H1/diff?from=1&to=2")
        self.assertEqual(status, 200)
        self.assertTrue(diff["changed"])
        self.assertIn("配时方案", diff["unified_diff"])

        status, release = self.call("POST", "/releases", {
            "release_id": "REL-H1", "proposal_id": "P-HTTP"})
        self.assertEqual(status, 201)
        status, reproduction = self.call("GET", "/releases/REL-H1/reproduction")
        self.assertEqual(status, 200)
        self.assertTrue(reproduction["digest_verified"])
        self.assertEqual(reproduction["digest"], release["digest"])

        # 未授权代表投票被拒
        status, error = self.call("POST", "/rounds/RD-H1/votes", {
            "delegation_code": "CN", "representative_id": "R-UNKNOWN",
            "choice": "approve"})
        self.assertEqual(status, 409)  # 轮次已关闭
        status, _ = self.call("POST", "/rounds", {
            "round_id": "RD-H2", "clause_id": "CL-H1"})
        self.assertEqual(status, 201)
        status, error = self.call("POST", "/rounds/RD-H2/votes", {
            "delegation_code": "CN", "representative_id": "R-UNKNOWN",
            "choice": "approve"})
        self.assertEqual(status, 403)
        self.assertIn("有效授权", error["error"])


if __name__ == "__main__":
    unittest.main()
