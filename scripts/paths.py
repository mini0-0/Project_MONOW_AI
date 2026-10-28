"""실행 위치와 무관한 프로젝트 경로."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "data" / "source"
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
DEBUG_DIR = PROJECT_ROOT / "data" / "debug"
LEGACY_DIR = DEBUG_DIR / "legacy_results"
DEFAULT_PDF = SOURCE_DIR / "2026_경제금융용어 800선.pdf"
REVIEW_DECISIONS = SOURCE_DIR / "review_decisions.json"
