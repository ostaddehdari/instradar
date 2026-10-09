"""Small streaming XLSX writer with RTL/Persian-friendly defaults.

It intentionally uses only the Python standard library so a production
upgrade cannot fail because an extra spreadsheet package was not installed.
Strings are written as inline strings (never formulas), which also prevents
CSV/Excel formula injection from Instagram captions or comments.
"""
from __future__ import annotations

import re
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile
from xml.sax.saxutils import escape

from django.http import FileResponse


INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(value):
    if value is None:
        return ""
    return INVALID_XML.sub("", str(value))


def _column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(reference, value, style=0):
    if isinstance(value, bool):
        return f'<c r="{reference}" s="{style}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    text = escape(_clean(value))
    return (f'<c r="{reference}" s="{style}" t="inlineStr"><is>'
            f'<t xml:space="preserve">{text}</t></is></c>')


def build_xlsx(headers, rows, *, sheet_name="گزارش", widths=None):
    headers = list(headers)
    widths = list(widths or [max(12, min(42, len(str(item)) + 5)) for item in headers])
    while len(widths) < len(headers):
        widths.append(16)
    sheet_name = _clean(sheet_name).replace("'", "").replace("[", "").replace("]", "")[:31] or "گزارش"
    output = SpooledTemporaryFile(max_size=12 * 1024 * 1024, mode="w+b")
    with ZipFile(output, "w", ZIP_DEFLATED, allowZip64=True) as book:
        book.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
 <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
 <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>""")
        book.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        book.writestr("xl/workbook.xml", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <workbookPr/><bookViews><workbookView/></bookViews><sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>""")
        book.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""")
        book.writestr("xl/styles.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <fonts count="2"><font><sz val="11"/><name val="Tahoma"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Tahoma"/></font></fonts>
 <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF4B67ED"/><bgColor indexed="64"/></patternFill></fill></fills>
 <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
 <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
 <cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment horizontal="right" vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"><alignment horizontal="right" vertical="center" wrapText="1"/></xf></cellXfs>
 <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>""")

        with book.open("xl/worksheets/sheet1.xml", "w") as sheet:
            def write(value):
                sheet.write(value.encode("utf-8"))

            write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
            write('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
            write('<sheetViews><sheetView workbookViewId="0" rightToLeft="1"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
            write('<sheetFormatPr defaultRowHeight="20"/>')
            write('<cols>')
            for index, width in enumerate(widths[:len(headers)], 1):
                write(f'<col min="{index}" max="{index}" width="{max(8, min(80, width))}" customWidth="1"/>')
            write('</cols><sheetData>')
            write('<row r="1" ht="26" customHeight="1">')
            for column, value in enumerate(headers, 1):
                write(_cell(f"{_column_name(column)}1", value, style=1))
            write('</row>')
            row_number = 1
            for row_number, values in enumerate(rows, 2):
                write(f'<row r="{row_number}">')
                for column, value in enumerate(values, 1):
                    write(_cell(f"{_column_name(column)}{row_number}", value))
                write('</row>')
            write('</sheetData>')
            last_column = _column_name(max(1, len(headers)))
            write(f'<autoFilter ref="A1:{last_column}{max(1, row_number)}"/>')
            write('<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>')
            write('</worksheet>')
    output.seek(0)
    return output


def xlsx_response(headers, rows, *, filename, sheet_name="گزارش", widths=None):
    file_handle = build_xlsx(headers, rows, sheet_name=sheet_name, widths=widths)
    response = FileResponse(
        file_handle,
        as_attachment=True,
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Cache-Control"] = "private, no-store"
    return response
