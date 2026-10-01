from __future__ import annotations

from datetime import timedelta
import unittest

from autofollow.db import TrackedThread
from autofollow.followup import find_external_reply, guess_name, is_due, latest_outgoing_at, render_template
from autofollow.timeutil import to_iso, utc_now


def fake_thread(**overrides):
    now = utc_now()
    data = dict(
        id=1,
        recipient="alex@example.com",
        recipient_name="Alex",
        subject="Test subject",
        thread_id="thread-1",
        initial_message_id="gmail-1",
        initial_rfc_message_id="<initial@example.com>",
        initial_sent_at_utc=to_iso(now - timedelta(days=6)),
        latest_known_message_count=1,
        followup_delay_days=5,
        max_followups=1,
        followup_count=0,
        followup_template="Hi {name},\n\nFollowing up.\n\nBest,\n{sender_name}\n",
        status="waiting",
        last_followup_at_utc=None,
        last_checked_at_utc=None,
        replied_at_utc=None,
        error_message=None,
        created_at_utc=to_iso(now - timedelta(days=6)),
        updated_at_utc=to_iso(now - timedelta(days=6)),
    )
    data.update(overrides)
    return TrackedThread(**data)


class FollowupLogicTests(unittest.TestCase):
    def test_missing_name_uses_neutral_greeting(self):
        self.assertEqual(guess_name("jsmith23@example.edu"), "there")
        self.assertEqual(guess_name("admissions@example.edu"), "there")
        self.assertEqual(guess_name("Alex Smith <alex@example.edu>"), "Alex")
        self.assertEqual(guess_name("alex@example.edu", "Professor Smith"), "Professor Smith")

    def test_drafts_are_neither_replies_nor_outgoing_deliveries(self):
        now = utc_now()
        for sender in ("kai@example.com", "alex@example.com"):
            thread = {"messages": [{"id": "draft", "internalDate": str(int(now.timestamp() * 1000)),
                "labelIds": ["DRAFT"],
                "payload": {"headers": [{"name": "From", "value": sender}]}}]}
            self.assertIsNone(find_external_reply(thread, own_addresses={"kai@example.com"},
                after_utc=to_iso(now - timedelta(days=1))))
            self.assertIsNone(latest_outgoing_at(thread, {"kai@example.com"}))

    def test_sent_alias_is_not_a_reply_and_resets_outgoing_time(self):
        now = utc_now()
        thread = {"messages": [{"id": "alias", "internalDate": str(int(now.timestamp() * 1000)),
            "labelIds": ["SENT"],
            "payload": {"headers": [{"name": "From", "value": "alias@example.edu"}]}}]}
        self.assertIsNone(find_external_reply(thread, own_addresses={"kai@example.com"},
            after_utc=to_iso(now - timedelta(days=1))))
        self.assertEqual(latest_outgoing_at(thread, {"kai@example.com"}), to_iso(now))

    def test_same_second_reply_is_detected_with_initial_message_anchor(self):
        now = utc_now()
        messages = [{"id": message_id, "internalDate": str(int(now.timestamp() * 1000) + offset),
            "payload": {"headers": [{"name": "From", "value": sender}]}}
            for message_id, offset, sender in [
                ("original", 100, "alias@example.edu"),
                ("reply", 600, "professor@example.edu")]]
        reply = find_external_reply({"messages": messages}, own_addresses={"kai@example.com"},
            after_utc=to_iso(now), initial_message_id="original")
        self.assertEqual(reply.gmail_message_id, "reply")

    def test_own_addresses_are_case_insensitive(self):
        now = utc_now()
        thread = {"messages": [{"id": "own", "internalDate": str(int(now.timestamp() * 1000)),
            "payload": {"headers": [{"name": "From", "value": "Kai <KAI@example.com>"}]}}]}
        self.assertIsNone(find_external_reply(thread, own_addresses={"Kai@Example.com"},
            after_utc=to_iso(now - timedelta(days=1))))

    def test_is_due_after_delay(self):
        self.assertTrue(is_due(fake_thread()))

    def test_not_due_if_maxed(self):
        self.assertFalse(is_due(fake_thread(followup_count=1)))

    def test_template_rendering(self):
        body = render_template(
            "Hi {name}, follow-up {followup_number}/{max_followups} from {sender_name}",
            thread=fake_thread(),
            sender_name="Kai",
            sender_email="kai@example.com",
        )
        self.assertIn("Hi Alex", body)
        self.assertIn("1/1", body)
        self.assertIn("Kai", body)

    def test_external_reply_detection(self):
        sent_time = utc_now() - timedelta(days=1)
        internal_ms = int(utc_now().timestamp() * 1000)
        thread = {
            "messages": [
                {
                    "id": "m1",
                    "internalDate": str(int((sent_time - timedelta(minutes=1)).timestamp() * 1000)),
                    "payload": {"headers": [{"name": "From", "value": "kai@example.com"}]},
                },
                {
                    "id": "m2",
                    "internalDate": str(internal_ms),
                    "payload": {"headers": [{"name": "From", "value": "Alex <alex@example.com>"}]},
                },
            ]
        }
        reply = find_external_reply(thread, own_addresses={"kai@example.com"}, after_utc=to_iso(sent_time))
        self.assertIsNotNone(reply)
        self.assertEqual(reply.gmail_message_id, "m2")


if __name__ == "__main__":
    unittest.main()
