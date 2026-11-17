import asyncio
import inspect
from typing import Annotated, get_args, get_origin, get_type_hints

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


all_functions = {}
for name in dir(examples):
    f = getattr(examples, name)
    if hasattr(f, "flowrep_recipe"):
        all_functions[name] = {"function": f, "arguments": get_function_arguments(f)}


welcome_message = "Hello! Here's the list of available workflows:\n" + "\n".join(
    f"- {name}" for name in all_functions
)


loaded_function = None


def run_workflow(message: Message):
    global loaded_function
    if message.attachment:
        return f"Got {message.attachment.kind}: {message.attachment.path}"
    elif message.body == "!help":
        return "Commands:\n!list - List available workflows\n!status - Show the currently loaded workflow\n!load <workflow_name> - Load a specific workflow"
    elif message.body == "!list":
        return "Available workflows:\n" + "\n".join(f"- {name}" for name in all_functions)
    elif message.body == "!status":
        if loaded_function:
            return f"Loaded workflow: {loaded_function['function'].__name__}\nInputs: {loaded_function['inputs']}"
        else:
            return "No workflow loaded."
    elif message.body.startswith("!load "):
        function_name = message.body.split(" ", 1)[1]
        if function_name in all_functions:
            loaded_function = {"function": all_functions[function_name]["function"], "inputs": {}}
            for inp in all_functions[function_name]["arguments"]:
                if "default" in all_functions[function_name]["arguments"][inp]:
                    loaded_function["inputs"][inp] = all_functions[function_name]["arguments"][inp]["default"]
            input_info = "List of input:\n"
            for arg_name, arg_info in all_functions[function_name]["arguments"].items():
                input_info += f"- {arg_name}: {arg_info['type'].__name__}"
                if "default" in arg_info:
                    input_info += f" (default: {arg_info['default']})"
                input_info += "\n"
            return f"Loaded workflow: {function_name}\n\n" + input_info
        else:
            return f"Workflow {function_name} not found."
    elif message.body.startswith("!set "):
        if not loaded_function:
            return "No workflow loaded. Use !load <workflow_name> to load a workflow first."
        try:
            arg_name, arg_value = message.body.split(" ", 1)[1].split("=", 1)
        except ValueError:
            return "Invalid command format. Use !set <arg_name>=<arg_value>."
        if arg_name not in all_functions[loaded_function["function"].__name__]["arguments"]:
            return f"Argument {arg_name} not found in workflow {loaded_function["function"].__name__}."
        if "type" in all_functions[loaded_function["function"].__name__]["arguments"][arg_name]:
            arg_type = all_functions[loaded_function["function"].__name__]["arguments"][arg_name]["type"]
            try:
                converted_value = convert_input(arg_type, arg_value)
            except Exception as e:
                return f"Error converting value: {e}"
        else:
            converted_value = arg_value
        loaded_function["inputs"][arg_name] = converted_value
        return f"Set argument {arg_name} to {converted_value} for workflow {loaded_function['function'].__name__}."
    elif message.body == "!run":
        if not loaded_function:
            return "No workflow loaded. Use !load <workflow_name> to load a workflow first."
        try:
            result = loaded_function["function"](**loaded_function["inputs"])
            return f"Workflow {loaded_function['function'].__name__} executed successfully. Result: {result}"
        except Exception as e:
            return f"Error executing workflow: {e}"
    elif message.body.startswith("!"):
        return "Unknown command. Use !help for a list of commands."



if __name__ == "__main__":
    asyncio.run(Bot.from_env(run_workflow, welcome_message=welcome_message).run())
