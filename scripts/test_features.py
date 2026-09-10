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

    def test_meeting_type_modes(self):
        """Verify meeting_type is saved and returned across meeting endpoints."""
        # 1. Existing meeting in setUp defaults to 'general'
        res = self.client.get(
            "/api/meetings/1",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get("meeting_type"), "general")

        # 2. list_meetings returns meeting_type
        list_res = self.client.get(
            "/api/meetings?team_id=1",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(list_res.status_code, 200)
        meetings = list_res.json().get("meetings", [])
        self.assertTrue(any(m["id"] == 1 and m["meeting_type"] == "general" for m in meetings))

        # 3. start_meeting persists custom meeting_type (e.g. daily_standup)
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                content=b'{"id": "vexa-standup-1", "title": "Daily Standup"}',
                json=lambda: {"id": "vexa-standup-1", "title": "Daily Standup"},
                raise_for_status=lambda: None,
            )
            with patch.dict("os.environ", {"VEXA_API_KEY": "fake-key"}):
                start_res = self.client.post(
                    "/api/meetings/start",
                    json={
                        "platform": "google_meet",
                        "native_id": "standup-room-1",
                        "team_id": 1,
                        "meeting_type": "daily_standup",
                    },
                    cookies={"mm_session": "admin-token"},
                )
                self.assertEqual(start_res.status_code, 200, start_res.text)
                self.assertEqual(start_res.json().get("meeting_type"), "daily_standup")
                new_id = start_res.json()["meeting_id"]

        get_res = self.client.get(
            f"/api/meetings/{new_id}",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.json().get("meeting_type"), "daily_standup")

        # 4. update_meeting can update meeting_type
        patch_res = self.client.patch(
            f"/api/meetings/{new_id}",
            json={"meeting_type": "sprint"},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json().get("meeting_type"), "sprint_planning")

        # 5. create action with action_type='blocker'
        action_res = self.client.post(
            f"/api/meetings/{new_id}/actions",
            json={"action_type": "blocker", "content": "Database migration locked"},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(action_res.status_code, 200, action_res.text)

        # 6. list_actions returns blocker under "blocker"
        actions_list = self.client.get(
            f"/api/meetings/{new_id}/actions",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(actions_list.status_code, 200)
        actions_data = actions_list.json()
        self.assertIn("blocker", actions_data)
        self.assertTrue(any(a["content"] == "Database migration locked" for a in actions_data["blocker"]["accepted"]))

    def test_agile_roles_and_preferences(self):
        """Verify Agile roles creation, joining, updating, and self notification preferences."""
        from app.api.teams import DEFAULT_ROLE_PREFERENCES

        # 1. Create team assigns creator scrum_master with default preferences
        team_res = self.client.post(
            "/api/teams",
            json={"name": "Agile Alpha"},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(team_res.status_code, 200, team_res.text)
        team_id = team_res.json()["id"]

        members_res = self.client.get(
            f"/api/teams/{team_id}/members",
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(members_res.status_code, 200)
        creator_m = next(m for m in members_res.json()["members"] if m["id"] == 1)
        self.assertEqual(creator_m["role"], "scrum_master")
        self.assertEqual(creator_m["notification_preferences"], DEFAULT_ROLE_PREFERENCES["scrum_master"])

        # 2. Member joins team: assigned team_member with team_member preferences
        team_obj = self.db.get(Team, team_id)
        join_res = self.client.post(
            f"/api/teams/join/{team_obj.invite_token}",
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(join_res.status_code, 200)

        members_res = self.client.get(
            f"/api/teams/{team_id}/members",
            cookies={"mm_session": "admin-token"},
        )
        member_m = next(m for m in members_res.json()["members"] if m["id"] == 2)
        self.assertEqual(member_m["role"], "team_member")
        self.assertEqual(member_m["notification_preferences"], DEFAULT_ROLE_PREFERENCES["team_member"])

        # 3. Owner updates member role to product_manager -> auto-refreshes preferences
        update_role_res = self.client.patch(
            f"/api/teams/{team_id}/members/2",
            json={"role": "product_manager"},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(update_role_res.status_code, 200, update_role_res.text)
        self.assertEqual(update_role_res.json()["role"], "product_manager")
        self.assertEqual(update_role_res.json()["notification_preferences"], DEFAULT_ROLE_PREFERENCES["product_manager"])

        # 4. Non-owner member can update their OWN notification preferences without 403
        self_update_res = self.client.patch(
            f"/api/teams/{team_id}/members/2",
            json={"notification_preferences": ["type:blocker", "type:parking_lot:off"]},
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(self_update_res.status_code, 200, self_update_res.text)
        self.assertEqual(self_update_res.json()["notification_preferences"], ["type:blocker", "type:parking_lot:off"])

        # 5. Non-owner member CANNOT update other member's preferences (HTTP 403)
        other_update_res = self.client.patch(
            f"/api/teams/{team_id}/members/1",
            json={"notification_preferences": ["type:to_do"]},
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(other_update_res.status_code, 403)

        # 6. Non-owner member CANNOT update roles (HTTP 403)
        role_forbidden_res = self.client.patch(
            f"/api/teams/{team_id}/members/2",
            json={"role": "scrum_master"},
            cookies={"mm_session": "member-token"},
        )
        self.assertEqual(role_forbidden_res.status_code, 403)

        # 7. Invalid role is rejected with HTTP 400
        invalid_role_res = self.client.patch(
            f"/api/teams/{team_id}/members/2",
            json={"role": "superman"},
            cookies={"mm_session": "admin-token"},
        )
        self.assertEqual(invalid_role_res.status_code, 400)

    def test_scrum_master_edit_authorization(self):
        """Verify scrum_master role grants meeting edit permissions in _check_can_edit_meeting."""
        # Add user 3 as scrum_master in team 1
        self.db.add(TeamMembership(user_id=3, team_id=1, role="scrum_master"))
        self.db.commit()

        can_edit = _check_can_edit_meeting(self.db, 3, self.meeting)
        self.assertTrue(can_edit)


if __name__ == "__main__":
    unittest.main()
