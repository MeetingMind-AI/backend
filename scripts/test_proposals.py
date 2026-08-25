"""
test_proposals.py
Verification test for real-time prompt classification and proposal normalization in MeetingMind-AI.
"""

import unittest
from unittest.mock import AsyncMock, patch
from app.engine.controller import ControllerAgent
from app.engine.prompts import REALTIME_SCRUM_MASTER_PROMPT, REALTIME_USER_PROMPT


class TestRealtimeProposals(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = ControllerAgent()

    @patch("app.engine.controller.OllamaClient.generate", new_callable=AsyncMock)
    async def test_to_schedule_proposal(self, mock_generate):
        mock_generate.return_value = '{"summary": "The team agreed to schedule a follow-up meeting tomorrow.", "proposal": {"type": "to_schedule", "content": "Schedule follow-up meeting for tomorrow"}}'
        
        result = await self.controller.summarize("Let\'s schedule a meeting for tomorrow.")
        scrum = result.get("scrum_master", {})
        
        self.assertEqual(scrum.get("text"), "The team agreed to schedule a follow-up meeting tomorrow.")
        self.assertIsNotNone(scrum.get("proposal"))
        self.assertEqual(scrum["proposal"]["type"], "to_schedule")
        self.assertEqual(scrum["proposal"]["content"], "Schedule follow-up meeting for tomorrow")

    @patch("app.engine.controller.OllamaClient.generate", new_callable=AsyncMock)
    async def test_to_do_proposal(self, mock_generate):
        mock_generate.return_value = '{"summary": "JJ Singh committed to completing documentation.", "proposal": {"type": "to_do", "content": "Complete documentation"}}'
        
        result = await self.controller.summarize("We have to do the documentation.")
        scrum = result.get("scrum_master", {})
        
        self.assertIsNotNone(scrum.get("proposal"))
        self.assertEqual(scrum["proposal"]["type"], "to_do")
        self.assertEqual(scrum["proposal"]["content"], "Complete documentation")

    @patch("app.engine.controller.OllamaClient.generate", new_callable=AsyncMock)
    async def test_filler_ignored(self, mock_generate):
        mock_generate.return_value = '{"summary": "IGNORE", "proposal": null}'
        
        result = await self.controller.summarize("Okay sounds good.")
        scrum = result.get("scrum_master", {})
        
        self.assertEqual(scrum.get("text"), "IGNORE")
        self.assertIsNone(scrum.get("proposal"))

    @patch("app.engine.controller.OllamaClient.generate", new_callable=AsyncMock)
    async def test_alias_normalization(self, mock_generate):
        mock_generate.return_value = '{"summary": "Team needs to schedule a sync.", "proposal": {"type": "schedule", "content": "Sync with design team"}}'
        
        result = await self.controller.summarize("We need to schedule a sync with the design team.")
        scrum = result.get("scrum_master", {})
        
        self.assertIsNotNone(scrum.get("proposal"))
        self.assertEqual(scrum["proposal"]["type"], "to_schedule")
        self.assertEqual(scrum["proposal"]["content"], "Sync with design team")


if __name__ == "__main__":
    unittest.main()
