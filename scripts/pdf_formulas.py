from collections import Counter
from dataclasses import dataclass
import hashlib
from html import escape
import json
from pathlib import Path
import re
import unicodedata

import pymupdf

from scripts.pdf_terms import DEFAULT_PDF, Term, is_term_title, iter_content_lines, pdf_to_book_page


PDF_SHA256 = "d48f701f84324f734aa2b4fa255d69df2d46bc311e27a16fe50f48041cc9ac9b"


class FormulaRestorationError(ValueError):
    """근거 없는 보완을 막기 위한 검증 오류."""


@dataclass(frozen=True)
class Region:
    pdf_page: int
    bbox: tuple[float, float, float, float]
    fraction_count: int
    role: str = "equation"


# 원본 화면으로 검토한 영역. 정답 수식 문자열은 저장하지 않으며 실제 글자를 읽는다.
REGIONS = {
    "경제성장률": (Region(33, (174, 635, 374, 670), 1),),
    "경제활동인구/비경제활동인구/경제활동참가율": (Region(36, (160, 257, 387, 293), 1),),
    "금리평가이론": (Region(77, (190, 313, 357, 342), 1),
                  Region(77, (241.5, 385, 287, 409), 1)),
    "단위노동비용": (Region(108, (155, 88, 393, 165), 4),),
    "매출액영업이익률": (Region(135, (123, 556, 424, 591), 3),),
    "생산확산지수": (Region(184, (159, 425, 389, 462), 1),),
    "소비자동향지수(CSI)": (Region(195, (85, 538, 460, 574), 1),),
    "테일러 준칙(Taylor’s Rule)": (
        Region(357, (84, 595, 246.8, 614), 0),
        Region(357, (246.9, 595, 465.1, 614), 0, "variable_definitions"),
        Region(357, (84, 615, 204.1, 635), 0, "variable_definitions"),
    ),
}


def _inside(glyph, rect):
    x0, y0, x1, y1 = glyph["bbox"]
    return rect.contains(pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2))


def definition_glyphs(document, term: Term) -> list[dict]:
    """기존 본문 줄 순서와 동일한 글자에 원본 정의의 문자 인덱스를 연결한다."""
    glyphs, body, active, offset = [], [], False, 0
    finished = False
    for page_number in range(term.source_pdf_page, min(len(document), 423) + 1):
        page = document[page_number - 1]
        raw_lines = {
            tuple(line["bbox"]): [dict(char, font=span["font"], size=span["size"])
                                 for span in line["spans"] for char in span["chars"]]
            for block in page.get_text("rawdict")["blocks"]
            for line in block.get("lines", [])
        }
        for line in iter_content_lines(page):
            text = "".join(span["text"] for span in line["spans"])
            if is_term_title(line):
                if active:
                    finished = True
                    break
                active = text.strip() == term.term
                continue
            if not active:
                continue
            if text.lstrip().startswith("연관검색어"):
                finished = True
                break
            chars = raw_lines[tuple(line["bbox"])]
            if "".join(char["c"] for char in chars) != text:
                raise FormulaRestorationError(f"dict/rawdict 문자 불일치: {term.term}")
            for char in chars:
                glyphs.append(dict(char, index=offset, pdf_page=page_number))
                offset += 1
            body.append(text)
            offset += 1  # 기존 정의의 줄바꿈
        if finished:
            break
    if "\n".join(body).strip("\n") != term.definition:
        raise FormulaRestorationError(f"원본 정의와 좌표 정보 불일치: {term.term}")
    return glyphs


def _text_node(text):
    return {"type": "text", "text": re.sub(r"[ \t]+", " ", text)}


def linear_nodes(glyphs: list[dict], *, map_pi=False) -> dict:
    """왼쪽부터 읽고 작은 글자의 기준선으로 변수의 위/아래 첨자를 연결한다."""
    nodes, pending, last_variable = [], "", None
    for glyph in sorted(glyphs, key=lambda g: (g["bbox"][0], g["index"])):
        text = glyph["c"]
        if text == "\uf0bc" and glyph["font"] == "TeXplusMI" and map_pi:
            text = "π"
        if unicodedata.category(text) == "Co":
            raise FormulaRestorationError("확인되지 않은 전용영역 문자")
        small_script = glyph["size"] <= 8 and text in ("t", "*")
        if small_script and last_variable is not None:
            node, base = last_variable
            gap = glyph["bbox"][0] - base["bbox"][2]
            delta = glyph["origin"][1] - base["origin"][1]
            if -1 <= gap <= 5 and abs(delta) > 0.5:
                key = "subscript" if delta > 0 else "superscript"
                if key in node:
                    raise FormulaRestorationError("중복 첨자")
                node[key] = text
                node["source_indices"].append(glyph["index"])
                continue
        if glyph["font"] == "TeXplusMI" and glyph["size"] >= 8 and text.isalpha():
            if pending:
                nodes.append(_text_node(pending))
                pending = ""
            node = {"type": "variable", "name": text, "source_indices": [glyph["index"]]}
            nodes.append(node)
            last_variable = (node, glyph)
        else:
            pending += text
            if text.strip():
                last_variable = None
    if pending:
        nodes.append(_text_node(pending))
    return {"type": "sequence", "children": nodes}


def fraction_bars(page, region: Region) -> list[tuple]:
    """검토 영역 내부의 가는 수평선을 분수선으로 읽는다."""
    rect = pymupdf.Rect(region.bbox)
    bars = []
    for drawing in page.get_drawings():
        if drawing["width"] is None or drawing["width"] > 0.8:
            continue
        for item in drawing["items"]:
            if item[0] != "l":
                continue
            a, b = item[1:3]
            x0, x1 = sorted((a.x, b.x))
            if abs(a.y - b.y) < 0.2 and x1 - x0 > 5 and rect.contains(a) and rect.contains(b):
                bars.append((x0, a.y, x1))
    bars = sorted(set(bars), key=lambda bar: (round(bar[1], 1), bar[0]))
    if len(bars) != region.fraction_count:
        raise FormulaRestorationError(
            f"PDF {region.pdf_page}: 분수선 {len(bars)}개, 예상 {region.fraction_count}개")
    return bars


def restore_region(page, region: Region, glyphs: list[dict], *, map_pi=False) -> dict:
    """분수선 위/아래의 글자를 명시적인 numerator/denominator 노드로 만든다."""
    selected = [g for g in glyphs if g["pdf_page"] == region.pdf_page
                and _inside(g, pymupdf.Rect(region.bbox))]
    if not selected:
        raise FormulaRestorationError("빈 수식 영역")
    bars = fraction_bars(page, region)
    consumed, fractions = set(), []
    for x0, y, x1 in bars:
        numerator, denominator = [], []
        for glyph in selected:
            gx0, gy0, gx1, gy1 = glyph["bbox"]
            x, delta = (gx0 + gx1) / 2, (gy0 + gy1) / 2 - y
            if x0 <= x <= x1 and 2 < abs(delta) < 17:
                if glyph["index"] in consumed:
                    raise FormulaRestorationError("분수 영역 중복")
                (numerator if delta < 0 else denominator).append(glyph)
                consumed.add(glyph["index"])
        if not any(g["c"].strip() for g in numerator) or not any(g["c"].strip() for g in denominator):
            raise FormulaRestorationError("분자 또는 분모 누락")
        fractions.append((x0, y, {
            "type": "fraction", "numerator": linear_nodes(numerator, map_pi=map_pi),
            "denominator": linear_nodes(denominator, map_pi=map_pi),
            "source_bar": [x0, y, x1, y],
            "source_indices": [g["index"] for g in numerator + denominator],
        }))
    remaining = [g for g in selected if g["index"] not in consumed]
    if not bars:
        ast = linear_nodes(remaining, map_pi=map_pi)
    else:
        rows = []
        for _, y, _ in fractions:
            if not rows or abs(y - rows[-1]) > 3:
                rows.append(y)
        children = []
        for y in rows:
            row_glyphs = [g for g in remaining
                          if min(rows, key=lambda yy: abs(g["origin"][1] - (yy + 3))) == y]
            row_fractions = [(x, node) for x, yy, node in fractions if abs(yy - y) <= 3]
            # 분수선의 x 위치에 분수 노드를 삽입하고 중간의 등호·곱셈 등은 그대로 읽는다.
            start = float("-inf")
            for x, node in sorted(row_fractions):
                chunk = [g for g in row_glyphs if start <= g["bbox"][0] < x]
                children.extend(linear_nodes(chunk, map_pi=map_pi)["children"])
                children.append(node)
                start = x
            children.extend(linear_nodes([g for g in row_glyphs if g["bbox"][0] >= start],
                                         map_pi=map_pi)["children"])
        ast = {"type": "sequence", "children": children}
    source = "".join(g["c"] for g in sorted(selected, key=lambda g: g["index"]))
    return {"role": region.role, "source_pdf_page": region.pdf_page,
            "source_book_page": pdf_to_book_page(region.pdf_page), "source_bbox": list(region.bbox),
            "source_text": source, "source_indices": sorted(g["index"] for g in selected),
            "ast": ast, "text": re.sub(r"[ \t]+", " ", render_text(ast)).strip(),
            "mathml": render_mathml(ast),
            "fraction_count": len(bars)}


def render_text(node: dict) -> str:
    kind = node["type"]
    if kind == "text":
        return node["text"]
    if kind == "variable":
        return (node["name"] + ("_" + node["subscript"] if "subscript" in node else "")
                + ("^" + node["superscript"] if "superscript" in node else ""))
    if kind == "fraction":
        return f"({render_text(node['numerator']).strip()}) / ({render_text(node['denominator']).strip()})"
    if kind == "sequence":
        return "".join(render_text(child) for child in node["children"])
    raise FormulaRestorationError(f"알 수 없는 수식 노드: {kind}")


def render_mathml(node: dict) -> str:
    kind = node["type"]
    if kind == "text":
        return f"<mtext>{escape(node['text'])}</mtext>"
    if kind == "fraction":
        return f"<mfrac>{render_mathml(node['numerator'])}{render_mathml(node['denominator'])}</mfrac>"
    if kind == "sequence":
        return "<mrow>" + "".join(render_mathml(child) for child in node["children"]) + "</mrow>"
    if kind == "variable":
        base = f"<mi>{escape(node['name'])}</mi>"
        sub, sup = node.get("subscript"), node.get("superscript")
        if sub is not None and sup is not None:
            return f"<msubsup>{base}<mi>{escape(sub)}</mi><mo>{escape(sup)}</mo></msubsup>"
        if sub is not None:
            return f"<msub>{base}<mi>{escape(sub)}</mi></msub>"
        if sup is not None:
            return f"<msup>{base}<mo>{escape(sup)}</mo></msup>"
        return base
    raise FormulaRestorationError(f"알 수 없는 수식 노드: {kind}")


def _ast_source_characters(node):
    """표시용 /·괄호·첨자 표기를 제외하고 원본에서 읽은 글자만 센다."""
    if node["type"] == "text":
        return node["text"]
    if node["type"] == "variable":
        return node["name"] + node.get("subscript", "") + node.get("superscript", "")
    if node["type"] == "fraction":
        return _ast_source_characters(node["numerator"]) + _ast_source_characters(node["denominator"])
    return "".join(_ast_source_characters(child) for child in node["children"])


def enrich_formulas(pdf_path: str | Path, terms: list[Term]) -> tuple[list[dict], dict]:
    """원본 정의 사본에 구조화 수식과 복원 설명을 추가한다. 입력 객체는 수정하지 않는다."""
    digest = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    if digest != PDF_SHA256:
        raise FormulaRestorationError("검토한 2026년 PDF 판본과 SHA-256이 다릅니다")
    records, checks = [], []
    with pymupdf.open(pdf_path) as document:
        for term in terms:
            record = term.to_dict()
            record.update(definition_with_formulas=term.definition, formulas=[], formula_annotations=[])
            if term.term not in REGIONS:
                records.append(record)
                continue
            glyphs = definition_glyphs(document, term)
            replacements = []
            used = set()
            for region in REGIONS[term.term]:
                item = restore_region(document[region.pdf_page - 1], region, glyphs,
                                      map_pi=term.term == "테일러 준칙(Taylor’s Rule)")
                indices = set(item["source_indices"])
                if used & indices:
                    raise FormulaRestorationError("수식 교체 영역 중복")
                used |= indices
                source = item["source_text"].replace("\uf0bc", "π")
                char_counter = lambda text: Counter(re.sub(r"\s", "", text))
                if char_counter(source) != char_counter(_ast_source_characters(item["ast"])):
                    raise FormulaRestorationError(f"수식 문자 누락/추가: {term.term}")
                if any(unicodedata.category(c) == "Co" for c in item["text"]):
                    raise FormulaRestorationError("복원 결과에 전용영역 문자 잔존")
                replacements.append(item)
                record["formulas" if region.role == "equation" else "formula_annotations"].append(item)
            insertions = {min(item["source_indices"]): item["text"].strip() for item in replacements}
            pieces = []
            untouched = []
            for index, char in enumerate(term.definition):
                if index in insertions:
                    pieces.append(insertions[index])
                if index not in used:
                    pieces.append(char)
                    untouched.append(char)
            record["definition_with_formulas"] = re.sub(r"\n[ \t]*\n+", "\n", "".join(pieces))
            record["formula_restoration"] = {
                "method": "reviewed_regions_pdf_geometry", "pdf_sha256": digest,
                "original_definition_sha256": hashlib.sha256(term.definition.encode()).hexdigest(),
                "outside_region_characters_sha256": hashlib.sha256("".join(untouched).encode()).hexdigest(),
                "source_glyphs_accounted_for": True,
                "pi_mapping": "TeXplusMI U+F0BC -> π" if "\uf0bc" in term.definition else None,
            }
            checks.append({"term": term.term, "source_pdf_page": term.source_pdf_page,
                           "source_book_page": term.source_book_page,
                           "equation_count": len(record["formulas"]),
                           "fraction_count": sum(item["fraction_count"] for item in replacements),
                           "source_glyphs_accounted_for": True,
                           "original_definition_preserved": record["definition"] == term.definition})
            records.append(record)
    return records, {"method": "reviewed_regions_pdf_geometry", "pdf_sha256": digest,
                     "restored_term_count": len(checks), "terms": checks,
                     "equation_count": sum(item["equation_count"] for item in checks),
                     "fraction_count": sum(item["fraction_count"] for item in checks),
                     "original_fields_preserved": all(
                         all(record[key] == value for key, value in term.to_dict().items())
                         for record, term in zip(records, terms)),
                     "scope": "품질 보고서의 8개 용어만 복원. 다른 수식·표는 변경하지 않음"}


def write_formula_results(output_dir, records: list[dict], report: dict, *, scope="all",
                          pdf_path=DEFAULT_PDF) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = "all_terms" if scope == "all" else "terms"
    for filename, value in [(f"{prefix}_with_formulas.json", records),
                            (f"{prefix}_formula_validation.json", report)]:
        (output_dir / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    evidence_dir = output_dir / f"{prefix}_formula_evidence"
    evidence_dir.mkdir(exist_ok=True)
    cards = []
    with pymupdf.open(pdf_path) as document:
        for index, record in enumerate(records):
            if not record["formulas"]:
                continue
            cards.append(f"<h2>{escape(record['term'])}</h2>")
            for number, item in enumerate(record["formulas"] + record["formula_annotations"]):
                name = f"term_{index:03d}_{number}.png"
                page = document[item["source_pdf_page"] - 1]
                page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=pymupdf.Rect(item["source_bbox"]),
                                alpha=False).save(evidence_dir / name)
                cards.append(
                    f"<p>PDF {item['source_pdf_page']}쪽 / 책 {item['source_book_page']}쪽 · {item['role']}</p>"
                    f"<div class='pair'><figure><figcaption>원본 PDF 영역</figcaption>"
                    f"<img src='{escape(evidence_dir.name)}/{name}' alt='원본 수식'></figure>"
                    f"<figure><figcaption>복원한 구조</figcaption>"
                    f"<math xmlns='http://www.w3.org/1998/Math/MathML' display='block'>{item['mathml']}</math>"
                    f"<pre>{escape(item['text'])}</pre></figure></div>")
    html = ("<!doctype html><html lang='ko'><meta charset='utf-8'><title>수식 복원 비교</title>"
            "<style>body{font-family:sans-serif;margin:32px;line-height:1.6}"
            ".pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}"
            "figure{margin:0;padding:16px;border:1px solid #ccc;overflow:auto}"
            "img{max-width:100%;height:auto}pre{white-space:pre-wrap}math{font-size:1.2rem}"
            "@media(max-width:900px){.pair{grid-template-columns:1fr}}</style>"
            "<h1>수식 원본·복원 결과 비교</h1>"
            "<p>검토한 8개 용어에만 적용. 기존 definition은 원문이며 definition_with_formulas가 복원 설명입니다. "
            "별표는 원본의 첨자·주석 기호를 유지합니다. 원본에 없는 ×100 등을 추가하지 않았습니다.</p>"
            + "".join(cards) + "</html>")
    (output_dir / f"{prefix}_formula_comparison.html").write_text(html, encoding="utf-8")
