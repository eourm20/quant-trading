"""
신호 쿨다운 관리 (DB 기반 - 워커 재시작해도 유지)
같은 종목 + 같은 조건은 쿨다운 시간 내 재발송 안 함.
장 시작(기본 08:30) 시 모든 쿨다운 초기화.
"""

from datetime import timedelta

from data.db import get_cooldown, set_cooldown
from worker import now_kst


def filter_new_conditions(
    stock_code: str,
    triggered_ids: list[str],
    triggered_conditions: list[str],
    conditions: list[dict],
) -> tuple[list[str], list[str]]:
    """쿨다운이 지난 조건만 반환. 반환값: (new_ids, new_messages)"""
    cooldown_map = {c["id"]: c.get("cooldown_minutes", 60) for c in conditions}
    now = now_kst()
    new_ids, new_msgs = [], []

    for cid, msg in zip(triggered_ids, triggered_conditions):
        cooldown_minutes = cooldown_map.get(cid, 60)
        key = f"{stock_code}:{cid}"
        last = get_cooldown(key)
        if last is None or now - last >= timedelta(minutes=cooldown_minutes):
            new_ids.append(cid)
            new_msgs.append(msg)

    return new_ids, new_msgs


def mark_sent(stock_code: str, triggered_ids: list[str]):
    """발송된 조건의 시간을 DB에 기록"""
    for cid in triggered_ids:
        key = f"{stock_code}:{cid}"
        set_cooldown(key)
