import asyncio
import datetime
import inspect
import io
import logging
import mimetypes
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from nio import (
    AsyncClient,
    DownloadError,
    InviteMemberEvent,
    JoinResponse,
    LoginResponse,
    MatrixRoom,
    RoomEncryptedMedia,
    RoomMessageMedia,
    RoomMessageText,
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


@dataclass(frozen=True)
class Message:
    room_id: str
    sender: str
    body: str
    sent_at: datetime.datetime
    attachment: Attachment | None = None


Reply = Optional[str]
Callback = Callable[[Message], Reply | Awaitable[Reply]]

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
        storage_dir: str | Path = "downloads",
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
        logger.info("Invited to %s, joining", room.room_id)
        response = await self.client.join(room.room_id)
        if not isinstance(response, JoinResponse):
            logger.warning("Joining %s failed: %s", room.room_id, response)

    async def _on_message(self, room: MatrixRoom, event) -> None:
        if event.sender == self.client.user_id:
            return
        sent_at = datetime.datetime.fromtimestamp(event.server_timestamp / 1000)
        if sent_at < self._started_at:
            logger.debug("Ignoring old message in %s from %s", room.room_id, event.sender)
            return
        logger.info("Message in %s from %s", room.room_id, event.sender)
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
                logger.exception("Could not store attachment from %s", event.sender)
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
        """
        if isinstance(data, (str, Path)):
            filename = filename or Path(data).name
            data = Path(data).read_bytes()
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

    async def run(self) -> None:
        try:
            response = await self.client.login(self.password)
            if not isinstance(response, LoginResponse):
                raise RuntimeError(f"Login failed: {response}")
            logger.info("Logged in as %s", response.user_id)
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

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(Bot.from_env(respond).run())


if __name__ == "__main__":
    main()
