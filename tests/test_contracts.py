import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from standards_collaboration.contracts import BallotChoice, ClauseVersion, DelegationBallot


class StandardsContractTests(unittest.TestCase):
    def test_ballot_binds_clause_version(self):
        clause = ClauseVersion("C-1", 5, "示例条款")
        ballot = DelegationBallot("DE", "R-1", clause, BallotChoice.ABSTAIN)
        self.assertEqual(ballot.clause.version, 5)

    def test_clause_text_is_required(self):
        with self.assertRaises(ValueError):
            ClauseVersion("C-2", 1, " ")


if __name__ == "__main__":
    unittest.main()
