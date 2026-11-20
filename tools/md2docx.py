"""Minimal Markdown -> .docx converter (stdlib only), used to build the Word copy of the guide.

    python3 tools/md2docx.py docs/IMPLEMENTATION_GUIDE.md docs/CleanRooms_IaC_Implementation_Guide.docx \
        "Clean Rooms as Code: Implementation and Test Guide"

Supports: # to ###### headings (at most 3 leading spaces), paragraphs, - / 1. lists, [ ] / [x]
checkboxes at the START of a list item, quote, paragraph, heading or table cell (brackets elsewhere are
left as text),
pipe tables, ``` code blocks, > quotes, ---, **bold**, `code`, [text](url). The first H1 outside a code
block is replaced by the given title.
Not supported: escaped pipes (\\|) inside table cells, and code or links nested inside **bold**. A table row
with an unmatched backtick is split on every pipe; if that makes it wider than the header, the extra
cells are joined into its last cell.

Elements are written in the order the WordprocessingML schema (ECMA-376) requires, e.g. run properties
rFonts, b, color, sz and paragraph properties pStyle, numPr, pBdr, shd, spacing, ind; tables have tblGrid.
"""
import re
import sys
import zipfile
from xml.sax.saxutils import escape

FONT = "Calibri"
MONO = "Consolas"
CODE_COLOR = "9C2B0E"
HEADER_FILL = "1F3864"
HEADER_TEXT = "FFFFFF"
TEXT_WIDTH_TWIPS = 12240 - 2 * 1080          # US Letter minus left/right margins
CHECKBOXES = {"[ ]": "\u2610", "[x]": "\u2611", "[X]": "\u2611"}
LEADING_CHECKBOX = re.compile(r"^\[([ xX])\](?=\s|$)")   # "[ ] task", "[x]" -- not "[x](url)" or "[x]y"
HEADING = re.compile(r"^ {0,3}(#{1,6}) (.*)$")               # CommonMark: at most 3 leading spaces
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")
SEPARATOR_CELL = re.compile(r"^\s*:?-{3,}:?\s*$")
NS = "http://schemas.openxmlformats.org"


def rpr(bold=False, mono=False, color=None, size=None):
    """Run properties in schema order: rFonts, b, color, sz (one color at most)."""
    parts = []
    if mono:
        parts.append(f'<w:rFonts w:ascii="{MONO}" w:hAnsi="{MONO}" w:cs="{MONO}"/>')
    if bold:
        parts.append("<w:b/>")
    if color or mono:
        parts.append(f'<w:color w:val="{color or CODE_COLOR}"/>')
    if size:
        parts.append(f'<w:sz w:val="{size}"/>')
    return f"<w:rPr>{''.join(parts)}</w:rPr>" if parts else ""


def ppr(style=None, num=None, border=False, shade=None, spacing=None, indent=None):
    """Paragraph properties in schema order: pStyle, numPr, pBdr, shd, spacing, ind."""
    parts = []
    if style:
        parts.append(f'<w:pStyle w:val="{style}"/>')
    if num:
        parts.append(f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr>')
    if border:
        parts.append('<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="A6A6A6"/></w:pBdr>')
    if shade:
        parts.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>')
    if spacing:
        parts.append(f'<w:spacing w:before="{spacing[0]}" w:after="{spacing[1]}"/>')
    if indent:
        parts.append(f'<w:ind w:left="{indent}"/>')
    return f"<w:pPr>{''.join(parts)}</w:pPr>" if parts else ""


def runs(text, bold=False, size=None, color=None):
    out = []
    for part in INLINE.split(text):
        if not part:
            continue
        b, mono, t = bold, False, part
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            b, t = True, part[2:-2]
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            mono, t = True, part[1:-1]
        elif LINK.fullmatch(part):
            m = LINK.fullmatch(part)
            t = m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})"
        out.append(f'<w:r>{rpr(b, mono, color, size)}<w:t xml:space="preserve">{escape(t)}</w:t></w:r>')
    return "".join(out)


def checkbox(text):
    """A block (list item, quote, paragraph, heading or table cell) that STARTS with [ ] / [x] gets a
    checkbox glyph. Brackets anywhere else in the text are left exactly as written."""
    m = LEADING_CHECKBOX.match(text)
    return CHECKBOXES[f"[{m.group(1)}]"] + text[3:] if m else text


def para(text, **kw):
    return f"<w:p>{ppr(**kw)}{runs(checkbox(text))}</w:p>"


def spacer():
    return f"<w:p>{ppr(spacing=(0, 60))}</w:p>"


def code_block(lines):
    out = []
    for ln in lines or [""]:
        out.append(f'<w:p>{ppr(shade="F2F2F2", spacing=(0, 0), indent=200)}'
                   f'<w:r>{rpr(mono=True, color="000000", size=17)}'
                   f'<w:t xml:space="preserve">{escape(ln)}</w:t></w:r></w:p>')
    return "".join(out) + spacer()


def table(rows):
    ncols = max(len(r) for r in rows)
    col_w = TEXT_WIDTH_TWIPS // ncols
    border = "<w:tblBorders>" + "".join(
        f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
        for s in ("top", "left", "bottom", "right", "insideH", "insideV")) + "</w:tblBorders>"
    xml = ['<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>' + border +
           '<w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr>',
           "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in range(ncols)) + "</w:tblGrid>"]
    for i, cells in enumerate(rows):
        header = i == 0
        xml.append("<w:tr>" + ("<w:trPr><w:tblHeader/></w:trPr>" if header else ""))
        for c in cells + [""] * (ncols - len(cells)):
            shd = f'<w:shd w:val="clear" w:color="auto" w:fill="{HEADER_FILL}"/>' if header else ""
            xml.append(f'<w:tc><w:tcPr><w:tcW w:w="{col_w}" w:type="dxa"/>{shd}</w:tcPr>'
                       f'<w:p>{ppr(spacing=(20, 20))}'
                       f'{runs(checkbox(c.strip()), bold=header, size=18, color=HEADER_TEXT if header else None)}</w:p></w:tc>')
        xml.append("</w:tr>")
    return "".join(xml) + "</w:tbl>" + spacer()


def split_row(line):
    """Split a pipe-table row. Only the outermost pipes are removed, so empty edge cells survive."""
    return _split_row(line)[0]


def _split_row(line):
    """Return (cells, fell_back): fell_back is True when an unmatched backtick forced a plain split."""
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    cells, cur, in_code = [], "", False
    for ch in s:
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(cur)
            cur = ""
        else:
            cur += ch
    cells.append(cur)
    if in_code:                     # unmatched backtick: fall back to a plain split
        return s.split("|"), True
    return cells, False


def fit_to_header(rows):
    """Keep every row at the header's width: extra cells (e.g. from an unmatched backtick) are joined
    back into the last cell with '|', so one odd row can't add a column to the whole table."""
    width = len(rows[0])
    return [r if len(r) <= width else r[:width - 1] + ["|".join(r[width - 1:])] for r in rows]


def is_separator(cells):
    """A separator row has only dash cells (|---|:---:|); a data cell like `--mode` is not one."""
    return bool(cells) and all(SEPARATOR_CELL.match(c) for c in cells)


def strip_title(md):
    """Remove the first '# ' heading that is not inside a fenced code block."""
    lines, in_fence = md.splitlines(keepends=True), False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and (m := HEADING.match(ln.rstrip("\n"))) and m.group(1) == "#":
            return "".join(lines[:i] + lines[i + 1:])
    return md


def convert(md):
    body, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            block, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            body.append(code_block(block))
        elif s.startswith("|"):
            rows, fell_back = [], []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells, fb = _split_row(lines[i])
                if not is_separator(cells):
                    rows.append(cells)
                    fell_back.append(fb)
                i += 1
            if rows:
                # Only rows split by the unmatched-backtick fallback are fitted to the header width;
                # genuinely wider rows keep their cells (the table grows, as before).
                width = len(rows[0])
                rows = [rows[0]] + [fit_to_header([rows[0], r])[1] if fb and len(r) > width else r
                                    for r, fb in zip(rows[1:], fell_back[1:])]
                body.append(table(rows))
            continue
        elif HEADING.match(lines[i]):
            m = HEADING.match(lines[i])
            body.append(para(m.group(2).strip(), style=f"Heading{min(len(m.group(1)), 4)}"))
        elif s == "---":
            body.append(f"<w:p>{ppr(border=True)}</w:p>")
        elif s.startswith(">"):
            body.append(para(s.lstrip(">").strip(), shade="FFF4CE", indent=200))
        elif re.match(r"^[-*] ", s):
            body.append(para(s[2:], num=1))
        elif re.match(r"^\d+\. ", s):
            body.append(para(re.sub(r"^\d+\. ", "", s), num=2))
        elif s:
            body.append(para(s))
        i += 1
    return "".join(body)


def _style(sid, name, size, color, before, after, lvl=None):
    outline = f'<w:outlineLvl w:val="{lvl}"/>' if lvl is not None else ""
    return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="Normal"/>'
            f'<w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="{before}" w:after="{after}"/>{outline}'
            f'</w:pPr><w:rPr><w:b/><w:color w:val="{color}"/><w:sz w:val="{size}"/></w:rPr></w:style>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<w:styles xmlns:w="{NS}/wordprocessingml/2006/main">'
          f'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:cs="{FONT}"/>'
          '<w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="264" '
          'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
          '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
          + _style("Title", "Title", 40, "1F3864", 0, 200)
          + _style("Heading1", "heading 1", 34, "1F3864", 360, 120, 0)
          + _style("Heading2", "heading 2", 28, "2E74B5", 300, 100, 1)
          + _style("Heading3", "heading 3", 24, "2E74B5", 200, 80, 2)
          + _style("Heading4", "heading 4", 22, "000000", 160, 60, 3)
          + "</w:styles>")

NUMBERING = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             f'<w:numbering xmlns:w="{NS}/wordprocessingml/2006/main">'
             '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
             '<w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr>'
             '</w:lvl></w:abstractNum><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
             '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" '
             'w:hanging="360"/></w:pPr></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/>'
             '</w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>')


def document_xml(md, title):
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document '
            f'xmlns:w="{NS}/wordprocessingml/2006/main" xmlns:r="{NS}/officeDocument/2006/relationships"><w:body>'
            + para(title, style="Title") + convert(strip_title(md)) +
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1080" w:right="1080" w:bottom="1080" '
            'w:left="1080" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')


def build(md_path, out_path, title):
    with open(md_path, encoding="utf-8") as fh:
        md = fh.read()
    ct = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="{NS}/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
          '</Types>')
    rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{NS}/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{NS}/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    doc_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{NS}/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{NS}/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                f'<Relationship Id="rId2" Type="{NS}/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
                '</Relationships>')
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (("[Content_Types].xml", ct), ("_rels/.rels", rels),
                           ("word/document.xml", document_xml(md, title)),
                           ("word/_rels/document.xml.rels", doc_rels), ("word/styles.xml", STYLES),
                           ("word/numbering.xml", NUMBERING)):
            z.writestr(name, data)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    build(sys.argv[1], sys.argv[2], sys.argv[3])
