import asyncio
import datetime
import inspect
import io
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from dotenv import load_dotenv
from nio import (
    AsyncClient,
    DownloadError,
    InviteMemberEvent,
    JoinResponse,
    LoginResponse,
    LogoutResponse,
    MatrixRoom,
    RoomEncryptedMedia,
    RoomGetEventResponse,
    RoomMessageMedia,
    RoomMessageText,
    SyncResponse,
    UploadResponse,
)
from nio.crypto import decrypt_attachment
from nio.exceptions import EncryptionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Attachment:
    """An image or file sent to a room. Call `read()` to get its bytes."""

    kind: str  # "image", "file", "audio" or "video"
    filename: str
    mimetype: str | None
    _client: AsyncClient
    _event: RoomMessageMedia | RoomEncryptedMedia
    path: Path | None = None  # set once the file has been stored

    async def read(self) -> bytes:
        response = await self._client.download(self._event.url)
        if isinstance(response, DownloadError):
            raise RuntimeError(f"Download failed: {response}")
        if isinstance(self._event, RoomEncryptedMedia):
            return decrypt_attachment(
                response.body,
                self._event.key["k"],
                self._event.hashes["sha256"],
                self._event.iv,
            )
        return response.body


def _safe_name(name: str, default: str) -> str:
    name = re.sub(r"[^\w.-]", "_", Path(name).name).lstrip(".")
    return name or default


def _unique_path(directory: Path, filename: str) -> Path:
    path = directory / filename
    n = 1
    while path.exists():
        path = directory / f"{path.stem}_{n}{Path(filename).suffix}"
        n += 1
    return path


def _read_file(path: Path, max_file_size: int | None) -> bytes:
    with path.open("rb") as file:
        return (
            file.read(max_file_size + 1) if max_file_size is not None else file.read()
        )


@dataclass(frozen=True)
class ReplyMetadata:
    event_id: str
    sender: str
    body: str | None
    sent_at: datetime.datetime


@dataclass(frozen=True)
class Message:
    room_id: str
    sender: str
    body: str
    sent_at: datetime.datetime
    attachment: Attachment | None = None
    event_id: str | None = None
    thread_id: str | None = None
    reply_to_event_id: str | None = None
    reply_to: ReplyMetadata | None = None


class Figure(Protocol):
    def savefig(self, *args, **kwargs) -> None: ...


Item = str | Path | Figure | None
Reply = Item | dict[Any, Any] | tuple[Any, ...]
Callback = Callable[[Message], Reply | Awaitable[Reply]]

_KINDS = {"m.image": "image", "m.audio": "audio", "m.video": "video"}


class Bot:
    """Thin layer between Matrix and a callback.

    The callback receives each new text, image or file message from other
    users (media is first saved to disk under a per-sender folder, see
    `Attachment.path`) and may return text, a file or directory path, a figure
    with a `savefig` method, or None (no reply). For a dict or tuple, the
    whole value is sent as text first, then each file path or figure inside
    it is sent separately, preceded by a message naming its key or index. It can be a plain function or
    a coroutine function.
    """

    def __init__(
        self,
        callback: Callback,
        homeserver: str,
        user_id: str,
        password: str,
        storage_dir: str | Path = "downloads",
        welcome_message: str | None = None,
        max_file_size: int | None = None,
        convert_files: bool = True,
    ) -> None:
        if max_file_size is not None and (
            not isinstance(max_file_size, int)
            or isinstance(max_file_size, bool)
            or max_file_size < 0
        ):
            raise ValueError("max_file_size must be a non-negative integer or None")
        self.callback = callback
        self.welcome_message = welcome_message
        self.max_file_size = max_file_size
        self.min_reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.storage_dir = Path(storage_dir)
        self.password = password
        self.convert_files = convert_files
        self.client = AsyncClient(homeserver, user_id)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)

    @classmethod
    def from_env(
        cls,
        callback: Callback,
        welcome_message: str | None = None,
        max_file_size: int | None = None,
        convert_files: bool = True,
    ) -> "Bot":
        load_dotenv()
        required = ("HOMESERVER", "USER_ID", "PASSWORD")
        missing = [name for name in required if name not in os.environ]
        if missing:
            names = ", ".join(missing)
            raise ValueError(
                f"Missing required environment variable(s): {names}.\n"
                "Configure them in your environment or copy .env.example to .env, "
                "for example:\n"
                "HOMESERVER=https://matrix.org\n"
                "USER_ID=@my_workflow_bot:matrix.org\n"
                "PASSWORD=my_password"
            )
        return cls(
            callback,
            os.environ["HOMESERVER"],
            os.environ["USER_ID"],
            os.environ["PASSWORD"],
            os.environ.get("STORAGE_DIR", "downloads"),
            welcome_message=welcome_message,
            max_file_size=max_file_size,
            convert_files=convert_files
        )

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.state_key != self.client.user_id or event.membership != "invite":
            return
        logger.info("Invited to %s, joining", room.room_id)
        response = await self.client.join(room.room_id)
        if not isinstance(response, JoinResponse):
            logger.warning("Joining %s failed: %s", room.room_id, response)
        elif self.welcome_message:
            await self.send(room.room_id, self.welcome_message)

    async def _on_message(self, room: MatrixRoom, event) -> None:
        if event.sender == self.client.user_id:
            return
        sent_at = datetime.datetime.fromtimestamp(event.server_timestamp / 1000)
        logger.info(f"Message '{event.body}' from {event.sender} in {room.room_id}")
        content = event.source.get("content", {})
        relation = content.get("m.relates_to", {})
        if not isinstance(relation, dict):
            relation = {}
        thread_id = None
        if relation.get("rel_type") == "m.thread":
            thread_id = relation.get("event_id")
            if not isinstance(thread_id, str):
                thread_id = None
        reply_to_event_id = None
        in_reply_to = relation.get("m.in_reply_to")
        if isinstance(in_reply_to, dict) and not (
            relation.get("rel_type") == "m.thread"
            and relation.get("is_falling_back") is True
        ):
            target = in_reply_to.get("event_id")
            if isinstance(target, str) and target:
                reply_to_event_id = target
        reply_to = None
        if reply_to_event_id:
            reply_to = await self._reply_metadata(room.room_id, reply_to_event_id)
        attachment = None
        if not isinstance(event, RoomMessageText):
            attachment = Attachment(
                kind=_KINDS.get(content.get("msgtype"), "file"),
                filename=content.get("filename") or event.body,
                mimetype=getattr(event, "mimetype", None),
                _client=self.client,
                _event=event,
            )
            try:
                path = await self._store(event.sender, attachment)
            except (RuntimeError, EncryptionError, OSError):
                logger.exception("Could not store attachment from %s", event.sender)
                path = None  # the callback can still call attachment.read() to retry
            attachment = replace(attachment, path=path)
        reply = self.callback(
            Message(
                room.room_id,
                event.sender,
                event.body,
                sent_at,
                attachment,
                event_id=event.event_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
                reply_to=reply_to,
            )
        )
        if inspect.isawaitable(reply):
            reply = await reply
        if isinstance(reply, (dict, tuple)) and self.convert_files:
            await self.send(room.room_id, str(reply))
            items = reply.items() if isinstance(reply, dict) else enumerate(reply)
            for key, item in items:
                if isinstance(item, Path) or callable(getattr(item, "savefig", None)):
                    label = f"key {key!r}" if isinstance(reply, dict) else f"index {key}"
                    await self.send(room.room_id, f"Sending {label}:")
                    await self._send_reply(room.room_id, item)
        else:
            await self._send_reply(room.room_id, reply)

    async def _send_reply(self, room_id: str, reply: Item) -> None:
        if isinstance(reply, Path) and self.convert_files:
            if reply.is_dir():
                with tempfile.TemporaryDirectory() as tmp:
                    archive = shutil.make_archive(
                        str(Path(tmp) / (reply.resolve().name or "directory")),
                        "zip",
                        root_dir=reply,
                    )
                    await self.send_file(room_id, Path(archive))
            else:
                await self.send_file(room_id, reply)
        elif callable(getattr(reply, "savefig", None)) and self.convert_files:
            await self.send_figure(room_id, reply)
        elif reply:
            await self.send(room_id, reply)

    async def _reply_metadata(
        self, room_id: str, event_id: str
    ) -> ReplyMetadata | None:
        try:
            response = await self.client.room_get_event(room_id, event_id)
        except (TimeoutError, aiohttp.ClientError, OSError):
            logger.warning(
                "Could not fetch replied-to event %s", event_id, exc_info=True
            )
            return None
        if not isinstance(response, RoomGetEventResponse):
            logger.warning(
                "Could not fetch replied-to event %s: %s", event_id, response
            )
            return None
        event = response.event
        sender = getattr(event, "sender", None)
        timestamp = getattr(event, "server_timestamp", None)
        if not isinstance(sender, str) or not isinstance(timestamp, int):
            return None
        try:
            sent_at = datetime.datetime.fromtimestamp(timestamp / 1000)
        except (ValueError, OverflowError, OSError):
            return None
        return ReplyMetadata(
            event_id=event_id,
            sender=sender,
            body=getattr(event, "body", None),
            sent_at=sent_at,
        )

    async def _store(self, sender: str, attachment: Attachment) -> Path:
        """Save the file as-is under <storage_dir>/<sender>/ without inspecting it."""
        directory = self.storage_dir / _safe_name(sender.lstrip("@"), "unknown")
        directory.mkdir(parents=True, exist_ok=True)
        path = _unique_path(directory, _safe_name(attachment.filename, "file"))
        path.write_bytes(await attachment.read())
        logger.info("Stored %s", path)
        return path

    async def send(self, room_id: str, text: str) -> None:
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
        )
        logger.info("Sent text to %s", room_id)

    async def send_file(
        self,
        room_id: str,
        data: str | Path | bytes,
        filename: str | None = None,
        mimetype: str | None = None,
    ) -> None:
        """Upload a file (path or bytes) and send it to a room.

        Images, audio and video are sent as such, anything else as a file.
        Raises ValueError if the configured upload size limit is exceeded.
        """
        if isinstance(data, (str, Path)):
            filename = filename or Path(data).name
            data = _read_file(Path(data), self.max_file_size)
        if self.max_file_size is not None and len(data) > self.max_file_size:
            raise ValueError(
                f"File exceeds maximum upload size of {self.max_file_size} bytes"
            )
        filename = filename or "file"
        mimetype = (
            mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        room = self.client.rooms.get(room_id)
        encrypt = bool(room and room.encrypted)
        response, keys = await self.client.upload(
            lambda *_: io.BytesIO(data),
            content_type=mimetype,
            filename=filename,
            encrypt=encrypt,
            filesize=len(data),
        )
        if not isinstance(response, UploadResponse):
            logger.error("Upload of %r failed: %s", filename, response)
            raise RuntimeError(f"Upload failed: {response}")
        msgtype = {"image": "m.image", "audio": "m.audio", "video": "m.video"}.get(
            mimetype.split("/")[0], "m.file"
        )
        content = {
            "msgtype": msgtype,
            "body": filename,
            "info": {"mimetype": mimetype, "size": len(data)},
        }
        if keys:
            content["file"] = {**keys, "url": response.content_uri}
        else:
            content["url"] = response.content_uri
        await self.client.room_send(
            room_id=room_id, message_type="m.room.message", content=content
        )
        logger.info("Sent %s %r to %s", msgtype, filename, room_id)

    async def send_figure(
        self, room_id: str, figure, filename: str = "figure.png", **savefig_kwargs
    ) -> None:
        """Render a matplotlib figure and send it as an image."""
        buffer = io.BytesIO()
        figure.savefig(
            buffer, format=Path(filename).suffix.lstrip(".") or "png", **savefig_kwargs
        )
        await self.send_file(room_id, buffer.getvalue(), filename)

    async def _sync_with_reconnect(self) -> None:
        """Run sync_forever, retrying with exponential backoff on connection errors."""
        delay = self.min_reconnect_delay
        while True:
            started = time.monotonic()
            try:
                await self.client.sync_forever(timeout=30_000)
                return
            except (TimeoutError, aiohttp.ClientError, OSError) as error:
                if time.monotonic() - started > self.max_reconnect_delay:
                    delay = self.min_reconnect_delay  # it was healthy for a while
                logger.warning("Sync failed (%r), retrying in %.0fs", error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_reconnect_delay)

    async def run(self) -> None:
        try:
            response = await self.client.login(self.password)
            if not isinstance(response, LoginResponse):
                raise RuntimeError(f"Login failed: {response}")
            logger.info("Logged in as %s", response.user_id)
            # Messages are only handled from the sync token of this first sync
            # onwards, so history is skipped without relying on the local clock.
            sync_response = await self.client.sync(timeout=0, full_state=True)
            if not isinstance(sync_response, SyncResponse):
                raise RuntimeError(f"Initial sync failed: {sync_response}")
            self.client.add_event_callback(
                self._on_message,
                (RoomMessageText, RoomMessageMedia, RoomEncryptedMedia),
            )
            await self._sync_with_reconnect()
        finally:
            try:
                if self.client.access_token:
                    response = await self.client.logout()
                    if not isinstance(response, LogoutResponse):
                        logger.warning("Logout failed: %s", response)
            finally:
                await self.client.close()


def main() -> None:
    def respond(message: Message) -> Reply:
        if message.attachment:
            return f"Received {message.attachment.kind}: {message.attachment.filename}"
        if message.body.strip() == "!help":
            return "Send me text, images or files."
        return None

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(Bot.from_env(respond, welcome_message="Hello!").run())


if __name__ == "__main__":
    main()
