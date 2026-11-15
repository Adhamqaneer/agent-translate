"""Offline webhook, media-boundary, and delivery tests for attachments."""

import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

# Importing the existing fixture module sets placeholder credentials only.
from test_app import MEDIA_ID, MEDIA_URL, SECOND_SENDER, TEST_SENDER
import whatsapp


JPEG_BYTES = b"\xff\xd8\xff\xe0synthetic-private-image\xff\xd9"
PRIVATE_FILENAME = "private-financial-document.docx"
PRIVATE_CAPTION = "private caption that must not enter the translation pipeline"


def attachment_event(kind="image", *, sender=TEST_SENDER, phone_id=None):
    return {"object": "whatsapp_business_account", "entry": [{
        "id": "123456789", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_id or whatsapp.PHONE_ID},
            "contacts": [{"wa_id": sender, "profile": {"name": "Private Contact"}}],
            "messages": [{
                "from": sender, "id": "synthetic-incoming-message", "timestamp": "1800000000",
                "type": kind,
                kind: {"id": MEDIA_ID, "url": MEDIA_URL, "mime_type": "image/jpeg",
                       "sha256": "synthetic-sha", "caption": PRIVATE_CAPTION,
                       "filename": PRIVATE_FILENAME},
                "referral": {"source_url": "https://untrusted.example/private-ad", "body": PRIVATE_CAPTION},
            }],
        }}],
    }]}


class AttachmentWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.text = AsyncMock()
        self.audio = AsyncMock()
        self.attachment = AsyncMock()
        for name, mock in (("translate_and_reply", self.text), ("transcribe_and_reply", self.audio),
                           ("translate_attachment_and_reply", self.attachment)):
            patcher = patch(f"whatsapp.{name}", mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=whatsapp.app), base_url="http://test.invalid",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def post_event(self, event, *, signed=True):
        body = json.dumps(event).encode()
        signature = hmac.new(whatsapp.APP_SECRET, body, hashlib.sha256).hexdigest()
        return await self.client.post("/webhook/whatsapp", content=body, headers={
            "X-Hub-Signature-256": "sha256=" + (signature if signed else "invalid"),
        })

    async def test_images_and_documents_route_by_media_id_with_filename_as_a_hint_only(self):
        for kind in ("image", "document"):
            with self.subTest(kind=kind):
                self.attachment.reset_mock()
                response = await self.post_event(attachment_event(kind))
                self.assertEqual(response.status_code, 200)
                hint = {"filename": PRIVATE_FILENAME} if kind == "document" else {}
                self.attachment.assert_awaited_once_with(TEST_SENDER, MEDIA_ID, kind, **hint)
        self.text.assert_not_awaited()
        self.audio.assert_not_awaited()

    async def test_missing_or_null_webhook_url_still_routes_attachment(self):
        for kind in ("image", "document"):
            for url_present in (False, True):
                with self.subTest(kind=kind, null_url=url_present):
                    self.attachment.reset_mock()
                    event = attachment_event(kind)
                    media = event["entry"][0]["changes"][0]["value"]["messages"][0][kind]
                    if url_present:
                        media["url"] = None
                    else:
                        media.pop("url")
                    response = await self.post_event(event)
                    self.assertEqual(response.status_code, 200)
                    hint = {"filename": PRIVATE_FILENAME} if kind == "document" else {}
                    self.attachment.assert_awaited_once_with(TEST_SENDER, MEDIA_ID, kind, **hint)

    async def test_attachments_keep_signature_allowlist_phone_and_unknown_type_filters(self):
        cases = [
            (attachment_event(), False, 403),
            (attachment_event(sender="10000000000"), True, 200),
            (attachment_event(phone_id="999"), True, 200),
            (attachment_event("video"), True, 200),
        ]
        for event, signed, status in cases:
            with self.subTest(status=status, event=event):
                response = await self.post_event(event, signed=signed)
                self.assertEqual(response.status_code, status)
        self.attachment.assert_not_awaited()
        self.audio.assert_not_awaited()
        self.text.assert_not_awaited()

    async def test_mixed_batch_routes_each_media_kind_to_its_own_sender(self):
        event = attachment_event("image", sender=SECOND_SENDER)
        messages = event["entry"][0]["changes"][0]["value"]["messages"]
        messages.extend([
            {"from": TEST_SENDER, "type": "text", "text": {"body": "translate this text"}},
            {"from": SECOND_SENDER, "type": "audio", "audio": {"id": "222222", "voice": True}},
            {"from": TEST_SENDER, "type": "document", "document": {"id": "333333"}},
        ])
        with patch.object(whatsapp, "ALLOWED_SENDERS", frozenset({TEST_SENDER, SECOND_SENDER})):
            response = await self.post_event(event)
        self.assertEqual(response.status_code, 200)
        self.text.assert_awaited_once_with(TEST_SENDER, "translate this text")
        self.audio.assert_awaited_once_with(SECOND_SENDER, "222222")
        self.assertEqual(self.attachment.await_args_list, [
            call(SECOND_SENDER, MEDIA_ID, "image"), call(TEST_SENDER, "333333", "document"),
        ])

    async def test_malformed_attachment_rejects_the_whole_mixed_batch_before_scheduling(self):
        invalid_media = [None, [], {}, {"id": 123}, {"id": "../messages"}, {"id": "\u0661\u0662\u0663"},
                         {"id": MEDIA_ID, "url": "http://example.com/file"},
                         {"id": MEDIA_ID, "url": "https://user:password@example.com/file"}]
        for kind in ("image", "document"):
            for media in invalid_media:
                with self.subTest(kind=kind, media=media):
                    event = attachment_event("image")
                    messages = event["entry"][0]["changes"][0]["value"]["messages"]
                    messages.extend([
                        {"from": TEST_SENDER, "type": "text", "text": {"body": "valid text"}},
                        {"from": TEST_SENDER, "type": "audio", "audio": {"id": "222222"}},
                        {"from": TEST_SENDER, "type": kind, kind: media},
                    ])
                    response = await self.post_event(event)
                    self.assertEqual(response.status_code, 400)
        self.attachment.assert_not_awaited()
        self.audio.assert_not_awaited()
        self.text.assert_not_awaited()


class AttachmentPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_translate_and_send_all_parts_in_order_without_voice_processing(self):
        parts = ["first translated part " + "a" * 3900, "second translated part " + "b" * 3900, "last translated part"]
        sends = []
        metadata_calls = []
        media_calls = []
        client_factory = httpx.AsyncClient

        async def handler(request):
            self.assertEqual(request.headers["authorization"], f"Bearer {whatsapp.ACCESS_TOKEN}")
            if request.method == "GET" and request.url.host == "graph.facebook.com":
                metadata_calls.append(request)
                self.assertEqual(request.url.path, f"/{whatsapp.VERSION}/{MEDIA_ID}")
                self.assertEqual(dict(request.url.params), {"phone_number_id": whatsapp.PHONE_ID})
                return httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "image/jpeg"})
            if request.method == "GET" and request.url.host == "lookaside.fbsbx.com":
                media_calls.append(request)
                return httpx.Response(200, content=JPEG_BYTES, headers={"Content-Type": "image/jpeg"})
            self.assertEqual(str(request.url), whatsapp.MESSAGES_URL)
            self.assertEqual(request.method, "POST")
            payload = json.loads(request.content)
            self.assertEqual(payload["to"], SECOND_SENDER)
            self.assertEqual(payload["type"], "text")
            self.assertIs(payload["text"].get("preview_url", False), False)
            sends.append(payload["text"]["body"])
            return httpx.Response(200, json={"messages": [{"id": "synthetic-part-id"}]})

        def fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return client_factory(**kwargs)

        with patch("whatsapp.httpx.AsyncClient", side_effect=fake_client):
            with patch("whatsapp.translate_attachment", new_callable=AsyncMock, return_value=parts) as translate:
                with patch("whatsapp.transcribe_audio", new_callable=AsyncMock) as stt:
                    with patch("whatsapp.synthesize_speech", new_callable=AsyncMock) as tts:
                        with self.assertLogs(whatsapp.logger, level="INFO") as recorded:
                            await whatsapp.translate_attachment_and_reply(SECOND_SENDER, MEDIA_ID, "image")
        translate.assert_awaited_once_with(JPEG_BYTES, "image/jpeg")
        stt.assert_not_awaited()
        tts.assert_not_awaited()
        self.assertEqual(len(metadata_calls), 1)
        self.assertEqual(len(media_calls), 1)
        self.assertEqual(sends, parts)
        output = "\n".join(recorded.output)
        for private in (MEDIA_URL, JPEG_BYTES.decode("latin1"), *parts, os.environ["GEMINI_API_KEY"], whatsapp.ACCESS_TOKEN):
            self.assertNotIn(private, output)

    async def test_stops_after_first_unaccepted_part_without_repeating_or_sending_fallback(self):
        for accepted in ([False], [True, False]):
            with self.subTest(accepted=accepted):
                parts = ["first translation", "second translation", "third translation"]
                with patch("whatsapp.download_attachment", new_callable=AsyncMock, return_value=(JPEG_BYTES, "image/jpeg")):
                    with patch("whatsapp.translate_attachment", new_callable=AsyncMock, return_value=parts) as translate:
                        with patch("whatsapp.send_text", new_callable=AsyncMock, side_effect=accepted) as send:
                            await whatsapp.translate_attachment_and_reply(SECOND_SENDER, MEDIA_ID, "document")
                translate.assert_awaited_once_with(JPEG_BYTES, "image/jpeg")
                self.assertEqual(send.await_args_list, [
                    call(SECOND_SENDER, part, log_details=False) for part in parts[:len(accepted)]
                ])

    async def test_rejected_or_unconfirmed_delivery_stops_without_logging_provider_document_excerpt(self):
        parts = ["private first document translation", "private second document translation", "must not be sent"]
        client_factory = httpx.AsyncClient
        cases = [
            [httpx.Response(200, json={"messages": [{"id": "accepted-first-part"}]}),
             httpx.Response(400, json={"error": {"code": 100, "message":
                 "private provider excerpt " + parts[0] + " " + PRIVATE_FILENAME + " " + PRIVATE_CAPTION}})],
            [httpx.Response(200, json={"messages": []})],
        ]
        for responses in cases:
            with self.subTest(response_count=len(responses)):
                pending = list(responses)
                sends = []

                async def handler(request):
                    self.assertEqual(str(request.url), whatsapp.MESSAGES_URL)
                    self.assertEqual(request.method, "POST")
                    payload = json.loads(request.content)
                    self.assertEqual(payload["to"], SECOND_SENDER)
                    sends.append(payload["text"]["body"])
                    self.assertTrue(pending, "No later part or fallback may be sent after a failed part")
                    return pending.pop(0)

                def fake_client(**kwargs):
                    kwargs["transport"] = httpx.MockTransport(handler)
                    return client_factory(**kwargs)

                with patch("whatsapp.download_attachment", new_callable=AsyncMock, return_value=(JPEG_BYTES, "image/jpeg")):
                    with patch("whatsapp.translate_attachment", new_callable=AsyncMock, return_value=parts):
                        with patch("whatsapp.httpx.AsyncClient", side_effect=fake_client):
                            with self.assertLogs(whatsapp.logger, level="ERROR") as recorded:
                                await whatsapp.translate_attachment_and_reply(SECOND_SENDER, MEDIA_ID, "document")
                self.assertEqual(sends, parts[:len(responses)])
                output = "\n".join(recorded.output)
                for private in (*parts, PRIVATE_FILENAME, PRIVATE_CAPTION, "private provider excerpt", whatsapp.ACCESS_TOKEN):
                    self.assertNotIn(private, output)

    async def test_pre_delivery_errors_send_one_private_safe_fallback(self):
        private_details = PRIVATE_FILENAME + " " + PRIVATE_CAPTION + " " + os.environ["GEMINI_API_KEY"]
        cases = [
            ("download", whatsapp.HTTPException(status_code=415, detail=private_details)),
            ("download", whatsapp.HTTPException(status_code=413, detail=private_details)),
            ("download", whatsapp.HTTPException(status_code=502, detail=private_details)),
            ("translation", RuntimeError(private_details)),
            ("translation", ValueError(private_details)),
        ]
        for stage, error in cases:
            with self.subTest(stage=stage, error=type(error).__name__):
                download = AsyncMock(return_value=(JPEG_BYTES, "image/jpeg"))
                translate = AsyncMock(return_value=["unused text"])
                (download if stage == "download" else translate).side_effect = error
                with patch("whatsapp.download_attachment", download), patch("whatsapp.translate_attachment", translate):
                    with patch("whatsapp.send_text", new_callable=AsyncMock, return_value=True) as send:
                        with self.assertLogs(whatsapp.logger, level="ERROR") as recorded:
                            await whatsapp.translate_attachment_and_reply(SECOND_SENDER, MEDIA_ID, "document")
                download.assert_awaited_once_with(MEDIA_ID, "document", filename=None)
                if stage == "download":
                    translate.assert_not_awaited()
                send.assert_awaited_once()
                recipient, fallback = send.await_args.args
                self.assertEqual(recipient, SECOND_SENDER)
                self.assertEqual(send.await_args.kwargs, {"log_details": False})
                self.assertTrue(any("\u0600" <= letter <= "\u06ff" for letter in fallback))
                if stage == "download" and error.status_code == 415:
                    self.assertIn("pdf", fallback.lower())
                    self.assertTrue("docx" in fallback.lower() or "word" in fallback.lower())
                output = "\n".join(recorded.output) + fallback
                for private in (PRIVATE_FILENAME, PRIVATE_CAPTION, os.environ["GEMINI_API_KEY"], whatsapp.ACCESS_TOKEN):
                    self.assertNotIn(private, output)


class AttachmentDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def download(self, *, media_kind="document", metadata=None, media=None, filename=None):
        requests = []
        client_factory = httpx.AsyncClient
        if metadata is None:
            metadata = httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "image/jpeg"})
        if media is None:
            media = httpx.Response(200, content=JPEG_BYTES, headers={"Content-Type": "image/jpeg"})

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
                response = media
            if isinstance(response, type) and issubclass(response, httpx.RequestError):
                raise response("private-provider-details " + whatsapp.ACCESS_TOKEN, request=request)
            return response

        def fake_client(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return client_factory(**kwargs)

        with patch("whatsapp.httpx.AsyncClient", side_effect=fake_client):
            try:
                result = await whatsapp.download_attachment(MEDIA_ID, media_kind, filename=filename)
                return result, requests, None
            except whatsapp.HTTPException as error:
                return None, requests, error

    async def test_supported_media_types_include_images_sent_as_documents(self):
        for mime in ("image/jpeg", "image/png", "image/webp", "application/pdf", "application/msword",
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain",
                     "application/vnd.ms-powerpoint",
                     "application/vnd.openxmlformats-officedocument.presentationml.presentation"):
            with self.subTest(mime=mime):
                result, requests, error = await self.download(
                    metadata=httpx.Response(200, json={"url": MEDIA_URL, "mime_type": mime}),
                    media=httpx.Response(200, content=b"synthetic attachment", headers={"Content-Type": "application/octet-stream"}),
                )
                self.assertIsNone(error)
                self.assertEqual(result, (b"synthetic attachment", mime))
                self.assertEqual(len(requests), 2)

    async def test_generic_mime_uses_known_filename_hint_without_overriding_explicit_type(self):
        for declared, filename, expected in (
            ("application/octet-stream", "notes.md", "text/markdown"),
            ("application/zip", "workbook.XLSX", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("application/octet-stream", "settings.json", "application/json"),
        ):
            with self.subTest(filename=filename):
                result, requests, error = await self.download(
                    filename=filename,
                    metadata=httpx.Response(200, json={"url": MEDIA_URL, "mime_type": declared}),
                    media=httpx.Response(200, content=b"synthetic attachment", headers={"Content-Type": declared}),
                )
                self.assertIsNone(error)
                self.assertEqual(result, (b"synthetic attachment", expected))
                self.assertEqual(len(requests), 2)
        _, requests, error = await self.download(
            filename="renamed.txt",
            metadata=httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "application/x-msdownload"}),
        )
        self.assertEqual(error.status_code, 415)
        self.assertEqual(len(requests), 1)
        result, _, error = await self.download(
            metadata=httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "text/csv"}),
            media=httpx.Response(200, content=b"name,value\nHello,10", headers={"Content-Type": "text/plain"}),
        )
        self.assertIsNone(error)
        self.assertEqual(result[1], "text/csv")

    async def test_metadata_or_download_failures_are_general_errors_without_secrets(self):
        cases = [
            (httpx.Response(401, json={"error": {"message": PRIVATE_FILENAME + whatsapp.ACCESS_TOKEN}}), None, 502),
            (httpx.ConnectError, None, 502),
            (httpx.Response(200, content=b"not JSON"), None, 502),
            (httpx.Response(200, json=[]), None, 502),
            (httpx.Response(200, content=b"null"), None, 502),
            (httpx.Response(200, json={"url": MEDIA_URL, "mime_type": "application/zip"}), None, 415),
            (None, httpx.ReadTimeout, 502),
            (None, httpx.Response(200, content=b"<html>private</html>", headers={"Content-Type": "text/html"}), 502),
            (None, httpx.Response(302, headers={"Location": "https://attacker.example/stolen"}), 502),
        ]
        for metadata, media, status in cases:
            with self.subTest(metadata=metadata, media=media):
                with patch.object(whatsapp.logger, "error") as error_log, patch.object(whatsapp.logger, "warning") as warning_log:
                    _, requests, error = await self.download(metadata=metadata, media=media)
                self.assertIsNotNone(error)
                self.assertEqual(error.status_code, status)
                self.assertLessEqual(len(requests), 2)
                output = str(error_log.call_args_list) + str(warning_log.call_args_list) + str(error.detail)
                for private in (PRIVATE_FILENAME, PRIVATE_CAPTION, whatsapp.ACCESS_TOKEN, "private-provider-details", "attacker.example"):
                    self.assertNotIn(private, output)

    async def test_untrusted_media_host_is_blocked_before_sending_token(self):
        for url in ("https://attacker.example/file", "https://lookaside.fbsbx.com.attacker.example/file",
                    "http://lookaside.fbsbx.com/file", "https://user:password@lookaside.fbsbx.com/file",
                    "https://lookaside.fbsbx.com:444/file"):
            with self.subTest(url=url):
                _, requests, error = await self.download(metadata=httpx.Response(200, json={"url": url, "mime_type": "image/jpeg"}))
                self.assertEqual(error.status_code, 502)
                self.assertEqual(len(requests), 1)

    async def test_image_and_document_size_limits_apply_to_actual_mime_type(self):
        cases = [("image", "image/jpeg", 5 * 1024 * 1024),
                 ("document", "image/jpeg", 5 * 1024 * 1024),
                 ("document", "application/pdf", 20 * 1024 * 1024)]
        for kind, mime, limit in cases:
            with self.subTest(kind=kind, mime=mime):
                _, _, error = await self.download(
                    media_kind=kind,
                    metadata=httpx.Response(200, json={"url": MEDIA_URL, "mime_type": mime}),
                    media=httpx.Response(200, content=b"x", headers={"Content-Type": mime, "Content-Length": str(limit + 1)}),
                )
                self.assertIsNotNone(error)
                self.assertEqual(error.status_code, 413)


if __name__ == "__main__":
    unittest.main()
