import asyncio
import datetime
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from nio import RoomMessageText

from soylon.bot import BackgroundReply, Message
from tests.test_bot import ROOM, event, make_bot

with patch.dict(sys.modules, {"soylon.examples": ModuleType("soylon.examples")}):
    from soylon.workflow import WorkflowBot


class TestWorkflowConversations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.function = Mock(side_effect=lambda value: f"result: {value}")
        self.bot = WorkflowBot(
            {
                "example": {
                    "function": self.function,
                    "arguments": {"value": {"type": str}},
                }
            }
        )

    def message(self, body, room="room", sender="alice", thread=None, attachment=None):
        return Message(
            room,
            sender,
            body,
            datetime.datetime.now(),
            attachment=attachment,
            thread_id=thread,
        )

    async def send(self, message):
        reply = await self.bot(message)
        return await reply.reply() if isinstance(reply, BackgroundReply) else reply

    async def test_pending_answers_are_isolated_by_room_sender_and_thread(self):
        alice = lambda body: self.message(body, thread="thread")
        others = (
            lambda body: self.message(body, room="other", thread="thread"),
            lambda body: self.message(body, sender="bob", thread="thread"),
            lambda body: self.message(body, thread="other"),
            lambda body: self.message(body),
        )

        self.assertIn("Please provide a value", await self.send(alice("!run example")))
        for other in others:
            with self.subTest(other=other):
                self.assertIsNone(await self.send(other("not Alice's answer")))
                self.assertEqual(await self.send(other("!status")), "No workflow loaded.")
        self.function.assert_not_called()
        self.assertEqual(await self.send(alice("secret")), "result: secret")
        self.function.assert_called_once_with(value="secret")
        self.assertIn("'value': 'secret'", await self.send(alice("!status")))
        for other in others:
            self.assertEqual(await self.send(other("!status")), "No workflow loaded.")

    async def test_interleaved_sessions_keep_inputs_and_pending_separate(self):
        alice = lambda body: self.message(body)
        bob = lambda body: self.message(body, sender="bob")

        self.assertIn("Please provide a value", await self.send(alice("!run example")))
        self.assertIn("Loaded workflow", await self.send(bob("!load example")))
        self.assertIn("Set argument", await self.send(bob("!set value=bob")))
        self.assertEqual(await self.send(bob("!run")), "result: bob")
        self.assertEqual(await self.send(alice("alice")), "result: alice")
        self.assertIn("'value': 'bob'", await self.send(bob("!status")))
        self.assertIn("'value': 'alice'", await self.send(alice("!status")))

    async def test_attachment_answer_belongs_to_initiating_conversation(self):
        await self.send(self.message("!run example"))
        attachment = SimpleNamespace(path=Path("/tmp/input.txt"))
        self.assertIsNone(
            await self.send(self.message("file", sender="bob", attachment=attachment))
        )
        self.function.assert_not_called()
        self.assertEqual(
            await self.send(self.message("file", attachment=attachment)),
            "result: /tmp/input.txt",
        )

    async def test_running_workflow_does_not_block_and_keeps_run_state(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        received = []

        def blocking_workflow(value):
            received.append(value)
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=5):
                raise TimeoutError("workflow was not released")

        self.bot.workflows["example"]["function"] = blocking_workflow
        self.bot.workflows["other"] = {"function": lambda: None, "arguments": {}}
        await self.send(self.message("!load example"))
        await self.send(self.message("!set value=original"))
        task = asyncio.create_task(self.send(self.message("!run")))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await self.send(self.message("!set value=changed"))
            await self.send(self.message("!load other"))
        finally:
            release.set()
        self.assertEqual(
            await asyncio.wait_for(task, timeout=2),
            "Workflow example executed successfully but has no return value.",
        )
        self.assertEqual(received, ["original"])

    async def test_workflow_exception_is_reported(self):
        self.function.side_effect = ValueError("failed")
        await self.send(self.message("!run example"))
        self.assertEqual(
            await self.send(self.message("value")), "Error executing workflow: failed"
        )

    async def test_transport_processes_messages_while_workflow_runs(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def blocking_workflow(value):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=5):
                raise TimeoutError("workflow was not released")
            return f"result: {value}"

        self.bot.workflows["example"]["function"] = blocking_workflow
        transport = make_bot(self.bot)
        try:
            for body in ("!load example", "!set value=first", "!run"):
                await asyncio.wait_for(
                    transport._on_message(ROOM, event(RoomMessageText, body=body)), 2
                )
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(
                transport._on_message(ROOM, event(RoomMessageText, body="!status")), 2
            )
            messages = [
                call.kwargs["content"]["body"]
                for call in transport.client.room_send.await_args_list
            ]
            self.assertIn("Loaded workflow: example\nInputs: {'value': 'first'}", messages)
            self.assertNotIn("result: first", messages)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*transport._reply_tasks), 2)
        messages = [
            call.kwargs["content"]["body"]
            for call in transport.client.room_send.await_args_list
        ]
        self.assertIn("result: first", messages)


if __name__ == "__main__":
    unittest.main()
