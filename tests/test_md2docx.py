"""Tests for tools/md2docx.py (the Word export of the guide)."""
import os
import sys
import tempfile
import unittest
import zipfile
import xml.dom.minidom

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import md2docx  # noqa: E402

GUIDE = os.path.join(os.path.dirname(__file__), "..", "docs", "IMPLEMENTATION_GUIDE.md")

# Child order required by ECMA-376 for the elements this converter emits (subset of the full sequence).
RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "color", "sz", "szCs"]
PPR_ORDER = ["pStyle", "keepNext", "keepLines", "numPr", "pBdr", "shd", "spacing", "ind", "jc", "outlineLvl"]
TBLPR_ORDER = ["tblStyle", "tblW", "jc", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook"]
TCPR_ORDER = ["tcW", "gridSpan", "vMerge", "tcBorders", "shd"]


def schema_problems(xml_text):
    """Return a list of ordering/structure violations found in a WordprocessingML part."""
    problems = []
    dom = xml.dom.minidom.parseString(xml_text)
    for tag, order in (("w:rPr", RPR_ORDER), ("w:pPr", PPR_ORDER), ("w:tblPr", TBLPR_ORDER), ("w:tcPr", TCPR_ORDER)):
        for node in dom.getElementsByTagName(tag):
            names = [c.localName for c in node.childNodes if c.nodeType == c.ELEMENT_NODE]
            unknown = [n for n in names if n not in order]
            if unknown:
                problems.append(f"{tag} has unexpected children {unknown}")
                continue
            if names != sorted(names, key=order.index):
                problems.append(f"{tag} children out of order: {names}")
            if len(names) != len(set(names)):
                problems.append(f"{tag} has duplicate children: {names}")
    for tbl in dom.getElementsByTagName("w:tbl"):
        kids = [c.localName for c in tbl.childNodes if c.nodeType == c.ELEMENT_NODE]
        if kids[:2] != ["tblPr", "tblGrid"]:
            problems.append(f"w:tbl must start with tblPr, tblGrid; got {kids[:2]}")
    return problems


class SchemaTests(unittest.TestCase):
    def test_full_guide_is_schema_ordered(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "guide.docx")
            md2docx.build(GUIDE, out, "Title")
            with zipfile.ZipFile(out) as z:
                for part in ("word/document.xml", "word/styles.xml", "word/numbering.xml"):
                    self.assertEqual(schema_problems(z.read(part).decode()), [], part)

    def test_code_in_header_cell_has_single_color(self):
        doc = f"<w:document xmlns:w='w'><w:body>{md2docx.convert('| `code` header | b |')}</w:body></w:document>"
        self.assertEqual(schema_problems(doc), [])
        self.assertIn(md2docx.HEADER_TEXT, doc)
        self.assertNotIn(md2docx.CODE_COLOR, doc)


class ConversionTests(unittest.TestCase):
    def test_dash_led_cell_is_not_a_separator(self):
        xml_ = md2docx.convert("| Flag | Meaning |\n|---|:---:|\n| --mode | plan or apply |\n")
        self.assertIn("--mode", xml_)
        self.assertEqual(xml_.count("<w:tr>"), 2)          # header + data row; separator dropped

    def test_separator_only_table_does_not_crash(self):
        self.assertNotIn("<w:tbl>", md2docx.convert("|---|---|\n"))

    def test_title_h1_removed_even_after_blank_lines(self):
        self.assertEqual(md2docx.strip_title("\n\n# Original Title\n\nBody\n"), "\n\n\nBody\n")

    def test_comment_in_code_fence_is_not_taken_as_title(self):
        md = "```bash\n# install deps\n```\n# Real Title\nBody\n"
        out = md2docx.strip_title(md)
        self.assertIn("# install deps", out)
        self.assertNotIn("Real Title", out)

    def test_checkboxes_in_lists_tables_and_text(self):
        self.assertIn("☐ Verify grants", md2docx.convert("- [ ] Verify grants"))
        self.assertIn("☐", md2docx.convert("| T1 | `plan` | [ ] |"))
        self.assertIn("☑ done", md2docx.convert("- [x] done"))
        self.assertIn("[ ]", md2docx.convert("`[ ]` stays literal in code"))

    def test_brackets_in_prose_are_left_alone(self):
        for text in ("matrix[x] and opt[X]", "use [ ] for empty", "**opt[X]** bold", "see [x] below"):
            xml_ = md2docx.convert(text)
            self.assertNotIn("\u2610", xml_, text)
            self.assertNotIn("\u2611", xml_, text)
        cell = md2docx.convert("| H1 | H2 |\n|---|---|\n| a [ ] b | [x] |")
        self.assertIn("a [ ] b", cell)                    # partial cell text untouched
        self.assertIn("\u2611", cell)                      # whole-cell [x] converted
        self.assertIn("\u2610 step", md2docx.convert("1. [ ] step"))
        self.assertIn("[x]y", md2docx.convert("- [x]y no space"))   # needs '[x] ' prefix

    def test_bare_and_leading_checkboxes_in_every_block(self):
        G, X = "\u2610", "\u2611"
        cases = {"- [ ]": G, "1. [x]": X, "> [ ] Confirm prerequisites": G + " Confirm",
                 "[x] done": X + " done", "## [ ] todo": G + " todo"}
        for md, expected in cases.items():
            self.assertIn(expected, md2docx.convert(md), md)
        cell = md2docx.convert("| H | I |\n|---|---|\n| [ ] pending | [x] |")
        self.assertIn(G + " pending", cell)
        self.assertIn(X, cell)
        for literal in ("[x](http://example.com)", "- [x]y", "Status [x] mid-text"):
            out = md2docx.convert(literal)
            self.assertNotIn(G, out, literal)
            self.assertNotIn(X, out, literal)

    def test_genuinely_wider_rows_are_not_merged(self):
        xml_ = md2docx.convert("| A | B |\n|---|---|\n| 1 | 2 | 3 |")
        self.assertEqual(xml_.count("<w:gridCol "), 3)          # table grows, as before round 3
        self.assertNotIn("2|3", xml_)
        self.assertIn(">3</w:t>", xml_)

    def test_four_space_indented_hash_is_not_a_heading_or_title(self):
        md = "    # install deps\n# Real Title\nBody\n"
        out = md2docx.strip_title(md)
        self.assertIn("    # install deps", out)
        self.assertNotIn("Real Title", out)
        self.assertNotIn("Heading", md2docx.convert("    # install deps"))
        self.assertIn('w:val="Heading1"', md2docx.convert("   # three spaces is still a heading"))

    def test_indented_title_is_removed(self):
        out = md2docx.strip_title("  # Indented Title\nBody\n")
        self.assertNotIn("Indented Title", out)
        self.assertIn("Body", out)

    def test_unmatched_backtick_row_fits_header(self):
        xml_ = md2docx.convert("| A | B |\n|---|---|\n| it`s | a|b |\n| ok | ok |")
        self.assertEqual(xml_.count("<w:gridCol "), 2)     # still 2 columns
        self.assertEqual(xml_.count("<w:tc>"), 6)           # 3 rows x 2 cells
        self.assertIn(">a|b</w:t>", xml_)                  # extra cell merged back (cell text is trimmed)
        self.assertEqual(md2docx.fit_to_header([["h1", "h2"], ["a", "b", "c", "d"], ["x"]]),
                         [["h1", "h2"], ["a", "b|c|d"], ["x"]])

    def test_empty_edge_cells_and_unmatched_backtick(self):
        self.assertEqual(md2docx.split_row("|a||"), ["a", ""])
        self.assertEqual(md2docx.split_row("| a | `x|y` | b |"), [" a ", " `x|y` ", " b "])
        self.assertEqual(md2docx.split_row("| it`s | x |"), [" it`s ", " x "])

    def test_headings_five_and_six(self):
        xml_ = md2docx.convert("##### Notes\n###### Deep")
        self.assertNotIn("#####", xml_)
        self.assertEqual(xml_.count('w:val="Heading4"'), 2)

    def test_build_writes_title_once_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = os.path.join(d, "in.md"), os.path.join(d, "out.docx")
            with open(src, "w") as fh:
                fh.write("\n# Original\n\nBody text\n")
            md2docx.build(src, out, "New Title")
            with zipfile.ZipFile(out) as z:
                doc = z.read("word/document.xml").decode()
        self.assertEqual(doc.count("New Title"), 1)
        self.assertNotIn("Original", doc)
        self.assertIn("Body text", doc)


if __name__ == "__main__":
    unittest.main()
