"""Tests for tools/md2docx.py (the Word export of the guide)."""
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import md2docx  # noqa: E402


class Md2DocxTests(unittest.TestCase):
    def test_dash_led_cell_is_not_a_separator(self):
        xml = md2docx.convert("| Flag | Meaning |\n|---|:---:|\n| --mode | plan or apply |\n")
        self.assertIn("--mode", xml)
        self.assertEqual(xml.count("<w:tr>"), 2)          # header + data row; separator dropped

    def test_separator_only_table_does_not_crash(self):
        self.assertNotIn("<w:tbl>", md2docx.convert("|---|---|\n"))

    def test_title_h1_removed_even_after_blank_lines(self):
        out = os.path.join(tempfile.mkdtemp(), "x.docx")
        with open(out + ".md", "w") as fh:
            fh.write("\n\n# Original Title\n\nBody text\n")
        md2docx.build(out + ".md", out, "New Title")
        doc = zipfile.ZipFile(out).read("word/document.xml").decode()
        self.assertIn("New Title", doc)
        self.assertNotIn("Original Title", doc)
        self.assertIn("Body text", doc)

    def test_checkbox_and_inline_code(self):
        xml = md2docx.convert("| T1 | `plan` | [ ] |\n")
        self.assertIn("☐", xml)
        self.assertIn("Consolas", xml)


if __name__ == "__main__":
    unittest.main()
