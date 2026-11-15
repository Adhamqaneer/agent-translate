"""Extract bounded spreadsheet values in memory, without evaluating formulas."""

import csv
from datetime import date, datetime, time, timedelta
from io import BytesIO, StringIO
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_CHARS = 100_000
MAX_SHEETS = 100
MAX_CELLS = 100_000
MAX_ROWS = 100_000
MAX_ZIP_ENTRIES = 2_000
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
ERROR_MESSAGE = "Could not read this spreadsheet, or it exceeds the supported limits."
WORKER_TIMEOUT_SECONDS = 30
WORKER_MEMORY_BYTES = 256 * 1024 * 1024
WORKER_CPU_SECONDS = 15
_WORKER_CODE = (
    "import json,sys;sys.path[:]=json.loads(sys.argv[1]);"
    "from app.spreadsheet_text import _worker_main;_worker_main()"
)
_CELL_REFERENCE = re.compile(r"([A-Za-z]+)([1-9][0-9]*)\Z")
_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"


def _rectangle(rows: int, columns: int) -> int:
    if rows < 0 or columns < 0 or rows > MAX_ROWS or columns > MAX_CELLS:
        raise ValueError(ERROR_MESSAGE)
    cells = rows * columns
    if cells > MAX_CELLS:
        raise ValueError(ERROR_MESSAGE)
    return cells


def _positive_integer(value: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError(ERROR_MESSAGE)
    # Avoid constructing enormous Python integers from malicious XML attributes.
    if len(value) > 10:
        raise ValueError(ERROR_MESSAGE)
    number = int(value)
    if number < 1:
        raise ValueError(ERROR_MESSAGE)
    return number


def _cell_position(reference: str) -> tuple[int, int]:
    match = _CELL_REFERENCE.fullmatch(reference)
    if not match or len(match[1]) > 6:
        raise ValueError(ERROR_MESSAGE)
    column = 0
    for letter in match[1].upper():
        column = column * 26 + ord(letter) - ord("A") + 1
    return _positive_integer(match[2]), column


def _check_xlsx_sheet(xml: bytes) -> None:
    row = column = maximum_row = maximum_column = 0
    for event, element in ET.iterparse(BytesIO(xml), events=("start", "end")):
        name = element.tag.rsplit("}", 1)[-1]
        if event == "start":
            if name == "dimension" and element.get("ref"):
                for reference in element.get("ref").split(":"):
                    dim_row, dim_column = _cell_position(reference)
                    _rectangle(dim_row, dim_column)
            elif name == "row":
                row = _positive_integer(element.get("r", str(row + 1)))
                column = 0
                _rectangle(row, max(1, maximum_column))
            elif name == "c":
                if element.get("r"):
                    cell_row, column = _cell_position(element.get("r"))
                else:
                    cell_row, column = max(1, row), column + 1
                maximum_row = max(maximum_row, cell_row)
                maximum_column = max(maximum_column, column)
                _rectangle(maximum_row, maximum_column)
        else:
            element.clear()


def _check_ods_tables(xml: bytes) -> None:
    sheets = rows = width = current_columns = row_repeat = 0
    row_tag = f"{{{_TABLE_NS}}}table-row"
    table_tag = f"{{{_TABLE_NS}}}table"
    cell_tags = {f"{{{_TABLE_NS}}}table-cell", f"{{{_TABLE_NS}}}covered-table-cell"}
    for event, element in ET.iterparse(BytesIO(xml), events=("start", "end")):
        if event == "start":
            if element.tag == table_tag:
                sheets += 1
                if sheets > MAX_SHEETS:
                    raise ValueError(ERROR_MESSAGE)
                rows = width = 0
            elif element.tag == row_tag:
                row_repeat = _positive_integer(element.get(f"{{{_TABLE_NS}}}number-rows-repeated", "1"))
                current_columns = 0
                _rectangle(rows + row_repeat, max(1, width))
            elif element.tag in cell_tags:
                current_columns += _positive_integer(element.get(f"{{{_TABLE_NS}}}number-columns-repeated", "1"))
                width = max(width, current_columns)
                _rectangle(rows + row_repeat, width)
        else:
            if element.tag == row_tag:
                rows += row_repeat
            element.clear()


def _preflight(data: bytes) -> None:
    source = BytesIO(data)
    if not zipfile.is_zipfile(source):
        return  # Binary XLS is validated by Calamine rather than a ZIP parser.
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ValueError(ERROR_MESSAGE)
        uncompressed = 0
        for entry in entries:
            if entry.flag_bits & 1 or entry.file_size < 0:
                raise ValueError(ERROR_MESSAGE)
            uncompressed += entry.file_size
            if uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ValueError(ERROR_MESSAGE)
        for entry in entries:
            name = entry.filename.lower()
            is_worksheet = name.startswith("xl/worksheets/") and name.endswith(".xml")
            is_manifest = name == "meta-inf/manifest.xml"
            if not is_worksheet and name != "content.xml" and not is_manifest:
                continue
            xml = archive.read(entry)
            if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                raise ValueError(ERROR_MESSAGE)
            if is_manifest:
                if any(element.tag.rsplit("}", 1)[-1] == "encryption-data"
                       for element in ET.fromstring(xml).iter()):
                    raise ValueError(ERROR_MESSAGE)
            elif is_worksheet:
                _check_xlsx_sheet(xml)
            else:
                _check_ods_tables(xml)


def _open_workbook(data: bytes):
    # Loading is optional for text-only deployments; never import the native
    # extension or inspect user files at module import time.
    from python_calamine import CalamineWorkbook

    return CalamineWorkbook.from_filelike(BytesIO(data))


def _cell_text(value) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (datetime, date, time)):
        text = value.isoformat()
    elif isinstance(value, timedelta):
        text = str(value)
    elif isinstance(value, (int, float)):
        text = repr(value)
    else:
        raise ValueError(ERROR_MESSAGE)
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ValueError(ERROR_MESSAGE)
    return text


def _sheet_text(workbook) -> str:
    names = workbook.sheet_names
    if not isinstance(names, list) or not names or len(names) > MAX_SHEETS:
        raise ValueError(ERROR_MESSAGE)
    pieces = []
    characters = cells = total_rows = 0

    def append(text: str) -> None:
        nonlocal characters
        characters += len(text)
        if characters > MAX_EXTRACTED_CHARS:
            raise ValueError(ERROR_MESSAGE)
        pieces.append(text)

    for index, name in enumerate(names, 1):
        if not isinstance(name, str) or len(name) > MAX_EXTRACTED_CHARS:
            raise ValueError(ERROR_MESSAGE)
        append(f"Sheet {index}: {json.dumps(name, ensure_ascii=False)}\n")
        sheet = workbook.get_sheet_by_name(name)
        start, end = sheet.start, sheet.end
        if start is None and end is None:
            append("[Empty sheet]\n\n")
            continue
        if (not isinstance(start, tuple) or not isinstance(end, tuple) or len(start) != 2 or len(end) != 2
                or any(type(value) is not int or value < 0 for value in (*start, *end))
                or start[0] > end[0] or start[1] > end[1]):
            raise ValueError(ERROR_MESSAGE)
        rows, columns = end[0] + 1, end[1] + 1
        cells += _rectangle(rows, columns)
        total_rows += rows
        if cells > MAX_CELLS or total_rows > MAX_ROWS:
            raise ValueError(ERROR_MESSAGE)
        width = end[1] - start[1] + 1
        row_count = 0
        # iter_rows preserves leading empty rows but trims the columns before
        # start[1]. Reinsert those columns so TSV columns match the workbook.
        for row_count, row in enumerate(sheet.iter_rows(), 1):
            if row_count > rows or not isinstance(row, list) or len(row) != width:
                raise ValueError(ERROR_MESSAGE)
            values = [""] * start[1] + [_cell_text(value) for value in row]
            label = f"Row {row_count}" + (" [empty]" if not any(values) else "")
            buffer = StringIO(newline="")
            csv.writer(buffer, delimiter="\t", lineterminator="\n").writerow([label, *values])
            append(buffer.getvalue())
        if row_count != rows:
            raise ValueError(ERROR_MESSAGE)
        append("\n")
    return "".join(pieces).rstrip("\n")


def _extract_spreadsheet_core(data: bytes) -> str:
    """Return every worksheet's cached values, labels, and cell positions.

    Calamine reads XLS/XLSX/XLSM/XLSB/ODS without running macros, following
    external links, or evaluating formulas. Formula results are cached values
    saved by the spreadsheet author. Archive and output limits fail closed.
    """
    workbook = None
    try:
        if not isinstance(data, bytes) or not data or len(data) > MAX_FILE_BYTES:
            raise ValueError(ERROR_MESSAGE)
        _preflight(data)
        workbook = _open_workbook(data)
        return _sheet_text(workbook)
    except Exception:
        raise ValueError(ERROR_MESSAGE) from None
    finally:
        if workbook is not None:
            try:
                workbook.close()
            except Exception:
                pass


def _worker_main() -> None:
    """Apply limits in the child, before importing Calamine or parsing bytes."""
    try:
        if sys.platform.startswith("linux"):
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
            resource.setrlimit(resource.RLIMIT_CPU, (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS))
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        text = _extract_spreadsheet_core(data)
        output = text.encode("utf-8")
        if len(output) > MAX_EXTRACTED_CHARS * 4:
            raise ValueError(ERROR_MESSAGE)
        sys.stdout.buffer.write(output)
    except Exception:
        # Native-library details and workbook contents stay inside the child.
        raise SystemExit(1) from None


def extract_spreadsheet(data: bytes) -> str:
    """Extract values in a disposable worker, bounded by time and Linux memory.

    Workbook data travels through stdin, never command arguments or disk.
    subprocess.run kills and reaps the worker if its timeout expires. Linux
    additionally limits native allocations and CPU, protecting the web process
    before Calamine can expand a malicious binary XLS/XLSB cell range.
    """
    try:
        if not isinstance(data, bytes) or not data or len(data) > MAX_FILE_BYTES:
            raise ValueError(ERROR_MESSAGE)
        paths = [path for path in sys.path if isinstance(path, str)]
        result = subprocess.run(
            [sys.executable, "-B", "-c", _WORKER_CODE, json.dumps(paths)],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=WORKER_TIMEOUT_SECONDS, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0 or len(result.stdout) > MAX_EXTRACTED_CHARS * 4:
            raise ValueError(ERROR_MESSAGE)
        text = result.stdout.decode("utf-8")
        if not text or len(text) > MAX_EXTRACTED_CHARS:
            raise ValueError(ERROR_MESSAGE)
        return text
    except Exception:
        raise ValueError(ERROR_MESSAGE) from None
