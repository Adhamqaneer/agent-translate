"""In-memory fixtures and limits for extracting every spreadsheet sheet."""

import builtins
import csv
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO, StringIO
import re
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
import zipfile

import spreadsheet_text


def make_zip(entries):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            # The ODF package's leading mimetype entry must be uncompressed.
            compression = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            archive.writestr(name, value, compress_type=compression)
    return buffer.getvalue()


def make_xlsx(*, empty_sheet=False):
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheets = [
        '<sheet name="First" sheetId="1" r:id="rId1"/>',
        '<sheet name="Hidden Second" sheetId="2" state="hidden" r:id="rId2"/>',
    ]
    if empty_sheet:
        sheets.append('<sheet name="Empty" sheetId="3" r:id="rId3"/>')
    relationships = "".join(
        f'<Relationship Id="rId{i}" Type="{relationship}/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    entries = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + overrides + '</Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{relationship}/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{namespace}" xmlns:r="{relationship}"><sheets>'
            + "".join(sheets) + '</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + relationships + '</Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{namespace}"><dimension ref="B2:D4"/><sheetData>'
            '<row r="2"><c r="B2" t="inlineStr"><is><t>مرحبا</t></is></c>'
            '<c r="D2"><v>12.3456789012345</v></c></row>'
            '<row r="4"><c r="B4" t="b"><v>1</v></c>'
            '<c r="C4"><f>1+6</f><v>7</v></c></row></sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{namespace}"><dimension ref="A1:B1"/><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>hidden value</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>tab&#9;and&#10;newline</t></is></c></row>'
            '</sheetData></worksheet>',
    }
    if empty_sheet:
        entries["xl/worksheets/sheet3.xml"] = f'<worksheet xmlns="{namespace}"><sheetData/></worksheet>'
    return make_zip(entries)


def make_ods():
    content = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">
 <office:body><office:spreadsheet>
  <table:table table:name="ODS First">
   <table:table-row><table:table-cell office:value-type="string"><text:p>واحد</text:p></table:table-cell>
    <table:table-cell/><table:table-cell office:value-type="float" office:value="12.3456789012345"/></table:table-row>
   <table:table-row><table:table-cell/></table:table-row>
   <table:table-row><table:table-cell office:value-type="string"><text:p>last row</text:p></table:table-cell></table:table-row>
  </table:table>
  <table:table table:name="ODS Second">
   <table:table-row><table:table-cell office:value-type="string"><text:p>second sheet value</text:p></table:table-cell>
    <table:table-cell office:value-type="boolean" office:boolean-value="true"/></table:table-row>
  </table:table>
 </office:spreadsheet></office:body>
</office:document-content>'''
    content = re.sub(r">\s+<", "><", content)
    return make_zip({
        "mimetype": "application/vnd.oasis.opendocument.spreadsheet",
        "content.xml": content,
        "META-INF/manifest.xml": '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>',
    })


class FakeSheet:
    def __init__(self, rows, *, start=(0, 0), end=None):
        self.rows = rows
        self.start = start
        self.end = end if end is not None else (None if start is None else (len(rows) - 1, len(rows[0]) - 1))
        self.iterated = False

    def iter_rows(self):
        self.iterated = True
        return iter(self.rows)


class FakeWorkbook:
    def __init__(self, sheets):
        self.sheets = sheets
        self.sheet_names = list(sheets)
        self.closed = False
        self.requested = []

    def get_sheet_by_name(self, name):
        self.requested.append(name)
        return self.sheets[name]

    def close(self):
        self.closed = True


class SpreadsheetExtractionTests(unittest.TestCase):
    def test_real_xlsx_extracts_all_sheets_hidden_values_positions_and_cached_formula(self):
        text = spreadsheet_text.extract_spreadsheet(make_xlsx())
        self.assertLess(text.index('Sheet 1: "First"'), text.index('Sheet 2: "Hidden Second"'))
        first = text.split('Sheet 2: "Hidden Second"')[0]
        rows = {row[0]: row[1:] for row in csv.reader(StringIO(first), delimiter="\t") if row and row[0].startswith("Row ")}
        self.assertEqual(rows["Row 2"], ["", "مرحبا", "", "12.3456789012345"])
        self.assertIn("Row 1 [empty]", rows)
        self.assertIn("Row 3 [empty]", rows)
        self.assertEqual(rows["Row 4"], ["", "true", "7.0", ""])
        self.assertNotIn("1+6", text)
        second_rows = list(csv.reader(StringIO(text.split('Sheet 2: "Hidden Second"')[1]), delimiter="\t"))
        self.assertIn(["Row 1", "hidden value", "tab\tand\nnewline"], second_rows)

    def test_real_ods_extracts_every_sheet_and_interior_empty_row(self):
        text = spreadsheet_text.extract_spreadsheet(make_ods())
        self.assertLess(text.index('Sheet 1: "ODS First"'), text.index('Sheet 2: "ODS Second"'))
        self.assertIn("واحد\t\t12.3456789012345", text)
        self.assertIn("Row 2 [empty]", text)
        self.assertIn("Row 3\tlast row", text)
        self.assertIn("Row 1\tsecond sheet value\ttrue", text)

    def test_empty_sheet_is_labeled_and_workbook_is_closed(self):
        text = spreadsheet_text.extract_spreadsheet(make_xlsx(empty_sheet=True))
        self.assertIn('Sheet 3: "Empty"\n[Empty sheet]', text)
        workbook = FakeWorkbook({"Only Empty": FakeSheet([], start=None)})
        with patch("spreadsheet_text._open_workbook", return_value=workbook):
            self.assertIn("[Empty sheet]", spreadsheet_text._extract_spreadsheet_core(b"fake"))
        self.assertTrue(workbook.closed)

    def test_dates_numbers_and_embedded_delimiters_are_preserved_deterministically(self):
        values = [date(2026, 11, 15), datetime(2026, 11, 15, 12, 34, 56, tzinfo=timezone.utc),
                  time(1, 2, 3, 456789), timedelta(days=1, microseconds=1), 123456789012345,
                  0.12345678901234566, False, "a\tb\nc", "", None]
        workbook = FakeWorkbook({"Typed": FakeSheet([values])})
        with patch("spreadsheet_text._open_workbook", return_value=workbook):
            text = spreadsheet_text._extract_spreadsheet_core(b"fake")
        row = next(row for row in csv.reader(StringIO(text), delimiter="\t") if row and row[0] == "Row 1")
        self.assertEqual(row[1:], ["2026-11-15", "2026-11-15T12:34:56+00:00", "01:02:03.456789",
                                  "1 day, 0:00:00.000001", "123456789012345", "0.12345678901234566",
                                  "false", "a\tb\nc", "", ""])
        self.assertTrue(workbook.closed)

    def test_large_sheet_is_rejected_before_iterating_or_converting_rows(self):
        sheet = FakeSheet([], start=(0, 0), end=(999_999, 16_383))
        workbook = FakeWorkbook({"Huge": sheet})
        with patch("spreadsheet_text._open_workbook", return_value=workbook):
            with self.assertRaisesRegex(ValueError, spreadsheet_text.ERROR_MESSAGE):
                spreadsheet_text._extract_spreadsheet_core(b"fake")
        self.assertFalse(sheet.iterated)
        self.assertTrue(workbook.closed)

    def test_workbook_sheet_cell_and_output_limits_fail_without_partial_result(self):
        cases = [
            ("MAX_SHEETS", 1, FakeWorkbook({"One": FakeSheet([[1]]), "Two": FakeSheet([[2]])})),
            ("MAX_CELLS", 3, FakeWorkbook({"One": FakeSheet([[1, 2]]), "Two": FakeSheet([[3, 4]])})),
            ("MAX_EXTRACTED_CHARS", 30, FakeWorkbook({"One": FakeSheet([["private" * 20]])})),
        ]
        for limit, maximum, workbook in cases:
            with self.subTest(limit=limit):
                with patch.object(spreadsheet_text, limit, maximum), patch("spreadsheet_text._open_workbook", return_value=workbook):
                    with self.assertRaises(ValueError) as caught:
                        spreadsheet_text._extract_spreadsheet_core(b"fake")
                self.assertEqual(str(caught.exception), spreadsheet_text.ERROR_MESSAGE)
                self.assertTrue(workbook.closed)

    def test_missing_dependency_invalid_data_and_loader_failures_have_no_private_details(self):
        original_import = builtins.__import__

        def missing_dependency(name, *args, **kwargs):
            if name == "python_calamine":
                raise ImportError("private-loader-path")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_dependency):
            with self.assertRaises(ValueError) as caught:
                spreadsheet_text._extract_spreadsheet_core(b"not a workbook")
        self.assertEqual(str(caught.exception), spreadsheet_text.ERROR_MESSAGE)
        for data in (b"", "not bytes", b"not a workbook"):
            with self.subTest(data=data), self.assertRaises(ValueError) as caught:
                spreadsheet_text._extract_spreadsheet_core(data)
            self.assertEqual(str(caught.exception), spreadsheet_text.ERROR_MESSAGE)
        with patch("spreadsheet_text._open_workbook", side_effect=RuntimeError("private-filename secret-cell")):
            with self.assertRaises(ValueError) as caught:
                spreadsheet_text._extract_spreadsheet_core(b"fake")
        self.assertEqual(str(caught.exception), spreadsheet_text.ERROR_MESSAGE)

    def test_input_and_zip_limits_run_before_native_loader(self):
        entries = {"one": b"12345", "two": b"67890"}
        cases = [("MAX_FILE_BYTES", 3, b"four"),
                 ("MAX_ZIP_ENTRIES", 1, make_zip(entries)),
                 ("MAX_UNCOMPRESSED_BYTES", 9, make_zip(entries))]
        with patch("spreadsheet_text._open_workbook") as loader:
            for limit, maximum, data in cases:
                with self.subTest(limit=limit), patch.object(spreadsheet_text, limit, maximum):
                    with self.assertRaises(ValueError):
                        spreadsheet_text._extract_spreadsheet_core(data)
            loader.assert_not_called()

    def test_encrypted_zip_huge_xlsx_range_and_ods_repeats_are_rejected_before_loader(self):
        encrypted = bytearray(make_zip({"one": "content"}))
        encrypted[encrypted.index(b"PK\x03\x04") + 6] |= 1
        encrypted[encrypted.index(b"PK\x01\x02") + 8] |= 1
        xlsx = make_zip({"xl/worksheets/sheet1.xml":
            '<worksheet><dimension ref="A1:XFD1048576"/><sheetData/></worksheet>'})
        ods = make_zip({"content.xml":
            '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
            '<table:table-row table:number-rows-repeated="1000000000"><table:table-cell/></table:table-row></table:table>'})
        dtd = make_zip({"content.xml": '<!DOCTYPE x [<!ENTITY secret "private">]><x>&secret;</x>'})
        ods_encrypted = make_zip({"META-INF/manifest.xml":
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry><manifest:encryption-data/></manifest:file-entry></manifest:manifest>'})
        with patch("spreadsheet_text._open_workbook") as loader:
            for data in (bytes(encrypted), xlsx, ods, dtd, ods_encrypted):
                with self.subTest(length=len(data)), self.assertRaises(ValueError):
                    spreadsheet_text._extract_spreadsheet_core(data)
            loader.assert_not_called()

    def test_worker_timeout_failures_and_invalid_output_are_safe_and_use_private_pipes(self):
        private_data = b"private workbook bytes must never enter arguments"
        failures = [
            subprocess.TimeoutExpired("private-worker-command", 30),
            subprocess.CompletedProcess([], -9, stdout=b"private-native-error"),
            subprocess.CompletedProcess([], 0, stdout=b""),
            subprocess.CompletedProcess([], 0, stdout=b"\xff"),
            subprocess.CompletedProcess([], 0, stdout=b"x" * (spreadsheet_text.MAX_EXTRACTED_CHARS * 4 + 1)),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch("spreadsheet_text.subprocess.run", **options) as run:
                    with self.assertRaises(ValueError) as caught:
                        spreadsheet_text.extract_spreadsheet(private_data)
                self.assertEqual(str(caught.exception), spreadsheet_text.ERROR_MESSAGE)
                arguments = run.call_args.args[0]
                kwargs = run.call_args.kwargs
                self.assertNotIn(private_data.decode(), str(arguments))
                self.assertEqual(kwargs["input"], private_data)
                self.assertEqual(kwargs["timeout"], 30)
                self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
                self.assertFalse(kwargs.get("shell", False))
                self.assertNotIn("preexec_fn", kwargs)
        with patch("spreadsheet_text.subprocess.run") as run:
            with self.assertRaises(ValueError):
                spreadsheet_text.extract_spreadsheet(b"")
            run.assert_not_called()

    def test_linux_worker_sets_memory_and_cpu_limits_before_parsing(self):
        calls = Mock()
        fake_resource = SimpleNamespace(RLIMIT_AS=1, RLIMIT_CPU=2, setrlimit=calls.limit)
        output = BytesIO()
        calls.parse.return_value = 'Sheet 1: "Test"\n[Empty sheet]'
        with patch.dict(sys.modules, {"resource": fake_resource}):
            with patch("spreadsheet_text.sys.platform", "linux"):
                with patch("spreadsheet_text.sys.stdin", SimpleNamespace(buffer=BytesIO(b"private bytes"))):
                    with patch("spreadsheet_text.sys.stdout", SimpleNamespace(buffer=output)):
                        with patch("spreadsheet_text._extract_spreadsheet_core", calls.parse):
                            spreadsheet_text._worker_main()
        self.assertEqual(calls.mock_calls, [
            call.limit(1, (256 * 1024 * 1024, 256 * 1024 * 1024)),
            call.limit(2, (15, 15)),
            call.parse(b"private bytes"),
        ])
        self.assertEqual(output.getvalue().decode(), calls.parse.return_value)


if __name__ == "__main__":
    unittest.main()
