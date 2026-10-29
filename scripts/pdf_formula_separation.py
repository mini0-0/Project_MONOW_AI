from collections import Counter
import hashlib
from pathlib import Path
import re
import unicodedata

import pymupdf

from scripts.pdf_formulas import PDF_SHA256, definition_glyphs
from scripts.pdf_terms import Term, clean_definition, pdf_to_book_page


# 공백을 제외한 원문에서 찾는 경계. expression 캡처의 글자는 PDF에서 다시 읽는다.
FORMULA_PATTERNS = {
    "가계순저축률": (r"(?P<expression>가계순저축률\(%\)=.+)$",),
    "고용률": (r"(?P<expression>고용률\(%\)=.+?×100)", r"(?P<expression>실업률\(%\)=.+?×100)"),
    "공급탄력성": (r"측정한다\((?P<expression>.+?÷.+?)\)",),
    "국민소득": (r"(?P<expression>국민소득\(NI\)=.+)$",),
    "국외저축": (r"(?P<expression>순대외채무\(=.+?\))",),
    "국외투자율": (
        r"(?P<expression>GNDI=.+?)(?=총투자율\(%\))",
        r"(?P<expression>총투자율\(%\)=.+?)(?=국내총투자율\(%\)=)",
        r"(?P<expression>국내총투자율\(%\)=.+?×100)",
        r"(?P<expression>국외투자율\(%\)=.+?×100)",
    ),
    "기업경기실사지수(BSI)": (r"(?P<expression>BSI=.+)$",),
    "노동생산성/노동생산성지수": (r"(?P<expression>노동생산성지수=.+)$",),
    "듀레이션": (r"사이에는(?P<expression>.+?)(?=의관계가성립한다)",),
    "레버리지비율/단순기본자본비율": (r"은‘(?P<expression>.+?)’으로정의",),
    "레버리지 효과": (r"(?P<expression>투자레버리지\(=.+?\))",),
    "마샬의 k": (r"방정식(?P<expression>MV=PY)에따르면", r"따르면(?P<expression>M=.+?)의관계",
               r"는(?P<expression>MS=Md=kPY)에의거"),
    "배당할인모형": (r"간에는(?P<expression>P0=.+?)의관계가성립",),
    "본원소득": (r"(?P<expression>본원소득=.+)$",),
    "본원통화": (r"화폐발행액\((?P<expression>민간보유현금\+금융기관보유시재금)\)",
               r"‘(?P<expression>통화량=.+?)’"),
    "부채비율": (r"(?P<expression>부채비율=.+)$",),
    "성장기여도": (r"(?P<expression>X1의성장기여도\(%p\)=.+?)(?=X1의성장기여율\(%\)=)",
               r"(?P<expression>X1의성장기여율\(%\)=.+?×100)"),
    "소득교역조건지수": (r"곱하여(?P<expression>\{.+?\})(?=산출하는)",),
    "순안정자금조달비율": (r"산식은‘(?P<expression>.+?)’",),
    "스왑레이트": (r"차이\(=(?P<expression>.+?)\)를연율", r"‘(?P<expression>내외금리차-스왑레이트)’"),
    "시뇨리지": (r"\((?P<expression>화폐시뇨리지=.+?)\)가",),
    "예상손실": (r"실은‘(?P<expression>.+?)’산식",),
    "요소비용 국민소득": (r"(?P<expression>요소비용에의한국민소득=.+?)의관계가성립",),
    "유동비율": (r"(?P<expression>유동비율=.+)$",),
    "유동성커버리지비율": (r"산식은‘(?P<expression>.+?)’",),
    "자기자본비율": (r"(?P<expression>자산\(=.+?\))", r"비중\((?P<expression>자기자본비율=.+?)\)을"),
    "제조업생산능력/가동률지수": (r"(?P<expression>가동률=.+)$",),
    "조세부담률": (r"(?P<expression>조세부담률=.+?×100)", r"(?P<expression>국민부담률=.+?×100)"),
    "총부채상환비율(DTI)": (r"“(?P<expression>총부채상환비율=.+?)”",),
    "총부채원리금상환비율(DSR)": (r"“(?P<expression>총부채원리금상환비율=.+?)”",),
    "통화승수": (r"(?P<expression>M\(통화\)=.+?)(?=,k=)", r"(?P<expression>k=.+?)(?=\(단c는)"),
    "현시비교우위지수(RCA)": (r"“(?P<expression>RCAij=.+?)”",),
    "EC방식": (r"즉(?P<expression>MV=PY)에근거", r"\((?P<expression>V=PY/M)\)",
             r"(?P<expression>dM/M=.+?)\.이방식"),
    "국민부담률": (r"비중\((?P<expression>조세부담률\+사회보장부담률)\)",),
    "신용레버리지": (r"수준으로(?P<expression>민간신용/명목GDP)비율",),
    "한계소비성향": (r"하여(?P<expression>△C/△Y)로나타낸다",),
}


class FormulaSeparationError(ValueError):
    pass


def parse_structure(expression: str) -> dict:
    """음수,사칙연산,등식/첨자 표기를 보존하는 검증용 트리, 계산하지 않는다."""
    opens = {"(": ")", "[": "]", "{": "}", "〔": "〕"}
    closes = set(opens.values())
    operators = {"=": 0, "≒": 0, "≠": 0, "≤": 0, "≥": 0,
                 "+": 1, "-": 1, "−": 1, "×": 2, "/": 2, "÷": 2}
    # 별표는 주석/첨자일 수 있으므로 곱셈 연산자로 해석하거나 ×로 치환하지 않는다.
    tokens = [token.strip() for token in re.split(r"([()\[\]{}〔〕=≒≠≤≥+−×/÷-])", expression) if token.strip()]
    position = 0

    def primary():
        nonlocal position
        if position == len(tokens):
            raise FormulaSeparationError("연산자의 피연산자가 없습니다")
        token = tokens[position]
        position += 1
        if token in ("+", "-", "−"):
            return {"type": "unary", "operator": token, "value": primary()}
        if token in opens:
            inline_definition = position < len(tokens) and tokens[position] == "="
            if inline_definition:
                position += 1
            value = expression_tree(0)
            if position == len(tokens) or tokens[position] != opens[token]:
                raise FormulaSeparationError("괄호가 완결되지 않았거나 서로 다릅니다")
            position += 1
            return {"type": "inline_definition" if inline_definition else "group",
                    "brackets": token + opens[token], "value": value}
        if token in closes or token in operators:
            raise FormulaSeparationError("괄호/연산자 위치가 불확실합니다")
        return {"type": "operand", "value": token}

    def operand():
        parts = [primary()]
        while position < len(tokens) and tokens[position] not in closes and tokens[position] not in operators:
            parts.append(primary())
        if any(p["type"] == "inline_definition" for p in parts) and len(parts) != 2:
            raise FormulaSeparationError("괄호 속 정의와 대상 이름의 연결이 불확실합니다")
        return parts[0] if len(parts) == 1 else {"type": "adjacent", "children": parts}

    def expression_tree(minimum):
        nonlocal position
        left = operand()
        while position < len(tokens) and tokens[position] in operators and operators[tokens[position]] >= minimum:
            operator = tokens[position]
            position += 1
            right = expression_tree(operators[operator] + 1)
            left = {"type": "division" if operator in ("/", "÷") else "binary",
                    "operator": operator,
                    **({"numerator": left, "denominator": right} if operator in ("/", "÷")
                       else {"left": left, "right": right})}
        return left

    if not any(token in operators for token in tokens):
        raise FormulaSeparationError("명시적인 계산 연산자가 없습니다")
    tree = expression_tree(0)
    if position != len(tokens):
        raise FormulaSeparationError("수식 뒤에 해석되지 않은 괄호/문자가 있습니다")
    return tree


def _select_span(definition: str, pattern: str) -> tuple[int, int]:
    positions = [index for index, char in enumerate(definition) if not char.isspace()]
    compact = "".join(definition[index] for index in positions)
    matches = list(re.finditer(pattern, compact))
    if len(matches) != 1:
        raise FormulaSeparationError(f"수식 경계 일치 수 {len(matches)}개: 예상 1개")
    start, end = matches[0].span("expression")
    if start == end:
        raise FormulaSeparationError("빈 수식 구간")
    return positions[start], positions[end - 1] + 1


def read_expression(term: Term, glyphs: list[dict], start: int, end: int) -> dict:
    selected = [g for g in glyphs if start <= g["index"] < end]
    if not selected:
        raise FormulaSeparationError("선택 구간의 PDF 글자가 없습니다")
    source = term.definition[start:end]
    compact = lambda text: re.sub(r"\s", "", text)
    if compact("".join(g["c"] for g in selected)) != compact(source):
        raise FormulaSeparationError("원문 구간과 PDF 글자의 누락/순서 차이")
    scripts, last = {}, None
    for glyph in selected:
        if unicodedata.category(glyph["c"]) in ("Co", "Cf"):
            raise FormulaSeparationError("확인되지 않은 전용/비가시 문자가 있습니다")
        if last and glyph["pdf_page"] == last["pdf_page"]:
            same_line = "\n" not in term.definition[last["index"]:glyph["index"]]
            if same_line and glyph["origin"][0] < last["origin"][0] - 1:
                raise FormulaSeparationError("PDF 가로 글자 순서가 원문 순서와 다릅니다")
            if glyph["size"] < 8 and (glyph["c"].isalnum() or glyph["c"] == "*"):
                gap = glyph["bbox"][0] - last["bbox"][2]
                delta = glyph["origin"][1] - last["origin"][1]
                if not (same_line and last["size"] >= 9 and -1 <= gap <= 4 and abs(delta) > 0.5):
                    raise FormulaSeparationError("첨자와 원래 변수의 대응이 불확실합니다")
                scripts[glyph["index"]] = "_" if delta > 0 else "^"
        if glyph["c"].strip():
            last = glyph
    marked = "".join(scripts.get(index, "") + char for index, char in enumerate(term.definition) if start <= index < end)
    expression = re.sub(r"\s+", " ", clean_definition(marked)).strip()
    # 추가한 첨자 표시 외의 원문 문자는 모두 남아 있어야 한다.
    if Counter(compact(expression)) != Counter(compact(source)) + Counter(scripts.values()):
        raise FormulaSeparationError("수식 정리 과정의 문자 누락/추가")
    structure = parse_structure(expression)
    pages = sorted({g["pdf_page"] for g in selected})
    return {"expression": expression, "source_pdf_page": pages[0],
            "source_book_page": pdf_to_book_page(pages[0]), "structure": structure,
            "source_pages": pages, "source_range": [start, end], "source_text": source,
            "subscript_indices": sorted(index for index, marker in scripts.items() if marker == "_"),
            "superscript_indices": sorted(index for index, marker in scripts.items() if marker == "^"),
            "source_glyphs": [{key: g[key] for key in ("index", "c", "pdf_page", "bbox", "origin", "font", "size")}
                              for g in selected]}


def separate_general_formulas(pdf_path: str | Path, terms: list[Term]) -> tuple[dict, dict]:
    """대상 용어별로 전부 검증한 뒤 반환. 불확실하면 해당 용어의 수식은 전부 보류한다."""
    if hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest() != PDF_SHA256:
        raise FormulaSeparationError("검토된 PDF 판본의 SHA-256과 다릅니다")
    result, outcomes = {}, []
    with pymupdf.open(pdf_path) as document:
        for term in terms:
            if term.term not in FORMULA_PATTERNS:
                continue
            try:
                glyphs = definition_glyphs(document, term)
                spans = [_select_span(term.definition, pattern) for pattern in FORMULA_PATTERNS[term.term]]
                if any(a < d and c < b for index, (a, b) in enumerate(spans) for c, d in spans[index + 1:]):
                    raise FormulaSeparationError("수식 구간이 중복됩니다")
                items = [read_expression(term, glyphs, start, end) for start, end in sorted(spans)]
                keys = [re.sub(r"\s", "", item["expression"]) for item in items]
                if len(keys) != len(set(keys)):
                    raise FormulaSeparationError("같은 수식이 중복 추출됐습니다")
                result[term.term] = items
                outcomes.append({"term": term.term, "status": "verified", "formula_count": len(items),
                                 "expected_formula_count": len(spans), "source_pdf_page": term.source_pdf_page,
                                 "source_book_page": term.source_book_page})
            except ValueError as error:
                result[term.term] = []
                outcomes.append({"term": term.term, "status": "needs_review", "formula_count": 0,
                                 "expected_formula_count": len(FORMULA_PATTERNS[term.term]),
                                 "source_pdf_page": term.source_pdf_page, "source_book_page": term.source_book_page,
                                 "reason": str(error)})
    return result, {"configured_term_count": len(FORMULA_PATTERNS), "target_term_count": len(outcomes),
                    "success_term_count": sum(item["status"] == "verified" for item in outcomes),
                    "failed_term_count": sum(item["status"] == "needs_review" for item in outcomes),
                    "formula_count": sum(len(items) for items in result.values()), "terms": outcomes,
                    "scope": "대상36개 용어의 명시적 산식,성분 관계만 분리. 별도 관계식/예시/보류 대상 용어는 제외"}
