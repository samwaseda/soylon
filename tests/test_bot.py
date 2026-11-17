import asyncio
import datetime
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from nio import (
    DownloadError,
    InviteMemberEvent,
    JoinResponse,
    LoginResponse,
    LogoutResponse,
    RoomEncryptedMedia,
    RoomGetEventError,
    RoomGetEventResponse,
    RoomMessageMedia,
    RoomMessageText,
    SyncResponse,
    UploadResponse,
)

from soylon.bot import Bot


def make_bot(callback, storage_dir=None, welcome_message=None):
    storage_dir = storage_dir or tempfile.mkdtemp()
    bot = Bot(
        callback,
        "https://example.org",
        "@bot:example.org",
        "pw",
        storage_dir,
        welcome_message=welcome_message,
    )
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
        event_id="$incoming",
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

    def test_callback_file_path_is_uploaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cat.png"
            path.write_bytes(b"image")

            async def cb(message):
                return path

            bot = self._upload_bot()
            bot.callback = cb
            handle(bot, event(RoomMessageText))
        content = bot.client.room_send.call_args.kwargs["content"]
        self.assertEqual((content["msgtype"], content["body"]), ("m.image", "cat.png"))
        self.assertEqual(bot.client.upload.call_args.kwargs["filesize"], 5)

    def test_callback_directory_is_uploaded_as_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "report"
            (directory / "empty").mkdir(parents=True)
            (directory / "notes.txt").write_text("hello")
            bot = self._upload_bot()
            bot.callback = lambda message: directory
            captured = {}

            async def upload(provider, **kwargs):
                captured["archive"] = provider().read()
                captured["filename"] = kwargs["filename"]
                return UploadResponse("mxc://x/up"), None

            bot.client.upload.side_effect = upload
            handle(bot, event(RoomMessageText))

        self.assertEqual(captured["filename"], "report.zip")
        with zipfile.ZipFile(io.BytesIO(captured["archive"])) as archive:
            self.assertEqual(archive.read("notes.txt"), b"hello")
            self.assertIn("empty/", archive.namelist())
        content = bot.client.room_send.call_args.kwargs["content"]
        self.assertEqual(
            (content["msgtype"], content["body"]), ("m.file", "report.zip")
        )

    def test_callback_figure_is_rendered(self):
        figure = SimpleNamespace(savefig=lambda buf, format: buf.write(b"PNGDATA"))
        bot = self._upload_bot()
        bot.callback = lambda message: figure
        handle(bot, event(RoomMessageText))
        content = bot.client.room_send.call_args.kwargs["content"]
        self.assertEqual(
            (content["msgtype"], content["body"]), ("m.image", "figure.png")
        )
        self.assertEqual(bot.client.upload.call_args.kwargs["filesize"], 7)

    def test_callback_missing_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._upload_bot()
            bot.callback = lambda message: Path(tmp) / "missing"
            with self.assertRaises(FileNotFoundError):
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

    def test_reply_metadata_is_fetched(self):
        seen = []
        bot = make_bot(seen.append)
        bot.client.room_get_event = AsyncMock(
            return_value=RoomGetEventResponse.from_dict(
                {
                    "type": "m.room.message",
                    "event_id": "$parent",
                    "sender": "@bob:example.org",
                    "origin_server_ts": 1000000,
                    "content": {"msgtype": "m.text", "body": "original"},
                }
            )
        )
        handle(
            bot,
            event(
                RoomMessageText,
                source={
                    "content": {
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$parent"}}
                    }
                },
            ),
        )
        message = seen[0]
        self.assertEqual(message.event_id, "$incoming")
        self.assertIsNone(message.thread_id)
        self.assertEqual(message.reply_to_event_id, "$parent")
        self.assertEqual(message.reply_to.event_id, "$parent")
        self.assertEqual(message.reply_to.sender, "@bob:example.org")
        self.assertEqual(message.reply_to.body, "original")
        self.assertEqual(
            message.reply_to.sent_at, datetime.datetime.fromtimestamp(1000)
        )
        bot.client.room_get_event.assert_awaited_once_with(ROOM.room_id, "$parent")

    def test_reply_metadata_without_body(self):
        seen = []
        bot = make_bot(seen.append)
        response = RoomGetEventResponse()
        response.event = SimpleNamespace(
            sender="@bob:example.org", server_timestamp=1000000
        )
        bot.client.room_get_event = AsyncMock(return_value=response)
        handle(
            bot,
            event(
                RoomMessageText,
                source={
                    "content": {
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$parent"}}
                    }
                },
            ),
        )
        self.assertEqual(seen[0].reply_to.sender, "@bob:example.org")
        self.assertIsNone(seen[0].reply_to.body)

    def test_thread_fallback_is_not_an_explicit_reply(self):
        seen = []
        bot = make_bot(seen.append)
        bot.client.room_get_event = AsyncMock()
        handle(
            bot,
            event(
                RoomMessageText,
                source={
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$root",
                            "m.in_reply_to": {"event_id": "$previous"},
                            "is_falling_back": True,
                        }
                    }
                },
            ),
        )
        self.assertEqual(seen[0].thread_id, "$root")
        self.assertIsNone(seen[0].reply_to_event_id)
        self.assertIsNone(seen[0].reply_to)
        bot.client.room_get_event.assert_not_awaited()

    def test_reply_in_thread_with_unavailable_target(self):
        for result in (RoomGetEventError("not found"), aiohttp.ClientError("offline")):
            with self.subTest(result=result):
                seen = []
                bot = make_bot(seen.append)
                bot.client.room_get_event = AsyncMock(
                    side_effect=result if isinstance(result, Exception) else None,
                    return_value=result,
                )
                handle(
                    bot,
                    event(
                        RoomMessageMedia,
                        url="mxc://x/y",
                        mimetype=None,
                        source={
                            "content": {
                                "m.relates_to": {
                                    "rel_type": "m.thread",
                                    "event_id": "$root",
                                    "m.in_reply_to": {"event_id": "$parent"},
                                }
                            }
                        },
                    ),
                )
                self.assertEqual(seen[0].thread_id, "$root")
                self.assertEqual(seen[0].reply_to_event_id, "$parent")
                self.assertIsNone(seen[0].reply_to)
                self.assertIsNotNone(seen[0].attachment)

    def test_missing_or_malformed_relations(self):
        for relation in (None, {}, {"m.in_reply_to": "invalid"}):
            with self.subTest(relation=relation):
                seen = []
                bot = make_bot(seen.append)
                bot.client.room_get_event = AsyncMock()
                handle(
                    bot,
                    event(
                        RoomMessageText, source={"content": {"m.relates_to": relation}}
                    ),
                )
                self.assertIsNone(seen[0].thread_id)
                self.assertIsNone(seen[0].reply_to_event_id)
                self.assertIsNone(seen[0].reply_to)
                bot.client.room_get_event.assert_not_awaited()

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
            side_effect=[aiohttp.ClientError(), TimeoutError(), OSError(), None]
        )
        with patch("soylon.bot.asyncio.sleep", new=AsyncMock()) as sleep:
            asyncio.run(bot._sync_with_reconnect())
        self.assertEqual(bot.client.sync_forever.await_count, 4)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [1.0, 2.0, 4.0])

    def test_run_logs_out_on_cancellation(self):
        bot = make_bot(lambda m: None)
        bot.client.access_token = "token"
        bot.client.login = AsyncMock(
            return_value=LoginResponse("@bot:example.org", "DEVICE", "token")
        )
        bot.client.sync = AsyncMock(return_value=SyncResponse.__new__(SyncResponse))
        bot._sync_with_reconnect = AsyncMock(side_effect=asyncio.CancelledError)
        bot.client.logout = AsyncMock(return_value=LogoutResponse())
        bot.client.close = AsyncMock()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(bot.run())

        bot.client.logout.assert_awaited_once_with()
        bot.client.close.assert_awaited_once_with()

    def test_run_closes_without_logout_on_login_failure(self):
        bot = make_bot(lambda m: None)
        bot.client.login = AsyncMock(return_value=SimpleNamespace())
        bot.client.logout = AsyncMock()
        bot.client.close = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "Login failed"):
            asyncio.run(bot.run())

        bot.client.logout.assert_not_awaited()
        bot.client.close.assert_awaited_once_with()

    def test_run_closes_if_logout_raises(self):
        bot = make_bot(lambda m: None)
        bot.client.access_token = "token"
        bot.client.login = AsyncMock(
            return_value=LoginResponse("@bot:example.org", "DEVICE", "token")
        )
        bot.client.sync = AsyncMock(return_value=SyncResponse.__new__(SyncResponse))
        bot._sync_with_reconnect = AsyncMock()
        bot.client.logout = AsyncMock(side_effect=OSError("network error"))
        bot.client.close = AsyncMock()

        with self.assertRaisesRegex(OSError, "network error"):
            asyncio.run(bot.run())

        bot.client.close.assert_awaited_once_with()

    def test_invite_is_joined(self):
        bot = make_bot(lambda m: None)
        bot.client.join.return_value = JoinResponse(ROOM.room_id)
        invite = event(
            InviteMemberEvent, state_key=bot.client.user_id, membership="invite"
        )
        asyncio.run(bot._on_invite(ROOM, invite))
        bot.client.join.assert_awaited_once_with(ROOM.room_id)
        bot.client.room_send.assert_not_awaited()

    def test_invite_sends_welcome_after_join(self):
        bot = make_bot(lambda m: None, welcome_message="Hello!")
        bot.client.join.return_value = JoinResponse(ROOM.room_id)
        invite = event(
            InviteMemberEvent, state_key=bot.client.user_id, membership="invite"
        )
        asyncio.run(bot._on_invite(ROOM, invite))
        bot.client.room_send.assert_awaited_once_with(
            room_id=ROOM.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": "Hello!"},
        )

    def test_failed_join_does_not_send_welcome(self):
        bot = make_bot(lambda m: None, welcome_message="Hello!")
        bot.client.join.return_value = SimpleNamespace()
        invite = event(
            InviteMemberEvent, state_key=bot.client.user_id, membership="invite"
        )
        asyncio.run(bot._on_invite(ROOM, invite))
        bot.client.room_send.assert_not_awaited()

    def test_only_own_invite_triggers_welcome(self):
        bot = make_bot(lambda m: None, welcome_message="Hello!")
        bot.client.join.return_value = JoinResponse(ROOM.room_id)
        events = [
            event(InviteMemberEvent, state_key="@alice:example.org", membership="join"),
            event(
                InviteMemberEvent, state_key="@other:example.org", membership="invite"
            ),
            event(InviteMemberEvent, state_key=bot.client.user_id, membership="join"),
            event(InviteMemberEvent, state_key=bot.client.user_id, membership="invite"),
        ]

        async def dispatch():
            for invite in events:
                await bot._on_invite(ROOM, invite)

        asyncio.run(dispatch())
        bot.client.join.assert_awaited_once_with(ROOM.room_id)
        bot.client.room_send.assert_awaited_once()

    def test_from_env_accepts_welcome_message(self):
        env = {
            "HOMESERVER": "https://example.org",
            "USER_ID": "@bot:example.org",
            "PASSWORD": "pw",
        }
        with patch("soylon.bot.load_dotenv"), patch.dict("os.environ", env):
            bot = Bot.from_env(lambda m: None, welcome_message="Hello!")
        bot.client.join = AsyncMock(return_value=JoinResponse(ROOM.room_id))
        bot.client.room_send = AsyncMock()
        invite = event(
            InviteMemberEvent, state_key=bot.client.user_id, membership="invite"
        )
        asyncio.run(bot._on_invite(ROOM, invite))
        self.assertEqual(
            bot.client.room_send.await_args.kwargs["content"]["body"], "Hello!"
        )

    def test_from_env_reports_missing_required_variables(self):
        with (
            patch("soylon.bot.load_dotenv"),
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(
                ValueError,
                r"(?s)Missing required environment variable\(s\): "
                r"HOMESERVER, USER_ID, PASSWORD.*"
                r"HOMESERVER=https://matrix.org",
            ),
        ):
            Bot.from_env(lambda m: None)


if __name__ == "__main__":
    unittest.main()
