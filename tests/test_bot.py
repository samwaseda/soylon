import asyncio
import aiohttp
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nio import (
    DownloadError,
    RoomEncryptedMedia,
    RoomMessageMedia,
    RoomMessageText,
    UploadResponse,
)

from soylon.bot import Bot


def make_bot(callback, storage_dir=None):
    storage_dir = storage_dir or tempfile.mkdtemp()
    bot = Bot(callback, "https://example.org", "@bot:example.org", "pw", storage_dir)
    bot.client.download = AsyncMock(return_value=SimpleNamespace(body=b"data"))
    bot.client.room_send = AsyncMock()
    bot.client.join = AsyncMock()
    return bot


def event(cls, sender="@alice:example.org", body="hi", offset=10, source=None, **attrs):
    ev = cls.__new__(cls)
    ts = (datetime.datetime.now() + datetime.timedelta(seconds=offset)).timestamp()
    ev.__dict__.update(
        sender=sender,
        body=body,
        server_timestamp=int(ts * 1000),
        source=source or {"content": {}},
        **attrs,
    )
    return ev


ROOM = SimpleNamespace(room_id="!room:example.org")


def handle(bot, ev):
    asyncio.run(bot._on_message(ROOM, ev))


class TestBot(unittest.TestCase):
    def test_sync_callback_reply_is_sent(self):
        bot = make_bot(lambda m: f"echo {m.body}")
        handle(bot, event(RoomMessageText))
        self.assertEqual(
            bot.client.room_send.call_args.kwargs["content"]["body"], "echo hi"
        )
        self.assertEqual(bot.client.room_send.call_args.kwargs["room_id"], ROOM.room_id)

    def test_async_callback_and_none_reply(self):
        async def cb(m):
            return None

        bot = make_bot(cb)
        handle(bot, event(RoomMessageText))
        bot.client.room_send.assert_not_called()

    def test_ignores_own_messages(self):
        calls = []
        bot = make_bot(calls.append)
        handle(bot, event(RoomMessageText, sender=bot.client.user_id))
        self.assertEqual(calls, [])

    def test_media_message_has_attachment(self):
        seen = []
        bot = make_bot(seen.append)
        ev = event(
            RoomMessageMedia,
            body="cat.png",
            url="mxc://x/y",
            mimetype="image/png",
            source={"content": {"msgtype": "m.image", "filename": "cat.png"}},
        )
        handle(bot, ev)
        att = seen[0].attachment
        self.assertEqual(
            (att.kind, att.filename, att.mimetype), ("image", "cat.png", "image/png")
        )
        self.assertEqual(asyncio.run(att.read()), b"data")

    def test_media_is_stored_in_sender_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            bot = make_bot(seen.append, tmp)
            source = {"content": {"msgtype": "m.file", "filename": "../../evil.txt"}}
            for _ in range(2):
                handle(
                    bot,
                    event(
                        RoomMessageMedia, url="mxc://x/y", mimetype=None, source=source
                    ),
                )
            first, second = (m.attachment.path for m in seen)
            self.assertEqual(first, Path(tmp) / "alice_example.org" / "evil.txt")
            self.assertEqual(second.name, "evil_1.txt")
            self.assertEqual(first.read_bytes(), b"data")

    def test_text_message_has_no_attachment(self):
        seen = []
        handle(make_bot(seen.append), event(RoomMessageText))
        self.assertIsNone(seen[0].attachment)

    def test_encrypted_media_is_decrypted(self):
        seen = []
        bot = make_bot(seen.append)
        ev = event(
            RoomEncryptedMedia,
            body="a.bin",
            url="mxc://x/y",
            mimetype=None,
            key={"k": "key"},
            hashes={"sha256": "hash"},
            iv="iv",
            source={"content": {"msgtype": "m.file"}},
        )
        bot.client.download = AsyncMock(return_value=SimpleNamespace(body=b"cipher"))
        with patch(
            "soylon.bot.decrypt_attachment",
            lambda *a: b"plain" if a[0] == b"cipher" else b"",
        ):
            handle(bot, ev)
        att = seen[0].attachment
        self.assertEqual(att.kind, "file")
        self.assertEqual(att.path.read_bytes(), b"plain")

    def test_download_error_still_calls_callback(self):
        seen = []
        bot = make_bot(seen.append)
        bot.client.download = AsyncMock(return_value=DownloadError("boom"))
        handle(bot, event(RoomMessageMedia, url="mxc://x/y", mimetype=None))
        self.assertIsNone(seen[0].attachment.path)
        with self.assertRaises(RuntimeError):
            asyncio.run(seen[0].attachment.read())

    def _upload_bot(self, encrypted=False, keys=None):
        bot = make_bot(lambda m: None)
        bot.client.rooms = {ROOM.room_id: SimpleNamespace(encrypted=encrypted)}
        bot.client.upload = AsyncMock(return_value=(UploadResponse("mxc://x/up"), keys))
        return bot

    def test_send_file_from_bytes_and_path(self):
        bot = self._upload_bot()
        asyncio.run(bot.send_file(ROOM.room_id, b"abc", "cat.png"))
        content = bot.client.room_send.call_args.kwargs["content"]
        self.assertEqual(content["msgtype"], "m.image")
        self.assertEqual(content["url"], "mxc://x/up")
        self.assertEqual(
            bot.client.upload.call_args.kwargs["content_type"], "image/png"
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_bytes(b"hello")
            asyncio.run(bot.send_file(ROOM.room_id, path))
        content = bot.client.room_send.call_args.kwargs["content"]
        self.assertEqual((content["msgtype"], content["body"]), ("m.file", "notes.txt"))

    def test_send_file_encrypted_room(self):
        bot = self._upload_bot(encrypted=True, keys={"k": "key"})
        asyncio.run(bot.send_file(ROOM.room_id, b"abc", "a.bin"))
        content = bot.client.room_send.call_args.kwargs["content"]
        self.assertTrue(bot.client.upload.call_args.kwargs["encrypt"])
        self.assertEqual(content["file"], {"k": "key", "url": "mxc://x/up"})
        self.assertNotIn("url", content)

    def test_send_file_upload_error(self):
        bot = self._upload_bot()
        bot.client.upload.return_value = (SimpleNamespace(), None)
        with self.assertRaises(RuntimeError):
            asyncio.run(bot.send_file(ROOM.room_id, b"abc", "a.bin"))
        bot.client.room_send.assert_not_called()

    def test_send_figure(self):
        bot = self._upload_bot()
        figure = SimpleNamespace(savefig=lambda buf, format: buf.write(b"PNGDATA"))
        asyncio.run(bot.send_figure(ROOM.room_id, figure))
        self.assertEqual(
            bot.client.room_send.call_args.kwargs["content"]["msgtype"], "m.image"
        )
        self.assertEqual(bot.client.upload.call_args.kwargs["filesize"], 7)

    def test_sync_reconnects_with_backoff(self):
        bot = make_bot(lambda m: None)
        bot.client.sync_forever = AsyncMock(
            side_effect=[aiohttp.ClientError(), asyncio.TimeoutError(), OSError(), None]
        )
        with patch("soylon.bot.asyncio.sleep", new=AsyncMock()) as sleep:
            asyncio.run(bot._sync_with_reconnect())
        self.assertEqual(bot.client.sync_forever.await_count, 4)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [1.0, 2.0, 4.0])

    def test_invite_is_joined(self):
        bot = make_bot(lambda m: None)
        asyncio.run(bot._on_invite(ROOM, None))
        bot.client.join.assert_awaited_once_with(ROOM.room_id)


if __name__ == "__main__":
    unittest.main()
