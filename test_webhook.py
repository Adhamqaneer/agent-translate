import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

with patch.dict(os.environ, {
    "WA_PHONE_NUMBER_ID": "123", "WA_TEST_RECIPIENT": "962700000000",
    "WA_GRAPH_VERSION": "v25.0", "WA_ACCESS_TOKEN": "unit-test-token",
    "META_APP_SECRET": "unit-test-secret", "WA_VERIFY_TOKEN": "unit-test-verify",
}):
    import whatsapp

from translator import translate


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reply = AsyncMock()
        self.reply_patch = patch("whatsapp.translate_and_reply", self.reply)
        self.reply_patch.start()
        self.addCleanup(self.reply_patch.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=whatsapp.app), base_url="http://test.invalid",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    def event(self, *, sender=None, phone_id=None, message_type="text", text="Hello"):
        return {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": phone_id or whatsapp.PHONE_ID},
            "messages": [{"from": sender or whatsapp.TEST_RECIPIENT,
                          "type": message_type, "text": {"body": text}}],
        }}]}]}

    async def post_event(self, event=None, *, raw=None, signed=True):
        body = raw if raw is not None else json.dumps(event).encode()
        signature = "sha256=" + hmac.new(whatsapp.APP_SECRET, body, hashlib.sha256).hexdigest()
        return await self.client.post(
            "/webhook/whatsapp", content=body,
            headers={"X-Hub-Signature-256": signature if signed else "sha256=invalid"},
        )

    async def test_health_does_not_schedule_translation(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.reply.assert_not_awaited()

    async def test_webhook_verification_challenge(self):
        response = await self.client.get("/webhook/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": whatsapp.VERIFY_TOKEN.decode(),
            "hub.challenge": "12345",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "12345")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    async def test_invalid_webhook_verification_is_rejected(self):
        for params in ({}, {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"}):
            with self.subTest(params=params):
                response = await self.client.get("/webhook/whatsapp", params=params)
                self.assertEqual(response.status_code, 403)

    async def test_allowed_signed_text_schedules_one_reply(self):
        response = await self.post_event(self.event())
        self.assertEqual(response.status_code, 200)
        self.reply.assert_awaited_once_with(whatsapp.TEST_RECIPIENT, "Hello")

    async def test_bad_signature_is_rejected(self):
        response = await self.post_event(self.event(), signed=False)
        self.assertEqual(response.status_code, 403)
        self.reply.assert_not_awaited()

    async def test_non_target_events_are_ignored(self):
        events = [self.event(sender="10000000000"), self.event(phone_id="999"),
                  self.event(message_type="image"), self.event(text="   "),
                  {"entry": [{"changes": [{"field": "messages", "value": {
                      "metadata": {"phone_number_id": whatsapp.PHONE_ID}, "statuses": [],
                  }}]}]}]
        for event in events:
            with self.subTest(event=event):
                response = await self.post_event(event)
                self.assertEqual(response.status_code, 200)
        self.reply.assert_not_awaited()

    async def test_malformed_signed_json_is_rejected(self):
        response = await self.post_event(raw=b"{")
        self.assertEqual(response.status_code, 400)
        self.reply.assert_not_awaited()

    async def test_malformed_payload_shapes_are_rejected_without_replies(self):
        events = [[], None, {"entry": None}, {"entry": [None]},
                  {"entry": [{"changes": [None]}]},
                  {"entry": [{"changes": [{"field": "messages", "value": []}]}]}]
        invalid_body = self.event(text=None)
        events.append(invalid_body)
        for event in events:
            with self.subTest(event=event):
                response = await self.post_event(event)
                self.assertEqual(response.status_code, 400)
        self.reply.assert_not_awaited()

    async def test_no_partial_work_is_scheduled_for_a_malformed_batch(self):
        event = self.event()
        event["entry"].append(None)
        response = await self.post_event(event)
        self.assertEqual(response.status_code, 400)
        self.reply.assert_not_awaited()

    async def test_malformed_successful_gemini_shapes_become_handled_failures(self):
        payloads = [[], None, {"candidates": [None]}, {"candidates": {"first": {}}},
                    {"candidates": [{"finishReason": "STOP", "content": None}]},
                    {"candidates": [{"finishReason": "STOP", "content": {"parts": [None]}}]},
                    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": 123}]}}]}]
        for payload in payloads:
            with self.subTest(payload=payload):
                async def handler(request):
                    return httpx.Response(200, content=json.dumps(payload).encode())

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(RuntimeError):
                        await translate("synthetic input", client, api_key="unit-test-key")


if __name__ == "__main__":
    unittest.main()
