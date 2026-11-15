import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, call, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

import retry_http
import translator

# Unit tests use placeholders only. No .env or external services are accessed.
TEST_SENDER = "962700000000"
SECOND_SENDER = "962700000001"
MEDIA_ID = "123456789012345"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123&token=private-media-token"
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
        self.audio_reply = AsyncMock()
        self.audio_reply_patch = patch("whatsapp.reply_with_audio_url", self.audio_reply)
        self.audio_reply_patch.start()
        self.addCleanup(self.audio_reply_patch.stop)
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

    def audio_event(self, *, sender=None, phone_id=None, voice=True):
        event = self.event(sender=sender, phone_id=phone_id, message_type="audio")
        message = event["entry"][0]["changes"][0]["value"]["messages"][0]
        message.pop("text")
        message["audio"] = {
            "id": MEDIA_ID, "url": MEDIA_URL, "mime_type": "audio/ogg", "voice": voice,
        }
        return event

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
        self.audio_reply.assert_not_awaited()

    async def test_voice_and_other_audio_route_to_audio_handler(self):
        for voice in (True, False):
            with self.subTest(voice=voice):
                self.audio_reply.reset_mock()
                response = await self.post_event(self.audio_event(voice=voice))
                self.assertEqual(response.status_code, 200)
                self.audio_reply.assert_awaited_once_with(TEST_SENDER, MEDIA_ID, MEDIA_URL)
        self.reply.assert_not_awaited()

    async def test_audio_without_webhook_url_can_use_media_lookup(self):
        for present in (False, True):
            with self.subTest(null_url=present):
                self.audio_reply.reset_mock()
                event = self.audio_event()
                audio = event["entry"][0]["changes"][0]["value"]["messages"][0]["audio"]
                if present:
                    audio["url"] = None
                else:
                    audio.pop("url")
                response = await self.post_event(event)
                self.assertEqual(response.status_code, 200)
                self.audio_reply.assert_awaited_once_with(TEST_SENDER, MEDIA_ID, None)
        self.reply.assert_not_awaited()

    async def test_audio_retains_signature_allowlist_and_phone_filters(self):
        cases = [
            (self.audio_event(), False, 403),
            (self.audio_event(sender="10000000000"), True, 200),
            (self.audio_event(phone_id="999"), True, 200),
        ]
        for event, signed, expected_status in cases:
            with self.subTest(signed=signed, event=event):
                response = await self.post_event(event, signed=signed)
                self.assertEqual(response.status_code, expected_status)
        self.reply.assert_not_awaited()
        self.audio_reply.assert_not_awaited()

    async def test_mixed_batch_routes_text_and_audio_to_their_own_senders(self):
        event = self.event(text="translate this")
        messages = event["entry"][0]["changes"][0]["value"]["messages"]
        messages.append({
            "from": SECOND_SENDER, "type": "audio",
            "audio": {"id": MEDIA_ID, "url": MEDIA_URL, "voice": True},
        })
        with patch.object(whatsapp, "ALLOWED_SENDERS", frozenset({TEST_SENDER, SECOND_SENDER})):
            response = await self.post_event(event)
        self.assertEqual(response.status_code, 200)
        self.reply.assert_awaited_once_with(TEST_SENDER, "translate this")
        self.audio_reply.assert_awaited_once_with(SECOND_SENDER, MEDIA_ID, MEDIA_URL)

    async def test_malformed_audio_prevents_all_work_in_mixed_batch(self):
        invalid_audio = [
            None, [], {}, {"id": None}, {"id": 123}, {"id": ""},
            {"id": "../messages"}, {"id": "\u0661\u0662\u0663"},
            {"id": MEDIA_ID, "url": 123}, {"id": MEDIA_ID, "url": ""},
            {"id": MEDIA_ID, "url": "http://lookaside.fbsbx.com/audio"},
            {"id": MEDIA_ID, "url": "https://user:password@example.com/audio"},
            {"id": MEDIA_ID, "url": "https://example.com/audio file"},
            {"id": MEDIA_ID, "url": "https://example.com/" + "a" * 4096},
        ]
        for audio in invalid_audio:
            with self.subTest(audio=audio):
                event = self.event()
                event["entry"][0]["changes"][0]["value"]["messages"].append({
                    "from": TEST_SENDER, "type": "audio", "audio": audio,
                })
                response = await self.post_event(event)
                self.assertEqual(response.status_code, 400)
        self.reply.assert_not_awaited()
        self.audio_reply.assert_not_awaited()

    async def test_malformed_text_after_audio_does_not_send_audio_reply(self):
        event = self.audio_event()
        event["entry"][0]["changes"][0]["value"]["messages"].append({
            "from": TEST_SENDER, "type": "text", "text": {"body": None},
        })
        response = await self.post_event(event)
        self.assertEqual(response.status_code, 400)
        self.reply.assert_not_awaited()
        self.audio_reply.assert_not_awaited()

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


class AudioUrlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env_patch = patch.dict(os.environ, {
            "PUBLIC_BASE_URL": "https://voice.example", "RENDER_EXTERNAL_URL": "https://fallback.onrender.com",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=whatsapp.app), base_url="https://voice.example",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    def signed_path(self, *, media_id=MEDIA_ID, expires=None):
        if expires is None:
            expires = int(time.time()) + 600
        signature = hmac.new(
            whatsapp.APP_SECRET, f"audio:{media_id}:{expires}".encode(), hashlib.sha256,
        ).hexdigest()
        return f"/media/audio/{media_id}?" + urlencode({"expires": expires, "signature": signature})

    def assert_safe_signed_link(self, link, *, base="https://voice.example"):
        parsed = urlsplit(link)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}", base)
        self.assertEqual(parsed.path, f"/media/audio/{MEDIA_ID}")
        query = parse_qs(parsed.query)
        self.assertEqual(set(query), {"expires", "signature"})
        expires = int(query["expires"][0])
        self.assertGreaterEqual(expires, int(time.time()) + 895)
        self.assertLessEqual(expires, int(time.time()) + 900)
        expected = hmac.new(
            whatsapp.APP_SECRET, f"audio:{MEDIA_ID}:{expires}".encode(), hashlib.sha256,
        ).hexdigest()
        self.assertEqual(query["signature"], [expected])
        for secret in (whatsapp.ACCESS_TOKEN, whatsapp.APP_SECRET.decode(), "private-media-token"):
            self.assertNotIn(secret, link)

    async def serve_link(self, *, metadata=None, audio_response=None, link=None):
        requests = []
        client_factory = httpx.AsyncClient
        if metadata is None:
            metadata = httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "audio/ogg"})
        if audio_response is None:
            audio_response = httpx.Response(200, content=b"OggS-synthetic-voice", headers={"Content-Type": "audio/ogg"})

        async def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["authorization"], f"Bearer {whatsapp.ACCESS_TOKEN}")
            if request.url.host == "graph.facebook.com":
                self.assertEqual(request.url.path, f"/{whatsapp.VERSION}/{MEDIA_ID}")
                self.assertEqual(dict(request.url.params), {"phone_number_id": whatsapp.PHONE_ID})
                response = metadata
            else:
                self.assertIn(request.url.host, {"lookaside.fbsbx.com", "lookaside.facebook.com"})
                response = audio_response
            if isinstance(response, type) and issubclass(response, httpx.RequestError):
                raise response("private-upstream-details " + MEDIA_URL, request=request)
            return response

        def fake_client(**kwargs):
            return client_factory(transport=httpx.MockTransport(handler), **kwargs)

        with patch("whatsapp.httpx.AsyncClient", side_effect=fake_client):
            with patch("whatsapp.translate_message", new_callable=AsyncMock) as translate:
                response = await self.client.get(link or self.signed_path())
                translate.assert_not_awaited()
        return response, requests

    async def test_audio_reply_builds_private_safe_link_without_media_lookup(self):
        for media_url in (None, MEDIA_URL):
            with self.subTest(webhook_url_present=media_url is not None):
                with patch("whatsapp.send_text", new_callable=AsyncMock) as send:
                    with patch("whatsapp.httpx.AsyncClient", side_effect=AssertionError("No media lookup during reply")):
                        with patch("whatsapp.translate_message", new_callable=AsyncMock) as translate:
                            await whatsapp.reply_with_audio_url(SECOND_SENDER, MEDIA_ID, media_url)
                send.assert_awaited_once()
                self.assertEqual(send.await_args.args[0], SECOND_SENDER)
                self.assert_safe_signed_link(send.await_args.args[1])
                translate.assert_not_awaited()

    async def test_signed_link_uses_public_base_before_render_fallback(self):
        self.assertEqual(whatsapp.AUDIO_LINK_TTL, 900)
        self.assert_safe_signed_link(whatsapp.build_audio_link(MEDIA_ID))
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}):
            self.assert_safe_signed_link(
                whatsapp.build_audio_link(MEDIA_ID), base="https://fallback.onrender.com",
            )

    async def test_missing_or_invalid_public_base_sends_one_fallback(self):
        for base in ("", "http://voice.example", "https://user:password@voice.example", "https://voice.example/has space"):
            with self.subTest(base=base), patch.dict(os.environ, {"PUBLIC_BASE_URL": base, "RENDER_EXTERNAL_URL": ""}):
                with patch("whatsapp.send_text", new_callable=AsyncMock) as send:
                    with patch("whatsapp.httpx.AsyncClient", side_effect=AssertionError("No media lookup during reply")):
                        await whatsapp.reply_with_audio_url(SECOND_SENDER, MEDIA_ID, MEDIA_URL)
                send.assert_awaited_once()
                recipient, fallback = send.await_args.args
                self.assertEqual(recipient, SECOND_SENDER)
                self.assertTrue(any("\u0600" <= letter <= "\u06ff" for letter in fallback))
                self.assertNotIn("https://", fallback)
                self.assertNotIn(whatsapp.ACCESS_TOKEN, fallback)

    async def test_tampering_and_expiry_are_rejected_before_any_outbound_request(self):
        valid = self.signed_path()
        query = parse_qs(urlsplit(valid).query)
        changed_expiry = int(query["expires"][0]) + 1
        cases = [
            (valid.replace(MEDIA_ID, MEDIA_ID + "1"), 403),
            (valid.replace(query["signature"][0], "0" * 64), 403),
            (valid.replace(query["expires"][0], str(changed_expiry)), 403),
            (self.signed_path(media_id="not-a-number"), 403),
            (self.signed_path(expires=int(time.time()) - 1), 410),
        ]
        with patch("whatsapp.httpx.AsyncClient", side_effect=AssertionError("Invalid links must not contact Meta")):
            for link, status in cases:
                with self.subTest(status=status, link=link):
                    response = await self.client.get(link)
                    self.assertEqual(response.status_code, status)

    async def test_signed_link_fetches_audio_with_security_headers(self):
        link = whatsapp.build_audio_link(MEDIA_ID)
        response, requests = await self.serve_link(link=link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OggS-synthetic-voice")
        self.assertEqual(len(requests), 2)
        self.assertTrue(response.headers["content-type"].startswith("audio/ogg"))
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertIn(f"voice-{MEDIA_ID}.ogg", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    async def test_untrusted_metadata_url_never_receives_authorization(self):
        bad_urls = [
            "https://attacker.example/audio", "https://lookaside.fbsbx.com.attacker.example/audio",
            "http://lookaside.fbsbx.com/audio", "https://user:password@lookaside.fbsbx.com/audio",
            "https://lookaside.fbsbx.com:444/audio", "https://lookaside.fbsbx.com/audio\nprivate",
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                response, requests = await self.serve_link(metadata=httpx.Response(
                    200, json={"url": url, "mime_type": "audio/ogg"},
                ))
                self.assertEqual(response.status_code, 502)
                self.assertEqual(len(requests), 1)

    async def test_invalid_metadata_or_upstream_failures_are_private_safe(self):
        cases = [
            (httpx.Response(401, json={"error": {"message": whatsapp.ACCESS_TOKEN + MEDIA_URL}}), None),
            (httpx.ConnectError, None),
            (httpx.Response(200, content=b"not JSON"), None),
            (httpx.Response(200, json=[]), None),
            (httpx.Response(200, content=b"null"), None),
            (httpx.Response(200, json={}), None),
            (httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "text/html"}), None),
            (None, httpx.Response(503, text="private-upstream-details " + whatsapp.ACCESS_TOKEN)),
            (None, httpx.ReadTimeout),
            (None, httpx.Response(302, headers={"Location": "https://attacker.example/stolen"})),
        ]
        for metadata, audio in cases:
            with self.subTest(metadata=metadata, audio=audio):
                with self.assertLogs(whatsapp.logger, level="ERROR") as recorded:
                    response, requests = await self.serve_link(metadata=metadata, audio_response=audio)
                self.assertEqual(response.status_code, 502)
                self.assertLessEqual(len(requests), 2)
                output = "\n".join(recorded.output) + response.text
                for private in (MEDIA_URL, whatsapp.ACCESS_TOKEN, "private-upstream-details", "attacker.example"):
                    self.assertNotIn(private, output)

    async def test_stream_size_limit_stops_reading_when_exceeded(self):
        class CountedAudio(httpx.AsyncByteStream):
            def __init__(self):
                self.yielded = 0

            async def __aiter__(self):
                for chunk in (b"123", b"456", b"must-not-be-read"):
                    self.yielded += 1
                    yield chunk

        stream = CountedAudio()
        with patch.object(whatsapp, "MAX_AUDIO_BYTES", 5):
            response, _ = await self.serve_link(audio_response=httpx.Response(
                200, stream=stream, headers={"Content-Type": "audio/ogg"},
            ))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(stream.yielded, 2)


if __name__ == "__main__":
    unittest.main()
