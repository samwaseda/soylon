import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nio import DownloadError, RoomEncryptedMedia, RoomMessageMedia, RoomMessageText

from soylon.bot import Bot


def make_bot(callback):
    bot = Bot(callback, "https://example.org", "@bot:example.org", "pw")
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


def test_sync_callback_reply_is_sent():
    bot = make_bot(lambda m: f"echo {m.body}")
    handle(bot, event(RoomMessageText))
    assert bot.client.room_send.call_args.kwargs["content"]["body"] == "echo hi"
    assert bot.client.room_send.call_args.kwargs["room_id"] == ROOM.room_id


def test_async_callback_and_none_reply():
    async def cb(m):
        return None

    bot = make_bot(cb)
    handle(bot, event(RoomMessageText))
    bot.client.room_send.assert_not_called()


def test_ignores_own_and_old_messages():
    calls = []
    bot = make_bot(calls.append)
    handle(bot, event(RoomMessageText, sender=bot.client.user_id))
    handle(bot, event(RoomMessageText, offset=-3600))
    assert calls == []


def test_media_message_has_attachment():
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
    assert (att.kind, att.filename, att.mimetype) == ("image", "cat.png", "image/png")

    bot.client.download = AsyncMock(return_value=SimpleNamespace(body=b"data"))
    assert asyncio.run(att.read()) == b"data"


def test_text_message_has_no_attachment():
    seen = []
    handle(make_bot(seen.append), event(RoomMessageText))
    assert seen[0].attachment is None


def test_encrypted_media_is_decrypted(monkeypatch):
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
    handle(bot, ev)
    bot.client.download = AsyncMock(return_value=SimpleNamespace(body=b"cipher"))
    monkeypatch.setattr(
        "soylon.bot.decrypt_attachment", lambda *a: b"plain" if a[0] == b"cipher" else b""
    )
    att = seen[0].attachment
    assert att.kind == "file"
    assert asyncio.run(att.read()) == b"plain"


def test_download_error_raises():
    seen = []
    bot = make_bot(seen.append)
    handle(bot, event(RoomMessageMedia, url="mxc://x/y", mimetype=None))
    bot.client.download = AsyncMock(return_value=DownloadError("boom"))
    try:
        asyncio.run(seen[0].attachment.read())
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")


def test_invite_is_joined():
    bot = make_bot(lambda m: None)
    asyncio.run(bot._on_invite(ROOM, None))
    bot.client.join.assert_awaited_once_with(ROOM.room_id)
