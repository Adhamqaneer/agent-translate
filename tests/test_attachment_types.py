import unittest
from unittest.mock import patch

from app.attachment_types import (
    DOCUMENT_MIME_TYPES,
    IMAGE_MIME_TYPES,
    RTF_MIME_TYPES,
    SPREADSHEET_MIME_TYPES,
    SUPPORTED_MIME_TYPES,
    is_text_type,
    resolve_mime_type,
)


class AttachmentTypeTests(unittest.TestCase):
    def test_document_types_include_native_word_and_presentation_formats(self):
        self.assertEqual(resolve_mime_type("application/pdf"), "application/pdf")
        self.assertIn("application/msword", DOCUMENT_MIME_TYPES)
        self.assertIn("application/vnd.openxmlformats-officedocument.wordprocessingml.template", DOCUMENT_MIME_TYPES)
        self.assertIn("application/vnd.oasis.opendocument.text", DOCUMENT_MIME_TYPES)
        self.assertIn("application/vnd.ms-powerpoint", DOCUMENT_MIME_TYPES)
        self.assertIn("application/vnd.oasis.opendocument.presentation", DOCUMENT_MIME_TYPES)
        self.assertFalse(is_text_type("application/msword"))

    def test_spreadsheets_are_separate_from_plain_text_and_native_documents(self):
        self.assertIn("application/vnd.ms-excel", SPREADSHEET_MIME_TYPES)
        self.assertIn("application/vnd.oasis.opendocument.spreadsheet", SPREADSHEET_MIME_TYPES)
        self.assertEqual(
            resolve_mime_type("application/vnd.ms-excel.sheet.macroEnabled.12"),
            "application/vnd.ms-excel.sheet.macroenabled.12",
        )
        self.assertNotIn("application/vnd.ms-excel", DOCUMENT_MIME_TYPES)
        self.assertFalse(is_text_type("application/vnd.ms-excel"))

    def test_image_catalog_includes_documented_formats_but_not_unverified_formats(self):
        self.assertIn("image/heic", IMAGE_MIME_TYPES)
        self.assertIn("image/heif", IMAGE_MIME_TYPES)
        self.assertIn("image/gif", IMAGE_MIME_TYPES)
        self.assertIn("image/avif", IMAGE_MIME_TYPES)
        self.assertIn("image/bmp", IMAGE_MIME_TYPES)
        self.assertIsNone(resolve_mime_type("image/tiff", "scan.pdf"))
        self.assertIsNone(resolve_mime_type("image/x-icon", "icon.png"))

    def test_aliases_and_mime_parameters_are_normalized(self):
        self.assertEqual(resolve_mime_type(" IMAGE/JPG ; charset=UTF-8 "), "image/jpeg")
        self.assertEqual(resolve_mime_type("application/x-pdf"), "application/pdf")
        self.assertEqual(resolve_mime_type("text/x-markdown"), "text/markdown")
        self.assertEqual(resolve_mime_type("application/x-msexcel"), "application/vnd.ms-excel")
        self.assertEqual(resolve_mime_type("IMAGE/JPG; name=sample.jpg"), "image/jpeg")

    def test_rtf_is_structured_and_richtext_remains_a_distinct_text_format(self):
        self.assertEqual(resolve_mime_type("text/rtf"), "application/rtf")
        self.assertEqual(resolve_mime_type("application/x-rtf"), "application/rtf")
        self.assertIn("application/rtf", RTF_MIME_TYPES)
        self.assertIn("application/rtf", SUPPORTED_MIME_TYPES)
        self.assertFalse(is_text_type("text/rtf"))
        self.assertFalse(is_text_type("application/rtf"))
        self.assertFalse(is_text_type("application/x-rtf"))
        self.assertEqual(resolve_mime_type("text/richtext"), "text/richtext")
        self.assertTrue(is_text_type("text/richtext"))

    def test_generic_binary_and_zip_types_use_only_a_known_last_suffix(self):
        self.assertEqual(resolve_mime_type("application/octet-stream", "report.PDF"), "application/pdf")
        self.assertEqual(
            resolve_mime_type("application/zip", "report.docx"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(
            resolve_mime_type("application/x-zip-compressed", "budget.xlsb"),
            "application/vnd.ms-excel.sheet.binary.macroenabled.12",
        )
        self.assertEqual(resolve_mime_type("binary/octet-stream", "notes.MD"), "text/markdown")
        self.assertEqual(resolve_mime_type("application/octet-stream", "message.rtf"), "application/rtf")
        self.assertIsNone(resolve_mime_type("application/octet-stream", "report.pdf.exe"))
        self.assertIsNone(resolve_mime_type("application/zip", "archive.zip"))
        self.assertIsNone(resolve_mime_type("application/zip", "folder.pdf/unknown"))
        self.assertIsNone(resolve_mime_type("application/octet-stream"))

    def test_specific_content_types_cannot_be_overridden_by_a_filename(self):
        self.assertIsNone(resolve_mime_type("application/x-msdownload", "harmless.txt"))
        self.assertIsNone(resolve_mime_type("application/x-executable", "photo.png"))
        self.assertIsNone(resolve_mime_type("application/epub+zip", "book.pdf"))
        self.assertEqual(resolve_mime_type("image/png", "fake.exe"), "image/png")

    def test_text_and_code_extensions_produce_readable_types(self):
        self.assertEqual(resolve_mime_type("application/octet-stream", "data.csv"), "text/csv")
        self.assertEqual(resolve_mime_type("application/octet-stream", "data.tsv"), "text/tab-separated-values")
        self.assertEqual(resolve_mime_type("application/octet-stream", "data.json"), "application/json")
        self.assertEqual(resolve_mime_type("application/octet-stream", "config.yml"), "application/yaml")
        self.assertEqual(resolve_mime_type("application/octet-stream", "script.py"), "text/x-python")
        self.assertEqual(resolve_mime_type("application/octet-stream", "styles.css"), "text/css")
        self.assertEqual(resolve_mime_type("application/octet-stream", "image.svg"), "image/svg+xml")
        self.assertTrue(is_text_type("image/svg+xml"))
        self.assertTrue(is_text_type("application/sql"))

    def test_arbitrary_valid_text_subtypes_preserve_their_actual_type(self):
        self.assertEqual(resolve_mime_type("text/x-custom-source; charset=utf-8"), "text/x-custom-source")
        self.assertTrue(is_text_type("text/vnd.example+source"))
        self.assertEqual(resolve_mime_type("text/x-unlisted-format"), "text/x-unlisted-format")
        self.assertTrue(is_text_type("application/xhtml+xml"))
        self.assertTrue(is_text_type("application/x-python-code"))
        self.assertFalse(is_text_type("image/jpeg"))

    def test_malformed_or_injected_headers_are_rejected_even_with_a_known_filename(self):
        malformed = [
            None, 123, [], {}, b"text/plain", "", "text", "text/", "text/*",
            "text/pl ain", "te xt/plain", "text/plain\r\nX-Injected: true",
            "text/plain; charset=utf-8\n", "text/plain\x00", "text/plain\x7f",
            "text/\u0646\u0635", "text/" + "a" * 128, "text/plain; " + "a" * 256,
        ]
        for value in malformed:
            with self.subTest(value=value):
                self.assertIsNone(resolve_mime_type(value, "safe.txt"))

    def test_filename_is_only_a_suffix_hint_and_never_a_path_or_command(self):
        with patch("builtins.open", side_effect=AssertionError("unexpected file access")):
            self.assertEqual(
                resolve_mime_type("application/octet-stream", "../../$(whoami).txt"),
                "text/plain",
            )
            self.assertEqual(
                resolve_mime_type("application/octet-stream", "C:\\private\\config.json"),
                "application/json",
            )
        self.assertIsNone(resolve_mime_type("application/octet-stream", "safe.txt\x00.exe"))
        self.assertIsNone(resolve_mime_type("application/octet-stream", "file.txt?download=1"))
        self.assertIsNone(resolve_mime_type("application/octet-stream", "a" * 4097 + ".txt"))


if __name__ == "__main__":
    unittest.main()
