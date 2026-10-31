# soylon

A very thin layer between [Matrix](https://matrix.org) and a callback function.
Write a function that takes a message and returns a reply; `soylon` handles
login, invites, syncing, file download/upload and encryption of media.

## Installation

```
conda env create -f .ci_support/environment.yml
```

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable      | Meaning                                              |
|---------------|------------------------------------------------------|
| `HOMESERVER`  | Homeserver URL, e.g. `https://matrix.org`            |
| `USER_ID`     | The bot's Matrix ID, e.g. `@my_bot:matrix.org`       |
| `PASSWORD`    | The bot's password                                   |
| `STORAGE_DIR` | Optional. Where received files go (default `downloads`) |
| `LOG_LEVEL`   | Optional. Used by `main()` (default `INFO`)          |

## Usage

```python
import asyncio
from soylon.bot import Bot, Message

def respond(message: Message):
    if message.attachment:
        return f"Got {message.attachment.kind}: {message.attachment.path}"
    if message.body == "!help":
        return "Send me text, images or files."

asyncio.run(Bot.from_env(respond).run())
```

The callback may be a plain function or a coroutine function. Return a string
to reply in the same room, or `None` to stay silent. The bot ignores its own
messages and anything sent before it started, and joins every room it is
invited to.

### `Message`

`room_id`, `sender`, `body`, `sent_at` and `attachment` (`None` for plain text).

### Receiving files and images

Incoming images, files, audio and video are saved as-is (never inspected) to
`<storage_dir>/<sender>/<filename>` before the callback runs. Filenames are
sanitised and never overwrite existing files. `attachment` has `kind`,
`filename`, `mimetype`, `path` (`None` if saving failed) and
`await attachment.read()` for the raw bytes. Encrypted media is decrypted.

### Sending files and figures

From inside an async callback (or anywhere else) use the bot instance:

```python
await bot.send_file(room_id, "report.pdf")             # path or bytes
await bot.send_file(room_id, data, "cat.png")          # mimetype is guessed
await bot.send_figure(room_id, matplotlib_figure)      # any object with .savefig
```

Uploads are encrypted automatically in encrypted rooms.

## Logging

`soylon` logs through the standard `logging` module (logger `soylon.bot`):
`INFO` for logins, invites, messages and sends, `DEBUG` for ignored old
messages, and warnings/errors for failed joins, stores and uploads. Configure
it in your application with `logging.basicConfig(...)`; running
`python -m soylon.bot` does so using `LOG_LEVEL`.

## Reconnecting

If syncing fails because of a network error, the bot retries with exponential
backoff (1s doubling up to 60s, configurable via `bot.min_reconnect_delay` and
`bot.max_reconnect_delay`). Login failures still raise.

## Limitations

- No size or type limit on received files.
- Text in end-to-end encrypted rooms is not supported (only media).

## Tests

```
python -m unittest discover -s tests -t .
```
