# MONOW AI 금융 용어 데이터

FastAPI 앱은 기존 `main.py`에 유지합니다. PDF 데이터 생성 도구는 `scripts` 패키지이며 DB나 OpenAI API에 연결하지 않습니다.

## 디렉터리

```text
monow_ai/
├── main.py
├── requirements-pdf.txt
├── scripts/
│   ├── __init__.py
│   ├── paths.py
│   ├── pdf_terms.py
│   ├── pdf_formulas.py
│   ├── pdf_formula_separation.py
│   ├── pdf_definition_corrections.py
│   ├── pdf_validation.py
│   └── glossary.py
├── tests/
│   ├── __init__.py
│   ├── test_pdf_terms.py
│   ├── test_pdf_all_terms.py
│   ├── test_pdf_formulas.py
│   ├── test_pdf_formula_separation.py
│   └── test_glossary.py
└── data/
    ├── source/
    │   ├── 2026_경제금융용어 800선.pdf
    │   └── review_decisions.json
    ├── output/
    │   ├── glossary.json
    │   └── validation_report.json
    └── debug/
        ├── legacy_results/  # 이전 results 폴더 전체, 내용 그대로 보존
        └── ...              # --debug 결과 및 작업 검증 로그
```

PDF의 기존 Zone.Identifier 파일도 `data/source`로 함께 이동했습니다. 이전에 생성한 JSON·HTML·이미지·품질 보고서는 `data/debug/legacy_results`에서 확인할 수 있습니다. 과거 보고서 본문에 적힌 옛 명령은 당시 기록이며 실행은 아래 새 명령을 사용합니다.

`review_decisions.json`은 기존 상세 품질 보고서의 판정84개와 전체789개 원문 레코드 해시를 옮긴 출처 메타데이터입니다. 일반 실행은 디버그 아카이브에 의존하지 않습니다. 판정을 새로 추정하거나 원문을 교정한 자료가 아닙니다.

## 실행

프로젝트 루트에서 실행합니다.

```bash
.venv/bin/python -m pip install -r requirements-pdf.txt

# 전체789개, 검증된 수식 복원 및 줄바꿈 정제가 기본 적용됨
.venv/bin/python -m scripts.pdf_terms

# 기존 세 용어 선택
.venv/bin/python -m scripts.pdf_terms --scope three

# 개별/복수 용어 선택
.venv/bin/python -m scripts.pdf_terms --terms PER
.venv/bin/python -m scripts.pdf_terms --terms PBR EPS
.venv/bin/python -m scripts.pdf_terms --terms '경제성장률'

# 상세 검증 자료는 요청할 때만 생성
.venv/bin/python -m scripts.pdf_terms --debug

# 테스트 결과 파일을 자동 생성하지 않으며 임시 출력은 자동 정리됨
.venv/bin/python -m unittest discover -s tests -v

# 기존 FastAPI 실행 구조
.venv/bin/uvicorn main:app --reload
```

현재 작업 환경에서는 기존 가상환경의 패키지를 바꾸지 않고 임시 설치를 사용했습니다. 위 PDF 실행·테스트 명령 앞에 `PYTHONPATH=/tmp/monow-pdf-inspect`를 붙일 수 있습니다. 임시 폴더가 없으면 의존성을 설치하세요.

`python scripts/pdf_terms.py` 직접 실행도 지원하며 기본 PDF·출력 경로는 실행 위치와 무관하게 프로젝트 루트를 기준으로 합니다. `--output-dir`와 `--debug-dir`로 경로를 바꿀 수 있습니다. 디버그 폴더는 일반 출력 폴더 밖에 지정해야 합니다.

기존 `--restore-formulas`와 `--clean-linebreaks`는 호환 옵션으로 받아들이지만 해당 처리는 이미 기본 적용합니다. `--scope`와 `--terms`는 동시에 지정하지 않습니다. 선택 추출의 콘솔 출력도 최종 일곱 필드의 JSON입니다.

## 최종 적재용 데이터

`data/output/glossary.json`은 다음 일곱 필드만 포함합니다.

| 필드 | 내용 |
| --- | --- |
| `term` | PDF 본문 제목 원문 |
| `definition` | 원래 수식 위치에 복원 결과를 반영하고 줄바꿈을 정리한 설명 |
| `formulas` | 검증한 수식의 `expression`만 포함 |
| `related_terms` | 연관검색어 배열 |
| `source_pdf_page` | PDF 뷰어 기준1부터 세는 용어 시작 쪽 |
| `source_book_page` | 책 인쇄 시작 쪽 |
| `review_status` | 아래 검증 상태 |

수식이 없는 항목과 이번 분리 대상이 아닌 항목은 `formulas: []`입니다. 기존8개 용어의 복원 수식9개에 일반 산식36개 용어의50개를 추가하여 총44개 용어,59개 수식을 저장합니다. 일반 산식 분리는 기존 `definition`을 변경하지 않습니다. 수식 자체의 페이지는 용어 시작 페이지와 다를 수 있으며 내부 검증 자료에서 유지합니다.

일반 산식은 현재 PDF 판본의 검토된 원문 경계에서 선택하고 문자 좌표·원문 문자·괄호·연산 구조·기준 수식과 대조합니다. 여러 수식을 각각 저장하며, 한 수식이라도 검증에 실패하면 해당 용어의 새 수식 전체를 보류하고 `needs_review`로 기록합니다. 수식 분리 성공만으로 설명 전체의 검증 상태를 승격하지 않습니다.

AST·MathML·문자 인덱스·영역 좌표 등은 최종 `glossary.json`에 들어가지 않습니다. `--debug`를 사용하면 상세 JSON과 비교 화면에서 확인할 수 있습니다.

## 검증 상태

- `verified`: 이전 상세 화면 검토 통과 또는 검증된 수식/설명 복원 통과. 현재83개입니다.
- `automated_checks_passed`: 자동 구조/문자 검사만 통과. 상세 육안 검증 완료를 뜻하지 않습니다. 현재704개입니다.
- `needs_review`: 도형 누락·확정하지 못한 원문 문자 등 검토 사유가 남음. 현재2개입니다.

기존 검토9개 중 표3개, 고정자본소모 주석, 국민처분가능소득 비가시문자·경계 공백, 한계비용 차감기호, IFRS17 비가시문자를 수정하여7개를 검증 완료했습니다. 로렌츠곡선은 그래프 문자 혼입을 정리했으나 도형이 텍스트로 복원되지 않아, 총저축 관련 항목은 비가시문자를 제거했으나 주석 끝 닫는 괄호의 화면 대조가 추가로 필요하여 검토 대상으로 유지합니다.

`pdf_definition_corrections.py`는 기준 원문 레코드 해시와 모든 수정 구간이 일치할 때만 서비스용 설명을 수정합니다. 실패하면 부분 적용하지 않고 검토 상태를 유지합니다. `validation_report.json`의 `service_definition_corrections`에서9개 용어의 수정 전후 구간·페이지·남은 사유를 확인할 수 있습니다. 원문과 수식 배열, 최종 용어의 일곱 필드는 유지합니다.

## 원문 보존과 출력 정책

일반 실행에서는 `data/output`에 `glossary.json`, `validation_report.json` 두 파일만 생성·갱신합니다. 원래 추출된 다섯 필드의 전체 원문은 보고서의 `original_records`에 별도로 보존합니다. 원본 PDF와 이전 생성 파일도 그대로 남습니다.

`validation_report.json`에는 최종 데이터 검사, 원문 레코드 해시 대조, 복원 요약, 상태별 개수와 남은 사유가 있습니다. `raw_pdf_comparison`은 복원 전 원문을 PDF 추출 모드끼리 대조한 검사이므로 그 불일치와 최종 데이터의 검토 상태를 구분합니다.

`--debug`일 때만 `data/debug`에 원문/정제 상세 JSON, 복원 AST·MathML·좌표, HTML, 원본 수식 이미지 등을 생성합니다. 기존 `legacy_results`는 덮어쓰지 않습니다. 기존 파일을 청소하거나 삭제하는 코드는 없습니다.

테스트는 `TemporaryDirectory`를 사용하여 파일 쓰기를 검증합니다. 테스트를 실행하는 것만으로 프로젝트 출력이나 디버그 파일이 누적되지 않습니다. 이번 작업에서 저장한 테스트 로그는 별도의 명시적 실행 기록입니다.

[추출 상세](PDF_EXTRACTION.md) · [수식 복원 상세](FORMULA_RESTORATION.md)
