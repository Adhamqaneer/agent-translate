import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import os
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

import retry_http
import translator

# Unit tests use placeholders only. No .env or external services are accessed.
TEST_SENDER = "962700000000"
SECOND_SENDER = "962700000001"
os.environ.update({
    "WA_PHONE_NUMBER_ID": "123", "WA_ALLOWED_SENDERS": TEST_SENDER,
    "WA_GRAPH_VERSION": "v25.0", "WA_ACCESS_TOKEN": "unit-test-token",
    "META_APP_SECRET": "unit-test-secret", "WA_VERIFY_TOKEN": "unit-test-verify",
    "GEMINI_API_KEY": "unit-test-key",
})
import whatsapp


class SenderConfigurationTests(unittest.TestCase):
    def test_allowlist_is_required_when_missing_or_blank(self):
        for configured in (None, "", "   "):
            with self.subTest(configured=configured), patch.dict(os.environ):
                if configured is None:
                    os.environ.pop("WA_ALLOWED_SENDERS", None)
                else:
                    os.environ["WA_ALLOWED_SENDERS"] = configured
                with self.assertRaises(RuntimeError):
                    whatsapp._load_allowed_senders()

    def test_allowlist_accepts_single_or_multiple_normalized_numbers(self):
        configurations = [
            (" +" + TEST_SENDER + " ", frozenset({TEST_SENDER})),
            (" +" + TEST_SENDER + " , " + SECOND_SENDER + "," + TEST_SENDER,
             frozenset({TEST_SENDER, SECOND_SENDER})),
        ]
        for configured, expected in configurations:
            with self.subTest(configured=configured), patch.dict(os.environ, {"WA_ALLOWED_SENDERS": configured}):
                self.assertEqual(whatsapp._load_allowed_senders(), expected)

    def test_invalid_sender_configuration_is_rejected(self):
        configurations = [
            ",", TEST_SENDER + ",", "*", "00962700000000", "9627 00000000",
            "123456", "1234567890123456", "++" + TEST_SENDER,
            "\u0669\u0666\u0662\u0667\u0660\u0660\u0660\u0660",
        ]
        for configured in configurations:
            with self.subTest(configured=configured), patch.dict(os.environ, {"WA_ALLOWED_SENDERS": configured}):
                with self.assertRaises(RuntimeError):
                    whatsapp._load_allowed_senders()


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep = AsyncMock()
        self.sleep_patch = patch("retry_http.asyncio.sleep", self.sleep)
        self.jitter_patch = patch("retry_http.random.uniform", return_value=0.0)
        self.sleep_patch.start()
        self.jitter_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.addCleanup(self.jitter_patch.stop)

    async def request_sequence(self, outcomes, service="gemini"):
        self.requests = []
        iterator = iter(outcomes)

        async def handler(request):
            self.requests.append(request)
            outcome = next(iterator)
            if isinstance(outcome, type) and issubclass(outcome, Exception):
                raise outcome("simulated failure", request=request)
            if isinstance(outcome, BaseException):
                raise outcome
            status, body, headers = outcome
            return httpx.Response(status, json=body, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await retry_http.post_with_retry(
                client, "https://service.invalid/messages", service=service,
                json={"text": "synthetic message"},
            )

    async def run_pipeline(self, handler):
        client_factory = httpx.AsyncClient

        def fake_client(**kwargs):
            return client_factory(transport=httpx.MockTransport(handler), **kwargs)

        with patch("whatsapp.httpx.AsyncClient", side_effect=fake_client):
            await whatsapp.translate_and_reply("962700000000", "synthetic source")

    async def test_success_has_no_retry_or_wait(self):
        response = await self.request_sequence([(200, {}, {})])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_awaited()

    async def test_gemini_temporary_http_failures_recover(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                response = await self.request_sequence([(status, {}, {}), (200, {}, {})])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.requests), 2)

    async def test_attempts_are_bounded_with_increasing_delay(self):
        response = await self.request_sequence([(503, {}, {})] * 3)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [1.0, 2.0])

    async def test_gemini_permanent_errors_stop_immediately(self):
        for status in (400, 401, 402, 403, 404, 501):
            with self.subTest(status=status):
                response = await self.request_sequence([(status, {}, {})])
                self.assertEqual(response.status_code, status)
                self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_awaited()

    async def test_gemini_read_timeout_can_recover(self):
        response = await self.request_sequence([httpx.ReadTimeout, (200, {}, {})])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.requests), 2)

    async def test_connection_attempts_are_bounded(self):
        with self.assertRaises(httpx.ConnectError):
            await self.request_sequence([httpx.ConnectError] * 3)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.sleep.await_count, 2)

    async def test_whatsapp_connection_failures_can_recover(self):
        for error in retry_http.SAFE_CONNECTION_ERRORS:
            with self.subTest(error=error.__name__):
                response = await self.request_sequence([error, (200, {}, {})], "whatsapp")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.requests), 2)

    async def test_whatsapp_uncertain_sends_are_not_repeated(self):
        for error in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                      httpx.WriteError, httpx.CloseError, httpx.RemoteProtocolError):
            with self.subTest(error=error.__name__), self.assertRaises(error):
                await self.request_sequence([error], "whatsapp")
            self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_awaited()

    async def test_whatsapp_explicit_temporary_rejections_can_recover(self):
        failures = [
            (429, {}, {}),
            (400, {"error": {"code": 130429}}, {}),
            (400, {"error": {"code": 131056}}, {}),
            (503, {"error": {"code": 131016}}, {}),
            (500, {"error": {"is_transient": True}}, {}),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                response = await self.request_sequence([failure, (200, {}, {})], "whatsapp")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.requests), 2)

    async def test_whatsapp_permanent_and_ambiguous_errors_stop(self):
        failures = [
            (400, {"error": {"code": 100}}, {}),
            (400, {"error": {"code": 131030}}, {}),
            (401, {"error": {"code": 190}}, {}),
            (503, {"error": {"code": 190, "is_transient": True}}, {}),
            (403, {"error": {"is_transient": True}}, {}),
            (502, "gateway outcome unknown", {}),
            (500, {}, {}),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                response = await self.request_sequence([failure], "whatsapp")
                self.assertEqual(response.status_code, failure[0])
                self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_awaited()

    async def test_retry_after_seconds_is_respected(self):
        await self.request_sequence([(429, {}, {"Retry-After": "7"}), (200, {}, {})])
        self.sleep.assert_awaited_once_with(7.0)

    async def test_retry_after_date_is_respected(self):
        date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=12))
        await self.request_sequence([(503, {}, {"Retry-After": date}), (200, {}, {})])
        delay = self.sleep.await_args.args[0]
        self.assertGreater(delay, 10)
        self.assertLessEqual(delay, 12)

    async def test_long_retry_after_stops_without_retrying_early(self):
        response = await self.request_sequence([(429, {}, {"Retry-After": "60"})])
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_awaited()

    async def test_malformed_retry_after_falls_back(self):
        for header in ("garbage", "NaN", "-1"):
            with self.subTest(header=header):
                await self.request_sequence([(503, {}, {"Retry-After": header}), (200, {}, {})])
                self.assertEqual(self.sleep.await_args.args[0], 1.0)

    async def test_gemini_retry_info_is_respected(self):
        body = {"error": {"details": [{
            "@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5.5s",
        }]}}
        await self.request_sequence([(429, body, {}), (200, {}, {})])
        self.sleep.assert_awaited_once_with(5.5)

    async def test_long_gemini_retry_info_stops(self):
        body = {"error": {"details": [{
            "@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "120s",
        }]}}
        await self.request_sequence([(429, body, {})])
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_awaited()

    async def test_pair_limit_has_a_longer_wait(self):
        await self.request_sequence([
            (400, {"error": {"code": 131056}}, {}), (200, {}, {}),
        ], "whatsapp")
        self.sleep.assert_awaited_once_with(4.0)

    async def test_cancellation_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.request_sequence([asyncio.CancelledError()])
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_awaited()

    async def test_unknown_service_is_rejected_before_request(self):
        with self.assertRaises(ValueError):
            await self.request_sequence([], "unknown")
        self.assertEqual(len(self.requests), 0)

    async def test_translation_and_send_retry_independently(self):
        requests = []
        gemini_calls = 0
        whatsapp_calls = 0

        async def handler(request):
            nonlocal gemini_calls, whatsapp_calls
            requests.append(request)
            if request.url.host == "generativelanguage.googleapis.com":
                gemini_calls += 1
                if gemini_calls == 1:
                    return httpx.Response(503, json={})
                payload = json.loads(request.content)
                self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], translator.INSTRUCTIONS)
                return httpx.Response(200, json={"candidates": [{
                    "finishReason": "STOP", "content": {"parts": [{"text": "Hello!"}]},
                }]})
            self.assertEqual(request.url.host, "graph.facebook.com")
            whatsapp_calls += 1
            self.assertEqual(json.loads(request.content)["text"]["body"], "Hello!")
            if whatsapp_calls == 1:
                return httpx.Response(429, json={})
            return httpx.Response(200, json={"messages": [{"id": "synthetic-message-id"}]})

        await self.run_pipeline(handler)
        self.assertEqual((gemini_calls, whatsapp_calls), (2, 2))
        self.assertEqual(len(requests), 4)

    async def test_meta_error_details_are_redacted(self):
        request = httpx.Request("POST", "https://graph.facebook.com/messages")
        response = httpx.Response(400, request=request, json={"error": {
            "code": 100, "error_subcode": 33,
            "message": "Invalid parameter " + whatsapp.ACCESS_TOKEN + " " + os.environ["GEMINI_API_KEY"],
            "error_data": {"details": "Bad recipient 962700000000, private-message-text, "
                          + whatsapp.APP_SECRET.decode() + " " + whatsapp.VERIFY_TOKEN.decode()
                          + " phone ID 123456789012345"},
        }})
        error = httpx.HTTPStatusError("private-request-details", request=request, response=response)
        with patch("whatsapp.post_with_retry", side_effect=error):
            with self.assertLogs(whatsapp.logger, level="ERROR") as recorded:
                await whatsapp.send_text("962700000000", "private-message-text")
        output = "\n".join(recorded.output)
        for expected in ("HTTP 400, code 100, subcode 33", "Invalid parameter", "Bad recipient", "[redacted]", "[id]"):
            self.assertIn(expected, output)
        for private_value in ("private-request-details", "private-message-text", "962700000000",
                              "123456789012345", whatsapp.ACCESS_TOKEN, whatsapp.APP_SECRET.decode(),
                              whatsapp.VERIFY_TOKEN.decode(), os.environ["GEMINI_API_KEY"]):
            self.assertNotIn(private_value, output)

    async def test_translation_exhaustion_sends_one_fallback(self):
        gemini_calls = 0
        whatsapp_payloads = []

        async def handler(request):
            nonlocal gemini_calls
            if request.url.host == "generativelanguage.googleapis.com":
                gemini_calls += 1
                return httpx.Response(503, json={})
            whatsapp_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"messages": [{"id": "synthetic-message-id"}]})

        await self.run_pipeline(handler)
        self.assertEqual(gemini_calls, 3)
        self.assertEqual(len(whatsapp_payloads), 1)
        self.assertEqual(whatsapp_payloads[0]["text"]["body"], "تعذرت الترجمة حاليًا. جرّب كمان شوي.")


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
            "messages": [{"from": sender or TEST_SENDER,
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
        self.reply.assert_awaited_once_with(TEST_SENDER, "Hello")

    async def test_two_allowed_senders_in_one_batch_receive_their_own_replies(self):
        event = self.event(sender=TEST_SENDER, text="first message")
        messages = event["entry"][0]["changes"][0]["value"]["messages"]
        messages.extend([
            {"from": SECOND_SENDER, "type": "text", "text": {"body": "second message"}},
            {"from": "10000000000", "type": "text", "text": {"body": "ignore me"}},
        ])
        with patch.object(whatsapp, "ALLOWED_SENDERS", frozenset({TEST_SENDER, SECOND_SENDER})):
            response = await self.post_event(event)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reply.await_args_list, [
            call(TEST_SENDER, "first message"), call(SECOND_SENDER, "second message"),
        ])

    async def test_concurrent_requests_keep_recipients_separate(self):
        with patch.object(whatsapp, "ALLOWED_SENDERS", frozenset({TEST_SENDER, SECOND_SENDER})):
            responses = await asyncio.gather(
                self.post_event(self.event(sender=TEST_SENDER, text="first message")),
                self.post_event(self.event(sender=SECOND_SENDER, text="second message")),
            )
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.reply.assert_has_awaits([
            call(TEST_SENDER, "first message"), call(SECOND_SENDER, "second message"),
        ], any_order=True)
        self.assertEqual(self.reply.await_count, 2)

    async def test_malformed_batch_from_second_sender_schedules_no_replies(self):
        event = self.event(sender=TEST_SENDER)
        event["entry"][0]["changes"][0]["value"]["messages"].append({
            "from": SECOND_SENDER, "type": "text", "text": {"body": None},
        })
        with patch.object(whatsapp, "ALLOWED_SENDERS", frozenset({TEST_SENDER, SECOND_SENDER})):
            response = await self.post_event(event)
        self.assertEqual(response.status_code, 400)
        self.reply.assert_not_awaited()

    async def test_malformed_sender_fields_are_ignored(self):
        for sender in (None, [], {}, 123, True):
            with self.subTest(sender=sender):
                event = self.event()
                event["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = sender
                response = await self.post_event(event)
                self.assertEqual(response.status_code, 200)
        self.reply.assert_not_awaited()

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
                        await translator.translate("synthetic input", client, api_key="unit-test-key")


if __name__ == "__main__":
    unittest.main()
