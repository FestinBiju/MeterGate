import unittest

from scripts.secret_scan import findings


class SecretScanTests(unittest.TestCase):
    def test_long_mcp_identifier_is_not_a_bearer_token(self) -> None:
        self.assertEqual(findings(b"new_mcp_agent_session_id_and_related_identifier"), 0)

    def test_complete_mcp_bearer_token_is_detected(self) -> None:
        token = b"mcp_" + (b"Ab3_Cd4-Ef5Gh" * 4)[:43]
        self.assertEqual(len(token), 47)
        self.assertEqual(findings(b'token = "' + token + b'"'), 1)

    def test_synthetic_mcp_fixture_is_ignored(self) -> None:
        token = b"mcp_" + b"a" * 43
        self.assertEqual(findings(token), 0)


if __name__ == "__main__":
    unittest.main()
