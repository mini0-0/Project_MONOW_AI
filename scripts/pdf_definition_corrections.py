import hashlib
import json


# 원본 PDF 화면에서 확인한 경계만 수정한다. 범용 문자 삭제/내용 교정은 하지 않는다.
PATCHES = {
    "고정자본소모": [("순생산액 = 총생산액(= 산출액 - 중간투입액) - 고정자본소모 (순소득) (총소득)",
                  "순생산액(순소득) = 총생산액(총소득)(= 산출액 - 중간투입액) - 고정자본소모")],
    "국민처분가능소득(NDI)/국민총처분가능소득(GNDI)": [
        ("** \u200c\x07교포송금", "** 교포송금"),
        ("뺀 금액국민총처분가능소득(GNDI)", "뺀 금액 국민총처분가능소득(GNDI)")],
    "총저축/총저축률/평균소비성향/평균저축성향": [
        ("** \u200c\x07피용자보수", "** 피용자보수")],
    "한계비용": [("10,080원 \xad10,000원", "10,080원 − 10,000원")],
    "IFRS17": [("도입으로 \u200b일관성있는", "도입으로 일관성있는")],
    "로렌츠곡선": [(
        "0′ 100 소득의 완전 균등분배를소나타내는 대각선득 누적점유로렌츠 곡선율 ( % ) T 0 100 인구의 누적비율(%)",
        "[그림 문자: 가로축 인구의 누적비율(%), 0~100; 세로축 소득 누적점유율(%), 0~100; "
        "지점 0, 0′, T; 소득의 완전 균등분배를 나타내는 대각선; 로렌츠 곡선. 도형은 원본 PDF 참조.]"
    )],
    "서킷브레이커": [(
        "발동기준1) 구분 내용 전일 대비 8% 이상 하락 20분간 매매거래중단 1단계 "
        "전일 대비 15% 이상 하락 2단계2) 20분간 매매거래중단 "
        "1단계 서킷브레이커 발동시점 대비 1% 이상 추가 하락 전일 대비 20% 이상 하락 "
        "3단계2) 당일 장종료 조치 2단계 서킷브레이커 발동시점 대비 1% 이상 추가 하락주석: "
        "1) 1분간 지속시 발동 2) 발동기준 동시 충족 필요",
        "[표: 구분 | 발동기준1) | 내용] "
        "1단계 | 전일 대비 8% 이상 하락 | 20분간 매매거래중단; "
        "2단계2) | 전일 대비 15% 이상 하락, 1단계 서킷브레이커 발동시점 대비 1% 이상 추가 하락 | 20분간 매매거래중단; "
        "3단계2) | 전일 대비 20% 이상 하락, 2단계 서킷브레이커 발동시점 대비 1% 이상 추가 하락 | 당일 장종료 조치. "
        "주석: 1) 1분간 지속시 발동 2) 발동기준 동시 충족 필요"
    )],
    "죄수의 딜레마": [(
        "용의자 Ⅱ 구분 자백함자백하지 않음 자백함 (-1, -1) (1, -2) 용의자 Ⅰ 자백하지 않음 (-2, 1) (0, 0)",
        "[표: 행 용의자 Ⅰ, 열 용의자 Ⅱ; 구분 자백함/자백하지 않음] "
        "용의자 Ⅰ 자백함 | 용의자 Ⅱ 자백함: (-1, -1) | 용의자 Ⅱ 자백하지 않음: (1, -2); "
        "용의자 Ⅰ 자백하지 않음 | 용의자 Ⅱ 자백함: (-2, 1) | 용의자 Ⅱ 자백하지 않음: (0, 0)"
    )],
    "BIS 자기자본비율": [(
        "총자본(일반은행에 대한 2025년 규제기준) 바젤Ⅲ 기본자본(Tier 1) 보완자본 (최소비율) "
        "(Tier 2) 보통주자본(CET 1) AT 1 4.5% 6% 8.0% "
        "총자본(국내 금융체계상 중요한 일반은행에 대한 2025년 규제기준) 바젤Ⅲ 기본자본(Tier 1) "
        "(최소비율보완자본보통주자본(CET 1) 기타 +완충자본) (Tier 2) 기본자본최소비율 CCoB CCyB SIB (AT 1) 9.0% 10.5% 12.5%",
        "[표: 바젤Ⅲ(최소비율), 총자본(일반은행에 대한 2025년 규제기준)] "
        "보통주자본(CET 1): 4.5%; 기본자본(Tier 1)=보통주자본(CET 1)+AT 1: 6%; "
        "총자본=기본자본(Tier 1)+보완자본(Tier 2): 8.0%. "
        "[표: 바젤Ⅲ(최소비율+완충자본), 총자본(국내 금융체계상 중요한 일반은행에 대한 2025년 규제기준)] "
        "보통주자본(CET 1)=최소비율+CCoB+CCyB+SIB: 9.0%; "
        "기본자본(Tier 1)=보통주자본(CET 1)+기타 기본자본(AT 1): 10.5%; "
        "총자본=기본자본(Tier 1)+보완자본(Tier 2): 12.5%"
    )],
}

REMAINING = {
    "로렌츠곡선": "그래프 문자 혼입은 정리했으나 곡선·화살표·면적의 벡터 도형은 definition에 복원되지 않음. PDF 128쪽 그림 참조",
    "총저축/총저축률/평균소비성향/평균저축성향": "비가시문자는 제거했으나 원문 추출의 '고정자본소모)' 닫는 괄호가 화면과 불일치할 가능성이 있어 유지. PDF 343쪽 주석 재검토 필요",
}


def correct_definition(raw_record: dict, definition: str, decisions: dict):
    """기준 원문 해시와 모든 수정 구간이 맞을 때만 전체 수정. 실패 시 원문과 사유 반환."""
    title = raw_record["term"]
    if title not in PATCHES:
        return definition, None
    digest = hashlib.sha256(json.dumps(raw_record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    reason = None
    if digest != decisions["original_records_sha256"].get(title):
        reason = "원문 레코드 해시 불일치. 검토된 수정 적용 보류"
    updated, changes = definition, []
    if reason is None:
        for before, after in PATCHES[title]:
            if updated.count(before) != 1:
                reason = "검토된 수정 구간이 없거나 중복됨. 수정 적용 보류"
                break
            updated = updated.replace(before, after, 1)
            changes.append({"before": before, "after": after})
    if reason:
        return definition, {"term": title, "status": "needs_review", "applied": False,
                            "reason": reason, "changes": []}
    return updated, {"term": title, "source_pdf_page": raw_record["source_pdf_page"],
                     "source_book_page": raw_record["source_book_page"],
                     "status": "needs_review" if title in REMAINING else "verified",
                     "applied": True, "reason": REMAINING.get(title), "changes": changes}
