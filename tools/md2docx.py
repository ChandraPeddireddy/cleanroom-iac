"""Minimal Markdown -> .docx converter (stdlib only), used to build the Word copy of the guide.

    python3 tools/md2docx.py docs/IMPLEMENTATION_GUIDE.md docs/CleanRooms_IaC_Implementation_Guide.docx \
        "Clean Rooms as Code: Implementation and Test Guide"

Supports: # headings, paragraphs, - / 1. lists, [ ] checkboxes, pipe tables, ``` code blocks,
> quotes, ---, **bold**, `code`, [text](url). The first H1 is replaced by the given title.
Not supported: escaped pipes (\|) inside table cells, and code or links nested inside **bold**.
"""
import re
import sys
import zipfile
from xml.sax.saxutils import escape

FONT = "Calibri"
MONO = "Consolas"
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
NS = "http://schemas.openxmlformats.org"


def runs(text, base_bold=False, size=None):
    out = []
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))", text):
        if not part:
            continue
        bold, mono, t = base_bold, False, part
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            bold, t = True, part[2:-2]
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            mono, t = True, part[1:-1]
        elif LINK.fullmatch(part):
            m = LINK.fullmatch(part)
            t = m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})"
        if t.strip() == "[ ]":
            t = "☐"
        rpr = "<w:b/>" if bold else ""
        if mono:
            rpr += f'<w:rFonts w:ascii="{MONO}" w:hAnsi="{MONO}"/><w:color w:val="9C2B0E"/>'
        if size:
            rpr += f'<w:sz w:val="{size}"/>'
        out.append(f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(t)}</w:t></w:r>')
    return "".join(out)


def para(text, style=None, num=None, shade=None, indent=None):
    ppr = f'<w:pStyle w:val="{style}"/>' if style else ""
    if num:
        ppr += f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr>'
    if shade:
        ppr += f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>'
    if indent:
        ppr += f'<w:ind w:left="{indent}"/>'
    return f"<w:p><w:pPr>{ppr}</w:pPr>{runs(text)}</w:p>"


def code_block(lines):
    out = [('<w:p><w:pPr><w:spacing w:before="0" w:after="0"/>'
            '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/><w:ind w:left="200"/></w:pPr>'
            f'<w:r><w:rPr><w:rFonts w:ascii="{MONO}" w:hAnsi="{MONO}"/><w:sz w:val="17"/></w:rPr>'
            f'<w:t xml:space="preserve">{escape(ln)}</w:t></w:r></w:p>') for ln in (lines or [""])]
    return "".join(out) + '<w:p><w:pPr><w:spacing w:after="60"/></w:pPr></w:p>'


def table(rows):
    border = "<w:tblBorders>" + "".join(
        f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'
        for s in ("top", "left", "bottom", "right", "insideH", "insideV")) + "</w:tblBorders>"
    xml = [f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>{border}'
           '<w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr>']
    for i, cells in enumerate(rows):
        xml.append("<w:tr>" + ("<w:trPr><w:tblHeader/></w:trPr>" if i == 0 else ""))
        for c in cells:
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="1F3864"/>' if i == 0 else ""
            r = runs(c.strip(), base_bold=(i == 0), size=18)
            if i == 0:
                r = r.replace("<w:rPr>", '<w:rPr><w:color w:val="FFFFFF"/>')
            xml.append(f'<w:tc><w:tcPr>{shade}</w:tcPr><w:p><w:pPr><w:spacing w:before="20" w:after="20"/>'
                       f'</w:pPr>{r}</w:p></w:tc>')
        xml.append("</w:tr>")
    return "".join(xml) + '</w:tbl><w:p><w:pPr><w:spacing w:after="60"/></w:pPr></w:p>'


SEPARATOR_CELL = re.compile(r"^\s*:?-{3,}:?\s*$")


def is_separator(cells):
    """A table separator row has only dash cells (|---|:---:|); a data cell like `--mode` is not one."""
    return bool(cells) and all(SEPARATOR_CELL.match(c) for c in cells)


def split_row(line):
    cells, cur, in_code = [], "", False
    for ch in line.strip().strip("|"):
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(cur)
            cur = ""
        else:
            cur += ch
    return cells + [cur]


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
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = split_row(lines[i])
                if not is_separator(cells):
                    rows.append(cells)
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                body.append(table([r + [""] * (width - len(r)) for r in rows]))
            continue
        elif re.match(r"^#{1,4} ", s):
            level = len(s.split(" ")[0])
            body.append(para(s[level + 1:], style=f"Heading{level}"))
        elif s == "---":
            body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="A6A6A6"/>'
                        '</w:pBdr></w:pPr></w:p>')
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


def build(md_path, out_path, title):
    md = re.sub(r"^# .*(\n|$)", "", open(md_path, encoding="utf-8").read(), count=1, flags=re.MULTILINE)
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document '
           f'xmlns:w="{NS}/wordprocessingml/2006/main" xmlns:r="{NS}/officeDocument/2006/relationships"><w:body>'
           + para(title, style="Title") + convert(md) +
           '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1080" w:right="1080" w:bottom="1080" '
           'w:left="1080" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
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
        for name, data in (("[Content_Types].xml", ct), ("_rels/.rels", rels), ("word/document.xml", doc),
                           ("word/_rels/document.xml.rels", doc_rels), ("word/styles.xml", STYLES),
                           ("word/numbering.xml", NUMBERING)):
            z.writestr(name, data)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    build(sys.argv[1], sys.argv[2], sys.argv[3])
