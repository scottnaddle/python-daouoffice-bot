"""DaouOffice bot command-line interface (``daoubot``).

Onboarding flow for SDK developers::

    daoubot login --base-url https://acme.daouoffice.com \\
        --login-id my-bot --password '...'        # → saves ~/.daoubot/profile.yaml

    daoubot whoami                                # company + bot identity
    daoubot config                                # view the saved profile (masked)
    daoubot config set base_url <url>             # edit a connection field
    daoubot rooms                                 # list rooms (with room ids)
    daoubot room create --users 110...,110... --name "Bot Test"
    daoubot room open <room_id>                   # members + detail
    daoubot send <room_id> "hello"

The bot itself is a Python program (``DaouBot(on_message=...)`` run as
``python my_bot.py``) — the CLI cannot carry a handler, so there is no
``start`` subcommand; see the README / examples.

After ``login`` the saved session token is reused, so later commands need no
credentials. Settings precedence: CLI flag > environment variable > app config
(``--app-config`` / ``DAOU_APP_CONFIG``, the operator's own YAML's
``daouoffice:`` section, read-only) > profile.
Connection env vars: DAOU_BASE_URL, DAOU_COMPANY_ID, DAOU_LOGIN_ID, DAOU_PASSWORD.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import sys

import httpx

from daouoffice import BotClient
from daouoffice.client import DaouAuthError, DaouConfigError
from daouoffice.config import load_app_config
from daouoffice.profile import Profile, load_profile, profile_path, save_profile


def _pick(
    flag: str | None,
    env: str,
    prof: str | None,
    *,
    app: str | None = None,
) -> str | None:
    """Resolve a setting: CLI flag > env > app config > profile.

    The optional ``app`` tier comes from ``--app-config`` / ``DAOU_APP_CONFIG``
    (an operator-owned YAML's ``daouoffice:`` section). Sites that don't
    care about that tier just omit the kwarg and behave as before.
    """
    return flag or os.getenv(env) or app or (prof or None)


def _app(args: argparse.Namespace) -> dict[str, str]:
    """``daouoffice:`` section of the ``--app-config`` YAML, or ``{}``.

    Cheap to call repeatedly: small file, infrequent CLI invocations.
    """
    path = getattr(args, "app_config", None) or os.getenv("DAOU_APP_CONFIG")
    return load_app_config(path) if path else {}


def _die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _resolve_password(args: argparse.Namespace, profile_pw: str | None = None) -> str | None:
    """Password: --password > DAOU_PASSWORD > app config > saved profile > hidden prompt.

    The profile (chmod 600, gitignored) keeps the password so commands
    re-authenticate unattended when the token expires. Prompting (TTY) is the
    last resort — it keeps the secret out of argv (`ps` / shell history) and
    sidesteps shell quoting of special chars like ``!``.
    """
    pw = _pick(args.password, "DAOU_PASSWORD", profile_pw, app=_app(args).get("password"))
    if pw:
        return pw
    if sys.stdin.isatty():
        return getpass.getpass("DaouOffice password: ") or None
    return None


def _resolve_login_id(args: argparse.Namespace) -> str | None:
    """Login id: --login-id > DAOU_LOGIN_ID > app config > visible prompt (TTY only)."""
    lid = _pick(args.login_id, "DAOU_LOGIN_ID", None, app=_app(args).get("login_id"))
    if lid:
        return lid
    if sys.stdin.isatty():
        return input("DaouOffice login id: ").strip() or None
    return None


def _cfg(args: argparse.Namespace) -> str | None:
    """The --config profile path, if given."""
    return getattr(args, "config", None)


def _settings(args: argparse.Namespace) -> tuple[Profile | None, str, str | None]:
    """Return (profile, base_url, company_id), erroring if base_url is unknown."""
    prof = load_profile(path=_cfg(args))
    app = _app(args)
    base_url = _pick(
        args.base_url, "DAOU_BASE_URL", prof.base_url if prof else None, app=app.get("base_url")
    )
    if not base_url:
        _die(
            "base_url unknown — pass --base-url, set DAOU_BASE_URL, "
            "point --app-config / DAOU_APP_CONFIG at a YAML with a "
            "'daouoffice:' section, or run `daoubot login`"
        )
    company_id = _pick(
        args.company_id,
        "DAOU_COMPANY_ID",
        prof.company_id if prof else None,
        app=app.get("company_id"),
    )
    return prof, base_url, company_id


def _authed_client(args: argparse.Namespace) -> BotClient:
    """A logged-in client: reuse the saved token, else log in with creds.

    Any (re-)login persists the fresh token back to the profile via on_auth,
    so subsequent commands reuse it instead of re-authenticating.
    """
    prof, base_url, company_id = _settings(args)
    cfg = _cfg(args)

    def _persist(c: BotClient) -> None:
        _store(c, base_url, cfg)

    if prof and prof.access_token:
        # Keep the saved credentials on the client: from_token() drops them,
        # so a mid-run 401 could not re-login and _store() then persisted
        # password="" — silently erasing the saved password from the profile.
        client = BotClient(
            prof.login_id or "",
            _pick(args.password, "DAOU_PASSWORD", prof.password,
                  app=_app(args).get("password")) or "",  # no TTY prompt here
            base_url=base_url,
            company_id=company_id,
            access_token=prof.access_token,
            refresh_token=prof.refresh_token,
            on_auth=_persist,
        )
        try:
            client.identity = client.whoami()
            return client
        except (httpx.HTTPError, DaouAuthError):
            print("saved session expired — re-authenticating...", file=sys.stderr)

    login_id = _pick(
        args.login_id,
        "DAOU_LOGIN_ID",
        prof.login_id if prof else None,
        app=_app(args).get("login_id"),
    )
    password = _resolve_password(args, prof.password if prof else None)
    if not (login_id and company_id):
        _die("no profile found — run `daoubot login` first")
    if not password:
        _die(
            "session expired; password required to re-authenticate "
            "(set DAOU_PASSWORD or run interactively)"
        )
    client = BotClient(
        login_id, password, base_url=base_url, company_id=company_id, on_auth=_persist
    )
    client.login()
    return client


def _store(client: BotClient, base_url: str, config_path: str | None = None) -> None:
    ident = client.identity
    if ident is None:
        return
    save_profile(
        Profile(
            base_url=base_url,
            company_id=ident.company_id,
            company_uuid=ident.company_uuid,
            company_domain=ident.company_domain,
            login_id=ident.login_id,
            user_id=ident.user_id,
            name=ident.name,
            access_token=client.access_token,
            refresh_token=client.refresh_token,
            password=client._password,
        ),
        path=config_path,
    )


# -- commands -----------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> None:
    app = _app(args)
    base_url = _pick(args.base_url, "DAOU_BASE_URL", None, app=app.get("base_url"))
    if not base_url:
        _die("--base-url (or DAOU_BASE_URL / --app-config) is required for login")
    # Ask for the missing required values in a natural order — login id
    # first, then the password — instead of prompting for the password and
    # only then failing because the login id was never given.
    login_id = _resolve_login_id(args)
    if not login_id:
        _die("--login-id (or DAOU_LOGIN_ID, or run interactively) is required for login")
    password = _resolve_password(args)
    if not password:
        _die("a password (--password / DAOU_PASSWORD / prompt) is required for login")

    company_id = _pick(args.company_id, "DAOU_COMPANY_ID", None, app=app.get("company_id"))
    if not company_id:
        print("company_id not given — discovering from public endpoint...")
        try:
            info = BotClient.discover_company(base_url)
            company_id = str(info.get("id") or info.get("companyId") or "")
            if company_id:
                print(f"  resolved company_id={company_id}")
        except Exception as e:
            _die(f"could not auto-discover company_id ({e}); pass --company-id")

    client = BotClient(login_id, password, base_url=base_url, company_id=company_id)
    client.login()
    cfg = _cfg(args)
    _store(client, base_url, cfg)
    print(f"\nsaved → {profile_path(path=cfg)}\n")
    print(json.dumps(load_profile(path=cfg).public_dict(), indent=2, ensure_ascii=False))


# Profile fields a user may edit by hand. The rest (user_id, company_uuid,
# company_domain, access_token, refresh_token, saved_at) are resolved/managed
# by `login` and the daemon — editing them manually would only desync the
# profile.
_EDITABLE = ("base_url", "company_id", "login_id", "password")


def cmd_config(args: argparse.Namespace) -> None:
    """View or edit the saved profile (``~/.daoubot/profile.yaml``)."""
    cfg = _cfg(args)
    action = getattr(args, "config_action", None) or "show"

    if action == "path":
        print(profile_path(path=cfg))
        return

    prof = load_profile(path=cfg)
    if prof is None:
        _die(f"no profile at {profile_path(path=cfg)} — run `daoubot login` first")

    if action == "show":
        print(json.dumps(prof.public_dict(), indent=2, ensure_ascii=False))
        return

    # action == "set"
    key, value = args.key, args.value
    if value is None:
        if key == "password" and sys.stdin.isatty():
            value = getpass.getpass("New password: ")  # keep it out of argv
        else:
            _die(f"a value is required: `daoubot config set {key} <value>`")
    setattr(prof, key, value)
    save_profile(prof, path=cfg)
    print(f"updated {key} → {profile_path(path=cfg)}\n")
    print(json.dumps(prof.public_dict(), indent=2, ensure_ascii=False))


def cmd_whoami(args: argparse.Namespace) -> None:
    client = _authed_client(args)
    try:
        print(json.dumps(dataclasses.asdict(client.identity), indent=2, ensure_ascii=False))
    finally:
        client._client.close()


def cmd_rooms(args: argparse.Namespace) -> None:
    client = _authed_client(args)
    try:
        page_size = int(getattr(args, "page_size", 50) or 50)
        rooms = []
        page = 0
        while True:
            batch = client.get_rooms(page=page, size=page_size)
            if not batch:
                break
            rooms.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        # Room name last and untruncated: names mix Korean/ASCII and are the
        # only thing a human IDs a room by, so never clip them. Every column
        # before it is fixed-width, so the ragged name tail aligns regardless.
        print(f"\n{'#':>3}  {'type':6} {'mbr':>4} {'unread':>6}  {'room id':<19}  room name")
        print("-" * 78)
        for i, r in enumerate(rooms, 1):
            rtype = {"SINGLE": "1:1", "GROUP": "group"}.get(r.roomType, r.roomType)
            print(
                f"{i:>3}  {rtype:6} {r.roomMemberCount:>4} "
                f"{r.unreadMessageCount:>6}  {r.roomId:<19}  {r.roomName}"
            )
        print(f"\nTotal {len(rooms)} rooms\n")
    finally:
        client._client.close()


def cmd_room_create(args: argparse.Namespace) -> None:
    client = _authed_client(args)
    try:
        users = [u.strip() for u in args.users.split(",") if u.strip()]
        room_id = client.create_room(users, room_name=args.name, room_type=args.type)
        print(f"created room: {room_id}")
    finally:
        client._client.close()


def cmd_room_open(args: argparse.Namespace) -> None:
    client = _authed_client(args)
    try:
        data = client.open_room(args.room_id)
        print(json.dumps(data.model_dump(), indent=2, ensure_ascii=False))
    finally:
        client._client.close()


def cmd_send(args: argparse.Namespace) -> None:
    client = _authed_client(args)
    try:
        client.send_message(args.room_id, args.message)
        print(f"sent to {args.room_id}")
    finally:
        client._client.close()


def build_parser() -> argparse.ArgumentParser:
    # Connection options live on a shared parent applied to every
    # subcommand, so they work AFTER the subcommand
    # (`daoubot login --base-url ...`) — which is how all docs show it.
    # (argparse does not accept main-parser options after a subcommand.)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        help="profile file path (default: ~/.daoubot/profile.yaml); "
        "use a per-bot/tenant path for multiple accounts on one host",
    )
    common.add_argument(
        "--app-config",
        help="operator app config YAML (env DAOU_APP_CONFIG); the SDK reads "
        "its 'daouoffice:' section for connection values (read-only). Lets a "
        "downstream app keep base_url/login_id/password alongside its own "
        "settings in one declarative file instead of `daoubot login`.",
    )
    common.add_argument("--base-url", help="tenant URL (env DAOU_BASE_URL)")
    common.add_argument("--company-id", help="tenant company id (env DAOU_COMPANY_ID)")
    common.add_argument("--login-id", help="bot login id (env DAOU_LOGIN_ID)")
    common.add_argument(
        "--password",
        help="bot password (env DAOU_PASSWORD; omit to be prompted securely)",
    )

    parser = argparse.ArgumentParser(
        prog="daoubot",
        description="DaouOffice bot CLI — options go after the subcommand, "
        "e.g. `daoubot login --base-url ... --login-id ...`",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    add("login", "authenticate and save the profile")
    add("whoami", "print the saved bot identity")
    p_rooms = add("rooms", "list all chat rooms with their room ids")
    p_rooms.add_argument("--page-size", type=int, default=50, help="API page size (default: 50)")

    p_room = sub.add_parser("room", help="room operations").add_subparsers(
        dest="room_command", required=True
    )
    p_list = p_room.add_parser(
        "list", help="list all chat rooms with their room ids", parents=[common]
    )
    p_list.add_argument("--page-size", type=int, default=50, help="API page size (default: 50)")
    p_create = p_room.add_parser("create", help="create a chat room", parents=[common])
    p_create.add_argument("--users", required=True, help="comma-separated user ids")
    p_create.add_argument("--name", default=None)
    p_create.add_argument("--type", default="SINGLE", choices=("SINGLE", "GROUP"))
    p_open = p_room.add_parser("open", help="show room detail + members", parents=[common])
    p_open.add_argument("room_id")

    p_send = add("send", "send a message to a room")
    p_send.add_argument("room_id")
    p_send.add_argument("message")

    p_config = sub.add_parser("config", help="view/edit the saved profile", parents=[common])
    c_sub = p_config.add_subparsers(dest="config_action")  # default → show
    c_sub.add_parser("show", help="print the profile (secrets masked)", parents=[common])
    c_sub.add_parser("path", help="print the profile file path", parents=[common])
    c_set = c_sub.add_parser("set", help="set a connection field", parents=[common])
    c_set.add_argument("key", choices=_EDITABLE)
    c_set.add_argument("value", nargs="?", help="omit for password to be prompted")
    return parser


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "room":
        {
            "create": cmd_room_create,
            "open": cmd_room_open,
            "list": cmd_rooms,
        }[args.room_command](args)
        return
    {
        "login": cmd_login,
        "whoami": cmd_whoami,
        "rooms": cmd_rooms,
        "send": cmd_send,
        "config": cmd_config,
    }[args.command](args)


def _force_utf8_io() -> None:
    """Make stdout/stderr UTF-8.

    Korean Windows consoles default to cp949, so printing Korean JSON (room
    names, identity) or an em-dash in ``--help`` raises UnicodeEncodeError
    and crashes the CLI. Re-encode as UTF-8, replacing anything the console
    cannot render instead of raising.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> None:
    _force_utf8_io()
    args = build_parser().parse_args(argv)
    try:
        _dispatch(args)
    except (DaouConfigError, DaouAuthError) as e:
        _die(str(e))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
