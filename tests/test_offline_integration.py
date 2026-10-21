"""Offline integration tests with mocked HTTP/WebSocket for verification.

These tests verify complete bot workflows without contacting real servers:
- Login flow with credential handling
- Room listing and polling
- Message send/receive
- Automatic re-authentication on token expiry
"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx

from daouoffice import DaouBot
from daouoffice.client import BotClient, ChatHistoryItem, ChatRoomItem
from daouoffice.engine import BotEngine
from daouoffice.profile import Profile, save_profile
from daouoffice.state import MemoryCursorStore


BASE = "https://test.daouoffice.com"
COMPANY_ID = "99000"


def _mock_login(router: respx.MockRouter, access_token: str = "tok-1"):
    """Mock successful login with token in Set-Cookie."""
    router.post("/api/portal/public/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"code": "SUCCESS-0000"},
            headers=[
                ("set-cookie", f"AccessToken={access_token}; Path=/; HttpOnly"),
                ("set-cookie", "RefreshToken=refresh-tok; Path=/; HttpOnly"),
            ],
        )
    )


def _mock_identity(router: respx.MockRouter, user_id: str = "11000001"):
    """Mock GraphQL me query for bot identity."""
    router.post("/api/portal/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "me": {
                        "id": user_id,
                        "name": "Test Bot",
                        "loginId": "testbot",
                        "company": {
                            "id": COMPANY_ID,
                            "uuid": "TEST-UUID",
                            "domain": "test",
                            "name": "Test Co",
                        },
                    }
                }
            },
        )
    )


@respx.mock
def test_offline_login_flow():
    """Test complete login flow with credential persistence."""
    _mock_login(respx.mock)
    _mock_identity(respx.mock)

    client = BotClient("testbot", "secret", base_url=BASE, company_id=COMPANY_ID)
    identity = client.login()

    assert identity.user_id == "11000001"
    assert identity.login_id == "testbot"
    assert identity.company_id == COMPANY_ID
    assert client.access_token == "tok-1"
    assert client.refresh_token == "refresh-tok"


@respx.mock
def test_offline_room_list_polling():
    """Test room listing returns unread rooms for polling."""
    _mock_login(respx.mock)
    _mock_identity(respx.mock)

    rooms_route = respx.get("/api/chat/room").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "elements": [
                        {
                            "roomId": "r1",
                            "roomName": "Test Room",
                            "roomType": "GROUP",
                            "unreadMessageCount": 2,
                        },
                        {
                            "roomId": "r2",
                            "roomName": "Read Room",
                            "roomType": "SINGLE",
                            "unreadMessageCount": 0,
                            "latestMessage": {"chatMessageId": "100"},
                        },
                    ]
                }
            },
        )
    )

    client = BotClient("testbot", "secret", base_url=BASE, company_id=COMPANY_ID)
    client.login()
    rooms = client.get_rooms()

    assert len(rooms) == 2
    assert rooms[0].roomId == "r1"
    assert rooms[0].unreadMessageCount == 2
    assert rooms[1].latest_message_id == 100
    assert rooms_route.called


@respx.mock
def test_offline_message_send_receive():
    """Test sending message and receiving history."""
    _mock_login(respx.mock)
    _mock_identity(respx.mock)

    history_route = respx.get("/api/chat/room/r1/chat/range").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "chatRoomId": "r1",
                            "chatMessageId": 42,
                            "createdAt": datetime.now(UTC).isoformat(),
                            "sender": {
                                "platformUserId": "11000002",
                                "platformUserName": "Alice",
                            },
                            "contents": {"message": {"text": "Hello bot"}},
                            "metadata": {},
                            "messageStatus": "SENT",
                        }
                    ]
                }
            },
        )
    )

    send_route = respx.post("/api/chat/message").mock(
        return_value=httpx.Response(200, json={"data": {"cmid": "msg-uuid"}})
    )

    client = BotClient("testbot", "secret", base_url=BASE, company_id=COMPANY_ID)
    client.login()

    history = client.get_chat_history("r1", offset=20, message_id=0)
    assert len(history) == 1
    assert history[0].chatMessageId == 42
    assert history[0].contents["message"]["text"] == "Hello bot"

    cmid = client.send_message("r1", "Hi Alice", reply_to="42")
    assert cmid == "msg-uuid"
    assert send_route.called
    assert history_route.called


@respx.mock
def test_offline_401_auto_relogin():
    """Test automatic re-authentication on 401 expiry."""
    login_route = respx.post("/api/portal/public/auth/login").mock(
        side_effect=[
            # Initial login
            httpx.Response(
                200,
                json={"code": "SUCCESS-0000"},
                headers={"set-cookie": "AccessToken=tok-1; Path=/"},
            ),
            # Re-login after 401
            httpx.Response(
                200,
                json={"code": "SUCCESS-0000"},
                headers={"set-cookie": "AccessToken=tok-2; Path=/"},
            ),
        ]
    )

    _mock_identity(respx.mock)

    rooms_route = respx.get("/api/chat/room").mock(
        side_effect=[
            httpx.Response(401, json={"code": "ROUTE-0004"}),
            httpx.Response(200, json={"data": {"elements": [{"roomId": "r1"}]}}),
        ]
    )

    client = BotClient("testbot", "secret", base_url=BASE, company_id=COMPANY_ID)
    client.login()
    assert client.access_token == "tok-1"

    rooms = client.get_rooms()
    assert len(rooms) == 1
    assert client.access_token == "tok-2"
    assert login_route.call_count == 2
    assert rooms_route.call_count == 2


@pytest.mark.asyncio
async def test_offline_bot_message_polling():
    """Test complete bot polling cycle with message handler."""

    class FakeBotClient:
        def __init__(self):
            self.user_id = "11000001"
            self.history = [
                ChatHistoryItem(
                    chatRoomId="r1",
                    chatMessageId=1,
                    createdAt=datetime.now(UTC).isoformat(),
                    sender={"platformUserId": "11000002", "platformUserName": "User"},
                    contents={"message": {"text": "ping"}},
                    metadata={},
                    messageStatus="SENT",
                )
            ]
            self.sent_messages = []
            self.marked_read = []

        def get_rooms(self):
            return [
                ChatRoomItem(
                    roomId="r1",
                    roomName="Test",
                    roomType="GROUP",
                    unreadMessageCount=1,
                )
            ]

        def get_chat_history(self, room_id: str, *, offset: int = 20, message_id: int = 0):
            return self.history

        def send_message(self, room_id: str, content: str, *, reply_to=None):
            self.sent_messages.append((room_id, content, reply_to))
            return "cmid-001"

        def mark_read(self, message_id, room_id):
            self.marked_read.append((message_id, room_id))

    async def echo_handler(msg):
        return f"echo: {msg.message_text}"

    client = FakeBotClient()
    cursors = MemoryCursorStore()
    engine = BotEngine(client, echo_handler, cursors=cursors)

    # First poll: baseline (no reply)
    await engine._poll_once()
    await engine._join()
    assert len(client.sent_messages) == 0  # baseline doesn't reply
    assert cursors.get("r1") == 1

    # Add new message
    client.history.append(
        ChatHistoryItem(
            chatRoomId="r1",
            chatMessageId=2,
            createdAt=datetime.now(UTC).isoformat(),
            sender={"platformUserId": "11000002", "platformUserName": "User"},
            contents={"message": {"text": "test"}},
            metadata={},
            messageStatus="SENT",
        )
    )

    # Second poll: should reply
    await engine._poll_once()
    await engine._join()
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0] == ("r1", "echo: test", "2")
    assert cursors.get("r1") == 2


@respx.mock
def test_offline_profile_persistence(tmp_path):
    """Test profile save/load with credentials."""
    _mock_login(respx.mock)
    _mock_identity(respx.mock)

    client = BotClient("testbot", "secret", base_url=BASE, company_id=COMPANY_ID)

    def on_auth(c):
        profile = Profile(
            base_url=c._base_url,
            company_id=c._company_id,
            company_uuid=c.identity.company_uuid if c.identity else "",
            company_domain=c.identity.company_domain if c.identity else "",
            login_id=c._login_id,
            user_id=c.user_id,
            name=c.identity.name if c.identity else "",
            access_token=c.access_token,
            refresh_token=c.refresh_token,
            password=c._password,
        )
        save_profile(profile, base_dir=tmp_path)

    client._on_auth = on_auth
    client.login()

    profile_file = tmp_path / ".daoubot" / "profile.yaml"
    assert profile_file.exists()

    content = profile_file.read_text()
    assert "testbot" in content
    assert "secret" in content  # password stored
    assert "tok-1" in content  # token stored

    # Public dict masks secrets
    from daouoffice.profile import load_profile

    loaded = load_profile(base_dir=tmp_path)
    assert loaded is not None
    assert loaded.login_id == "testbot"
    assert loaded.password == "secret"
    public = loaded.public_dict()
    assert public["password"] == "****"
    assert public["access_token"] == "****"


@respx.mock
def test_offline_mention_parsing():
    """Test mention token parsing in messages."""
    _mock_login(respx.mock)
    _mock_identity(respx.mock)

    respx.get("/api/chat/room/r1/chat/range").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "items": [
                        {
                            "chatRoomId": "r1",
                            "chatMessageId": 10,
                            "createdAt": datetime.now(UTC).isoformat(),
                            "sender": {
                                "platformUserId": "11000002",
                                "platformUserName": "User",
                            },
                            "contents": {
                                "message": {
                                    "text": "Hey {{550e8400-e29b-41d4-a716-446655440000::USER::@TestBot::11000001}} can you help?"
                                }
                            },
                            "metadata": {},
                            "messageStatus": "SENT",
                        }
                    ]
                }
            },
        )
    )

    client = BotClient("testbot", "secret", base_url=BASE, company_id=COMPANY_ID)
    client.login()

    history = client.get_chat_history("r1")
    assert len(history) == 1

    from daouoffice.client import parse_mentions

    raw = history[0].contents["message"]["text"]
    clean, user_ids, mention_all = parse_mentions(raw)

    assert clean == "Hey @TestBot can you help?"
    assert user_ids == ["11000001"]
    assert not mention_all


@respx.mock
def test_offline_attachment_flow():
    """Test file upload and download."""
    _mock_login(respx.mock)
    _mock_identity(respx.mock)

    upload_route = respx.post("/api/upload/attach/app").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "filePath": "UPLOAD/2026/test.pdf",
                    "fileName": "test.pdf",
                    "fileSize": 1024,
                }
            },
        )
    )

    send_route = respx.post("/api/chat/message").mock(
        return_value=httpx.Response(200, json={"data": {"cmid": "msg-with-file"}})
    )

    download_route = respx.get("/api/chat/attachment/555/download").mock(
        return_value=httpx.Response(
            200,
            content=b"PDF content here",
            headers={"content-disposition": 'attachment; filename="test.pdf"'},
        )
    )

    client = BotClient("testbot", "secret", base_url=BASE, company_id=COMPANY_ID)
    client.login()

    # Mock file for upload
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.pdf"
        test_file.write_bytes(b"PDF content here")

        # Upload and send
        meta = client.upload_attachment(test_file)
        assert meta["fileName"] == "test.pdf"
        assert upload_route.called

        cmid = client.send_message("r1", "See attachment", attachments=[meta])
        assert cmid == "msg-with-file"
        assert send_route.called

        # Download
        downloaded = client.download_attachment(
            {"attachmentId": 555, "fileName": "test.pdf"}, tmpdir
        )
        assert downloaded.read_bytes() == b"PDF content here"
        assert download_route.called


@respx.mock
def test_offline_company_discovery():
    """Test company ID discovery from public endpoint."""
    respx.get("/api/portal/public/auth/company").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "companyList": [
                        {
                            "companyId": "99000",
                            "uuid": "TEST-UUID",
                            "domain": "test",
                            "name": "Test Company",
                        }
                    ]
                }
            },
        )
    )

    info = BotClient.discover_company(BASE)
    assert info["companyId"] == "99000"
    assert info["domain"] == "test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
