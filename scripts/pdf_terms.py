"""2026 경제금융용어 800선 전체 또는 PER/PBR/EPS 추출 (DB 및 API와 독립)."""

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf


from scripts.paths import DEFAULT_PDF, DEBUG_DIR, OUTPUT_DIR
TARGETS = ("주가수익비율(PER)", "주가순자산비율(PBR)", "주당순이익(EPS)")
TITLE_FONTS = {"SUIT-ExtraBold", "DIN-Bold"}


def pdf_to_book_page(pdf_page: int) -> int | None:
    """현재 2026년 판본의 PDF 19~423쪽만 책 본문 1~405쪽으로 변환한다.

    표지·목차·본문 뒤 부록 등 변환 범위 밖은 추정하지 않고 None을 반환한다.
    다른 판본에 재사용할 수 있는 일반 변환 규칙이 아니다.
    """
    if 19 <= pdf_page <= 423:
        return pdf_page - 18
    return None


@dataclass
class Term:
    term: str
    definition: str
    related_terms: list[str]
    source_pdf_page: int
    source_book_page: int | None

    def to_dict(self, clean_linebreaks: bool = False) -> dict:
        result = asdict(self)
        if clean_linebreaks:
            result["definition"] = clean_definition(self.definition)
        return result


def clean_definition(text: str) -> str:
    """원본의 줄 끝 공백을 단서로 조판 줄바꿈만 연결한다. 원본은 변경하지 않는다."""
    lines = text.splitlines()
    if not lines:
        return ""
    result = lines[0].strip()
    for previous, current in zip(lines, lines[1:]):
        if not current.strip():
            continue
        if not previous.strip():
            result += "\n\n" + current.strip()
            continue
        # 이 PDF는 어절 중간에서 줄이 바뀌면 줄 끝 공백이 없다.
        # 영문 Book- / value의 하이픈도 그대로 유지한다.
        joined_word = (
            not previous[-1].isspace()
            and (previous.endswith("-") or bool(re.search(r"[가-힣]$", previous)
                                                 and re.match(r"[가-힣]", current)))
        )
        result += ("" if joined_word else " ") + current.strip()
    return result.strip()


def iter_content_lines(page):
    """본문 영역의 줄을 위치순으로 반환. 머리말, 쪽번호, 바깥쪽 색인은 제외."""
    lines = [
        line
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        if 70 <= line["bbox"][1] and line["bbox"][3] <= page.rect.height - 55
        and 70 <= line["bbox"][0] < page.rect.width - 55
    ]
    return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))


def is_term_title(line) -> bool:
    """이 판본의 제목을 글꼴, 14pt, 왼쪽 위치로 판별. 영문 제목도 포함한다."""
    spans = [span for span in line["spans"] if span["text"].strip()]
    return bool(spans) and abs(line["bbox"][0] - 85.04) < 3 and all(
        span["font"] in TITLE_FONTS and abs(span["size"] - 14) < 0.2
        for span in spans
    )


def split_related_terms(text: str) -> list[str]:
    """괄호 안의 쉼표를 유지하면서 관련 용어를 구분한다."""
    terms, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char in "(（":
            depth += 1
        elif char in ")）":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            terms.append(text[start:index].strip())
            start = index + 1
    terms.append(text[start:].strip())
    return [term for term in terms if term]


def extract_terms(pdf_path: str | Path = DEFAULT_PDF,
                  targets: tuple[str, ...] | None = None) -> list[Term]:
    """전체 본문을 수집한다. targets를 지정하면 모든 제목을 경계로 쓰면서 대상만 반환."""
    found = []
    active = None
    body, related = [], []
    collecting_related = False

    def finish():
        if active is None:
            return
        title, page_number = active
        definition = "\n".join(body).strip("\n")
        if targets is not None and any(term.term == title for term in found):
            raise ValueError(f"중복 제목 발견: {title}")
        if targets is not None and not definition.strip():
            raise ValueError(f"설명이 비어 있음: {title}")
        found.append(Term(
            term=title,
            definition=definition,
            related_terms=split_related_terms(" ".join(related)),
            source_pdf_page=page_number,
            source_book_page=pdf_to_book_page(page_number),
        ))

    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            if pdf_to_book_page(page_number) is None:
                continue
            for line in iter_content_lines(page):
                text = "".join(span["text"] for span in line["spans"])
                if is_term_title(line):
                    finish()
                    title = text.strip()
                    active = (title, page_number) if targets is None or title in targets else None
                    body, related = [], []
                    collecting_related = False
                elif active is not None:
                    if text.lstrip().startswith("연관검색어"):
                        collecting_related = True
                        related.append(text.lstrip().removeprefix("연관검색어").strip())
                    elif collecting_related:
                        related.append(text.strip())
                    else:
                        body.append(text)
        finish()
    if targets is not None:
        by_title = {term.term: term for term in found}
        missing = set(targets) - by_title.keys()
        if missing:
            raise ValueError(f"대상 제목을 찾지 못함: {', '.join(sorted(missing))}")
        return [by_title[title] for title in targets]
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, default=DEFAULT_PDF)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--scope", choices=("all", "three"), default="all")
    selection.add_argument("--terms", nargs="+", help="PER/PBR/EPS 또는 정식 용어 이름 선택")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--debug", action="store_true", help="상세 JSON·수식 비교 이미지·HTML 저장")
    parser.add_argument("--debug-dir", type=Path, default=DEBUG_DIR)
    parser.add_argument("--restore-formulas", action="store_true", help="호환 옵션. 검증된 수식 복원은 기본 적용")
    parser.add_argument("--clean-linebreaks", action="store_true", help="호환 옵션. 최종 definition 정제는 기본 적용")
    args = parser.parse_args()
    targets = TARGETS if args.scope == "three" else None
    if args.terms:
        aliases = dict(zip(("PER", "PBR", "EPS"), TARGETS))
        targets = tuple(dict.fromkeys(aliases.get(term.upper(), term) for term in args.terms))
    from scripts.glossary import build_glossary, write_outputs
    glossary, report, debug_data = build_glossary(args.pdf, targets=targets)
    write_outputs(args.output_dir, glossary, report,
                  debug_data=debug_data if args.debug else None,
                  debug_dir=args.debug_dir if args.debug else None, pdf_path=args.pdf)
    if targets is not None:
        print(json.dumps(glossary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
