"""현재 2026년 판본의 목차 대조, 원문 보존 검사, 수동 검토 후보 보고."""

from collections import Counter
from contextlib import redirect_stdout
import json
from pathlib import Path
import re
import sys

import pymupdf

from scripts.pdf_terms import (Term, clean_definition, is_term_title, iter_content_lines,
                       pdf_to_book_page)


def title_key(text: str) -> str:
    """비교에만 사용. 저장할 원문 제목은 바꾸지 않는다."""
    return re.sub(r"\s", "", text).replace("’", "'").replace("‘", "'")


def extract_toc(document) -> tuple[list[dict], list[dict]]:
    """PDF 4~17쪽의 두 단 목차를 왼쪽 단부터 읽고 줄바꿈 항목을 연결한다."""
    entries, issues = [], []
    for pdf_page in range(4, 18):
        lines = [line for block in document[pdf_page - 1].get_text("dict")["blocks"]
                 for line in block.get("lines", [])
                 if any(s["font"] == "NanumSquareOTFL" for s in line["spans"])]
        for right_column in (False, True):
            pending = []
            for line in sorted((line for line in lines
                                if (line["bbox"][0] > 260) == right_column),
                               key=lambda line: line["bbox"][1]):
                text = "".join(s["text"] for s in line["spans"])
                match = re.search(r"·{2,}\s*(\d+)\s*$", text)
                if match:
                    title = " ".join(pending + [text[:match.start()].strip()])
                    entries.append({"term": title, "source_book_page": int(match[1]),
                                    "toc_pdf_page": pdf_page})
                    pending = []
                else:
                    # 이 판본의 목차 줄바꿈 끝에 붙는 장식용 가운데점을 제거한다.
                    pending.append(text.rstrip().rstrip("·").strip())
            if pending:
                issues.append({"toc_pdf_page": pdf_page, "unparsed_text": pending})
    return entries, issues


def _anchors(document) -> list[dict]:
    return [{"pdf_page": number, "line": line,
             "term": "".join(s["text"] for s in line["spans"]).strip()}
            for number in range(19, min(len(document), 423) + 1)
            for line in iter_content_lines(document[number - 1]) if is_term_title(line)]


def _regions(document, anchors, index):
    """제목 직후부터 다음 제목 직전까지 페이지별 본문 사각형을 만든다."""
    anchor = anchors[index]
    following = anchors[index + 1] if index + 1 < len(anchors) else None
    last_page = following["pdf_page"] if following else 423
    for number in range(anchor["pdf_page"], last_page + 1):
        page = document[number - 1]
        top = anchor["line"]["bbox"][3] + 0.01 if number == anchor["pdf_page"] else 70
        bottom = following["line"]["bbox"][1] - 0.01 if (
            following and number == following["pdf_page"]
        ) else page.rect.height - 55
        if bottom > top:
            yield number, pymupdf.Rect(70, top, page.rect.width - 55, bottom)


def inspect_layout(document, terms: list[Term]) -> tuple[list[dict], list[dict], list[dict]]:
    """수식/표/이미지 후보와 별도 텍스트 모드 대조 차이를 기록한다. 구조 복원은 하지 않는다."""
    anchors = _anchors(document)
    positions = {(a["term"], a["pdf_page"]): index for index, a in enumerate(anchors)}
    tables_by_page = {}
    reviews, coverage_issues, boundary_issues = [], [], []
    for term in terms:
        index = positions.get((term.term, term.source_pdf_page))
        if index is None:
            boundary_issues.append({"term": term.term, "reason": "title_anchor_missing"})
            continue
        reasons, evidence, reference_parts = set(), [], []
        related_started = False
        for number, region in _regions(document, anchors, index):
            page = document[number - 1]
            region_lines = [line for line in iter_content_lines(page)
                            if region.y0 <= line["bbox"][1] and line["bbox"][3] <= region.y1]
            body_region = pymupdf.Rect(region)
            for line in region_lines:
                text = "".join(s["text"] for s in line["spans"])
                if text.lstrip().startswith("연관검색어"):
                    body_region.y1 = min(body_region.y1, line["bbox"][1] - 0.01)
                    break
            if not related_started and body_region.y1 > body_region.y0:
                reference_parts.append(page.get_text("text", clip=body_region, sort=True))
            if any("".join(s["text"] for s in line["spans"]).lstrip().startswith("연관검색어")
                   for line in region_lines):
                related_started = True
            for line in region_lines:
                for span in line["spans"]:
                    if "TeX" in span["font"] or "Symbol" in span["font"]:
                        reasons.add("formula_font")
                        evidence.append({"pdf_page": number, "font": span["font"],
                                         "text": span["text"]})
            if number not in tables_by_page:
                # 셀 내용은 추출하지 않고 표가 차지하는 위치만 검사한다.
                # 라이브러리의 안내 문구가 CLI의 JSON 표준 출력을 오염시키지 않게 한다.
                with redirect_stdout(sys.stderr):
                    tables_by_page[number] = [list(table.bbox) for table in page.find_tables().tables]
            for bbox in tables_by_page[number]:
                if (region & pymupdf.Rect(bbox)).get_area() > 0:
                    reasons.add("table_layout")
                    evidence.append({"pdf_page": number, "table_bbox": bbox})
            for image in page.get_image_info():
                if (region & pymupdf.Rect(image["bbox"])).get_area() > 0:
                    reasons.add("image_layout")
                    evidence.append({"pdf_page": number, "image_bbox": list(image["bbox"])})
        if re.search(r"[=＝×÷∑∏√∫≠≤≥±]", term.definition):
            reasons.add("formula_symbols")
        controls = sorted({f"U+{ord(char):04X}" for char in term.definition
                           if ord(char) < 32 and char not in "\n\r\t"})
        if controls:
            reasons.add("pdf_control_character")
            evidence.append({"control_characters": controls})
        # 두 방법의 읽기 순서 차이와 실제 문자 누락을 구분해 보고한다.
        actual = re.sub(r"\s", "", term.definition)
        reference = re.sub(r"\s", "", "".join(reference_parts))
        if actual != reference:
            missing = Counter(reference) - Counter(actual)
            extra = Counter(actual) - Counter(reference)
            issue = {"term": term.term, "source_pdf_page": term.source_pdf_page,
                     "missing_character_counts": dict(missing), "extra_character_counts": dict(extra),
                     "order_only": not missing and not extra}
            coverage_issues.append(issue)
            reasons.add("reference_text_difference")
        if reasons:
            reviews.append({"term": term.term, "source_pdf_page": term.source_pdf_page,
                            "source_book_page": term.source_book_page,
                            "reasons": sorted(reasons), "evidence": evidence})
    return reviews, coverage_issues, boundary_issues


def validate_extraction(pdf_path, terms: list[Term], scope="all") -> dict:
    """데이터 검증 실패와 휴리스틱 검토 경고를 분리해 JSON 보고서를 만든다."""
    with pymupdf.open(pdf_path) as document:
        toc, toc_issues = extract_toc(document)
        reviews, coverage, boundaries = inspect_layout(document, terms)
    selected = {title_key(term.term) for term in terms}
    expected = toc if scope == "all" else [entry for entry in toc if title_key(entry["term"]) in selected]
    actual_pairs = {(title_key(term.term), term.source_book_page) for term in terms}
    expected_pairs = {(title_key(entry["term"]), entry["source_book_page"]) for entry in expected}
    missing = [entry for entry in expected if (title_key(entry["term"]), entry["source_book_page"]) not in actual_pairs]
    unexpected = [term.to_dict() | {"definition": ""} for term in terms
                  if (title_key(term.term), term.source_book_page) not in expected_pairs]
    counts = Counter(title_key(term.term) for term in terms)
    duplicates = [{"key": key, "count": count} for key, count in counts.items() if count > 1]
    toc_counts = Counter(title_key(entry["term"]) for entry in toc)
    toc_duplicates = [{"key": key, "count": count} for key, count in toc_counts.items() if count > 1]
    by_pair = {(title_key(term.term), term.source_book_page): term for term in terms}
    presentation_differences = []
    for entry in expected:
        term = by_pair.get((title_key(entry["term"]), entry["source_book_page"]))
        if term and entry["term"] != term.term:
            presentation_differences.append({
                "toc_term": entry["term"], "body_term": term.term,
                "source_book_page": term.source_book_page,
                "kind": "whitespace_only" if re.sub(r"\s", "", entry["term"]) ==
                re.sub(r"\s", "", term.term) else "quote_variant",
            })
    empty, lengths, preservation, page_errors, contamination = [], [], [], [], []
    for term in terms:
        length = len(re.sub(r"\s", "", term.definition))
        if not length:
            empty.append(term.term)
        elif length < 80 or length > 2000:
            lengths.append({"term": term.term, "non_whitespace_length": length})
        if re.sub(r"\s", "", term.definition) != re.sub(r"\s", "", clean_definition(term.definition)):
            preservation.append(term.term)
        if term.source_book_page is None or term.source_book_page != pdf_to_book_page(term.source_pdf_page):
            page_errors.append(term.term)
        if "연관검색어" in term.definition or "I 경제금융용어  800선" in term.definition:
            contamination.append(term.term)
    for issue in lengths:
        term = next(term for term in terms if term.term == issue["term"])
        reviews.append({**issue, "source_pdf_page": term.source_pdf_page,
                        "source_book_page": term.source_book_page,
                        "reasons": ["unusual_definition_length"], "evidence": []})
    merged_reviews = {}
    for review in reviews:
        if review["term"] in merged_reviews:
            previous = merged_reviews[review["term"]]
            previous["reasons"] = sorted(set(previous["reasons"] + review["reasons"]))
            previous["evidence"].extend(review.get("evidence", []))
            if "non_whitespace_length" in review:
                previous["non_whitespace_length"] = review["non_whitespace_length"]
        else:
            merged_reviews[review["term"]] = review
    reviews = list(merged_reviews.values())
    # 목차의 모든 제목·시작 페이지가 맞는지는 경계 누락/오탐의 독립된 검사다.
    checks = {
        "toc_parse": not toc_issues,
        "toc_count_matches_extraction": len(expected) == len(terms),
        "toc_title_and_page_match": not missing and not unexpected,
        "no_duplicate_terms": not duplicates,
        "no_duplicate_toc_entries": not toc_duplicates,
        "no_empty_definitions": not empty,
        "page_mapping": not page_errors,
        "cleaned_characters_preserved": not preservation,
        "no_obvious_body_contamination": not contamination and not boundaries,
        "independent_reference_no_missing_characters": not any(
            issue["missing_character_counts"] for issue in coverage),
        "independent_reference_text_match": not coverage,
    }
    return {
        "summary": {"scope": scope, "extracted_count": len(terms), "toc_count": len(toc),
                    "book_title_expected_count": 800, "difference_from_book_title": len(toc) - 800,
                    "missing_count": len(missing), "unexpected_count": len(unexpected),
                    "duplicate_count": len(duplicates), "empty_count": len(empty),
                    "review_term_count": len({entry["term"] for entry in reviews}),
                    "formula_candidate_count": sum(any(reason.startswith("formula_") for reason in entry["reasons"])
                                                   for entry in reviews),
                    "table_candidate_count": sum("table_layout" in entry["reasons"] for entry in reviews),
                    "image_candidate_count": sum("image_layout" in entry["reasons"] for entry in reviews),
                    "unusual_length_count": len(lengths),
                    "reference_difference_count": len(coverage),
                    "reference_order_only_count": sum(issue["order_only"] for issue in coverage),
                    "passed_checks": [name for name, passed in checks.items() if passed],
                    "failed_checks": [name for name, passed in checks.items() if not passed]},
        "checks": checks, "toc_entries": toc, "toc_parse_issues": toc_issues,
        "missing_terms": missing, "unexpected_terms": unexpected,
        "duplicate_terms": duplicates, "duplicate_toc_entries": toc_duplicates,
        "presentation_differences": presentation_differences, "empty_definitions": empty,
        "unusual_lengths": lengths, "length_thresholds": {"short_below": 80, "long_above": 2000},
        "character_preservation_errors": preservation, "page_mapping_errors": page_errors,
        "body_contamination_candidates": contamination, "boundary_issues": boundaries,
        "reference_text_differences": coverage, "manual_review": reviews,
        "limitations": [
            "수식/표/이미지 분류는 위치·글꼴·기호에 의한 후보 탐지이며 완전 탐지를 보장하지 않음",
            "본문 혼합 검사는 목차 경계 대조 및 별도 텍스트 모드 대조이며 의미 수준의 혼합을 보장하지 않음",
            "목차와 본문 제목 원문은 수정하지 않으며 비교할 때만 공백·작은따옴표 표기를 통일함",
            "수식/표 구조 복원과 수식 정규화는 수행하지 않음",
        ],
    }


def write_results(output_dir, terms: list[Term], report: dict, scope="all") -> None:
    """원문/정제 데이터, 검증 보고서, 수동 검토 목록을 각각 저장한다."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = "all_terms" if scope == "all" else "terms"
    payloads = {
        f"{prefix}_raw.json": [term.to_dict() for term in terms],
        f"{prefix}_cleaned.json": [term.to_dict(True) for term in terms],
        f"{prefix}_validation.json": report,
        f"{prefix}_manual_review.json": report["manual_review"],
    }
    for filename, payload in payloads.items():
        (output_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / f"{prefix}_report.md").write_text(format_report(report), encoding="utf-8")


def format_report(report: dict) -> str:
    """검증 결과와 수동 검토 대상 전체를 사람이 읽을 수 있는 보고서로 만든다."""
    summary = report["summary"]
    lines = ["# 전체 경제금융용어 추출 결과", "",
             f"- 추출: {summary['extracted_count']}개 / 목차: {summary['toc_count']}개",
             f"- 파일명 800선과 실제 목차 항목 수 차이: {summary['difference_from_book_title']}개. 임의 항목 생성 없음.",
             f"- 목차 대비 누락 {summary['missing_count']}개, 추가 {summary['unexpected_count']}개, "
             f"중복 {summary['duplicate_count']}개, 빈 설명 {summary['empty_count']}개",
             f"- 수동 검토 대상: {summary['review_term_count']}개",
             f"- 수식 후보 {summary['formula_candidate_count']}개, 표 후보 {summary['table_candidate_count']}개, "
             f"이미지 후보 {summary['image_candidate_count']}개, 길이 경고 {summary['unusual_length_count']}개 (중복 포함)", "",
             "## 검증 결과", "", "| 검사 | 결과 |", "| --- | --- |"]
    lines.extend(f"| {name} | {'통과' if passed else '실패·수동 검토'} |"
                 for name, passed in report["checks"].items())
    lines += ["", "## 목차와 본문 제목 표기 차이", "",
              "비교할 때만 공백·작은따옴표를 통일했고 원문 제목은 유지했습니다.", "",
              "| 목차 | 본문 | 책 쪽 | 차이 |", "| --- | --- | --- | --- |"]
    lines.extend(f"| {entry['toc_term']} | {entry['body_term']} | {entry['source_book_page']} | {entry['kind']} |"
                 for entry in report["presentation_differences"])
    lines += ["", "## 별도 텍스트 모드 대조 차이", "",
              "순서만 다른 항목은 수식·표·PDF 내부 저장 순서 등을 원본 화면에서 검토해야 합니다. "
              "제어문자는 임의 삭제하지 않았습니다.", "",
              "| 용어 | PDF 쪽 | 차이 |", "| --- | --- | --- |"]
    for issue in report["reference_text_differences"]:
        detail = "문자 순서 차이" if issue["order_only"] else (
            f"누락 {json.dumps(issue['missing_character_counts'], ensure_ascii=False)} / "
            f"추가 {json.dumps(issue['extra_character_counts'], ensure_ascii=False)}")
        lines.append(f"| {issue['term']} | {issue['source_pdf_page']} | {detail} |")
    lines += ["", "## 수동 검토 대상 전체", "",
              "수식 기호·전용 글꼴, 표 경계, 이미지 위치, 비정상 길이 등을 이용한 후보 목록입니다. "
              "모든 수식·표의 완전 탐지를 보장하지 않으며 자동 복원·정규화를 하지 않습니다.", "",
              "| 용어 | PDF 쪽 | 책 쪽 | 검토 이유 |", "| --- | --- | --- | --- |"]
    lines.extend(f"| {entry['term']} | {entry['source_pdf_page']} | {entry['source_book_page']} | "
                 f"{', '.join(entry['reasons'])} |" for entry in report["manual_review"])
    lines += ["", "## 제한 사항", ""] + [f"- {item}" for item in report["limitations"]]
    return "\n".join(lines) + "\n"
