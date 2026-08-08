"""Authentication: who may see the dashboard, and who gets the mail.

Users are *declared*, not registered. The registry is a JSON array loaded from a
mounted Secret, which keeps the collector as stateless as the rest of the bot --
no database, no signup flow, no password-reset surface to secure. To add someone
you edit the Secret; to remove them you edit it back.

The same record carries the address notifications go to, so there is exactly one
list of people rather than an access list and a mailing list that drift apart.

Credentials:
  * password -- scrypt, per-user random salt, verified in constant time
  * API token -- random, stored only as a SHA-256 hash, shown once at creation

Sessions live in memory (the collector is single-replica by design), so a restart
signs everyone out. That is the correct trade for a tool with no database.

Generate credentials with:
    python -m app.auth hash-password
    python -m app.auth new-token
    python -m app.auth new-user  admin@example.com --role admin
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException, Request

from app.config import Settings, get_settings
from app.models import Role, User

logger = logging.getLogger("k8s-defect-bot.auth")

# scrypt cost. n=2**14 keeps a verify near ~50ms on a small container CPU, which
# is slow enough to make offline cracking expensive and fast enough for a login.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _login_redirect(next_url: str) -> HTTPException:
    """A 303 to the login form, carrying where the caller was trying to go.

    Raised rather than returned because FastAPI dependencies cannot return a
    response. A plain HTTPException with a Location header is enough -- relying
    on an app-level exception handler would make the guard depend on how the
    app happens to be assembled.
    """
    from urllib.parse import quote

    target = "/login"
    if next_url and next_url != "/":
        target = f"/login?next={quote(next_url, safe='/')}"
    return HTTPException(
        status_code=303, detail="authentication required", headers={"Location": target}
    )


# --------------------------------------------------------------------------
# Password and token primitives
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32, maxmem=_SCRYPT_MAXMEM,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: Optional[str]) -> bool:
    """Constant-time verify. A malformed or missing hash is a failure, never a pass."""
    if not password or not encoded:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def new_api_token() -> tuple[str, str]:
    """Returns (plaintext, hash). Only the hash is ever stored."""
    token = "kdb_" + secrets.token_urlsafe(32)
    return token, hash_api_token(token)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


@dataclass
class Session:
    token: str
    email: str
    created_at: datetime
    last_seen_at: datetime
    ip: str = ""


@dataclass
class _LoginAttempts:
    failures: int = 0
    locked_until: Optional[datetime] = None


@dataclass
class AuthManager:
    settings: Optional[Settings] = None
    users: dict[str, User] = field(default_factory=dict)
    _sessions: dict[str, Session] = field(default_factory=dict)
    _attempts: dict[str, _LoginAttempts] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    load_error: Optional[str] = None

    # -- configuration -----------------------------------------------------

    def configure(self, settings: Settings) -> None:
        self.settings = settings
        self.users = {}
        self.load_error = None
        raw = self._read_registry(settings)
        if raw is None:
            return
        try:
            entries = json.loads(raw)
            if isinstance(entries, dict) and "users" in entries:
                entries = entries["users"]
            if not isinstance(entries, list):
                raise ValueError("expected a JSON array of users")
        except (json.JSONDecodeError, ValueError) as exc:
            # Refuse to silently fall back to "no users", which would mean
            # "no authentication" -- a typo in the Secret must not open the door.
            self.load_error = f"could not parse the user registry: {exc}"
            logger.error("%s -- refusing all logins until it is fixed", self.load_error)
            return

        plaintext_users: list[str] = []
        for entry in entries:
            try:
                user = User(**entry)
            except Exception as exc:
                logger.error("skipping malformed user entry: %s", exc)
                continue
            email = user.email.strip().lower()
            if not _EMAIL_RE.match(email):
                logger.error("skipping user with invalid email address: %r", user.email)
                continue

            if user.password and not user.password_hash:
                # Bootstrap path. Hash it now and drop the plaintext so it never
                # reaches a log line, an API response, or a traceback.
                user.password_hash = hash_password(user.password)
                plaintext_users.append(email)
            user.password = None

            if not user.password_hash and not user.api_token_hash:
                logger.error("skipping user %s: no password_hash and no api_token_hash", email)
                continue
            user.email = email
            self.users[email] = user

        if plaintext_users:
            logger.warning(
                "%s configured with a plaintext password. Anyone who can read the "
                "Secret can read it. Replace it with password_hash from "
                "`python -m app.auth hash-password`.",
                ", ".join(plaintext_users),
            )

        logger.info(
            "loaded %d user(s): %s",
            len(self.users),
            ", ".join(sorted(self.users)) or "none",
        )

    def _read_registry(self, settings: Settings) -> Optional[str]:
        if settings.auth_users:
            return settings.auth_users
        path = settings.auth_users_file
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read()
            except OSError as exc:
                self.load_error = f"could not read {path}: {exc}"
                logger.error("%s", self.load_error)
        return None

    @property
    def enabled(self) -> bool:
        """Auth is enforced once anyone is configured, or if the registry is broken.

        A broken registry counts as enabled-and-failing rather than disabled, so a
        malformed Secret locks the door instead of removing it.
        """
        settings = self.settings or get_settings()
        if not settings.auth_enabled:
            return False
        return bool(self.users) or self.load_error is not None

    # -- login -------------------------------------------------------------

    def _throttle_key(self, email: str, ip: str) -> str:
        return f"{email}|{ip}"

    def locked_out(self, email: str, ip: str) -> Optional[int]:
        """Seconds remaining on a lockout, or None."""
        settings = self.settings or get_settings()
        with self._lock:
            record = self._attempts.get(self._throttle_key(email, ip))
            if not record or not record.locked_until:
                return None
            remaining = (record.locked_until - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                self._attempts.pop(self._throttle_key(email, ip), None)
                return None
            return int(remaining) or 1

    def _record_failure(self, email: str, ip: str) -> None:
        settings = self.settings or get_settings()
        with self._lock:
            key = self._throttle_key(email, ip)
            record = self._attempts.setdefault(key, _LoginAttempts())
            record.failures += 1
            if record.failures >= settings.login_max_attempts:
                record.locked_until = datetime.now(timezone.utc) + timedelta(
                    seconds=settings.login_lockout_seconds
                )
                logger.warning(
                    "locking out %s from %s for %ss after %d failed attempts",
                    email, ip or "unknown", settings.login_lockout_seconds, record.failures,
                )

    def _clear_failures(self, email: str, ip: str) -> None:
        with self._lock:
            self._attempts.pop(self._throttle_key(email, ip), None)

    def authenticate(self, email: str, password: str, ip: str = "") -> Optional[User]:
        """Returns the user on success. Deliberately indistinguishable failures."""
        email = (email or "").strip().lower()
        user = self.users.get(email)

        # Hash even when the user does not exist, so response time doesn't
        # reveal which addresses are registered.
        stored = user.password_hash if user else None
        ok = verify_password(password, stored)
        if not ok or user is None or user.disabled:
            self._record_failure(email, ip)
            return None
        self._clear_failures(email, ip)
        return user

    # -- session lifecycle -------------------------------------------------

    def create_session(self, user: User, ip: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self._lock:
            self._sessions[token] = Session(
                token=token, email=user.email, created_at=now, last_seen_at=now, ip=ip
            )
        return token

    def session_user(self, token: Optional[str]) -> Optional[User]:
        """Resolves a session cookie, enforcing both absolute and idle expiry."""
        if not token:
            return None
        settings = self.settings or get_settings()
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            too_old = (now - session.created_at).total_seconds() > settings.session_ttl_seconds
            idle = (
                now - session.last_seen_at
            ).total_seconds() > settings.session_idle_timeout_seconds
            if too_old or idle:
                self._sessions.pop(token, None)
                return None
            session.last_seen_at = now
            email = session.email

        user = self.users.get(email)
        if user is None or user.disabled:
            # The Secret changed under a live session; drop it.
            self.destroy_session(token)
            return None
        return user

    def destroy_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def sessions_for(self, email: str) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.email == email)

    def purge_expired(self) -> int:
        settings = self.settings or get_settings()
        now = datetime.now(timezone.utc)
        with self._lock:
            dead = [
                token for token, s in self._sessions.items()
                if (now - s.created_at).total_seconds() > settings.session_ttl_seconds
                or (now - s.last_seen_at).total_seconds() > settings.session_idle_timeout_seconds
            ]
            for token in dead:
                self._sessions.pop(token, None)
        return len(dead)

    # -- API tokens --------------------------------------------------------

    def user_for_api_token(self, token: Optional[str]) -> Optional[User]:
        if not token:
            return None
        presented = hash_api_token(token)
        for user in self.users.values():
            if user.disabled or not user.api_token_hash:
                continue
            if hmac.compare_digest(presented, user.api_token_hash):
                return user
        return None

    # -- notification routing ---------------------------------------------

    def recipients(self) -> list[User]:
        """Users who should receive mail, in a stable order."""
        from app.models import NotifyMode

        return sorted(
            (
                u for u in self.users.values()
                if not u.disabled and u.notify.mode != NotifyMode.OFF
            ),
            key=lambda u: u.email,
        )


auth = AuthManager()


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    # X-Forwarded-For is set by an ALB; fall back to the socket peer.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


async def current_user(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Optional[User]:
    """Resolves a caller from a bearer API token or a session cookie. Never raises."""
    if not auth.enabled:
        return None
    settings = auth.settings or get_settings()

    if authorization and authorization.lower().startswith("bearer "):
        user = auth.user_for_api_token(authorization.split(" ", 1)[1])
        if user:
            return user

    return auth.session_user(request.cookies.get(settings.session_cookie_name))


async def require_user(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Optional[User]:
    """Route guard. Browsers get a redirect to /login; API callers get a 401."""
    if not auth.enabled:
        return None

    user = await current_user(request, authorization)
    if user is not None:
        request.state.user = user
        return user

    if auth.load_error:
        raise HTTPException(
            status_code=503,
            detail=f"authentication is misconfigured: {auth.load_error}",
        )

    wants_html = "text/html" in (request.headers.get("accept") or "") and not (
        request.url.path.startswith("/api")
    )
    if wants_html:
        raise _login_redirect(request.url.path)
    raise HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Optional[User]:
    user = await require_user(request, authorization)
    if user is None:
        return None  # auth disabled entirely
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="this action requires the admin role")
    return user


# --------------------------------------------------------------------------
# Credential CLI
# --------------------------------------------------------------------------


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import getpass

    parser = argparse.ArgumentParser(
        prog="python -m app.auth",
        description="Generate credentials for the k8s-defect-bot user registry.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    hp = sub.add_parser("hash-password", help="hash a password for users.json")
    hp.add_argument("password", nargs="?", help="omit to be prompted (does not echo)")

    sub.add_parser("new-token", help="generate an API token and its hash")

    nu = sub.add_parser("new-user", help="print a complete users.json entry")
    nu.add_argument("email")
    nu.add_argument("--name", default="")
    nu.add_argument("--role", default="viewer", choices=[r.value for r in Role])
    nu.add_argument("--password", help="omit to be prompted")
    nu.add_argument("--with-token", action="store_true", help="also mint an API token")

    args = parser.parse_args(argv)

    if args.command == "hash-password":
        password = args.password or getpass.getpass("Password: ")
        print(hash_password(password))
        return 0

    if args.command == "new-token":
        token, digest = new_api_token()
        print(f"token (store this now, it is not recoverable):\n  {token}\n")
        print(f'api_token_hash (put this in users.json):\n  "{digest}"')
        return 0

    if args.command == "new-user":
        password = args.password or getpass.getpass("Password: ")
        entry = {
            "email": args.email,
            "name": args.name or args.email.split("@")[0],
            "role": args.role,
            "password_hash": hash_password(password),
            "notify": {"mode": "immediate", "min_severity": "critical"},
        }
        token = None
        if args.with_token:
            token, entry["api_token_hash"] = new_api_token()
        print(json.dumps([entry], indent=2))
        if token:
            print(f"\n# API token (store now, not recoverable): {token}", flush=True)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
