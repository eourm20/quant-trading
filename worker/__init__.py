from datetime import datetime, timezone, timedelta

_KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """UTC/로컬 관계없이 항상 KST 현재 시각 반환 (naive — 기존 코드 호환)."""
    return datetime.now(_KST).replace(tzinfo=None)
