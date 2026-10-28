"""검증된 복원 결과를 적용한 적재용 JSON 생성. DB에 연결하지 않는다."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from scripts.paths import DEFAULT_PDF, REVIEW_DECISIONS
from scripts.pdf_formulas import REGIONS, enrich_formulas, write_formula_results
from scripts.pdf_formula_separation import separate_general_formulas
from scripts.pdf_definition_corrections import correct_definition
from scripts.pdf_terms import Term, clean_definition, extract_terms
from scripts.pdf_validation import validate_extraction, write_results


GLOSSARY_FIELDS = {"term", "definition", "formulas", "related_terms",
                   "source_pdf_page", "source_book_page", "review_status"}
FORMULA_FIELDS = ("expression",)


def _compact(text: str) -> str:
    return re.sub(r"\s", "", text)


def prepare_definition(text: str) -> str:
    """기존 조판 정제를 적용하고 남은 문단/공백을 한 칸으로 연결한다."""
    return re.sub(r"\s+", " ", clean_definition(text)).strip()


def _record_digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _outside_formula_content_preserved(record: dict, definition: str) -> bool:
    parts = record["formulas"] + record["formula_annotations"]
    used = {index for part in parts for index in part["source_indices"]}
    expected = "".join(c for index, c in enumerate(record["definition"]) if index not in used)
    remaining = _compact(definition)
    for part in parts:
        token = _compact(part["text"])
        if token not in remaining:
            return False
        remaining = remaining.replace(token, "", 1)
    return remaining == _compact(expected)


def classify_review(record: dict, raw_record: dict, decisions: dict,
                    candidates: dict) -> tuple[str, list[dict]]:
    """보존된 사람의 판정과 이번 복원/문자 검사를 함께 사용한다."""
    title = record["term"]
    previous = next((entry for entry in decisions["reviews"] if entry["term"] == title), None)
    issues = []
    expected_digest = decisions["original_records_sha256"].get(title)
    if expected_digest != _record_digest(raw_record):
        issues.append({"code": "baseline_record_mismatch", "severity": "error",
                       "detail": "이전 원문 추출 레코드와 다름. 임의 승인하지 않음"})
    restored = title in REGIONS and bool(record["formulas"])
    if previous and previous["status"] != "검증 통과" and not restored:
        issues.append({"code": "unresolved_quality_finding",
                       "severity": "error" if previous["status"] == "추출 오류" else "review",
                       "detail": previous["finding"], "finding_codes": previous["finding_codes"]})
    if not previous and title in candidates:
        issues.append({"code": "unadjudicated_layout_candidate", "severity": "review",
                       "detail": ", ".join(candidates[title]["reasons"])})
    unusual = sorted({f"U+{ord(char):04X}" for char in record["definition_with_formulas"]
                      if unicodedata.category(char) in ("Co", "Cf")
                      or (ord(char) < 32 and char not in "\n\r\t")})
    if unusual:
        issues.append({"code": "unresolved_nonstandard_characters", "severity": "review",
                       "detail": ", ".join(unusual)})
    if issues:
        return "needs_review", issues
    if restored or (previous and previous["status"] == "검증 통과"):
        return "verified", []
    return "automated_checks_passed", []


def build_glossary(pdf_path: str | Path = DEFAULT_PDF,
                   targets: tuple[str, ...] | None = None) -> tuple[list[dict], dict, dict]:
    """추출→선택적 복원→최종 필드 축약→내용 보존/품질 검증을 수행한다."""
    terms = extract_terms(pdf_path, targets=targets)
    enriched, formula_report = enrich_formulas(pdf_path, terms)
    separated, separation_report = separate_general_formulas(pdf_path, terms)
    separation_outcomes = {item["term"]: item for item in separation_report["terms"]}
    decisions = json.loads(REVIEW_DECISIONS.read_text(encoding="utf-8"))
    if decisions["pdf_sha256"] != formula_report["pdf_sha256"]:
        raise ValueError("검토 판정이 현재 PDF 판본과 맞지 않습니다")
    scope = "all" if targets is None else "selected"
    raw_validation = validate_extraction(pdf_path, terms, scope=scope)
    candidates = {item["term"]: item for item in raw_validation["manual_review"]}
    glossary, review_items, corrections = [], [], []
    preserved, outside_preserved = [], []
    for term, record in zip(terms, enriched):
        definition = prepare_definition(record["definition_with_formulas"])
        preserved.append(_compact(definition) == _compact(record["definition_with_formulas"]))
        outside_preserved.append(_outside_formula_content_preserved(record, definition))
        status, issues = classify_review(record, term.to_dict(), decisions, candidates)
        definition, correction = correct_definition(term.to_dict(), definition, decisions)
        if correction:
            corrections.append(correction)
            if correction["applied"]:
                issues = [issue for issue in issues if issue["code"] not in
                          ("unresolved_quality_finding", "unresolved_nonstandard_characters")]
            if correction["status"] == "needs_review":
                issues.append({"code": "definition_correction_review", "severity": "review",
                               "detail": correction["reason"]})
            status = "needs_review" if issues else "verified"
        outcome = separation_outcomes.get(term.term)
        if outcome and outcome["status"] == "needs_review":
            status = "needs_review"
            issues.append({"code": "formula_separation_uncertain", "severity": "review",
                           "detail": outcome["reason"]})
        formulas = [{"expression": item["text"]} for item in record["formulas"]]
        formulas.extend({key: item[key] for key in FORMULA_FIELDS} for item in separated.get(term.term, []))
        glossary.append({
            "term": term.term, "definition": definition,
            "formulas": formulas,
            "related_terms": list(term.related_terms),
            "source_pdf_page": term.source_pdf_page, "source_book_page": term.source_book_page,
            "review_status": status,
        })
        if issues:
            review_items.append({"term": term.term, "source_pdf_page": term.source_pdf_page,
                                 "source_book_page": term.source_book_page, "issues": issues})
    baseline_matches = all(_record_digest(term.to_dict()) == decisions["original_records_sha256"].get(term.term)
                           for term in terms)
    checks = {
        "record_count_matches_toc": raw_validation["checks"]["toc_count_matches_extraction"],
        "toc_titles_and_pages_match": raw_validation["checks"]["toc_title_and_page_match"],
        "no_duplicate_terms": raw_validation["checks"]["no_duplicate_terms"],
        "no_empty_definitions": all(item["definition"] for item in glossary),
        "page_mapping": raw_validation["checks"]["page_mapping"],
        "original_records_match_baseline": baseline_matches,
        "original_fields_preserved_in_restoration": formula_report["original_fields_preserved"],
        "normalized_characters_preserved": all(preserved),
        "outside_formula_characters_preserved": all(outside_preserved),
        "only_service_fields_exported": all(set(item) == GLOSSARY_FIELDS for item in glossary),
        "general_formula_verification_complete": separation_report["failed_term_count"] == 0,
        "general_formula_count_matches_expected": all(
            item["formula_count"] == item["expected_formula_count"] for item in separation_report["terms"]),
        "no_duplicate_formulas": all(
            len(item["formulas"]) == len({_compact(f["expression"]) for f in item["formulas"]}) for item in glossary),
        "reviewed_definition_corrections_applied": all(item["applied"] for item in corrections),
    }
    if not checks["normalized_characters_preserved"] or not checks["outside_formula_characters_preserved"]:
        raise ValueError("복원 또는 정제 중 수식/본문 문자가 누락됐습니다. 출력하지 않습니다")
    # 원문은 최종 서비스용 정의와 별개의 보고서에 항상 보존한다.
    report = {
        "summary": {"record_count": len(glossary), "toc_count": raw_validation["summary"]["toc_count"],
                    "restored_term_count": formula_report["restored_term_count"],
                    "equation_count": formula_report["equation_count"],
                    "fraction_count": formula_report["fraction_count"],
                    "general_formula_success_count": separation_report["success_term_count"],
                    "general_formula_failed_count": separation_report["failed_term_count"],
                    "separated_formula_count": separation_report["formula_count"],
                    "total_formula_count": sum(len(item["formulas"]) for item in glossary),
                    "formula_term_count": sum(bool(item["formulas"]) for item in glossary),
                    "review_status_counts": dict(Counter(item["review_status"] for item in glossary)),
                    "unresolved_error_term_count": sum(any(i["severity"] == "error" for i in item["issues"])
                                                       for item in review_items),
                    "failed_checks": [name for name, passed in checks.items() if not passed]},
        "source": {"pdf_filename": Path(pdf_path).name, "pdf_sha256": formula_report["pdf_sha256"],
                   "scope": scope, "targets": list(targets) if targets is not None else None,
                   "review_decisions_sha256": hashlib.sha256(REVIEW_DECISIONS.read_bytes()).hexdigest()},
        "review_status_definitions": {
            "verified": "이전 원본 화면 검토 통과 또는 검증된 수식/설명 복원 통과",
            "automated_checks_passed": "자동 구조·문자 검사만 통과. 상세 육안 검토를 마쳤다는 뜻이 아님",
            "needs_review": "표/수식/문자/기존 판정 문제 등이 남아 있어 검토 필요",
        },
        "checks": checks, "review_required": review_items,
        "original_records": [term.to_dict() for term in terms],
        "formula_restoration_summary": formula_report,
        "general_formula_separation": separation_report,
        "service_definition_corrections": corrections,
        "raw_pdf_comparison": {
            "description": "복원 전 원문의 PDF 모드 간 대조. 형식 차이/복원 전 손실이 남아 있으며 최종 데이터 판정과 구분함",
            "checks": raw_validation["checks"],
            "reference_text_differences": raw_validation["reference_text_differences"],
            "presentation_differences": raw_validation["presentation_differences"],
        },
        "limitations": ["기존 8개 용어의 수식 복원과 검토된 일반 산식36개에 한정함",
                        "문장 설명만 있는 항목에 원문에 없는 등식을 생성하지 않음",
                        "원본 화면 대조로 확인한 9개 설명의 지정 구간만 수정. 그래프 도형 및 총저축 주석 괄호는 추가 검토 필요",
                        "자동 검사 통과 항목은 상세 육안 검증 완료로 간주하지 않음"],
    }
    debug_data = {"terms": terms, "enriched": enriched, "raw_validation": raw_validation,
                  "formula_report": formula_report, "general_formulas": separated,
                  "scope": "all" if targets is None else "three"}
    return glossary, report, debug_data


def write_outputs(output_dir, glossary: list[dict], report: dict, *, debug_data=None,
                  debug_dir=None, pdf_path=DEFAULT_PDF) -> None:
    """일반 출력은 두 JSON만 저장한다. 상세 자료는 명시적인 디버그 요청에서만 저장."""
    output_dir = Path(output_dir).resolve()
    if debug_data is not None:
        if debug_dir is None:
            raise ValueError("디버그 저장 경로가 필요합니다")
        debug_dir = Path(debug_dir).resolve()
        if debug_dir == output_dir or output_dir in debug_dir.parents:
            raise ValueError("디버그 경로는 일반 출력 디렉터리 밖이어야 합니다")
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in [("glossary.json", glossary), ("validation_report.json", report)]:
        (output_dir / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    if debug_data is not None:
        write_results(debug_dir, debug_data["terms"], debug_data["raw_validation"], scope=debug_data["scope"])
        write_formula_results(debug_dir, debug_data["enriched"], debug_data["formula_report"],
                              scope=debug_data["scope"], pdf_path=pdf_path)
        (debug_dir / "general_formula_extraction.json").write_text(
            json.dumps(debug_data["general_formulas"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
