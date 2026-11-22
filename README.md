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
| `ALLOWED_USERS` | Optional. Comma-separated full Matrix IDs allowed to invite and message the bot |
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

asyncio.run(Bot.from_env(respond, welcome_message="Hello!").run())
```

The callback may be a plain function or a coroutine function. Return a string
to reply in the same room, a `pathlib.Path` to send a file or a ZIP archive of
a directory, a figure with a `savefig` method to send an image, or `None` to
stay silent. A missing file path raises an error. The bot ignores its own
messages and everything that happened before its first sync (history is
skipped using the sync token, not the local clock). By default it accepts
messages and joins rooms invited by any user. Set `ALLOWED_USERS` to restrict
both to the listed Matrix IDs; an empty value denies everyone. When constructing
`Bot` directly, pass `allowed_users` as a set of full Matrix IDs: `None` allows
everyone and an empty set denies everyone. Unauthorized messages are ignored
before attachments are downloaded or the callback is called. Replies are
visible to all members of the room, not just authorized users.

The optional `welcome_message` argument (also available on `Bot`
directly) is sent after a successful join; by default no message is sent.

### `Message`

`room_id`, `sender`, `body`, `sent_at` and `attachment` (`None` for plain text).
`event_id` identifies the message; `thread_id` is the thread root event ID for
messages in a thread, or `None` otherwise. A thread root itself does not
identify itself as belonging to a thread.

For an explicit reply, `reply_to_event_id` identifies the target and `reply_to`
contains its `event_id`, `sender`, `body` and `sent_at`. The bot fetches the
target before calling the callback; `reply_to` is `None` if the fetch fails,
and its `body` can be `None` if the event is redacted or cannot be decrypted.
Thread fallback reply relations are not treated as explicit replies. Returned
callback strings are sent to the room, not automatically into a thread.

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

Set `max_file_size` on `Bot` or `Bot.from_env` to limit uploads in bytes, for
example `Bot.from_env(respond, max_file_size=10 * 1024 * 1024)`. The default
`None` imposes no limit. The limit applies to files, rendered figures and ZIP
archives, including callback replies; exceeding it raises `ValueError` before
uploading. It does not limit incoming attachments.

### Converting user input

`convert_input` converts a string using a type's constructor, with special
handling for booleans:

```python
from soylon.helper import convert_input

number = convert_input(int, "42")
enabled = convert_input(bool, "yes")
number_or_text = convert_input(int | str, "hello")
```

Boolean input is case-insensitive and ignores surrounding whitespace. It accepts
`1`, `true`, `t`, `yes`, `y`, `on`, `enable` and `enabled`, and their false
counterparts `0`, `false`, `f`, `no`, `n`, `off`, `disable` and `disabled`.
Other boolean input raises `ValueError`. Other types receive the original string
unchanged; their normal constructor behavior and errors apply.
For a union, members are tried in order and the first successful conversion is
returned. If none succeeds, `ValueError` is raised. A `None` member has no
special string representation and does not convert string input.

### Workflow inputs

After loading a workflow with `!load <workflow_name>`, set one or more inputs
with `!set`, for example `!set distance=10 time=2 mass=5`. Separate assignments
with spaces and quote values containing spaces, as in
`!set title="hello world" count=3`. All assignments must be valid; otherwise
none are applied. A single assignment continues to accept its entire value
as entered, including spaces. Text resembling another assignment (such as
`key=value` after a space) is interpreted as another input.

## Logging

`soylon` logs through the standard `logging` module (logger `soylon.bot`):
`INFO` for logins, invites, messages and sends, and warnings/errors for failed joins, stores and uploads. Configure
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
