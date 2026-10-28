import json
import re
import unittest

import pymupdf

from scripts.paths import LEGACY_DIR, PROJECT_ROOT

from scripts.pdf_terms import (DEFAULT_PDF, TARGETS, clean_definition, extract_terms,
                       is_term_title, iter_content_lines, pdf_to_book_page,
                       split_related_terms)


class ActualPDFTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.terms = {term.term: term for term in extract_terms(targets=TARGETS)}

    @classmethod
    def tearDownClass(cls):
        """직접 실행과 unittest 실행 모두에서 실제 추출 레코드 전체를 출력한다."""
        for label, clean_linebreaks in [("원문", False), ("줄바꿈 정제본", True)]:
            records = [cls.terms[title].to_dict(clean_linebreaks=clean_linebreaks)
                       for title in TARGETS]
            print(f"\n[DB 저장용 데이터 확인 · {label} · {len(records)}건]", flush=True)
            print(json.dumps(records, ensure_ascii=False, indent=2), flush=True)

    def test_three_titles_and_source_pages(self):
        self.assertEqual(set(self.terms), set(TARGETS))
        self.assertEqual([self.terms[t].source_pdf_page for t in TARGETS], [310, 310, 313])

    def test_pdf_and_book_page_pairs(self):
        for title, expected in zip(TARGETS, [(310, 292), (310, 292), (313, 295)]):
            with self.subTest(term=title):
                term = self.terms[title]
                self.assertEqual((term.source_pdf_page, term.source_book_page), expected)

    def test_conversion_matches_printed_body_page_numbers(self):
        # 변환식과 독립적으로 본문 전체 405쪽 하단에 실제 인쇄된 숫자와 대조한다.
        with pymupdf.open(DEFAULT_PDF) as document:
            for pdf_page in range(19, 424):
                with self.subTest(pdf_page=pdf_page):
                    page = document[pdf_page - 1]
                    printed = page.get_text("text", clip=pymupdf.Rect(
                        0, page.rect.height - 50, page.rect.width, page.rect.height
                    )).strip()
                    self.assertEqual(str(pdf_to_book_page(pdf_page)), printed)

    def test_complete_body_against_independently_selected_pdf_regions(self):
        # 실제 PDF를 대조해 고정한 본문 사각형. 제목 판별 코드를 쓰지 않는 별도 기준이다.
        regions = {
            TARGETS[0]: [(310, 230, 470)],
            TARGETS[1]: [(310, 550, 670), (311, 85, 108)],
            TARGETS[2]: [(313, 485, 670), (314, 85, 125)],
        }
        with pymupdf.open(DEFAULT_PDF) as document:
            for title, areas in regions.items():
                with self.subTest(term=title):
                    expected = "".join(
                        document[page - 1].get_text("text", clip=pymupdf.Rect(80, top, 475, bottom))
                        for page, top, bottom in areas
                    )
                    actual = self.terms[title].definition
                    self.assertEqual(re.sub(r"\s", "", actual), re.sub(r"\s", "", expected))

    def test_page_boundary_continuations(self):
        pbr = self.terms[TARGETS[1]]
        eps = self.terms[TARGETS[2]]
        self.assertIn("주식을 \n가치주식", pbr.definition)
        self.assertIn("주식을 가치주식", clean_definition(pbr.definition))
        self.assertIn("EPS 증가는 \n동 기업", eps.definition)
        self.assertTrue(clean_definition(eps.definition).endswith("신호로 해석될 수 있다."))

    def test_related_terms_including_absent_section(self):
        self.assertEqual(self.terms[TARGETS[0]].related_terms,
                         ["주당순이익(EPS)", "주가순자산비율(PBR)"])
        self.assertEqual(self.terms[TARGETS[1]].related_terms, ["주가수익비율(PER)"])
        self.assertEqual(self.terms[TARGETS[2]].related_terms, [])

    def test_no_neighboring_body_or_page_decoration(self):
        excluded = {
            TARGETS[0]: ["죄수의 딜레마", "치킨게임", "주가순자산가치"],
            TARGETS[1]: ["주가연계증권", "Equity Linked Security"],
            TARGETS[2]: ["콜옵션", "풋옵션", "주식시장은 주식회사의"],
        }
        for title, words in excluded.items():
            for word in words + ["연관검색어", "경제금융용어  800선"]:
                with self.subTest(term=title, excluded=word):
                    self.assertNotIn(word, self.terms[title].definition)

    def test_mixed_fonts_and_repeated_header(self):
        with pymupdf.open(DEFAULT_PDF) as document:
            lines = iter_content_lines(document[309])
            per = next(line for line in lines if "".join(s["text"] for s in line["spans"]) == TARGETS[0])
            self.assertEqual({s["font"] for s in per["spans"]}, {"SUIT-ExtraBold", "DIN-Bold"})
            self.assertTrue(is_term_title(per))
            headers = [line for block in document[312].get_text("dict")["blocks"]
                       for line in block.get("lines", []) if line["bbox"][1] < 60]
            self.assertTrue(any("EPS" in "".join(s["text"] for s in line["spans"]) for line in headers))
            self.assertFalse(any(is_term_title(line) for line in headers))

    def test_raw_preserved_when_exporting_cleaned_definition(self):
        for term in self.terms.values():
            original = term.definition
            cleaned = term.to_dict(clean_linebreaks=True)
            self.assertNotIn("\n", cleaned["definition"])
            self.assertEqual(term.definition, original)
            self.assertEqual(set(cleaned), {
                "term", "definition", "related_terms", "source_pdf_page", "source_book_page"
            })
            self.assertEqual(re.sub(r"\s", "", original), re.sub(r"\s", "", cleaned["definition"]))


class CleanupTests(unittest.TestCase):
    def test_book_page_conversion_boundaries_and_unknown_pages(self):
        self.assertEqual(pdf_to_book_page(19), 1)
        self.assertEqual(pdf_to_book_page(423), 405)
        for page in [-1, 0, 1, 4, 17, 18, 424, 425, 428, 429]:
            with self.subTest(pdf_page=page):
                self.assertIsNone(pdf_to_book_page(page))

    def test_wraps_spaces_hyphens_and_paragraphs(self):
        self.assertEqual(clean_definition("원리금상환비\n율"), "원리금상환비율")
        self.assertEqual(clean_definition("흑자를 \n냈다면"), "흑자를 냈다면")
        self.assertEqual(clean_definition("Book-\nvalue"), "Book-value")
        self.assertEqual(clean_definition("첫 문장.\n다음 문장."), "첫 문장. 다음 문장.")
        self.assertEqual(clean_definition("첫 문단\n\n다음 문단"), "첫 문단\n\n다음 문단")

    def test_commas_inside_parentheses(self):
        self.assertEqual(split_related_terms("기업분할(물적분할, 인적분할), 주식시장"),
                         ["기업분할(물적분할, 인적분할)", "주식시장"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
