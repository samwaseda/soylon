import asyncio
import inspect
from typing import Any, Annotated, Callable, Dict, Optional, get_args, get_origin, get_type_hints

from dotenv import load_dotenv

from soylon import examples
from soylon.helper import convert_input
from soylon.bot import Bot, Message

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


def collect_workflows(module) -> Dict[str, Dict[str, Any]]:
    workflows = {}
    for name in dir(module):
        f = getattr(module, name)
        if hasattr(f, "flowrep_recipe"):
            workflows[name] = {"function": f, "arguments": get_function_arguments(f)}
    return workflows


class WorkflowBot:
    """Chat command handler holding the state of the currently loaded workflow."""

    HELP = (
        "Commands:\n!list - List available workflows\n"
        "!status - Show the currently loaded workflow\n"
        "!load <workflow_name> - Load a specific workflow\n"
        "!set <arg_name>=<arg_value> - Set an input\n"
        "!run [workflow_name] - Run the loaded (or the given) workflow"
    )
    NOT_LOADED = "No workflow loaded. Use !load <workflow_name> to load a workflow first."

    def __init__(self, workflows: Dict[str, Dict[str, Any]]):
        self.workflows = workflows
        self.name: Optional[str] = None
        self.inputs: Dict[str, Any] = {}
        self.pending: Optional[str] = None
        self._commands: Dict[str, Callable[[str], str]] = {
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
    def _arguments(self) -> Dict[str, Dict[str, Any]]:
        return self.workflows[self.name]["arguments"]

    def __call__(self, message: Message) -> Optional[str]:
        if message.attachment:
            return f"Got {message.attachment.kind}: {message.attachment.path}"
        if self.pending is not None and not message.body.startswith("!"):
            return self.answer(message.body.strip())
        if not message.body.startswith("!"):
            return None
        self.pending = None
        command, _, arg = message.body.partition(" ")
        handler = self._commands.get(command)
        if handler is None:
            return "Unknown command. Use !help for a list of commands."
        return handler(arg.strip())

    def help(self, arg: str = "") -> str:
        return self.HELP

    def list(self, arg: str = "") -> str:
        return "Available workflows:\n" + self._names()

    def status(self, arg: str = "") -> str:
        if self.name is None:
            return "No workflow loaded."
        return f"Loaded workflow: {self.name}\nInputs: {self.inputs}"

    def _select(self, name: str) -> None:
        self.name = name
        self.pending = None
        self.inputs = {
            key: info["default"] for key, info in self._arguments.items() if "default" in info
        }

    def load(self, name: str) -> str:
        if name not in self.workflows:
            return f"Workflow {name} not found."
        self._select(name)
        return f"Loaded workflow: {name}\n\n" + self._describe_inputs()

    def _describe_inputs(self) -> str:
        lines = ["List of input:"]
        for key, info in self._arguments.items():
            line = f"- {key}: {info['type'].__name__}"
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
        key, sep, raw = arg.partition("=")
        if not sep:
            return "Invalid command format. Use !set <arg_name>=<arg_value>."
        if key not in self._arguments:
            return f"Argument {key} not found in workflow {self.name}."
        try:
            value = self._convert(key, raw)
        except Exception as e:
            return f"Error converting value: {e}"
        self.inputs[key] = value
        return f"Set argument {key} to {value} for workflow {self.name}."

    def run(self, arg: str = "") -> str:
        if arg:
            if arg not in self.workflows:
                return f"Workflow {arg} not found."
            self._select(arg)
        if self.name is None:
            return self.NOT_LOADED
        missing = [key for key in self._arguments if key not in self.inputs]
        if missing:
            return self._ask(missing[0])
        try:
            result = self.workflows[self.name]["function"](**self.inputs)
        except Exception as e:
            return f"Error executing workflow: {e}"
        return f"Workflow {self.name} executed successfully. Result: {result}"

    def _ask(self, key: str) -> str:
        self.pending = key
        info = self._arguments[key]
        type_name = getattr(info.get("type"), "__name__", "any")
        return f"Please provide a value for {key} ({type_name}):"

    def answer(self, raw: str) -> str:
        key = self.pending
        try:
            self.inputs[key] = self._convert(key, raw)
        except Exception as e:
            return f"Error converting value: {e}\n" + self._ask(key)
        self.pending = None
        return self.run()


if __name__ == "__main__":
    workflow_bot = WorkflowBot(collect_workflows(examples))
    asyncio.run(
        Bot.from_env(workflow_bot, welcome_message=workflow_bot.welcome_message).run()
    )
