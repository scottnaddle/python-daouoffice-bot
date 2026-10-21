"""DaouOffice Messenger Bot — REST API client layer.

DaouOffice is a multi-tenant SaaS: every company is served from its own
sub-domain (``https://<company>.daouoffice.com``) and identified by a numeric
``companyId``. Nothing in this module is hard-coded to a single tenant — the
base URL and company id are supplied by the caller (constructor argument or
environment variable). Use :meth:`BotClient.discover_company` /
:meth:`BotClient.whoami` (or the ``daoubot discover`` CLI) to look those values
up for your own account.

Unlike Telegram there is no BotFather: a "bot" is just a normal DaouOffice
account that an administrator issues for automation. You log in with that
account's id/password the same way the desktop messenger does.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

logger = logging.getLogger(__name__)

# DaouOffice PC messenger client User-Agent. The server gates the chat API on a
# messenger-looking UA, so we keep this shape by default; override if needed.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "dop-chat-front/4.3.3 Chrome/130.0.6723.137 "
    "Electron/33.2.1 Safari/537.36 DOP_PC_MESSENGER"
)


HTTP_OK = 200
HTTP_UNAUTHORIZED = 401


class DaouConfigError(ValueError):
    """Raised when required connection settings are missing."""


class DaouAuthError(RuntimeError):
    """Raised when login fails or the session is rejected.

    Attributes:
        code: The server error code (e.g. ``"PORTAL-0901"``).
    """

    code: str = ""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def _resolve(value: str | None, env: str) -> str | None:
    """Return ``value`` if set, otherwise fall back to environment ``env``."""
    if value:
        return value
    return os.getenv(env) or None


def _default_headers(base_url: str, user_agent: str) -> dict[str, str]:
    """Headers the DaouOffice server expects.

    ``X-Referer-Info`` is the tenant host; the unauthenticated public
    endpoints (e.g. ``/api/portal/public/auth/company``) use it to pick the
    tenant and return **400** without it (observed in the SAZ capture).
    """
    return {
        "User-Agent": user_agent,
        "X-Referer-Info": urlparse(base_url).hostname or "",
    }


def _safe_json(resp: httpx.Response) -> dict:
    """Parse a JSON object body; HTML error pages / 502s yield {} instead of
    raising JSONDecodeError past the DaouAuthError handling."""
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# ============================================================
# Models
# ============================================================


class ChatRoomItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    roomId: str
    roomName: str = ""
    roomType: str = ""  # "SINGLE" | "GROUP"
    roomMemberCount: int = 0
    unreadMessageCount: int = 0
    roomPinFlag: bool = False
    backgroundColor: str | None = None
    latestMessage: dict | None = None

    @property
    def latest_message_id(self) -> int | None:
        """The room's newest message id, independent of the unread badge.

        The badge is cleared by ``mark_read`` (which the bot itself calls),
        so it cannot tell whether the cursor is caught up; this can.
        """
        try:
            return int((self.latestMessage or {}).get("chatMessageId"))
        except (TypeError, ValueError):
            return None


class ChatHistoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chatRoomId: str = ""
    chatMessageId: int | str = ""
    createdAt: str = ""
    sender: dict = {}
    contents: dict = {}
    metadata: dict = {}
    messageStatus: dict | str = ""

    @field_validator("sender", "contents", "metadata", mode="before")
    @classmethod
    def _none_to_empty_dict(cls, v: object) -> object:
        # The server sends these as ``null`` for system/empty messages
        # (e.g. a member-left notice has no contents); treat as empty.
        return {} if v is None else v

    @field_validator("messageStatus", mode="before")
    @classmethod
    def _none_to_empty_str(cls, v: object) -> object:
        return "" if v is None else v


class RoomOpenData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    memberList: list[dict] = []
    lastReadMessageId: str = ""
    roomName: str = ""
    roomType: str = ""
    lastSentMessage: dict | None = None
    roomPushFlag: bool = True
    roomPinFlag: bool = False
    inputLockFlag: bool = False
    messageHistoryOpenFlag: bool = False
    backgroundColor: str | None = None


class OrganizationMember(BaseModel):
    """A user record from the organization tree.

    The organization tree returns mixed COMPANY / DEPARTMENT / MEMBER nodes;
    ``BotClient.get_user`` walks the tree and returns only the MEMBER entry
    whose ``userId`` matches the request. Fields here are the subset that
    capture-verified responses populate consistently — extra wire fields are
    ignored.
    """

    model_config = ConfigDict(extra="ignore")

    userId: str
    loginId: str = ""
    name: str = ""
    email: str = ""
    userStatus: str = ""
    employeeNumber: str | None = None
    positionName: str = ""
    dutyName: str = ""
    departmentId: str = ""
    departmentName: str = ""
    departmentNamePath: str = ""
    profileImagePath: str | None = None

    @field_validator("employeeNumber", "profileImagePath", mode="before")
    @classmethod
    def _none_passthrough(cls, v: object) -> object:
        return v

    @field_validator("loginId", "name", "email", "userStatus", "positionName",
                     "dutyName", "departmentId", "departmentName",
                     "departmentNamePath", mode="before")
    @classmethod
    def _none_to_empty(cls, v: object) -> object:
        return "" if v is None else v


# Inline mention token in content.message (see docs/api/03-messages.md §3.6):
#   {{<uuid>::USER::@<name>::<userId>}}   or   {{<uuid>::ALL::@ALL}}
_MENTION_RE = re.compile(
    r"\{\{[0-9a-fA-F-]+::(?P<type>USER|ALL)::@(?P<name>[^:}]+)(?:::(?P<uid>\d+))?\}\}"
)


def parse_mentions(text: str) -> tuple[str, list[str], bool]:
    """Parse mention tokens out of a raw message body.

    Returns ``(clean_text, mentioned_user_ids, mention_all)`` where
    ``clean_text`` has each token replaced by a human-readable ``@name``.
    """
    if not text or "{{" not in text:
        return text, [], False
    user_ids: list[str] = []
    mention_all = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal mention_all
        if m.group("type") == "ALL":
            mention_all = True
        elif m.group("uid"):
            user_ids.append(m.group("uid"))
        return f"@{m.group('name')}"

    clean = _MENTION_RE.sub(_sub, text).strip()
    return clean, user_ids, mention_all


def _filename_from_disposition(value: str) -> str:
    """Best-effort filename from a ``Content-Disposition`` header.

    Prefers RFC 5987 ``filename*=UTF-8''...`` (percent-decoded) over the
    plain quoted ``filename="..."``. Returns ``""`` if neither is present.
    """
    if not value:
        return ""
    ext = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", value)
    if ext:
        return unquote(ext.group(1).strip())
    plain = re.search(r'filename\s*=\s*"?([^";]+)"?', value)
    return plain.group(1).strip() if plain else ""


@dataclass(slots=True)
class NewMessage:
    """A single inbound chat message, normalized from the history payload.

    ``message_text`` is human-readable (mention tokens rendered as ``@name``);
    ``raw_text`` keeps the original wire text including ``{{...}}`` tokens.

    ``attachments`` carries the inbound ``attachmentList`` verbatim (each entry
    has ``filePath``/``fileName``/``fileSize``/``fileType``/...). A file-only
    message has empty ``message_text`` but a non-empty ``attachments``.
    """

    room_id: str
    room_type: str
    sender_user_id: str
    sender_name: str
    message_text: str
    message_id: str
    created_at: str
    raw_text: str = ""
    mentions: list[str] = field(default_factory=list)
    mentions_me: bool = False
    mention_all: bool = False
    attachments: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class BotIdentity:
    """The logged-in account's own identity, resolved at login time."""

    user_id: str
    name: str
    login_id: str
    company_id: str
    company_uuid: str
    company_domain: str


# ============================================================
# BotClient
# ============================================================


class BotClient:
    """DaouOffice Messenger REST API client.

    Args:
        login_id: Bot account login id.
        password: Bot account password.
        base_url: Tenant base URL, e.g. ``https://acme.daouoffice.com``.
            Falls back to the ``DAOU_BASE_URL`` environment variable.
        company_id: Numeric tenant company id. Falls back to the
            ``DAOU_COMPANY_ID`` environment variable. Not required for
            :meth:`discover_company`, but required for :meth:`login`.
        user_agent: Override the messenger User-Agent if necessary.
        timeout: Per-request timeout in seconds.

    Raises:
        DaouConfigError: If ``base_url`` is not provided via argument or env.
    """

    def __init__(
        self,
        login_id: str,
        password: str,
        *,
        base_url: str | None = None,
        company_id: str | None = None,
        access_token: str = "",
        refresh_token: str = "",
        on_auth: Callable[[BotClient], None] | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
    ) -> None:
        resolved_base = _resolve(base_url, "DAOU_BASE_URL")
        if not resolved_base:
            raise DaouConfigError(
                "base_url is required. Pass base_url=... or set the "
                "DAOU_BASE_URL environment variable "
                "(e.g. https://yourcompany.daouoffice.com)."
            )

        self._login_id = login_id
        self._password = password
        self._base_url = resolved_base.rstrip("/")
        self._company_id = _resolve(company_id, "DAOU_COMPANY_ID")
        self._client = httpx.Client(
            base_url=self._base_url,
            follow_redirects=True,
            http2=True,
            timeout=timeout,
            headers=_default_headers(self._base_url, user_agent),
        )
        self.access_token: str = access_token
        # 30-day RefreshToken: lets _refresh_session() mint a new
        # AccessToken without re-presenting the password.
        self.refresh_token: str = refresh_token
        self.identity: BotIdentity | None = None
        # Called after every successful login() (initial or 401-triggered),
        # so the owner can persist the refreshed token + identity.
        self._on_auth = on_auth

    @classmethod
    def from_token(
        cls,
        base_url: str,
        access_token: str,
        *,
        refresh_token: str = "",
        on_auth: Callable[[BotClient], None] | None = None,
        timeout: float = 30.0,
    ) -> BotClient:
        """Build a client from a previously obtained token (skips login).

        Validate it with :meth:`whoami`; an expired token raises on use.
        Pass ``refresh_token`` (and/or creds via env) so the 401 path can
        self-recover — refresh first if a RefreshToken exists, else full
        re-login if a password is available.
        """
        return cls(
            "",
            "",
            base_url=base_url,
            access_token=access_token,
            refresh_token=refresh_token,
            on_auth=on_auth,
            timeout=timeout,
        )

    @property
    def user_id(self) -> str:
        """The bot account's own platform user id (set after :meth:`login`)."""
        return self.identity.user_id if self.identity else ""

    # -- Authentication -------------------------------------------------

    def login(self) -> BotIdentity:
        """Log in, store the access token, and resolve the bot's identity.

        Returns:
            The resolved :class:`BotIdentity`.

        Raises:
            DaouConfigError: If ``company_id`` is unknown.
            DaouAuthError: If the credentials are rejected.
        """
        if not self._company_id:
            raise DaouConfigError(
                "company_id is required to log in. Pass company_id=..., set "
                "DAOU_COMPANY_ID, or run `daoubot discover` to look it up."
            )

        resp = self._client.post(
            "/api/portal/public/auth/login",
            json={
                "companyId": self._company_id,
                "loginId": self._login_id,
                "password": self._password,
                "captcha": "",
            },
        )

        body = _safe_json(resp)
        logger.debug("Login response: status=%d body=%s", resp.status_code, body)

        code = body.get("code", "")
        if not code and body.get("data") in ("OK", True):
            code = "SUCCESS-0000"

        # PORTAL-0901: 3-month password change required.
        # Try to delay the change via the change-delay API and retry login.
        # Only PORTAL-0901 is retryable. Retrying on any non-200 (bad
        # password, 403, 5xx) doubled failed attempts -> account lockout risk.
        if code == "PORTAL-0901":
            if not self._password:
                raise DaouAuthError(
                    "Login failed: PORTAL-0901 (password change required), "
                    "but no password available to delay. Run `daoubot login`.",
                    code=code or "PORTAL-0901",
                )
            self._try_password_change_delay(body)

            resp = self._client.post(
                "/api/portal/public/auth/login",
                json={
                    "companyId": self._company_id,
                    "loginId": self._login_id,
                    "password": self._password,
                    "captcha": "",
                },
            )
            body = _safe_json(resp)
            logger.debug("Login retry response: status=%d body=%s", resp.status_code, body)

            code = body.get("code", "")
            if not code and body.get("data") in ("OK", True):
                code = "SUCCESS-0000"

        if resp.status_code != HTTP_OK or code != "SUCCESS-0000":
            raise DaouAuthError(f"Login failed: {code or resp.text[:200]}", code=code or "")

        self.access_token = self._client.cookies.get("AccessToken", "")
        if not self.access_token:
            raise DaouAuthError("AccessToken cookie not present in login response")
        # 30-day RefreshToken (capture-verified): a missing value is not
        # fatal — full re-login still works — but without it the 401 path
        # cannot use the cheap refresh endpoint.
        self.refresh_token = self._client.cookies.get("RefreshToken", "")

        self.identity = self.whoami()
        logger.info(
            "Logged in as %s (user_id=%s, company=%s)",
            self.identity.login_id,
            self.identity.user_id,
            self.identity.company_domain or self._company_id,
        )
        if self._on_auth is not None:
            self._on_auth(self)  # persist the fresh token + identity
        return self.identity

    def get_auth_headers(self) -> dict[str, str]:
        parts = [f"AccessToken={self.access_token}"]
        if self.refresh_token:
            # The refresh endpoint requires it, and other endpoints tolerate
            # the extra cookie; send both so 401 → refresh works mid-request.
            parts.append(f"RefreshToken={self.refresh_token}")
        return {"Cookie": "; ".join(parts)}

    def _can_refresh(self) -> bool:
        return bool(self.refresh_token)

    def _can_relogin(self) -> bool:
        return bool(self._login_id and self._password and self._company_id)

    def _try_password_change_delay(self, login_body: dict) -> None:
        """Call the password-change-delay API when PORTAL-0901 is returned.

        The API returns ``{"data":"OK"}`` on success.  The ``changePeriod``
        field (e.g. 3) indicates how many months the delay is valid for.
        After a successful delay the caller is expected to retry login.
        """
        data = login_body.get("data")
        is_delayable = isinstance(data, dict) and data.get("isPasswordChangeDelayable")
        if not is_delayable:
            return
        logger.info("PORTAL-0901 detected — delaying password change requirement")
        try:
            self._client.put(
                "/api/portal/common/password/change-delay",
            )
        except Exception:
            logger.warning("password-change-delay API failed; retrying login anyway")

    def _refresh_session(self, failed_path: str) -> None:
        """Mint a fresh AccessToken from the RefreshToken (no password).

        Capture-verified contract (``POST /api/portal/public/auth/refresh/login``):
        send both token cookies and the absolute URL of the request that
        triggered the refresh as the body (Content-Type application/json,
        though the body is the bare URL — matches the PC client's wire
        format). Server returns 200 ``{"data":"OK"}`` and Set-Cookie:
        AccessToken (RefreshToken kept). The 30-day RefreshToken lets a
        long-running bot recover without re-presenting the password.

        Raises :class:`DaouAuthError` on any non-success so the 401 path
        falls back to full re-login.
        """
        url = f"{self._base_url}{failed_path}"
        resp = self._client.post(
            "/api/portal/public/auth/refresh/login",
            content=url.encode("utf-8"),
            headers={"Content-Type": "application/json", **self.get_auth_headers()},
        )
        body = resp.json() if resp.content else {}
        if resp.status_code != HTTP_OK:
            raise DaouAuthError(
                f"Refresh HTTP {resp.status_code}: {resp.text[:200]}",
                code=body.get("code", ""),
            )
        refresh_code = body.get("code", "SUCCESS-0000")
        if body.get("data") != "OK" and refresh_code != "SUCCESS-0000":
            # PORTAL-0901 during refresh: delay password change and retry
            if refresh_code == "PORTAL-0901" and self._password:
                self._try_password_change_delay(body)
                raise DaouAuthError(
                    "PORTAL-0901 — retrying login after password-change delay",
                    code="PORTAL-0901",
                )
            raise DaouAuthError(f"Refresh rejected: {body}", code=refresh_code)
        new_access = self._client.cookies.get("AccessToken", "")
        if not new_access:
            raise DaouAuthError("Refresh response did not set a new AccessToken")
        self.access_token = new_access
        # The capture re-sets RefreshToken with the same JWT iat (kept, not
        # rotated); reread it regardless so a future rotation is honored.
        self.refresh_token = self._client.cookies.get("RefreshToken", self.refresh_token)
        if self._on_auth is not None:
            self._on_auth(self)

    def _api(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated request; on 401, refresh once (cheap) before
        falling back to a full re-login (heavy) if a password is known.

        The AccessToken lives ~30 minutes. The 30-day RefreshToken mints a
        new AccessToken without re-presenting the password; only when the
        RefreshToken is missing or itself rejected do we fall back to a
        password login. A fresh login is a new server session and does not
        disturb other sessions.
        """
        headers = {**kwargs.pop("headers", {}), **self.get_auth_headers()}
        resp = self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code != HTTP_UNAUTHORIZED:
            return resp

        recovered = False
        if self._can_refresh():
            try:
                logger.info("401 (%s) — refreshing session", path)
                self._refresh_session(path)
                recovered = True
            except DaouAuthError as e:
                logger.info("Refresh failed (%s); falling back to re-login", e)
        if not recovered:
            if not self._can_relogin():
                raise DaouAuthError(
                    "Session expired (401), refresh unavailable, and no "
                    "credentials to re-authenticate. Set DAOU_PASSWORD for "
                    "unattended re-login, or run `daoubot login` again."
                )
            logger.info("401 (%s) — session expired, re-authenticating", path)
            self.login()  # persists the new tokens via on_auth
        headers = {**headers, **self.get_auth_headers()}
        return self._client.request(method, path, headers=headers, **kwargs)

    def whoami(self) -> BotIdentity:
        """Resolve the logged-in account's own identity via GraphQL ``me``."""
        resp = self._client.post(
            "/api/portal/graphql",
            json={
                "operationName": "userSessionQuery",
                "query": (
                    "query userSessionQuery { me { id name loginId "
                    "company { id uuid domain name } } }"
                ),
                "variables": {},
            },
            headers=self.get_auth_headers(),
        )
        resp.raise_for_status()
        me = (resp.json().get("data") or {}).get("me") or {}
        company = me.get("company") or {}
        return BotIdentity(
            user_id=str(me.get("id", "")),
            name=me.get("name", ""),
            login_id=me.get("loginId", self._login_id),
            company_id=str(company.get("id", self._company_id or "")),
            company_uuid=company.get("uuid", ""),
            company_domain=company.get("domain", ""),
        )

    @classmethod
    def discover_company(cls, base_url: str, *, timeout: float = 15.0) -> dict:
        """Fetch tenant metadata from the public (no-auth) company endpoint.

        Useful for finding ``companyId`` from a bare tenant URL.
        """
        url = (base_url or "").rstrip("/")
        if not url:
            raise DaouConfigError("base_url is required for discover_company")
        with httpx.Client(
            base_url=url,
            follow_redirects=True,
            timeout=timeout,
            headers=_default_headers(url, DEFAULT_USER_AGENT),
        ) as client:
            resp = client.get("/api/portal/public/auth/company")
            resp.raise_for_status()
            body = resp.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        # Response shape: {"data": {"companyList": [{companyId, uuid, ...}]}}
        if isinstance(data, dict) and data.get("companyList"):
            return data["companyList"][0]
        return data

    def logout(self) -> None:
        try:
            self._client.post(
                "/api/portal/common/auth/logout",
                headers=self.get_auth_headers(),
            )
        except Exception:
            pass
        finally:
            self._client.close()

    # -- Chat rooms -----------------------------------------------------

    def get_rooms(self, *, page: int = 0, size: int = 20) -> list[ChatRoomItem]:
        resp = self._api(
            "GET",
            "/api/chat/room",
            params={"pageNumber": page, "pageSize": size},
        )
        if resp.status_code != HTTP_OK:
            logger.error("get_rooms: %s %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
        elements = resp.json().get("data", {}).get("elements", [])
        return [ChatRoomItem(**item) for item in elements]

    def create_room(
        self,
        user_list: list[str],
        *,
        room_name: str | None = None,
        room_type: str = "SINGLE",
        background_color: str = "#00FFFF",
    ) -> str:
        """Create a chat room and return its ``roomId``."""
        body: dict = {
            "userList": user_list,
            "roomType": room_type,
            "backgroundColor": background_color,
        }
        if room_name:
            body["roomName"] = room_name
        resp = self._api("POST", "/api/chat/room", json=body)
        resp.raise_for_status()
        return resp.json()["data"]["roomId"]

    def open_room(self, room_id: str) -> RoomOpenData:
        resp = self._api("GET", f"/api/chat/room/{room_id}/open")
        resp.raise_for_status()
        return RoomOpenData(**resp.json()["data"])

    # -- Messages -------------------------------------------------------

    def send_message(
        self,
        room_id: str,
        content: str = "",
        *,
        attachments: list[dict] | None = None,
        reply_to: str | None = None,
    ) -> str:
        """Send a message (optionally with uploaded attachments).

        ``attachments`` is a list of dicts returned by
        :meth:`upload_attachment`. ``reply_to`` is the ``chatMessageId`` of an
        existing message to post this as a threaded reply to (the inbound
        :attr:`NewMessage.message_id`); the client renders a quote of it.
        Returns the client message id (``cmid``).
        """
        cmid = str(uuid.uuid4())
        body: dict = {"message": content}
        if reply_to:
            body["parentChatMessageId"] = str(reply_to)
        if attachments:
            body["attachmentList"] = [self._attachment_entry(a) for a in attachments]
        resp = self._api(
            "POST",
            "/api/chat/message",
            json={"chatRoomId": room_id, "cmid": cmid, "content": body},
        )
        resp.raise_for_status()
        return resp.json()["data"]["cmid"]

    def upload_attachment(self, path: str | os.PathLike[str]) -> dict:
        """Upload a file for chat and return its attachment metadata.

        The returned dict (server ``data``: ``filePath``/``fileName``/
        ``fileSize``/...) is passed to :meth:`send_message` via
        ``attachments=[...]`` or used by :meth:`send_file`.
        """
        p = Path(path)
        data = p.read_bytes()
        resp = self._api(
            "POST",
            "/api/upload/attach/app",
            params={"app-code": "dop-default-chat", "thumb-category": "attach"},
            files={"file": (p.name, data, "application/octet-stream")},
        )
        resp.raise_for_status()
        return resp.json()["data"]

    def send_file(self, room_id: str, path: str | os.PathLike[str], content: str = "") -> str:
        """Upload ``path`` and post it to ``room_id`` as an attachment."""
        meta = self.upload_attachment(path)
        return self.send_message(room_id, content, attachments=[meta])

    def _attachment_entry(self, meta: dict) -> dict:
        """Build a chat ``attachmentList`` entry from upload metadata."""
        ident = self.identity
        sender = {
            "companyUuid": ident.company_uuid if ident else "",
            "platformUserId": ident.user_id if ident else "",
            "platformUserName": ident.name if ident else "",
            "profilePath": "",
        }
        return {
            "attachmentId": -1,
            "filePath": meta["filePath"],
            "fileType": "",
            "fileName": meta["fileName"],
            "fileSize": meta.get("fileSize", 0),
            "fileStatus": "UPLOADED",
            "sender": sender,
            "createdAt": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }

    def attachment_url(self, attachment: dict) -> str:
        """Absolute download URL for an inbound chat attachment.

        ``attachment`` is one entry from :attr:`NewMessage.attachments`
        (the server's ``attachmentList``). The URL requires the bot's
        session (the same auth :meth:`download_attachment` applies) — it is
        a canonical reference, not an anonymously fetchable link.
        """
        aid = attachment.get("attachmentId")
        if aid in (None, "", -1, "-1"):
            raise ValueError(
                f"attachment has no usable attachmentId (got {aid!r}); cannot build a download URL"
            )
        return f"{self._base_url}/api/chat/attachment/{aid}/download"

    def download_attachment(
        self, attachment: dict, dest: str | os.PathLike[str] | None = None
    ) -> Path:
        """Download an inbound chat attachment to disk and return its path.

        ``dest`` may be a directory (the server/known filename is appended),
        a full file path, or ``None`` (filename in the current directory).
        Authentication and 401 re-login are handled by :meth:`_api`.
        """
        aid = attachment.get("attachmentId")
        if aid in (None, "", -1, "-1"):
            raise ValueError(f"attachment has no usable attachmentId (got {aid!r})")
        resp = self._api("GET", f"/api/chat/attachment/{aid}/download")
        resp.raise_for_status()

        name = (
            attachment.get("fileName")
            or _filename_from_disposition(resp.headers.get("content-disposition", ""))
            or str(aid)
        )
        target = Path(dest) if dest is not None else Path(name)
        if target.is_dir():
            target = target / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resp.content)
        return target

    def get_chat_history(
        self, room_id: str, *, offset: int = 20, message_id: int = 0
    ) -> list[ChatHistoryItem]:
        resp = self._api(
            "GET",
            f"/api/chat/room/{room_id}/chat/range",
            params={"offset": offset, "messageId": message_id},
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if isinstance(data, list):
            items = data
        else:
            items = data.get("items") or data.get("elements") or []
        return [ChatHistoryItem(**item) for item in items]

    def mark_read(self, message_id: int | str, room_id: str) -> None:
        """Mark the room read up to ``message_id``.

        The ``{"chatRoomId": ...}`` body is required: without it the server
        still answers 200 but does not register the read, so no read receipt
        reaches the sender (capture-verified — see docs §3.3).
        """
        self._api(
            "POST",
            f"/api/chat/message/{message_id}/read",
            json={"chatRoomId": str(room_id)},
        )

    # -- Organization / users ------------------------------------------

    def get_user(self, user_id: str | int) -> OrganizationMember | None:
        """Resolve a platform user id to a corporate record.

        Uses ``GET /api/portal/common/organization/tree`` with
        ``targetUserId`` so the response expands the tree around the
        requested user. The endpoint returns the target user plus their
        department peers; this method walks the tree and returns the MEMBER
        node whose ``userId`` matches the request. Returns ``None`` when the
        user is not present (e.g. removed, inactive, or outside the visible
        org scope).

        The response also includes peer members — callers that want to
        warm a directory cache can use :meth:`get_user_peers` instead.
        """
        target = str(user_id)
        if not target:
            return None
        tree = self._fetch_user_tree(target)
        for member in _walk_member_nodes(tree):
            if str(member.get("userId", "")) == target:
                return OrganizationMember.model_validate(member)
        return None

    def get_user_peers(self, user_id: str | int) -> list[OrganizationMember]:
        """Return every MEMBER record reachable from a ``targetUserId`` query.

        Useful for bulk-populating a user-directory cache: a single HTTP
        call typically yields the target user plus all of their direct
        department colleagues.
        """
        target = str(user_id)
        if not target:
            return []
        tree = self._fetch_user_tree(target)
        return [
            OrganizationMember.model_validate(member)
            for member in _walk_member_nodes(tree)
        ]

    def get_organization_members(self) -> list[OrganizationMember]:
        """Return every member in the tenant's organization chart.

        Walks the department tree from the root, visiting each department
        once, and keeps one record per person so secondments listed under
        several departments are not counted twice.

        This costs one request per department, so cache the result if you
        look people up often. For the much cheaper "who sits near this
        person" query, use :meth:`get_user_peers` instead.
        """
        found: dict[str, OrganizationMember] = {}
        visited: set[str] = set()

        def walk(department_id: str) -> None:
            for node in _walk_tree_nodes(self._fetch_department_tree(department_id)):
                node_type = node.get("nodeType")
                if node_type == "MEMBER":
                    member = OrganizationMember.model_validate(node)
                    if member.userId:
                        found.setdefault(member.userId, member)
                    continue
                if node_type != "DEPARTMENT":
                    continue
                child = str(node.get("departmentId") or node.get("id") or "").strip()
                if child and child not in visited:
                    visited.add(child)
                    walk(child)

        walk("")
        return sorted(
            found.values(), key=lambda member: (member.departmentNamePath, member.name)
        )

    def _fetch_user_tree(self, target_user_id: str) -> list[dict]:
        return self._fetch_tree({"targetUserId": target_user_id})

    def _fetch_department_tree(self, root_department_id: str) -> list[dict]:
        return self._fetch_tree({"rootDepartmentId": root_department_id})

    def _fetch_tree(self, selector: dict[str, str]) -> list[dict]:
        resp = self._api(
            "GET",
            "/api/portal/common/organization/tree",
            params={**selector, "shouldApplyOrganizationChartExpansion": "true"},
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if isinstance(data, dict):
            elements = data.get("elements") or []
        elif isinstance(data, list):
            elements = data
        else:
            elements = []
        return elements


# Nested node lists in the organization tree. ``unspecifiedMemberList`` holds
# people who are not filed under a department; skipping it loses them.
_NESTED_NODE_KEYS = ("childrenList", "unspecifiedMemberList")


def _walk_tree_nodes(nodes: list[dict]):
    """Yield every node reachable from ``nodes``, parents before children.

    The organization tree mixes COMPANY / DEPARTMENT / MEMBER nodes; recursion
    is bounded by the tenant's department depth and stays cheap in practice.
    """
    if not nodes:
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        for key in _NESTED_NODE_KEYS:
            nested = node.get(key)
            if isinstance(nested, list) and nested:
                yield from _walk_tree_nodes(nested)


def _walk_member_nodes(nodes: list[dict]):
    """Yield every ``nodeType == 'MEMBER'`` dict reachable from ``nodes``."""
    for node in _walk_tree_nodes(nodes):
        if node.get("nodeType") == "MEMBER":
            yield node
