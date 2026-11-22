import asyncio
import inspect
import re
import shlex
from collections.abc import Callable
from html import escape
from typing import (
    Annotated,
    Any,
    get_args,
    get_origin,
    get_type_hints,
)

from dotenv import load_dotenv

from soylon import examples
from soylon.bot import BackgroundReply, Bot, FormattedText, Message
from soylon.helper import convert_input

load_dotenv()


def get_function_arguments(func):
    signature = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)

    arguments = {}

    for name, param in signature.parameters.items():
        arguments[name] = {}
        dtype = hints.get(name, param.annotation)

        # Remove Annotated metadata
        if get_origin(dtype) is Annotated:
            dtype = get_args(dtype)[0]

        if dtype is not inspect.Parameter.empty:
            arguments[name] = {"type": dtype}

        if param.default is not inspect.Parameter.empty:
            arguments[name]["default"] = param.default

    return arguments


def collect_workflows(module) -> dict[str, dict[str, Any]]:
    workflows = {}
    for name in dir(module):
        f = getattr(module, name)
        if hasattr(f, "flowrep_recipe"):
            workflows[name] = {"function": f, "arguments": get_function_arguments(f)}
    return workflows


class _WorkflowSession:
    """Chat command handler holding the state of the currently loaded workflow."""

    HELP = (
        "Commands:\n!list - List available workflows\n"
        "!status - Show the currently loaded workflow\n"
        "!load <workflow_name> - Load a specific workflow\n"
        "!set <arg_name>=<arg_value> [<arg_name>=<arg_value> ...] - Set inputs "
        "(quote values containing spaces)\n"
        "!run [workflow_name] - Run the loaded (or the given) workflow"
    )
    NOT_LOADED = (
        "No workflow loaded. Use !load <workflow_name> to load a workflow first."
    )

    def __init__(self, workflows: dict[str, dict[str, Any]]):
        self.workflows = workflows
        self.name: str | None = None
        self.inputs: dict[str, Any] = {}
        self.pending: str | None = None
        self._commands: dict[str, Callable] = {
            "!help": self.help,
            "!list": self.list,
            "!status": self.status,
            "!load": self.load,
            "!set": self.set,
            "!run": self.run,
        }

    @property
    def welcome_message(self) -> str:
        return "Hello! Here's the list of available workflows:\n" + self._names()

    def _names(self) -> str:
        return "\n".join(f"- {name}" for name in self.workflows)

    @property
    def _arguments(self) -> dict[str, dict[str, Any]]:
        return self.workflows[self.name]["arguments"]

    async def __call__(self, message: Message) -> Any:
        if self.pending is not None and not message.body.startswith("!"):
            if message.attachment:
                return await self.answer(message.attachment.path)
            return await self.answer(message.body.strip())
        if not message.body.startswith("!"):
            return None
        self.pending = None
        command, _, arg = message.body.partition(" ")
        handler = self._commands.get(command)
        if handler is None:
            return "Unknown command. Use !help for a list of commands."
        result = handler(arg.strip())
        return await result if inspect.isawaitable(result) else result

    def help(self, arg: str = "") -> str:
        return self.HELP

    def list(self, arg: str = "") -> FormattedText:
        names = list(self.workflows)
        body = "Available workflows:\n" + "\n".join(names)
        formatted_body = "<p>Available workflows:</p>" + "".join(
            f"<pre><code>{escape(name)}</code></pre>" for name in names
        )
        return FormattedText(body, formatted_body)

    def status(self, arg: str = "") -> str:
        if self.name is None:
            return "No workflow loaded."
        return f"Loaded workflow: {self.name}\nInputs: {self.inputs}"

    def _select(self, name: str) -> None:
        self.name = name
        self.pending = None
        self.inputs = {
            key: info["default"]
            for key, info in self._arguments.items()
            if "default" in info
        }

    def load(self, name: str) -> str:
        if name not in self.workflows:
            return f"Workflow {name} not found."
        self._select(name)
        return f"Loaded workflow: {name}\n\n" + self._describe_inputs()

    def _describe_inputs(self) -> str:
        lines = ["List of input:"]
        for key, info in self._arguments.items():
            dtype = info.get("type")
            type_name = getattr(
                dtype, "__name__", str(dtype) if dtype is not None else "any"
            )
            line = f"- {key}: {type_name}"
            if "default" in info:
                line += f" (default: {info['default']})"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def _convert(self, key: str, raw: str) -> Any:
        info = self._arguments[key]
        return convert_input(info["type"], raw) if "type" in info else raw

    def set(self, arg: str) -> str:
        if self.name is None:
            return self.NOT_LOADED
        invalid_format = (
            "Invalid command format. Use !set <arg_name>=<arg_value> "
            "[<arg_name>=<arg_value> ...]."
        )
        try:
            tokens = shlex.split(arg)
        except ValueError:
            if re.search(r"(?:^|\s)[a-zA-Z_]\w*=['\"]", arg) and re.search(
                r"\s+[a-zA-Z_]\w*=", arg
            ):
                return invalid_format
            tokens = []

        if any("=" in token for token in tokens[1:]):
            assignments = [token.partition("=") for token in tokens]
        else:
            assignments = [arg.partition("=")]

        updates = {}
        for key, sep, raw in assignments:
            if not sep:
                return invalid_format
            if key not in self._arguments:
                return f"Argument {key} not found in workflow {self.name}."
            try:
                updates[key] = self._convert(key, raw)
            except Exception as e:
                return f"Error converting value: {e}"
        self.inputs.update(updates)
        return "\n".join(
            f"Set argument {key} to {value} for workflow {self.name}."
            for key, value in updates.items()
        )

    async def run(self, arg: str = "") -> Any:
        if arg:
            if arg not in self.workflows:
                return f"Workflow {arg} not found."
            self._select(arg)
        if self.name is None:
            return self.NOT_LOADED
        missing = [key for key in self._arguments if key not in self.inputs]
        if missing:
            return self._ask(missing[0])
        name = self.name
        function = self.workflows[name]["function"]
        inputs = self.inputs.copy()
        return BackgroundReply(lambda: self._execute(name, function, inputs))

    async def _execute(
        self, name: str, function: Callable, inputs: dict[str, Any]
    ) -> Any:
        try:
            result = await asyncio.to_thread(function, **inputs)
        except Exception as e:
            return f"Error executing workflow: {e}"
        if result is None:
            return f"Workflow {name} executed successfully but has no return value."
        else:
            return result

    def _ask(self, key: str) -> str:
        self.pending = key
        info = self._arguments[key]
        type_name = getattr(info.get("type"), "__name__", "any")
        return f"Please provide a value for {key} ({type_name}):"

    async def answer(self, raw: str) -> Any:
        key = self.pending
        try:
            self.inputs[key] = self._convert(key, raw)
        except Exception as e:
            return f"Error converting value: {e}\n" + self._ask(key)
        self.pending = None
        return await self.run()


class WorkflowBot(_WorkflowSession):
    """Chat command handler with a separate workflow state for each conversation."""

    def __init__(self, workflows: dict[str, dict[str, Any]]):
        super().__init__(workflows)
        self._sessions: dict[tuple[str, str, str | None], _WorkflowSession] = {}

    async def __call__(self, message: Message) -> Any:
        identity = (message.room_id, message.sender, message.thread_id)
        session = self._sessions.get(identity)
        if session is None:
            if not message.body.startswith("!"):
                return None
            session = self._sessions[identity] = _WorkflowSession(self.workflows)
        return await session(message)


if __name__ == "__main__":
    workflow_bot = WorkflowBot(collect_workflows(examples))
    asyncio.run(
        Bot.from_env(workflow_bot, welcome_message=workflow_bot.welcome_message).run()
    )
