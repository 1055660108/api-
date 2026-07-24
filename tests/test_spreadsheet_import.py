from __future__ import annotations

import io
import unittest

from openpyxl import Workbook

from app.spreadsheet_import import SpreadsheetImportError, extract_prompts, parse_spreadsheet


class SpreadsheetImportTests(unittest.TestCase):
    def test_extracts_named_prompt_column_and_removes_duplicates(self) -> None:
        prompts = extract_prompts([
            ["编号", "视频提示词", "备注"],
            [1, "雨夜街道的电影镜头", "a"],
            [2, "雨夜街道的电影镜头", "duplicate"],
            [3, "清晨森林中的薄雾", "b"],
        ])
        self.assertEqual([item["prompt"] for item in prompts], ["雨夜街道的电影镜头", "清晨森林中的薄雾"])
        self.assertEqual([item["row"] for item in prompts], [2, 4])

    def test_csv_and_tsv_are_supported(self) -> None:
        csv_rows = parse_spreadsheet("prompts.csv", "序号,提示词\n1,镜头一\n2,镜头二".encode())
        tsv_rows = parse_spreadsheet("prompts.tsv", "id\tvideo prompt\n1\tshot one".encode())
        self.assertEqual(len(csv_rows), 2)
        self.assertEqual(tsv_rows[0]["prompt"], "shot one")

    def test_xlsx_is_read_in_streaming_mode(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["视频提示词", "其他"])
        sheet.append(["海面上的日出", "x"])
        output = io.BytesIO()
        workbook.save(output)
        self.assertEqual(parse_spreadsheet("prompts.xlsx", output.getvalue())[0]["prompt"], "海面上的日出")

    def test_rejects_unsupported_or_empty_files(self) -> None:
        with self.assertRaisesRegex(SpreadsheetImportError, "不支持"):
            parse_spreadsheet("prompts.pdf", b"data")
        with self.assertRaisesRegex(SpreadsheetImportError, "为空"):
            parse_spreadsheet("prompts.csv", b"")


if __name__ == "__main__":
    unittest.main()
