"""DaouOffice Messenger Bot — REST polling engine.

This is the single polling implementation used by :class:`daouoffice.DaouBot`.
It periodically lists rooms and, for any room whose newest message id is past
the per-room cursor, dispatches only messages newer than the last one it has
handled (tracked by ``chatMessageId``). The cursor — not the unread badge,
which the bot itself clears via ``mark_read`` — decides what is pending.

On first contact with a room the existing backlog is **not** replayed — a
baseline is set so the bot only reacts to messages that arrive while it is
running. The bot's own messages are skipped via the identity resolved at login.

Rooms are dispatched **concurrently across rooms, serial within a room**: a
slow handler in one room no longer stalls other rooms or the poll loop. The
at-least-once / ordered guarantees are unchanged — a room still advances its
cursor (and ``mark_read``) only after its handler succeeds; a room with a
handler still in flight is simply skipped until it finishes.

Polling is the only delivery mechanism. A WebSocket/STOMP endpoint
(``GET /ws/pc``) was observed in the traffic capture but its flow was never
validated, so it is intentionally not implemented (see docs/api/04-websocket.md).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from daouoffice.client import BotClient, ChatRoomItem, NewMessage, parse_mentions
from daouoffice.markdown import to_chat_html
from daouoffice.state import CursorStore, MemoryCursorStore

logger = logging.getLogger(__name__)


def _apply_log_level() -> None:
    """Apply ``DAOU_LOG_LEVEL`` (e.g. ``DEBUG``/``WARNING``) to the
    ``daouoffice`` package logger when set.

    Scoped to this package's logger only — it never calls ``basicConfig``
    or touches the root logger, so it cannot hijack the host app's logging
    (a library must not). Unset → the app's own configuration stands. This
    is opt-in env, not a hardcoded level: per-message lines are already
    ``DEBUG`` so default ``INFO`` never logs conversation content; this
    only lets an operator turn that detail on, or quiet the SDK further,
    for the ``daouoffice`` namespace without editing the app.
    """
    raw = os.getenv("DAOU_LOG_LEVEL")
    if not raw:
        return
    level = logging.getLevelName(raw.strip().upper())
    if not isinstance(level, int):
        logger.warning("Ignoring invalid DAOU_LOG_LEVEL=%r", raw)
        return
    logging.getLogger("daouoffice").setLevel(level)


POLL_INTERVAL = 5

OnMessageCallback = Callable[[NewMessage], Awaitable[str | None]]
"""Async callback: receives a NewMessage, returns a reply string or None."""

RoomFilter = Callable[[ChatRoomItem], bool]
"""Predicate deciding whether a room may be fetched and marked read."""

OnReplySentCallback = Callable[[NewMessage, str, str], Awaitable[None]]
"""Async callback invoked after a handler reply is posted to DaouOffice."""


class BotEngine:
    """REST polling bot engine.

    Delivery is **at-least-once** (the message-delivery industry standard):
    a message is re-delivered until its handler returns without raising,
    processed in order per room. This is not configurable — make handlers
    idempotent if a duplicate reply would matter. For fire-and-forget, have
    the handler swallow its own errors (it then never "fails", so it is not
    retried).

    Args:
        client: A logged-in :class:`BotClient`.
        on_message: Async callback invoked per inbound message.
        poll_interval: Seconds between poll cycles.
        cursors: Where to persist the per-room processed cursor. Defaults to
            an in-memory store (not durable across restarts); pass a
            :class:`~daouoffice.state.FileCursorStore` to resume after restart.
        max_attempts: give up on a single message after this many failed
            handler attempts and move on (poison-message guard).
        room_filter: Optional predicate applied to rooms before history fetch
            and read acknowledgements. Host apps can use this to enforce an
            allowlist at the transport boundary.
        markdown: render each handler reply from Markdown to the chat-HTML
            subset (bold/italic/lists) before sending. Off by default —
            replies are sent verbatim.
    """

    def __init__(
        self,
        client: BotClient,
        on_message: OnMessageCallback,
        *,
        poll_interval: int = POLL_INTERVAL,
        cursors: CursorStore | None = None,
        max_attempts: int = 5,
        room_filter: RoomFilter | None = None,
        markdown: bool = False,
        on_reply_sent: OnReplySentCallback | None = None,
    ) -> None:
        self._client = client
        self._on_message = on_message
        self._poll_interval = poll_interval
        self._running = False
        self._max_attempts = max_attempts
        self._room_filter = room_filter
        self._render = to_chat_html if markdown else None
        self._on_reply_sent = on_reply_sent
        # room_id -> highest chatMessageId already handled
        self._cursors: CursorStore = cursors or MemoryCursorStore()
        # "room_id:mid" -> failed handler attempts
        self._attempts: dict[str, int] = {}
        # room_id -> in-flight handler task. Rooms are handled concurrently
        # (cross-room parallel); a room is never handled concurrently with
        # itself (per-room serial) — a still-running room is skipped this
        # cycle, its messages stay pending (cursor not advanced) and are
        # picked up next cycle. This preserves the at-least-once + ordered
        # guarantees while a slow handler in one room no longer stalls
        # other rooms or the poll loop.
        self._room_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """Run the poll loop until :meth:`stop` is called."""
        _apply_log_level()
        logger.info("Starting bot engine (REST polling, interval=%ss)", self._poll_interval)
        self._running = True
        failures = 0
        while self._running:
            try:
                await self._poll_once()
                failures = 0
            except Exception:
                failures += 1
                logger.exception("Poll cycle failed (consecutive=%d)", failures)
            # Exponential backoff on sustained failure (cap 5 min) so a
            # misconfigured/clearly-down server is not hammered every interval.
            delay = self._poll_interval
            if failures:
                delay = min(self._poll_interval * (2 ** min(failures, 6)), 300)
            await asyncio.sleep(delay)
        await self._drain_room_tasks()

    def stop(self) -> None:
        self._running = False

    async def _join(self) -> None:
        """Await the in-flight room handlers (does not schedule new work).

        The poll loop never calls this — it would reintroduce the
        head-of-line blocking this design removes. It exists for
        deterministic stepping in tests and for callers that explicitly
        want a quiescence barrier."""
        tasks = [t for t in self._room_tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _drain_room_tasks(self) -> None:
        """Cancel and await outstanding room handlers on shutdown. A
        cancelled handler simply did not advance its cursor, so its
        messages are redelivered on the next run (at-least-once holds)."""
        pending = [t for t in self._room_tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._room_tasks.clear()

    # -- internals ------------------------------------------------------

    async def _poll_once(self) -> None:
        rooms = await asyncio.to_thread(self._client.get_rooms)
        for room in rooms:
            if self._room_filter is not None and not self._room_filter(room):
                continue
            cursor = self._cursors.get(room.roomId)
            # Source of truth is the cursor vs. the room's newest message id,
            # NOT the unread badge — the bot clears that badge via mark_read,
            # so a burst arriving between fetch and ack would otherwise be
            # marked read and never polled again (stranded). The badge is
            # only used for first contact (no cursor yet → set a baseline).
            if cursor is None:
                if room.unreadMessageCount > 0:
                    self._schedule_room(room.roomId, room.roomType)
                elif room.latest_message_id is not None:
                    self._cursors.set(room.roomId, room.latest_message_id)
                    logger.info(
                        "Room %s: baseline at %s (no unread backlog)",
                        room.roomId,
                        room.latest_message_id,
                    )
                continue
            latest = room.latest_message_id
            if (latest is not None and latest > cursor) or room.unreadMessageCount > 0:
                self._schedule_room(room.roomId, room.roomType)

    def _schedule_room(self, room_id: str, room_type: str) -> None:
        """Run a room's handler concurrently with other rooms, but never
        twice for the same room at once. If the room's previous handler is
        still running, skip it this cycle: its messages stay pending (the
        cursor was not advanced) and are scheduled again next poll —
        per-room serial + at-least-once both preserved."""
        existing = self._room_tasks.get(room_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._handle_room(room_id, room_type))
        self._room_tasks[room_id] = task
        task.add_done_callback(lambda t, rid=room_id: self._room_done(rid, t))

    def _room_done(self, room_id: str, task: asyncio.Task) -> None:
        if self._room_tasks.get(room_id) is task:
            self._room_tasks.pop(room_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Room %s handler crashed", room_id, exc_info=exc)

    @staticmethod
    def _mid(item) -> int | None:
        try:
            return int(item.chatMessageId)
        except (TypeError, ValueError):
            return None

    async def _handle_room(self, room_id: str, room_type: str) -> None:
        try:
            # Wide window: a burst between two polls must not fall out of the
            # fetched range before the cursor catches up (messageId=0 always
            # returns the newest N, so N must cover an interval's worth).
            history = await asyncio.to_thread(self._client.get_chat_history, room_id, offset=100)
        except Exception:
            logger.exception("Failed to fetch history for %s", room_id)
            return
        if not history:
            return

        ids = [m for m in (self._mid(it) for it in history) if m is not None]
        # Don't assume the range is oldest-first: ack the highest id.
        latest = max(ids) if ids else history[-1].chatMessageId

        baseline = self._cursors.get(room_id)
        if baseline is None:
            # First time we ever see this room: don't replay the backlog.
            start_at = max(ids, default=0)
            self._cursors.set(room_id, start_at)
            logger.info(
                "Room %s: baseline at %s (%d backlog message(s) skipped)",
                room_id,
                start_at,
                len(history),
            )
            await self._mark_read(latest, room_id)
            return

        # New messages, oldest first (ordered processing).
        new_items = sorted(
            (
                (mid, item)
                for item in history
                if (mid := self._mid(item)) is not None and mid > baseline
            ),
            key=lambda pair: pair[0],
        )

        handled = baseline
        blocked = False
        for mid, item in new_items:
            msg = self._to_message(item, room_type)
            dispatchable = msg is not None and msg.sender_user_id != self._client.user_id
            if not dispatchable:
                handled = mid  # own / no-text: nothing to deliver, ack it
                continue

            # Only advance the cursor once the handler succeeds (at-least-once).
            if await self._dispatch(msg):
                handled = mid
                self._attempts.pop(f"{room_id}:{mid}", None)
                continue
            key = f"{room_id}:{mid}"
            self._attempts[key] = self._attempts.get(key, 0) + 1
            if self._attempts[key] >= self._max_attempts:
                logger.error(
                    "Giving up on message %s in %s after %d attempts (poison)",
                    mid,
                    room_id,
                    self._attempts[key],
                )
                self._attempts.pop(key, None)
                handled = mid  # skip poison message and keep going
                continue
            blocked = True  # retry this one next cycle; preserve order
            break

        if handled != baseline:
            self._cursors.set(room_id, handled)

        # Read receipts: clear the room only when nothing is pending retry,
        # otherwise leave it unread so the failed message is polled again.
        if not blocked:
            await self._mark_read(latest, room_id)
        elif handled != baseline:
            await self._mark_read(handled, room_id)

    async def _mark_read(self, message_id, room_id: str) -> None:
        try:
            await asyncio.to_thread(self._client.mark_read, message_id, room_id)
            # DEBUG (no content, per-cycle chatter — same class as the
            # reply log); a failure is logged at exception level below.
            logger.debug("Read ack [%s] up to %s", room_id, message_id)
        except Exception:
            logger.exception("mark_read failed for %s", message_id)

    def _to_message(self, item, room_type: str) -> NewMessage | None:
        sender = item.sender or {}
        contents = item.contents or {}
        # ``text`` may be missing OR explicitly null (file-only / system
        # messages), so coalesce to "" — a None here would crash downstream
        # slicing/handlers that assume message_text is always a string.
        raw = contents.get("message", {}).get("text") or ""
        attachments = contents.get("attachmentList") or []
        # Drop only truly empty payloads (system/member-left notices). A
        # file-only message has empty text but a non-empty attachmentList —
        # keep it so the attachment is delivered, not silently lost.
        if not raw and not attachments:
            return None
        clean, mentions, mention_all = parse_mentions(raw)
        return NewMessage(
            room_id=item.chatRoomId,
            room_type=room_type,
            sender_user_id=str(sender.get("platformUserId", "")),
            sender_name=sender.get("platformUserName", ""),
            message_text=clean,
            message_id=str(item.chatMessageId),
            created_at=item.createdAt,
            raw_text=raw,
            mentions=mentions,
            mentions_me=self._client.user_id in mentions,
            mention_all=mention_all,
            attachments=attachments,
        )

    async def _dispatch(self, msg: NewMessage) -> bool:
        """Run the handler and send any reply. Returns True on success."""
        # DEBUG, not INFO: this carries the sender and message content, so
        # at the default level the bot must not log conversations.
        logger.debug("[%s] %s: %s", msg.room_id, msg.sender_name, msg.message_text[:80])
        try:
            reply = await self._on_message(msg)
            if reply:
                if self._render is not None:
                    reply = self._render(reply)
                # A handler reply is, by definition, a response to this exact
                # message — post it as a threaded reply so the quote shows
                # what it answers (not a policy knob; reply attribution is a
                # delivery detail the SDK owns, like at-least-once).
                sent_id = await asyncio.to_thread(
                    self._client.send_message,
                    msg.room_id,
                    reply,
                    reply_to=msg.message_id,
                )
                if self._on_reply_sent is not None:
                    await self._on_reply_sent(msg, reply, str(sent_id or ""))
                logger.debug("Replied to [%s] (re: %s)", msg.room_id, msg.message_id)
            return True
        except Exception:
            logger.exception("Handler failed for [%s]", msg.room_id)
            return False
