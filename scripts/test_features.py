"""Unit and integration tests for:
1. AI Disclaimers in email service and API responses
2. Redo summary endpoint and authorization
3. Transcript editing, versioning, revert, deletion, and manual utterance addition with admin-only authorization
"""

import unittest
import asyncio
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

# SQLite compatibility for PostgreSQL JSONB
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app import main as main_mod
from app.api import deps as deps_mod
from app.api import auth as auth_mod
from app.api import teams as teams_mod
from app.main import app, _check_can_edit_meeting
from app.db.models import Base, User, Team, TeamMembership, Meeting, TranscriptChunk, Session as DbSession
from app.engine.email_service import build_email_html


# In-memory SQLite for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Override SessionLocal across modules
main_mod.SessionLocal = TestingSessionLocal
deps_mod.SessionLocal = TestingSessionLocal
auth_mod.SessionLocal = TestingSessionLocal
teams_mod.SessionLocal = TestingSessionLocal


class TestMeetingMindFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        # Create test users
        self.admin_user = User(id=1, email="admin@test.com", password_hash="hash", name="Admin User")
        self.member_user = User(id=2, email="member@test.com", password_hash="hash", name="Member User")
        self.outsider_user = User(id=3, email="outsider@test.com", password_hash="hash", name="Outsider User")
        self.db.add_all([self.admin_user, self.member_user, self.outsider_user])

        # Create test team
        self.team = Team(id=1, name="Test Team", owner_id=1, invite_token="test-invite-token")
        self.db.add(self.team)
        self.db.flush()

        # Memberships: user 1 is admin, user 2 is member
        self.db.add(TeamMembership(user_id=1, team_id=1, role="admin"))
        self.db.add(TeamMembership(user_id=2, team_id=1, role="member"))

        # Create test meeting
        self.meeting = Meeting(
            id=1,
            vexa_meeting_id="vexa-123",
            title="Sprint Planning",
            status="completed",
            team_id=1,
            created_by=1,
            summary={"scrum_master": {"summary": "Initial summary", "decisions": ["D1"]}},
        )
        self.db.add(self.meeting)
        self.db.flush()

        # Create test transcript chunk
        self.chunk = TranscriptChunk(
            id=1,
            meeting_id=1,
            speaker="Alice",
            text="Let us use PostgreSQL for database.",
            timestamp=datetime.now(timezone.utc),
            is_edited=False,
        )
        self.db.add(self.chunk)

        # Create auth sessions for users
        self.admin_session = DbSession(id=1, user_id=1, token="admin-token")
        self.member_session = DbSession(id=2, user_id=2, token="member-token")
        self.outsider_session = DbSession(id=3, user_id=3, token="outsider-token")
        self.db.add_all([self.admin_session, self.member_session, self.outsider_session])

        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_email_service_ai_disclaimers(self):
        """Verify build_email_html includes AI disclaimers in body and footer."""
        meeting_dict = {
            "title": self.meeting.title,
            "created_at": datetime.now(timezone.utc),
            "summary": {"scrum_master": {"summary": "Initial summary", "decisions": ["D1"]}},
        }
        html = build_email_html(meeting_dict, {})
        self.assertIn("✦ AI Disclaimer:", html)
        self.assertIn("may contain inaccuracies", html)
        self.assertIn("AI-generated content may be inaccurate or incomplete", html)

    def test_permission_check_helper(self):
        """Verify _check_can_edit_meeting checks owner, admin role, and creator."""
        # Team owner / admin
        self.assertTrue(_check_can_edit_meeting(self.db, user_id=1, meeting=self.meeting))
        # Normal team member
        self.assertFalse(_check_can_edit_meeting(self.db, user_id=2, meeting=self.meeting))
        # Non-member outsider
        self.assertFalse(_check_can_edit_meeting(self.db, user_id=3, meeting=self.meeting))

    def test_transcript_edit_first_and_second_time(self):
        """Verify PATCH transcript chunk records original text on first edit and preserves it on subsequent edits."""
        # Non-admin forbidden
        forbidden_res = self.client.patch(
            f"/api/meetings/{self.meeting.id}/transcript/{self.chunk.id}",
            json={"speaker": "Alice Johnson", "text": "Hacked text"},
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(forbidden_res.status_code, 403)

        # First edit as admin
        res = self.client.patch(
            f"/api/meetings/{self.meeting.id}/transcript/{self.chunk.id}",
            json={"speaker": "Alice Johnson", "text": "Let us use PostgreSQL and Redis."},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["chunk"]
        self.assertTrue(data["is_edited"])
        self.assertEqual(data["original_text"], "Let us use PostgreSQL for database.")
        self.assertEqual(data["original_speaker"], "Alice")
        self.assertEqual(data["text"], "Let us use PostgreSQL and Redis.")
        self.assertEqual(data["speaker"], "Alice Johnson")

        # Second edit — original_text must NOT change
        res2 = self.client.patch(
            f"/api/meetings/{self.meeting.id}/transcript/{self.chunk.id}",
            json={"text": "Let us use PostgreSQL and RabbitMQ."},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res2.status_code, 200, res2.text)
        data2 = res2.json()["chunk"]
        self.assertTrue(data2["is_edited"])
        self.assertEqual(data2["original_text"], "Let us use PostgreSQL for database.")
        self.assertEqual(data2["text"], "Let us use PostgreSQL and RabbitMQ.")

    def test_transcript_revert(self):
        """Verify POST revert restores original text & speaker and clears is_edited."""
        # Edit chunk first as admin
        self.client.patch(
            f"/api/meetings/{self.meeting.id}/transcript/{self.chunk.id}",
            json={"speaker": "Alice J.", "text": "Modified text"},
            cookies={"mm_session": "admin-token"},
        )

        # Non-admin forbidden to revert
        forbidden_res = self.client.post(
            f"/api/meetings/{self.meeting.id}/transcript/{self.chunk.id}/revert",
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(forbidden_res.status_code, 403)

        # Admin reverts
        res = self.client.post(
            f"/api/meetings/{self.meeting.id}/transcript/{self.chunk.id}/revert",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["chunk"]
        self.assertFalse(data["is_edited"])
        self.assertIsNone(data["original_text"])
        self.assertEqual(data["text"], "Let us use PostgreSQL for database.")
        self.assertEqual(data["speaker"], "Alice")

    def test_transcript_create_and_delete(self):
        """Verify POST create adds chunk with is_edited=True and DELETE removes it."""
        # Non-admin create forbidden
        forbidden_create = self.client.post(
            f"/api/meetings/{self.meeting.id}/transcript",
            json={"speaker": "Bob", "text": "I agree with the plan."},
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(forbidden_create.status_code, 403)

        # Admin create
        res = self.client.post(
            f"/api/meetings/{self.meeting.id}/transcript",
            json={"speaker": "Bob", "text": "I agree with the plan."},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        new_chunk_id = res.json()["chunk"]["id"]
        self.assertTrue(res.json()["chunk"]["is_edited"])

        # Non-admin delete forbidden
        forbidden_del = self.client.delete(
            f"/api/meetings/{self.meeting.id}/transcript/{new_chunk_id}",
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(forbidden_del.status_code, 403)

        # Admin delete
        del_res = self.client.delete(
            f"/api/meetings/{self.meeting.id}/transcript/{new_chunk_id}",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(del_res.status_code, 200, del_res.text)
        self.assertTrue(del_res.json()["ok"])

    @patch("app.main.ControllerAgent")
    def test_resummarize_endpoint(self, mock_agent_class):
        """Verify POST resummarize re-runs synthesis and updates meeting summary."""
        mock_agent_instance = MagicMock()
        mock_agent_class.return_value = mock_agent_instance

        async def fake_report(meeting_id, db, team_id=None, on_thought=None, **kwargs):
            if on_thought:
                t = {"id": "test-1", "time": "12:00:00", "agent": "scrum_master", "title": "Test Thought", "text": "Testing", "stage": "synthesis"}
                if asyncio.iscoroutinefunction(on_thought):
                    await on_thought(t)
                else:
                    on_thought(t)
            m = db.get(Meeting, meeting_id)
            if m:
                m.summary = {"scrum_master": {"summary": "Regenerated summary", "decisions": ["D2"]}}
                db.commit()
            return {"summary": "Regenerated summary", "decisions": ["D2"]}

        mock_agent_instance.generate_final_report.side_effect = fake_report

        # Non-admin forbidden
        forbidden_res = self.client.post(
            f"/api/meetings/{self.meeting.id}/resummarize",
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(forbidden_res.status_code, 403)

        # Admin ok
        res = self.client.post(
            f"/api/meetings/{self.meeting.id}/resummarize",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "processing")

        # Verify background task updated database record
        with TestingSessionLocal() as db:
            updated_meeting = db.get(Meeting, self.meeting.id)
            self.assertIsNotNone(updated_meeting.summary)
            self.assertEqual(
                updated_meeting.summary["scrum_master"]["summary"],
                "Regenerated summary",
            )

    def test_stop_summary(self):
        """Verify POST /api/meetings/{id}/stop-summary cancels generation."""
        # Non-admin forbidden
        forbidden_res = self.client.post(
            f"/api/meetings/{self.meeting.id}/stop-summary",
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(forbidden_res.status_code, 403)

        # Admin ok
        res = self.client.post(
            f"/api/meetings/{self.meeting.id}/stop-summary",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

    def test_summary_thoughts_endpoint(self):
        """Verify GET /api/meetings/{id}/summary-thoughts returns thoughts array."""
        res = self.client.get(
            f"/api/meetings/{self.meeting.id}/summary-thoughts",
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["thoughts"], list)

    def test_update_me_profile(self):
        """Verify PATCH /api/auth/me updates user display name and photo."""
        import base64
        test_photo = base64.b64encode(b"fake-image-bytes").decode()
        
        # Unauthenticated request rejected
        unauth_res = self.client.patch("/api/auth/me", json={"name": "New Admin Name"})
        self.assertEqual(unauth_res.status_code, 401)

        # Authenticated update name and photo
        res = self.client.patch(
            "/api/auth/me",
            json={"name": "Updated Admin Name", "photo_b64": test_photo},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()["user"]
        self.assertEqual(data["name"], "Updated Admin Name")
        self.assertTrue(data["has_photo"])
        self.assertIsNotNone(data["photo_url"])

        # Fetch photo to verify binary
        photo_res = self.client.get(data["photo_url"])
        self.assertEqual(photo_res.status_code, 200)
        self.assertEqual(photo_res.content, b"fake-image-bytes")


if __name__ == "__main__":
    unittest.main()
