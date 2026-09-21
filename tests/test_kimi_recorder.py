from __future__ import annotations

import unittest

from paper_task_launcher.kimi_recorder import KimiEventParser


class KimiRecorderTests(unittest.TestCase):
    def test_extracts_prompt_and_final_end_turn_text(self):
        turns: list[dict] = []
        parser = KimiEventParser(session_id="session_test", on_turn=turns.append)
        parser.feed(
            {
                "type": "turn.prompt",
                "agentId": "main",
                "input": [{"type": "text", "text": "make it clearer"}],
                "time": 1000,
            }
        )
        parser.feed(
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "turnId": 0,
                    "step": 1,
                    "part": {"type": "text", "text": "intermediate"},
                },
            }
        )
        parser.feed(
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "turnId": 0,
                    "step": 2,
                    "part": {"type": "text", "text": "finished"},
                },
            }
        )
        parser.feed(
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "step.end",
                    "turnId": 0,
                    "step": 2,
                    "finishReason": "end_turn",
                },
            }
        )
        parser.feed(
            {
                "type": "turn.ended",
                "agentId": "main",
                "turnId": 0,
                "reason": "completed",
                "durationMs": 123,
                "time": 2000,
            }
        )
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["turn_id"], "session_test:0")
        self.assertEqual(turns[0]["user_input"], "make it clearer")
        self.assertEqual(turns[0]["final_response"], "finished")
        self.assertEqual(turns[0]["duration_ms"], 123)


if __name__ == "__main__":
    unittest.main()
