"""Owner-approved sign-in accounts, with dummy signed tokens only."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "tests"))
import herald_oauth as oauth
import test_oauth as fixtures


class ApprovedAccounts(unittest.TestCase):
    def setUp(self):
        fixtures.ResourceTests.setUp(self)

    def verify(self, changes=None):
        return fixtures.ResourceTests.verify(self, changes)

    def refused(self, changes):
        with self.assertRaises(oauth.AuthError) as raised:
            self.verify(changes)
        return raised.exception.reason

    def test_legacy_single_subject_still_signs_in(self):
        self.assertEqual(self.verify()["subject"], "dummy|jamie")

    def test_any_approved_subject_signs_in_and_the_grant_names_it(self):
        self.path.write_text(json.dumps({**self.config, "subjects": ["google-oauth2|jamie"]}))

        legacy = self.verify()
        google = self.verify({"sub": "google-oauth2|jamie"})

        self.assertEqual((legacy["subject"], google["subject"]), ("dummy|jamie", "google-oauth2|jamie"))
        self.assertTrue(self.auth.active_grant(google))

    def test_valid_unapproved_sign_in_is_refused_and_recorded_once_per_interval(self):
        reasons = [self.refused({"sub": "google-oauth2|simon", "email": "simon@example.test"}) for _ in range(2)]

        approved, candidates = oauth.list_accounts(self.path)
        self.assertEqual(reasons, ["subject_mismatch"] * 2)
        self.assertEqual(approved, ["dummy|jamie"])
        self.assertEqual([(c["subject"], c["email"], c["count"]) for c in candidates],
                         [("google-oauth2|simon", "simon@example.test", 1)])
        self.assertEqual(oauth.pending_path(self.path).stat().st_mode & 0o777, 0o600)

    def test_invalid_tokens_from_an_unapproved_account_are_not_recorded(self):
        now = int(time.time())
        for changes in ({"exp": now - 1, "iat": now - 60}, {"aud": "wrong"}, {"iss": "https://evil.example/"},
                        {"nbf": now + 100}):
            self.refused({"sub": "google-oauth2|simon", **changes})

        self.assertEqual(oauth.list_accounts(self.path)[1], [])

    def test_approve_takes_effect_without_restart_and_revoke_ends_the_grant(self):
        self.refused({"sub": "google-oauth2|simon", "email": "simon@example.test"})

        oauth.approve_candidate(self.path, 1)
        grant = self.verify({"sub": "google-oauth2|simon"})
        approved, candidates = oauth.list_accounts(self.path)
        self.assertEqual((approved, candidates), (["dummy|jamie", "google-oauth2|simon"], []))
        self.assertNotIn("subject", json.loads(self.path.read_text()))

        oauth.revoke_subject(self.path, approved.index("google-oauth2|simon") + 1)
        self.assertFalse(self.auth.active_grant(grant))
        self.assertEqual(self.refused({"sub": "google-oauth2|simon"}), "subject_mismatch")

    def test_sign_in_without_email_needs_force_to_approve(self):
        self.refused({"sub": "google-oauth2|unknown"})

        with self.assertRaises(ValueError):
            oauth.approve_candidate(self.path, 1)
        self.assertEqual(oauth.list_accounts(self.path)[0], ["dummy|jamie"])

        oauth.approve_candidate(self.path, 1, force=True)
        self.assertIn("google-oauth2|unknown", oauth.list_accounts(self.path)[0])

    def test_empty_approved_list_loads_and_refuses_everyone(self):
        self.path.write_text(json.dumps({k: v for k, v in self.config.items() if k != "subject"}))

        self.assertEqual(self.refused({}), "subject_mismatch")
        self.assertEqual(oauth.list_accounts(self.path)[1][0]["subject"], "dummy|jamie")

    def test_bad_numbers_are_rejected_without_changing_config(self):
        before = self.path.read_text()

        for action in (lambda: oauth.approve_candidate(self.path, 1), lambda: oauth.revoke_subject(self.path, 2)):
            with self.assertRaises(ValueError):
                action()

        self.assertEqual(self.path.read_text(), before)

    def test_cli_lists_and_approves_a_refused_sign_in(self):
        self.refused({"sub": "google-oauth2|simon", "email": "simon@example.test"})
        env = {**os.environ, "HERALD_DIR": self.tmp.name}
        (Path(self.tmp.name) / "config.json").write_text(json.dumps({"me": "jamie", "peers": {},
                                                                    "mcp_bridge": {"oauth_config": str(self.path)}}))

        def run(*args):
            return subprocess.run([sys.executable, "-B", str(repo / "herald.py"), "mcp", *args], env=env,
                                  capture_output=True, text=True, timeout=30)

        listing = run("accounts")
        approval = run("approve", "1")

        self.assertIn("google-oauth2|simon  simon@example.test", listing.stdout)
        self.assertEqual(approval.returncode, 0, approval.stderr)
        self.assertIn("google-oauth2|simon", oauth.list_accounts(self.path)[0])


if __name__ == "__main__":
    unittest.main()
