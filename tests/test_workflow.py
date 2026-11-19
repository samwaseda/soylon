import datetime
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from soylon.bot import Message

with patch.dict(sys.modules, {"soylon.examples": ModuleType("soylon.examples")}):
    from soylon.workflow import WorkflowBot


class TestWorkflowConversations(unittest.TestCase):
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

    def test_pending_answers_are_isolated_by_room_sender_and_thread(self):
        alice = lambda body: self.message(body, thread="thread")
        others = (
            lambda body: self.message(body, room="other", thread="thread"),
            lambda body: self.message(body, sender="bob", thread="thread"),
            lambda body: self.message(body, thread="other"),
            lambda body: self.message(body),
        )

        self.assertIn("Please provide a value", self.bot(alice("!run example")))
        for other in others:
            with self.subTest(other=other):
                self.assertIsNone(self.bot(other("not Alice's answer")))
                self.assertEqual(self.bot(other("!status")), "No workflow loaded.")
        self.function.assert_not_called()
        self.assertEqual(self.bot(alice("secret")), "result: secret")
        self.function.assert_called_once_with(value="secret")
        self.assertIn("'value': 'secret'", self.bot(alice("!status")))
        for other in others:
            self.assertEqual(self.bot(other("!status")), "No workflow loaded.")

    def test_interleaved_sessions_keep_inputs_and_pending_separate(self):
        alice = lambda body: self.message(body)
        bob = lambda body: self.message(body, sender="bob")

        self.assertIn("Please provide a value", self.bot(alice("!run example")))
        self.assertIn("Loaded workflow", self.bot(bob("!load example")))
        self.assertIn("Set argument", self.bot(bob("!set value=bob")))
        self.assertEqual(self.bot(bob("!run")), "result: bob")
        self.assertEqual(self.bot(alice("alice")), "result: alice")
        self.assertIn("'value': 'bob'", self.bot(bob("!status")))
        self.assertIn("'value': 'alice'", self.bot(alice("!status")))

    def test_attachment_answer_belongs_to_initiating_conversation(self):
        self.bot(self.message("!run example"))
        attachment = SimpleNamespace(path=Path("/tmp/input.txt"))
        self.assertIsNone(
            self.bot(self.message("file", sender="bob", attachment=attachment))
        )
        self.function.assert_not_called()
        self.assertEqual(
            self.bot(self.message("file", attachment=attachment)),
            "result: /tmp/input.txt",
        )


if __name__ == "__main__":
    unittest.main()
