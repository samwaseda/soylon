import asyncio
import datetime
import inspect
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from dotenv import load_dotenv
from nio import (
    AsyncClient,
    DownloadError,
    InviteMemberEvent,
    LoginResponse,
    MatrixRoom,
    RoomEncryptedMedia,
    RoomMessageMedia,
    RoomMessageText,
)
from nio.crypto import decrypt_attachment
from nio.exceptions import EncryptionError


@dataclass(frozen=True)
class Attachment:
    """An image or file sent to a room. Call `read()` to get its bytes."""

    kind: str  # "image", "file", "audio" or "video"
    filename: str
    mimetype: Optional[str]
    _client: AsyncClient
    _event: Union[RoomMessageMedia, RoomEncryptedMedia]
    path: Optional[Path] = None  # set once the file has been stored

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


@dataclass(frozen=True)
class Message:
    room_id: str
    sender: str
    body: str
    sent_at: datetime.datetime
    attachment: Optional[Attachment] = None


Reply = Optional[str]
Callback = Callable[[Message], Union[Reply, Awaitable[Reply]]]

_KINDS = {"m.image": "image", "m.audio": "audio", "m.video": "video"}


class Bot:
    """Thin layer between Matrix and a callback.

    The callback receives each new text, image or file message from other
    users (media is first saved to disk under a per-sender folder, see
    `Attachment.path`) and may return a string (sent back to the same room) or None (no
    reply). It can be a plain function or a coroutine function.
    """

    def __init__(
        self,
        callback: Callback,
        homeserver: str,
        user_id: str,
        password: str,
        storage_dir: Union[str, Path] = "downloads",
    ) -> None:
        self.callback = callback
        self.storage_dir = Path(storage_dir)
        self.password = password
        self.client = AsyncClient(homeserver, user_id)
        self._started_at = datetime.datetime.now()
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(
            self._on_message, (RoomMessageText, RoomMessageMedia, RoomEncryptedMedia)
        )

    @classmethod
    def from_env(cls, callback: Callback) -> "Bot":
        load_dotenv()
        return cls(
            callback,
            os.environ["HOMESERVER"],
            os.environ["USER_ID"],
            os.environ["PASSWORD"],
            os.environ.get("STORAGE_DIR", "downloads"),
        )

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        await self.client.join(room.room_id)

    async def _on_message(self, room: MatrixRoom, event) -> None:
        if event.sender == self.client.user_id:
            return
        sent_at = datetime.datetime.fromtimestamp(event.server_timestamp / 1000)
        if sent_at < self._started_at:
            return
        attachment = None
        if not isinstance(event, RoomMessageText):
            content = event.source.get("content", {})
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
                path = None  # the callback can still call attachment.read() to retry
            attachment = replace(attachment, path=path)
        reply = self.callback(
            Message(room.room_id, event.sender, event.body, sent_at, attachment)
        )
        if inspect.isawaitable(reply):
            reply = await reply
        if reply:
            await self.send(room.room_id, reply)

    async def _store(self, sender: str, attachment: Attachment) -> Path:
        """Save the file as-is under <storage_dir>/<sender>/ without inspecting it."""
        directory = self.storage_dir / _safe_name(sender.lstrip("@"), "unknown")
        directory.mkdir(parents=True, exist_ok=True)
        path = _unique_path(directory, _safe_name(attachment.filename, "file"))
        path.write_bytes(await attachment.read())
        return path

    async def send(self, room_id: str, text: str) -> None:
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
        )

    async def run(self) -> None:
        try:
            response = await self.client.login(self.password)
            if not isinstance(response, LoginResponse):
                raise RuntimeError(f"Login failed: {response}")
            await self.client.sync_forever(timeout=30_000)
        finally:
            await self.client.close()


def main() -> None:
    def respond(message: Message) -> Reply:
        if message.attachment:
            return f"Received {message.attachment.kind}: {message.attachment.filename}"
        if message.body.strip() == "!help":
            return "Send me text, images or files."
        return None

    asyncio.run(Bot.from_env(respond).run())


if __name__ == "__main__":
    main()
