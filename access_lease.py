"""Signed access protocol shared by the authority and manager installations.

The authority keeps its Ed25519 private key. Installations receive only its
pinned public key. Transport must call accept() only for a fresh response to
its own request; background polling and bot integration are separate concerns.
"""
import base64
import json
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

OFFLINE_SECONDS = 24 * 60 * 60
POLL_SECONDS = 5 * 60


class AccessDenied(RuntimeError):
    pass


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def issue(private_key, *, subject, nonce, allowed, now=None):
    """Run on the authority only, after looking up the current permission."""
    now = int(time.time() if now is None else now)
    if type(allowed) is not bool or not subject or not nonce:
        raise ValueError("Invalid access response")
    payload = dict(version=1, subject=subject, nonce=nonce, allowed=allowed,
                   issued_at=now, expires_at=now + OFFLINE_SECONDS)
    return dict(payload=payload, signature=base64.b64encode(
        private_key.sign(canonical(payload))).decode("ascii"))


class LeaseStore:
    def __init__(self, path, public_key, subject, clock=time.time):
        self.path = Path(path)
        self.key = Ed25519PublicKey.from_public_bytes(public_key)
        self.subject = subject
        self.clock = clock

    def _verify(self, envelope):
        try:
            payload = envelope["payload"]
            signature = base64.b64decode(envelope["signature"], validate=True)
            self.key.verify(signature, canonical(payload))
            if (payload["version"] != 1 or payload["subject"] != self.subject
                    or type(payload["allowed"]) is not bool
                    or type(payload["issued_at"]) is not int
                    or type(payload["expires_at"]) is not int
                    or payload["expires_at"] - payload["issued_at"] != OFFLINE_SECONDS
                    or not isinstance(payload["nonce"], str)
                    or not payload["nonce"]):
                raise ValueError("Invalid payload")
            return payload
        except Exception as exc:
            raise AccessDenied("Access response is invalid") from exc

    def accept(self, envelope, nonce):
        payload = self._verify(envelope)
        now = self.clock()
        if payload["nonce"] != nonce or not nonce:
            raise AccessDenied("Access response belongs to another request")
        if not payload["issued_at"] <= now < payload["expires_at"]:
            raise AccessDenied("Access response is expired or clock is incorrect")
        # Refuse older responses when a newer grant or revocation is cached.
        if self.path.exists():
            try:
                previous = self._verify(json.loads(self.path.read_text("utf-8")))
            except (AccessDenied, OSError, ValueError, TypeError):
                # A fresh signed response can repair a corrupt cache. Invalid
                # local data must neither grant access nor block recovery.
                previous = None
            if previous and payload["issued_at"] < previous["issued_at"]:
                raise AccessDenied("Access response is older than cached permission")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(envelope, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def require_access(self):
        try:
            payload = self._verify(json.loads(self.path.read_text("utf-8")))
        except AccessDenied:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise AccessDenied("No valid cached access permission") from exc
        now = self.clock()
        if not payload["allowed"]:
            raise AccessDenied("Access has been revoked")
        if now < payload["issued_at"]:
            raise AccessDenied("Clock is earlier than the last access check")
        if now >= payload["expires_at"]:
            raise AccessDenied("Offline permission expired; reconnect to the authority")
        return payload
