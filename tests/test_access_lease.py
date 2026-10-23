import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from access_lease import AccessDenied, LeaseStore, OFFLINE_SECONDS, issue


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.now = 100000
        self.path = Path(self.tmp.name) / "access.json"
        self.store = self.restart()

    def restart(self):
        return LeaseStore(self.path, self.public, "manager:42", lambda: self.now)

    def response(self, allowed=True, nonce="request-1"):
        return issue(self.key, subject="manager:42", nonce=nonce,
                     allowed=allowed, now=self.now)

    def test_restart_does_not_extend_expiration(self):
        self.store.accept(self.response(), "request-1")
        self.now += OFFLINE_SECONDS - 1
        self.restart().require_access()
        self.now += 1
        with self.assertRaises(AccessDenied):
            self.restart().require_access()

    def test_revocation_persists_across_restart(self):
        self.store.accept(self.response(), "request-1")
        self.now += 300
        self.store.accept(self.response(False, "request-2"), "request-2")
        with self.assertRaisesRegex(AccessDenied, "revoked"):
            self.restart().require_access()

    def test_only_authority_can_extend_permission(self):
        response = self.response()
        self.store.accept(response, "request-1")
        response["payload"]["expires_at"] += 10000
        self.path.write_text(__import__("json").dumps(response), encoding="utf-8")
        with self.assertRaises(AccessDenied):
            self.store.require_access()

    def test_wrong_subject_key_and_request_are_rejected(self):
        for kind in ("subject", "key", "nonce"):
            response = self.response()
            if kind == "subject":
                response = issue(self.key, subject="manager:43", nonce="request-1",
                                 allowed=True, now=self.now)
            elif kind == "key":
                response = issue(Ed25519PrivateKey.generate(), subject="manager:42",
                                 nonce="request-1", allowed=True, now=self.now)
            else:
                response = self.response(nonce="old-request")
            with self.subTest(kind=kind), self.assertRaises(AccessDenied):
                self.store.accept(response, "request-1")

    def test_missing_corrupt_and_future_cache_fail_closed(self):
        with self.assertRaises(AccessDenied):
            self.store.require_access()
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(AccessDenied):
            self.store.require_access()
        self.path.unlink()
        self.store.accept(self.response(), "request-1")
        self.now -= 1
        with self.assertRaises(AccessDenied):
            self.store.require_access()

    def test_fresh_response_repairs_corrupt_cache(self):
        self.path.write_text("broken", encoding="utf-8")
        self.store.accept(self.response(), "request-1")
        self.store.require_access()

    def test_fresh_grant_extends_permission(self):
        self.store.accept(self.response(), "request-1")
        self.now += 1000
        self.store.accept(self.response(nonce="request-2"), "request-2")
        self.now += OFFLINE_SECONDS - 1
        self.store.require_access()

    def test_older_grant_cannot_overwrite_revocation(self):
        old = self.response()
        self.now += 300
        self.store.accept(self.response(False, "request-2"), "request-2")
        with self.assertRaises(AccessDenied):
            self.store.accept(old, "request-1")


if __name__ == "__main__":
    unittest.main()
