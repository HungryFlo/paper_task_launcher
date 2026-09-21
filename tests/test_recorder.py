from __future__ import annotations

import unittest

from paper_task_launcher.recorder import SessionEventParser


class RecorderParserTests(unittest.TestCase):
    def test_extracts_only_user_and_final_response(self):
        sessions = []
        turns = []
        parser = SessionEventParser(on_session=sessions.append, on_turn=turns.append)
        parser.feed({"type": "session_meta", "payload": {"session_id": "s1"}})
        parser.feed(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "t1"},
            }
        )
        parser.feed(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "t1",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "question"}],
                    },
                },
            }
        )
        parser.feed(
            {
                "type": "event_msg",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "last_agent_message": "final answer",
                },
            }
        )
        self.assertEqual(sessions[0]["session_id"], "s1")
        self.assertEqual(turns[0]["user_input"], "question")
        self.assertEqual(turns[0]["final_response"], "final answer")

    def test_extracts_new_user_message_event(self):
        turns = []
        parser = SessionEventParser(on_session=lambda payload: None, on_turn=turns.append)
        parser.feed(
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "t-new"},
            }
        )
        parser.feed(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "new schema question",
                },
            }
        )
        parser.feed(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "t-new",
                    "last_agent_message": "new schema answer",
                },
            }
        )

        self.assertEqual(turns[0]["turn_id"], "t-new")
        self.assertEqual(turns[0]["user_input"], "new schema question")
        self.assertEqual(turns[0]["final_response"], "new schema answer")


if __name__ == "__main__":
    unittest.main()
