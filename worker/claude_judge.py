"""
AI 매매 판단 — Anthropic Claude 또는 OpenAI GPT 사용
ANTHROPIC_API_KEY가 있으면 Claude, 없으면 OpenAI로 자동 전환
"""

import logging
import os
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

# Agent 모드 실행 시 마지막 trace 저장 (main.py에서 DB 기록에 활용)
_last_agent_trace: dict = {}


def get_last_agent_trace() -> dict:
    """마지막 JudgmentAgent 실행의 tool_sequence + reasoning_chain 반환.
    Agent 모드가 아니었거나 실패한 경우 빈 dict 반환."""
    return dict(_last_agent_trace)

_TRADING_KNOWLEDGE = """## 트레이딩 분석 지식 (기술적 분석 프레임워크)

### 1. 캔들 분석
[캔들 구조] 양봉=매수세 우위, 음봉=매도세 우위. 긴 아랫꼬리=매도 후 매수 반등. 긴 윗꼬리=매수 후 매도 압력.
[주요 캔들 패턴]
- 망치형: 하락세 후 긴 아랫꼬리 양봉 → 매수세 유입, 반등 신호
- 역망치형: 상승세 후 긴 윗꼬리 음봉 → 매도세 강화, 조정 신호
- 도지: 시가≈종가, 매수/매도 균형 → 추세 전환 초기 신호
[캔들 조합]
- 불리시 엔걸핑: 음봉 후 그것을 감싸는 양봉 → 강한 상승 전환
- 베어리시 엔걸핑: 양봉 후 그것을 감싸는 음봉 → 강한 하락 전환
- 하락삼법: 대음봉 + 작은 양봉 3개 + 대음봉 → 하락 추세 지속

### 2. 거래량 분석
[거래량 + 추세 조합 해석]
가격 상승 + 거래량 증가 = 강한 매수세, 추가 상승 가능 (추세 강화)
가격 상승 + 거래량 감소 = 매수세 약화, 상승 신뢰도 낮음 (추세 약화)
가격 하락 + 거래량 증가 = 강한 매도세, 추가 하락 가능 (추세 강화)
가격 하락 + 거래량 감소 = 매도압력 약화, 반등 가능성 (추세 약화)
[볼륨 스프레드 심화 — 몸통×거래량 4가지 조합]
큰 몸통 + 높은 거래량 = 강한 추세 형성 (양봉=매수세, 음봉=매도세 강력)
작은 몸통 + 높은 거래량 = 매수/매도 갈등 심화, 추세 전환 가능성 높음
큰 몸통 + 낮은 거래량 = 속임수 움직임 가능, 추세 신뢰도 낮음
작은 몸통 + 낮은 거래량 = 시장 관망세, 방향 미정
[실전 활용]
박스권 돌파/지지저항선에서 거래량 급증 = 새로운 추세 시작 가능성 높음
높은 거래량 + 작은 몸통 출현 = 기존 추세의 끝 가능, 반대 방향 진입 고려
[OBV] 상승=매수세 우위, 하락=매도세 우위. OBV 방향과 가격 방향 불일치=다이버전스

### 3. 추세 분석
상승 추세: 고점·저점 점진 상승 → 저점 근처에서 매수, 고점 돌파 실패 시 조정 대비
하락 추세: 고점·저점 점진 하락 → 고점 근처에서 매도, 저점 돌파 실패 시 반등
횡보: 상단 저항 매도, 하단 지지 매수. 돌파 확인 후 방향성 트레이딩
[추세선과 채널]
상승 추세선: 주요 저점 연결 → 지지선 역할. 이탈 시 추세 전환 주의
하락 추세선: 주요 고점 연결 → 저항선 역할. 돌파 시 추세 전환 가능
채널: 추세선과 평행선으로 구성. 채널 하단=매수, 상단=매도. 채널 돌파 시 강한 추세 형성
[추세 전환 신호 포착]
거래량 증가가 기존 추세 반대 방향 동반 시 주의
다이버전스(RSI/MACD) + 가격 괴리 = 전환 신호
추세선 돌파 후 리테스트 성공 + 거래량 동반 = 신뢰도 높은 전환 확인
[추세 전환 복합 확인 — 2가지 이상 동시 = 강한 전환]
① RSI 과매도(≤40) ② 거래량 급증(2배+)+반등 ③ 양봉 전환 ④ MA20 지지 확인
⑤ 다이버전스 발생 ⑥ 스토캐스틱 골든크로스

### 4. 지지와 저항
[설정] 과거 고점=저항, 과거 저점=지지. 거래량 집중 가격대=강한 지지/저항
[역할 전환] 저항 돌파 후 되돌림 성공 → 지지로 전환 (매수기회). 지지 이탈 후 반등 실패 → 저항으로 전환 (매도기회)
[돌파] 강한 거래량 동반 돌파 = 추가 움직임 기대. 거래량 없는 돌파 = 속임수 가능
[되돌림] 돌파 후 되돌림 구간에서 새 지지/저항 확인 후 재진입. 피보나치 61.8% 되돌림에서 매수세 강화 = 유력 재진입점
[균형 구간] 일정 기간 거래량 집중 가격대 = 시장 균형. 이탈 시 추세 방향으로 포지션 진입

### 5. 이동평균선(MA)
[SMA/EMA/WMA 차이]
SMA(단순): 모든 데이터 동일 가중치, 가장 느림, 장기 추세 확인
EMA(지수): 최근 데이터에 기하급수적 가중치, 빠른 반응, 중단기 추세
WMA(가중): 최근 데이터에 선형 가중치, EMA보다 민감
HMA(헐): WMA 기반 추가 계산, 가장 빠르고 부드러운 반응
본 시스템은 SMA5/SMA20 사용
[포지션 해석]
현재가 > MA5 > MA20: 강한 상승 (매수 유리)
MA5 > MA20 + 현재가 < MA5: 단기 눌림목, 지지 확인 후 entry
현재가 < MA5 < MA20: 강한 하락 (매수 신중)
MA 돌파 후 되돌림 테스트 성공 = 지지 전환 확인
골든크로스: 단기MA가 장기MA 상향돌파 → 상승 추세 시작 신호
데드크로스: 단기MA가 장기MA 하향돌파 → 하락 추세 시작 신호

### 6. RSI
RSI ≤ 30: 극과매도, 강한 반등 가능 (추세 확인 필수)
RSI 31~43: 과매도, entry 신뢰도 높음
RSI 44~55: 중립
RSI 56~65: 과매수 접근, exit 검토
RSI ≥ 66: 과매수, 상승 에너지 소진
[RSI 다이버전스]
강세: 가격 신저점 + RSI 저점 상승 → 하락 추세 약화, 반등 임박
약세: 가격 신고점 + RSI 고점 하락 → 상승 추세 약화, 조정 임박
강한 다이버전스: 가격과 지표의 괴리가 크고 여러 봉에 걸쳐 형성 → 신뢰도 높음
약한 다이버전스: 짧은 기간·작은 괴리 → 보조 확인 필요
다이버전스만으로 매매 결정 금지 — 거래량·지지저항과 함께 검토. 긴 시간프레임이 더 강한 신호

### 7. 스토캐스틱
%K: 단기 가격 움직임 반영, %D: %K의 이동평균
%K가 %D 아래→위 교차: 상승 신호 (과매도에서 더 강력)
%K가 %D 위→아래 교차: 하락 신호 (과매수에서 더 강력)
과매수(≥80): 상승 약화 가능, exit 검토
과매도(≤20): 하락 약화 가능, entry 검토
주의: 단독 사용 금지, 다른 지표와 교차 확인 필수

### 8. CCI
CCI < -100에서 위로 반등: 과매도 탈출, 매수 신호
CCI > +100에서 아래로 하락: 과매수 탈출, 매도 신호
CCI 다이버전스: RSI 다이버전스와 동일 논리

### 9. 일목균형표
구름대(선행스팬A/B 사이): 두꺼울수록 지지/저항 강함, 얇으면 돌파 가능성 높음
가격 > 구름대 위: 상승 추세. 가격 < 구름대 아래: 하락 추세. 구름대 내부: 중립/혼조
구름대 돌파 = 추세 전환 신호. 돌파 시 거래량 동반 여부로 신뢰도 판단
전환선(9일)이 기준선(26일) 상향 돌파: 매수 신호
전환선이 기준선 하향 돌파: 매도 신호
기준선 = 주요 지지/저항 역할. 가격이 기준선 위=상승세, 아래=하락세
전환선 = 단기 가격 움직임 반영. 기준선과의 교차가 핵심 신호

### 10. 차트 패턴
[반전 패턴]
- 이중 바닥: 유사한 두 저점 + 넥라인 돌파 → 강한 상승 전환. 돌파 후 되돌림에 매수
- 이중 천장: 유사한 두 고점 + 넥라인 이탈 → 강한 하락 전환. 이탈 시 매도
- 헤드 앤 숄더: 좌숄더-머리(최고)-우숄더 + 넥라인 이탈 → 하락 전환
- 역 헤드 앤 숄더: 좌숄더-머리(최저)-우숄더 + 넥라인 돌파 → 상승 전환
[지속 패턴]
- 상승 삼각형: 수평 저항 + 상승 지지 → 저항 돌파 시 강한 상승
- 하락 삼각형: 수평 지지 + 하락 저항 → 지지 이탈 시 강한 하락
- 대칭 삼각형: 고점·저점 수렴 → 돌파 방향으로 강한 추세 형성
- 깃발형: 강한 추세 후 잠시 쉬어가는 패턴 → 돌파 방향으로 추세 연장
- 쐐기형: 수렴하며 상승/하락 → 돌파 방향으로 강한 추세 형성
- 페넌트: 깃발형과 유사하나 더 빠른 움직임 동반
- 컵 위드 핸들: U자형 상승 후 핸들 조정 → 핸들 돌파 시 강한 상승
- 박스권: 일정 범위 횡보 → 돌파 시 새로운 추세 시작
[패턴 + 거래량 조합]
- 돌파 시 거래량 급증 동반 = 신뢰도 높음
- 돌파 시 거래량 미미 = 속임수 가능, 확인 필요

### 11. 피보나치 되돌림/확장
비율: 23.6% / 38.2% / 50% / 61.8% / 78.6%
상승 후 조정 시 38.2~61.8% = 정상 되돌림, 매수 구간
78.6% 초과 = 추세 전환 가능, 손절 검토
확장 비율: 127.2%, 161.8%, 200% — 목표가 설정에 활용
목표가: 직전 상승폭 × 161.8% = 1차 목표, × 200% = 2차 목표
피보나치 지지선 + MA 지지 + 거래량 증가 동시 = 높은 신뢰도 매수 포인트
손익비 고려하여 1차/2차 목표가를 분할 익절에 활용

### 12. 포지션 운용 전략
[매매 기간별 방법론 — horizon 참조]
단기(1~2주): 1~2주 내 수익 실현 목표. 단기 모멘텀·거래량·RSI 위주 판단. 목표가 +5~10%, 손절 -3~5%로 타이트하게. 5분봉 RSI 포함 단기 지표 중시.
중기(1~3개월): 1~3개월 내 수익 실현 목표. 일봉 기술적 분석+추세 추종. 목표가 +10~20%, 손절 -5~8%. 눌림목·지지선 위주 진입.
장기(3개월 이상): 3개월 이상 보유 목표. 펀더멘털+중장기 추세 중시. 목표가 +20% 이상, 손절 -8~10%로 여유있게. 단기 변동성에 흔들리지 않음.
[자금 배분] 현금 20~50% 유지. 나머지를 매매 기간별 비중 배분

### 13. 손익비(R/R)와 포지션 사이징
R/R 2:1 이상: 매수 적극 검토 (목표가 수익폭 ≥ 손절 손실폭 × 2)
R/R 1~2:1: 다른 조건(거래량·추세·섹터) 동시 충족 시만
R/R 1:1 미만: 홀드 권고
개별 거래 최대 손실 = 계좌 2~5%. 수량 = (계좌×0.03) ÷ (현재가-손절가)
한 종목 비중 15% 초과 시 신규 매수 신중
손절가 설정 시 시장 변동성 고려하여 여유 둘 것 (예: 피보나치 1.13 구간)

### 14. 리스크 관리
손절가 진입 전 확정 — 감정적 변경 금지
목표가 도달 시 익절 실행 — 탐욕으로 연장 금지
연속 손실 시 포지션 축소 + 시장 재평가 (뇌동매매 금지)
변동성 높은 구간(급락·급등 직후)은 포지션 축소 + 리스크 허용범위 1~3%로 축소
자동 손절(Limit Stop-Loss) 활용으로 감정적 판단 배제

### 15. 멘탈 관리 원칙
[감정적 매매의 함정]
두려움(Fear)·탐욕(Greed)·후회(Regret)에 기반한 비합리적 거래 = 감정적 매매
시장 급등 시 "놓치기 싫다"며 무리 진입 / 급락 시 공포에 휩싸여 손절 = 대표적 함정
연속 손실 → 자신감 저하 → 복구 매매(뇌동매매) = 더 큰 손실의 악순환
[승리의 함정] 연속 수익 → 자신감 과잉 → 경솔한 거래 = 위험. "시장을 지배할 수 있다"는 착각 금지
[패배의 교훈] 실패 인정 → 약점 발견 → 개선 기회. 같은 실수 반복 방지를 위해 기록
[대처법]
매매 계획 철저 준수, 임의 수정 금지 (특히 스탑로스 강제 변경)
모든 거래를 독립 사건으로 취급 — 이전 결과에 감정적 연결 금지

### 16. 시장 이해
[시장 참여자] 개인투자자(소액, 감정적 경향) / 기관투자자(대규모, 체계적) / 마켓메이커(유동성 제공)
[뉴스/이벤트 영향]
금리 결정·규제 변화 등 = 시장 큰 영향. 예정 이벤트(FOMC 등)는 사전 준비 가능
예기치 못한 뉴스 = 즉각 변동성. 대처: 이벤트 전후 포지션 축소, 매매 회피 고려
뉴스 캘린더로 경제 일정 사전 확인

### 17. 공시 분석 (DART)
[공시 유형별 영향]
수주/계약 체결: 매출 성장 기대 → 주가 상승 요인 (규모·마진 확인)
실적 발표: 매출·영업이익 증감 → 컨센서스 대비 서프라이즈/쇼크 판단
유상증자/CB/BW: 주식 희석 → 단기 악재, 자금 용도 확인 필요
대규모 내부자 매매: 내부자 매수=긍정, 매도=경계
거래소 공시(단기): 관리종목 지정, 상장폐지 심사 등 = 강한 리스크
[공시 + 기술적 분석 조합]
호재 공시 + RSI 과매도 + 거래량 급증 = 강한 entry 신호
악재 공시 + MA 하향 이탈 = 즉시 exit 검토
공시 없이 급등 = 루머 가능, 신뢰도 낮음

### 18. 판단 원칙
수치 기반 판단만 허용: RSI·스토캐스틱·CCI·R/R·거래량 비율·MA 위치·일목균형표로 판단
"시장이 내 예상과 달라서"는 매매 근거가 될 수 없음
과도한 지표 혼란 방지: 핵심 2~3개 지표가 동일 방향이면 신뢰도 높음
트레이딩은 기법보다 심법 — 스스로를 이해하고 통제하는 능력이 핵심

### 19. 포트폴리오 관리 원칙 (분산투자)
[현금 비중 관리]
- 총 포트폴리오 대비 현금 30% 이상: 신규 진입 적극 검토 가능
- 현금 15~30%: 정상 운용. 목표 비중 내 진입
- 현금 5~15%: 현금 부족. 신규 매수 규모 절반 이하로 제한. 기존 수익 종목 분할 익절 후 재원 마련 고려
- 현금 5% 미만: 매수 보류 원칙. 기존 수익 종목 익절 또는 물타기 대응만
[분산 원칙]
- 단일 종목 비중 20% 초과: 추가 매수 금지
- 보유 종목 5개 이상: 신규 진입 시 가장 성과 나쁜 종목 정리 후 재편 고려
- 동일 섹터 쏠림 경고: 같은 섹터 종목이 2개 이상이면 신규 진입 신중
[손익률 관리]
- 포트 전체 수익률 +5% 이상: 일부 익절로 수익 확정 후 현금 비중 확보
- 포트 전체 수익률 -5% 이하: 리스크 축소 모드 — 신규 매수 최소화, 손절 원칙 철저 준수
- 포트 전체 수익률 -10% 이하: 매수 중단, 손실 제한 최우선
- 수익 종목이 포트의 절반 미만: 전략 재점검 신호 — 신규 진입보다 기존 관리 집중
[물타기 우선순위]
- 보유 종목 중 물타기 조건 근접 종목이 있으면 신규 진입보다 해당 종목 현금 배정 우선
- 신규 entry와 물타기가 동시에 발생하면 보유 종목 물타기 우선"""

_TRADING_KNOWLEDGE_COMPACT = """## 트레이딩 핵심 규칙 (압축)
- 신호는 가격/거래량/추세 일치 여부를 우선 확인한다.
- RSI: 30 이하는 과매도, 70 이상은 과매수 경계로 본다.
- MA: 현재가가 MA5, MA20 위에 있으면 상승 우위, 아래면 하락 우위로 본다.
- 거래량 급증이 동반된 돌파만 신뢰하고, 거래량 없는 돌파는 보수적으로 본다.
- 손익비(R/R) 2:1 미만이면 신규 진입을 보수적으로 판단한다.
- 포트폴리오 현금 비중이 낮으면 추천 수량을 축소한다.
- 동일 섹터/종목 쏠림이 크면 분산 관점에서 진입 강도를 낮춘다.
- 악재 공시/부정 뉴스가 있으면 기술 신호보다 리스크를 우선한다.
- 불확실하면 홀드하고 전환 조건을 명시한다.
"""

_USE_COMPACT_PROMPT = os.getenv("AI_PROMPT_COMPACT", "true").lower() == "true"
_TRADING_KNOWLEDGE_ACTIVE = _TRADING_KNOWLEDGE_COMPACT if _USE_COMPACT_PROMPT else _TRADING_KNOWLEDGE

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if _ANTHROPIC_KEY:
    from anthropic import Anthropic
    _client = Anthropic(api_key=_ANTHROPIC_KEY)
    MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    MODEL_MINI = os.getenv("CLAUDE_MODEL_MINI", "claude-haiku-4-5-20251001")
    _BACKEND = "anthropic"
elif _OPENAI_KEY:
    from openai import OpenAI
    _client = OpenAI(api_key=_OPENAI_KEY)
    MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
    MODEL_MINI = os.getenv("OPENAI_MODEL_MINI", "gpt-4.1-mini")
    _BACKEND = "openai"
else:
    raise RuntimeError("ANTHROPIC_API_KEY 또는 OPENAI_API_KEY 중 하나를 .env에 설정하세요.")


def _p(value) -> int:
    if not value:
        return 0
    try:
        return abs(int(float(str(value).replace(",", "").strip() or "0")))
    except (ValueError, TypeError):
        return 0


def _f(value) -> float:
    if not value:
        return 0.0
    try:
        return float(str(value).replace(",", "").strip() or "0")
    except (ValueError, TypeError):
        return 0.0


def _fmt_index(d: dict, name: str) -> str:
    price = _p(d.get("cur_prc") or d.get("prpr"))
    rate = d.get("flu_rt") or d.get("prdy_ctrt") or "N/A"
    sign = "▲" if str(rate).startswith("-") is False and rate != "N/A" else "▼"
    if price:
        return f"{name} {price:,}pt ({sign}{rate}%)"
    return f"{name} 조회 실패"


def _fmt_chart(signal) -> str:
    c = signal.chart
    if not c:
        return "차트 데이터 없음"

    # ── 10일 흐름 화살표 ──
    trend_arrows = ""
    if c.recent_10d_prices and len(c.recent_10d_prices) >= 2:
        arrows = []
        for i in range(1, len(c.recent_10d_prices)):
            arrows.append("▲" if c.recent_10d_prices[i] > c.recent_10d_prices[i - 1] else "▼")
        arrows.append("▲" if signal.current_price > c.recent_10d_prices[-1] else "▼")
        prices_str = " → ".join(f"{p:,}" for p in c.recent_10d_prices)
        trend_arrows = f"{' '.join(arrows)}  ({prices_str} → {signal.current_price:,})"

    # ── 연속 하락일 ──
    consecutive_down = 0
    prices_with_cur = (list(c.recent_10d_prices) + [signal.current_price]) if c.recent_10d_prices else []
    if len(prices_with_cur) >= 2:
        for i in range(len(prices_with_cur) - 1, 0, -1):
            if prices_with_cur[i] < prices_with_cur[i - 1]:
                consecutive_down += 1
            else:
                break

    # ── 볼륨 스프레드 심화 (몸통×거래량 조합) ──
    vol_spread = ""
    if c.volume_spread:
        vol_spread = f"  - 볼륨 스프레드: {c.volume_spread}"

    # ── RSI 레벨 해석 ──
    rsi_val = getattr(signal, "rsi", None)
    rsi_interp = ""
    if rsi_val:
        try:
            r = float(rsi_val)
            if r <= 30: rsi_interp = "극과매도"
            elif r <= 43: rsi_interp = "과매도"
            elif r >= 66: rsi_interp = "극과매수"
            elif r >= 56: rsi_interp = "과매수"
            else: rsi_interp = "중립"
        except (ValueError, TypeError):
            pass

    # ── 스토캐스틱 해석 ──
    stoch_text = ""
    if c.stochastic_k is not None:
        stoch_level = ""
        if c.stochastic_k >= 80: stoch_level = "과매수"
        elif c.stochastic_k <= 20: stoch_level = "과매도"
        cross = ""
        if c.stochastic_golden_cross: cross = " ★골든크로스"
        elif c.stochastic_death_cross: cross = " ★데드크로스"
        d_str = f"/ %D {c.stochastic_d:.0f}" if c.stochastic_d is not None else ""
        stoch_text = f"  - 스토캐스틱: %K {c.stochastic_k:.0f} {d_str} {stoch_level}{cross}"

    # ── CCI 해석 ──
    cci_text = ""
    if c.cci is not None:
        cci_level = ""
        if c.cci > 100: cci_level = "(과매수 → exit 검토)"
        elif c.cci < -100: cci_level = "(과매도 → entry 검토)"
        cci_text = f"  - CCI: {c.cci:.0f} {cci_level}"

    # ── 일목균형표 해석 ──
    ichimoku_text = ""
    parts = []
    if c.ichimoku_tenkan is not None and c.ichimoku_kijun is not None:
        parts.append(f"전환선 {c.ichimoku_tenkan:,} / 기준선 {c.ichimoku_kijun:,}")
        if c.ichimoku_tenkan_cross == "golden":
            parts.append("★전환선 골든크로스")
        elif c.ichimoku_tenkan_cross == "dead":
            parts.append("★전환선 데드크로스")
    if c.ichimoku_above_cloud is True:
        parts.append("구름대 위 (상승)")
    elif c.ichimoku_above_cloud is False:
        parts.append("구름대 아래 (하락)")
    elif c.ichimoku_senkou_a is not None:
        parts.append("구름대 내부 (중립)")
    if c.ichimoku_cloud_thickness is not None:
        thickness_label = "두꺼움(강한 지지/저항)" if c.ichimoku_cloud_thickness > 3 else "얇음(돌파 가능)" if c.ichimoku_cloud_thickness < 1 else "보통"
        parts.append(f"구름대 두께 {c.ichimoku_cloud_thickness:.1f}% [{thickness_label}]")
    if parts:
        ichimoku_text = f"  - 일목균형표: {' / '.join(parts)}"

    # ── OBV ──
    obv_text = f"  - OBV: {c.obv_trend}" if c.obv_trend else ""

    # ── 캔들 패턴 ──
    candle_text = ""
    if c.candle_patterns:
        candle_text = "  - 캔들 패턴: " + " / ".join(c.candle_patterns)

    # ── 차트 패턴 ──
    chart_pat_text = ""
    if c.chart_patterns:
        chart_pat_text = "  - 차트 패턴: " + " / ".join(c.chart_patterns)

    # ── 다이버전스 ──
    div_text = ""
    div_parts = []
    if c.rsi_divergence:
        div_parts.append(f"RSI {c.rsi_divergence}")
    if c.macd_divergence:
        div_parts.append(f"MACD {c.macd_divergence}")
    if div_parts:
        div_text = "  - 다이버전스: " + " / ".join(div_parts)

    # ── 거래량 추세 (다일간) ──
    vol_trend_text = f"  - 거래량 추세: {c.volume_price_trend}" if c.volume_price_trend else ""

    # ── 피보나치 ──
    fib_text = ""
    if c.fibonacci:
        f = c.fibonacci
        cur = signal.current_price
        # 현재가와 가장 가까운 되돌림 레벨
        levels = [("23.6%", f["fib_236"]), ("38.2%", f["fib_382"]),
                  ("50%", f["fib_500"]), ("61.8%", f["fib_618"])]
        nearest = min(levels, key=lambda x: abs(x[1] - cur))
        fib_text = (f"  - 피보나치: 고점 {f['swing_high']:,} / 저점 {f['swing_low']:,}"
                    f" — 근접 레벨: {nearest[0]}({nearest[1]:,})"
                    f" | 확장: 127.2%={f.get('ext_1272', 0):,} / 161.8%={f.get('ext_1618', 0):,}")

    # ── 지지/저항 ──
    sr_text = ""
    if c.support_level and c.resistance_level:
        cur = signal.current_price
        to_support = (cur - c.support_level) / cur * 100
        to_resist = (c.resistance_level - cur) / cur * 100
        sr_text = (f"  - 지지/저항: 지지 {c.support_level:,}원({to_support:+.1f}%)"
                   f" / 저항 {c.resistance_level:,}원(+{to_resist:.1f}%)")

    lines = [
        f"  - MA5: {int(c.ma5):,}원 / MA20: {int(c.ma20):,}원" if c.ma5 and c.ma20 else "  - MA: 데이터 부족",
        f"  - 추세: {c.trend} (현재가 MA5 {'위' if c.above_ma5 else '아래'} / MA20 {'위' if c.above_ma20 else '아래'})" if c.above_ma5 is not None else "",
        f"  - RSI: {rsi_val} [{rsi_interp}]" if rsi_interp else "",
        stoch_text, cci_text, ichimoku_text, obv_text,
        f"  - 10일 흐름: {trend_arrows}" if trend_arrows else "",
        f"  - 5일 전 대비: {c.price_change_5d:+.2f}%" if c.price_change_5d is not None else "",
        f"  - 연속 하락: {consecutive_down}일" if consecutive_down > 0 else "",
        vol_spread, vol_trend_text, candle_text, chart_pat_text, div_text, fib_text, sr_text,
    ]
    return "\n".join(l for l in lines if l)


def _fmt_trades(trades: list[dict], signal_type: str, add_signal_mode: str = "") -> str:
    if not trades:
        return "없음"
    today = __import__("datetime").date.today().strftime("%Y-%m-%d")
    lines = []
    add_count = 0
    for t in trades:
        date = str(t.get("executed_at") or "")[:10]
        side = t.get("side", "")
        qty = _p(t.get("quantity"))
        price = _p(t.get("price"))
        label = "(오늘 신규 진입)" if date == today and side == "매수" else ""
        lines.append(f"  - {date} {side} {qty}주 @ {price:,}원 {label}".rstrip())
        if side == "매수" and date != str(trades[0].get("executed_at", ""))[:10]:
            # 첫 매수 이후 추가 매수 = 물타기
            pass
    # 물타기 횟수: 매수 건수 - 1 (첫 매수 제외)
    buy_trades = [t for t in trades if t.get("side") == "매수"]
    add_count = max(0, len(buy_trades) - 1)  # 첫 매수 제외, 이후가 물타기

    result = "\n".join(lines)
    if signal_type == "add":
        if add_signal_mode == "momentum_add":
            result += "\n  ▶ 반등 확인 후 추매 신호: 현재가가 평단보다 높을 때만 유효"
        else:
            result += f"\n  ▶ 물타기 실행 횟수: {add_count}회 (최대 1회 원칙 — 이미 1회 이상이면 홀드 권고)"
    if (
        any(str(t.get("executed_at", ""))[:10] == today and t.get("side") == "매수" for t in trades)
        and signal_type == "add"
        and add_signal_mode != "momentum_add"
    ):
        result += "\n  ⚠️ 오늘 신규 진입 종목 — add 신호 홀드 강력 권고"
    return result


def _fmt_sector(sector: dict, sector_code: str | None) -> str:
    if not sector:
        return "섹터 데이터 없음"
    price = _p(sector.get("cur_prc") or sector.get("prpr"))
    rate = sector.get("flu_rt") or sector.get("prdy_ctrt") or "N/A"
    name = sector.get("upjong_nm") or sector_code or "해당 섹터"
    if price:
        return f"{name}: {price:,}pt (등락률 {rate}%)"
    return "섹터 조회 실패"


def _fmt_portfolio(holdings: list[dict], stock_code: str) -> tuple[str, str]:
    """DB 캐시 기반 (보유상세, 포트폴리오전체) 반환"""
    if not holdings:
        return "미보유 (DB 캐시 없음)", "동기화 필요", 0

    holding_item: dict | None = None
    lines = []
    total_eval = 0
    total_profit = 0

    for h in holdings:
        code = str(h.get("stock_code") or "")
        name = h.get("stock_name") or code
        qty = _p(h.get("quantity"))
        avg = _p(h.get("avg_price"))
        cur = _p(h.get("current_price"))
        rate = _f(h.get("profit_rate"))
        eval_amt = _p(h.get("eval_amount"))

        if code == stock_code:
            holding_item = {"qty": qty, "avg": avg, "rate": rate, "eval_amt": eval_amt}

        total_eval += eval_amt
        total_profit += h.get("profit_loss", 0) if isinstance(h.get("profit_loss"), int) else _p(h.get("profit_loss"))
        lines.append(f"  - {name}: {qty}주 | 평단 {avg:,}원 | 현재 {cur:,}원 | {rate:+.2f}%")

    if total_eval:
        total_cost = total_eval - total_profit
        rate_total = total_profit / total_cost * 100 if total_cost else 0
        lines.append(f"  ▶ 합계: 평가 {total_eval:,}원 | 손익 {total_profit:+,}원 ({rate_total:+.1f}%)")

    if holding_item:
        weight = holding_item["eval_amt"] / total_eval * 100 if total_eval else 0
        holding_detail = (
            f"{holding_item['qty']}주 보유 | 평균단가 {holding_item['avg']:,}원 | "
            f"수익률 {holding_item['rate']:+.2f}% | 포트 비중 {weight:.1f}%"
        )
    else:
        holding_detail = "미보유"

    return holding_detail, "\n".join(lines) if lines else "보유 종목 없음", total_eval


def _fmt_signal_history(stock_code: str, signal_type: str = "") -> str:
    """해당 종목 + 동일 signal_type 과거 AI 판단 이력 + 결과 수익률 (최근 5건)."""
    try:
        from data.db import get_signal_history
        records = get_signal_history(stock_code, signal_type=signal_type, limit=5)
        if not records:
            return "없음"
        lines = []
        for r in records:
            dt = str(r["created_at"])[:10]
            price = r["current_price"]
            opinion = str(r["claude_opinion"] or "")
            first_line = opinion.strip().splitlines()[0] if opinion.strip() else ""
            verdict = next(
                (
                    tag for tag in [
                        "[추가매수(매수)]",
                        "[물타기(매수)]",
                        "[매수]",
                        "[매도]",
                        "[홀드]",
                    ] if tag in first_line
                ),
                "?",
            )
            action = r.get("action") or ""
            result = r.get("result_pct")
            result_str = f"{result:+.1f}%" if result is not None else "집계 중"
            action_str = f" → 실행:{action}" if action else ""
            lines.append(f"  - {dt} {verdict}{action_str} @ {price:,}원 | 3일 후: {result_str}")
        return "\n".join(lines)
    except Exception:
        return "조회 실패"


def _fmt_recent_insights() -> str:
    """최근 daily review 인사이트 + 적중률 요약 (판단 AI 프롬프트 주입용).
    프롬프트 토큰 절약을 위해 최대 5줄로 압축.
    """
    try:
        from data.db import get_recent_daily_reviews, get_verdict_accuracy
        # 최근 복기 1건 — 핵심만 추출
        reviews = get_recent_daily_reviews(limit=1)
        review_line = ""
        if reviews:
            detail = reviews[0].get("detail", "")
            # [개선제안]과 [적중률분석] 줄만 추출
            for line in detail.splitlines():
                stripped = line.strip()
                if stripped.startswith("[적중률분석]") or stripped.startswith("[개선제안]"):
                    review_line += stripped + "\n"
            if not review_line:
                # 없으면 첫 2줄
                lines = [l.strip() for l in detail.splitlines() if l.strip()]
                review_line = "\n".join(lines[:2])

        # 최근 14일 적중률 요약 — 1줄
        accuracy = get_verdict_accuracy(days=14)
        acc_parts = []
        for v in ["매수", "매도", "홀드"]:
            a = accuracy.get(v)
            if a and a["count"] >= 3:
                acc_parts.append(f"{v} {a['count']}건 적중{a['hit_rate_3d']}% 평균{a['avg_3d']:+.1f}%")
        acc_line = " / ".join(acc_parts) if acc_parts else ""

        parts = []
        if acc_line:
            parts.append(f"14일 성과: {acc_line}")
        if review_line:
            parts.append(review_line.strip())
        return "\n".join(parts) if parts else "데이터 부족"
    except Exception:
        return "조회 실패"


def _fmt_last_ai_decision(stock_code: str) -> str:
    """오늘 해당 종목의 직전 AI 판단 조회."""
    try:
        from data.db import get_conn
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                "SELECT created_at, claude_opinion FROM signals "
                "WHERE stock_code = ? AND created_at >= ? AND claude_opinion IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (stock_code, today),
            ).fetchone()
        if not row:
            return "오늘 없음"
        dt = str(row["created_at"])[11:16]
        opinion = str(row["claude_opinion"] or "")
        first_line = opinion.strip().splitlines()[0] if opinion.strip() else ""
        verdict = next(
            (
                tag for tag in [
                    "[추가매수(매수)]",
                    "[물타기(매수)]",
                    "[매수]",
                    "[매도]",
                    "[홀드]",
                ] if tag in first_line
            ),
            "",
        )
        # 첫 줄은 verdict 태그 자체이므로 두 번째 줄(첫 번째 근거)을 표시
        lines = [l.strip() for l in opinion.strip().splitlines() if l.strip() and not l.strip().startswith("[")]
        summary = lines[0][:80] if lines else ""
        return f"{verdict} ({dt}) — {summary}"
    except Exception:
        return "조회 실패"


def _fmt_last_hold_condition(stock_code: str) -> str:
    """해당 종목의 최근 7일 이내 홀드 전환조건 조회 (최신 1건)."""
    try:
        from data.db import get_conn
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                "SELECT created_at, claude_opinion FROM signals "
                "WHERE stock_code = ? AND created_at >= ? "
                "AND claude_opinion IS NOT NULL AND claude_opinion LIKE '%[홀드]%' "
                "ORDER BY created_at DESC LIMIT 1",
                (stock_code, cutoff),
            ).fetchone()
        if not row:
            return ""
        opinion = str(row["claude_opinion"] or "")
        # [전환조건] 라인 추출
        for line in opinion.splitlines():
            if "[전환조건]" in line:
                condition = line.split("[전환조건]", 1)[1].strip()
                if condition:
                    dt = str(row["created_at"])[:16]
                    return f"- 이전 홀드 전환조건 ({dt}): {condition}"
        return ""
    except Exception:
        return ""


def get_trade_opinion(
    signal,
    holdings: list[dict],
    kospi: dict,
    kosdaq: dict,
    sector: dict,
    recent_trades: list[dict] | None = None,
    deposit: int = 0,
) -> str:
    # Agent 모드 분기 — worker.yaml의 use_agent_mode: true 시 활성화
    try:
        import yaml
        _cfg_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'worker.yaml')
        with open(_cfg_path, encoding="utf-8") as _f:
            _cfg = yaml.safe_load(_f) or {}
        _use_agent = bool((_cfg.get("worker") or {}).get("use_agent_mode", False))
    except Exception:
        _use_agent = False

    if _use_agent:
        try:
            from worker.agents.judgment_agent import JudgmentAgent
            _wcfg = (_cfg.get("worker") or {})
            _agent = JudgmentAgent(
                max_steps=int(_wcfg.get("agent_max_steps", 7)),
                max_tokens=int(_wcfg.get("agent_max_tokens", 700)),
                target_unique_tools=int(_wcfg.get("agent_target_unique_tools", 0)),
            )
            _opinion = _agent.run(signal)
            # 마지막 agent trace를 모듈 변수에 저장 (main.py에서 DB 저장에 활용)
            global _last_agent_trace
            _last_agent_trace = {
                "tool_sequence": _agent.used_tools,
                "reasoning_chain": _agent.reasoning_chain,
            }
            return _opinion
        except Exception as _e:
            logger.warning(f"[Agent모드] 실패, 레거시로 폴백: {_e}")

    _last_agent_trace = {}
    return _legacy_get_trade_opinion(signal, holdings, kospi, kosdaq, sector, recent_trades, deposit)


def _legacy_get_trade_opinion(
    signal,
    holdings: list[dict],
    kospi: dict,
    kosdaq: dict,
    sector: dict,
    recent_trades: list[dict] | None = None,
    deposit: int = 0,
) -> str:
    holding_detail, portfolio_text, total_eval = _fmt_portfolio(holdings, signal.stock_code)

    # 총 포트폴리오 = 주식 평가액 + 현금
    total_portfolio = total_eval + deposit

    # 물타기 예비금: positions 테이블의 add_buy_price × 보유수량 × 0.5 합산
    # add_buy_price 미설정 종목은 평가액의 15%로 추정
    try:
        from data.db import get_positions
        _positions = {p["stock_code"]: p for p in get_positions()}
    except Exception:
        _positions = {}

    add_reserve = 0
    for h in holdings:
        code = str(h.get("stock_code", ""))
        if code == signal.stock_code:
            continue
        pos = _positions.get(code)
        if pos and pos.get("add_buy_price"):
            qty = _p(h.get("quantity"))
            add_reserve += pos["add_buy_price"] * int(qty * 0.5)
        else:
            add_reserve += int(_p(h.get("eval_amount")) * 0.15)
    add_reserve = int(add_reserve)

    # 실제 매수 여력 = 현금 - 물타기 예비금 (음수면 현금 부족)
    buy_budget = deposit - add_reserve

    # 포트폴리오 상태 지표
    cash_ratio = deposit / total_portfolio * 100 if total_portfolio else 0
    winning = [h for h in holdings if _f(h.get("profit_rate")) > 0]
    losing  = [h for h in holdings if _f(h.get("profit_rate")) < 0]
    near_add = [  # 물타기 가격 근접 종목 (현재가가 add_buy_price의 105% 이내)
        h for h in holdings
        if str(h.get("stock_code", "")) != signal.stock_code
        and _positions.get(str(h.get("stock_code", "")), {}).get("add_buy_price", 0)
        and _p(h.get("current_price")) <= _positions[str(h.get("stock_code", ""))]["add_buy_price"] * 1.05
    ] if _positions else []
    conditions_text = "\n".join(f"    - {c}" for c in signal.triggered_conditions)
    trades_text = _fmt_trades(recent_trades or [], signal.signal_type, getattr(signal, "add_signal_mode", ""))
    last_ai_text = _fmt_last_ai_decision(signal.stock_code)
    hold_condition_text = _fmt_last_hold_condition(signal.stock_code)
    history_text = _fmt_signal_history(signal.stock_code, signal_type=signal.signal_type)

    # ── SQL 범위 필터 RAG: 유사 지표 상황의 과거 신호 ──
    _rag_context_text = ""
    try:
        from data.db import search_similar_signals
        _rag_rows = search_similar_signals(
            rsi=signal.rsi if signal.rsi else None,
            signal_type=signal.signal_type,
            volume_ratio=signal.volume_ratio if signal.volume_ratio else None,
            limit=3,
            days=90,
        )
        if _rag_rows:
            _rag_lines = []
            for _r in _rag_rows:
                _v = _r.get("verdict") or "판정없음"
                _r3 = _r.get("result_pct")
                _r3_str = f"{_r3:+.1f}%" if _r3 is not None else "미집계"
                _conds = str(_r.get("triggered_conditions") or "")[:60]
                _rag_lines.append(
                    f"  - {_r.get('created_at','')[:10]} {_r.get('stock_name','')} "
                    f"[{_v}] 3일후:{_r3_str} | {_conds}"
                )
            _rag_context_text = "\n".join(_rag_lines)
    except Exception:
        pass

    insights_text = _fmt_recent_insights()

    add_mode = getattr(signal, "add_signal_mode", "")
    # add 신호: 물타기 트리거(평단 -8%) 또는 반등 추매 여부 표시
    add_trigger_text = ""
    if signal.signal_type == "add":
        avg_price = getattr(signal, "avg_price", 0) or next(
            (_p(h.get("avg_price")) for h in holdings
             if str(h.get("stock_code", "")) == signal.stock_code),
            0,
        )
        if avg_price and signal.current_price:
            if add_mode == "momentum_add":
                premium_pct = (signal.current_price - avg_price) / avg_price * 100
                add_trigger_text = (
                    f"\n- 이번 add 분류: momentum_add (물타기 아님, 반등 확인 후 추매)"
                    f"\n- 반등 추매 구간: 평단 {avg_price:,}원 대비 현재가 +{premium_pct:.1f}%"
                )
            else:
                trigger_price = int(avg_price * 0.92)
                gap_pct = (signal.current_price - trigger_price) / signal.current_price * 100
                if gap_pct > 0:
                    add_trigger_text = (
                        f"\n- 이번 add 분류: averaging_down (평단 하회 물타기)"
                        f"\n- 물타기 트리거 {trigger_price:,}원 (평단 -8%) | 현재가 대비 {gap_pct:.1f}% 위"
                    )
                else:
                    add_trigger_text = (
                        f"\n- 이번 add 분류: averaging_down (평단 하회 물타기)"
                        f"\n- 물타기 트리거 {trigger_price:,}원 (평단 -8%) | 이미 트리거 이하 ({abs(gap_pct):.1f}% 초과)"
                    )

    # watchlist 현재 설정값 조회 + positions 포지션 관리 정보
    _wl_settings_text = ""
    _position_text = ""
    try:
        from data.db import get_watchlist, get_position
        _stock = next((s for s in get_watchlist() if s["code"] == signal.stock_code), None)
        if _stock:
            _settings = []
            for _field, _label in [
                ("rsi_oversold", "RSI 과매도"),
                ("rsi_overbought", "RSI 과매수"),
                ("rsi_oversold_intraday", "RSI 과매도(분봉)"),
                ("volume_surge_ratio", "거래량 배율"),
            ]:
                _v = _stock.get(_field)
                if _v is not None:
                    _settings.append(f"{_label}: {_v} ({_field})")
            if _settings:
                _wl_settings_text = "\n- 현재 임계값 설정: " + " / ".join(_settings)

        # 섹터 집중도 계산
        _sector_text = ""
        if _stock and _stock.get("sector_code"):
            _sector = _stock["sector_code"]
            try:
                import yaml as _yaml
                _cfg_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'worker.yaml')
                with open(_cfg_path, encoding='utf-8') as _cf:
                    _sector_max = int((_yaml.safe_load(_cf) or {}).get('sector', {}).get('max_holdings', 3))
            except Exception:
                _sector_max = 3
            _holdings_codes = {str(h.get("stock_code", "")) for h in holdings}
            _all_wl = get_watchlist()
            _same_sector = [
                w["name"] for w in _all_wl
                if w.get("sector_code") == _sector
                and w["code"] in _holdings_codes
                and w["code"] != signal.stock_code
            ]
            _remain = _sector_max - len(_same_sector)
            _sector_text = (
                f"\n- 섹터: {_sector} | 동일 섹터 보유: {len(_same_sector)}종목"
                f" ({', '.join(_same_sector) or '없음'})"
                f" | 상한 {_sector_max}종목 → {'여유 있음' if _remain > 0 else '⚠️ 상한 도달'}"
            )

        # 보유 종목이면 positions에서 포지션 관리 정보 조회
        if signal.in_portfolio:
            _pos = get_position(signal.stock_code)
            if _pos:
                _pos_items = []
                if _pos.get("target_price"):
                    _pos_items.append(f"목표가: {_pos['target_price']:,}원")
                if _pos.get("stop_loss_price"):
                    _pos_items.append(f"손절가: {_pos['stop_loss_price']:,}원")
                if _pos.get("add_buy_price"):
                    _pos_items.append(f"추가매수가: {_pos['add_buy_price']:,}원")
                if _pos.get("mid_sell_price"):
                    _pos_items.append(f"중간매도가: {_pos['mid_sell_price']:,}원")
                if _pos_items:
                    _position_text = "\n- 포지션 관리: " + " / ".join(_pos_items)
    except Exception:
        pass

    # 목표가·손절가 R/R 계산 (보유종목은 positions에서, 미보유는 signal에서)
    tp = getattr(signal, "target_price", None) or None
    sl = getattr(signal, "stop_loss_price", None) or None
    cur = signal.current_price
    rr_text = "목표가·손절가 미설정"
    if tp and sl and cur:
        upside = (tp - cur) / cur * 100
        downside = (cur - sl) / cur * 100
        rr = upside / downside if downside else 0
        rr_text = f"목표가 {tp:,}원 ({upside:+.1f}%) / 손절가 {sl:,}원 ({-downside:.1f}%) / R/R {rr:.1f}:1"
    elif tp and cur:
        upside = (tp - cur) / cur * 100
        rr_text = f"목표가 {tp:,}원 ({upside:+.1f}%) / 손절가 미설정"
    elif sl and cur:
        downside = (cur - sl) / cur * 100
        rr_text = f"목표가 미설정 / 손절가 {sl:,}원 ({-downside:.1f}%)"

    if signal.signal_type == "add":
        if add_mode == "momentum_add":
            signal_type_label = "add   — 보유 중 반등 확인 후 추가매수(momentum_add) 검토"
        else:
            signal_type_label = "add   — 보유 중 평단 하회 물타기(averaging_down) 검토"
    else:
        signal_type_label = {
            "entry": "entry — 신규 매수 타이밍 검토",
            "exit":  "exit  — 매도·익절·손절 타이밍 검토",
            "both":  "both  — 매수/매도 모두 해당",
        }.get(signal.signal_type, signal.signal_type)

    # ── 정적 시스템 프롬프트 (캐싱 대상) ──
    _SYSTEM_PROMPT = f"""당신은 개인 투자자의 퀀트 트레이딩 시스템에서 최종 매매 판단을 내리는 AI입니다.
신호가 발생한 이유와 전략 맥락을 이해하고, 데이터 기반으로 판단하세요.

{_TRADING_KNOWLEDGE_ACTIVE}

## 운용 원칙 (변경 불가 규칙)
- 물타기 최대 1회 원칙은 averaging_down add 신호에만 적용
- momentum_add add 신호는 현재가가 평단보다 높을 때만 검토 가능
- 손절가 근접 시 홀드 유혹 금지 — 손절가 도달 시 즉시 매도 원칙
- entry 신호에서 시장 하락은 홀드 근거 아님 — R/R 2:1 이상이면 매수 검토
- 목표가 도달 시 익절 실행 원칙 — 홀드 연장 시 근거 명시 필수
- 감정적 판단(공포·욕심) 배제 — 수치 기반으로만 판단
- 추천수량은 반드시 실질 매수 여력(현금 - 물타기 예비금) 이내로 제한 — 초과 절대 금지
- 현금 비중 5% 미만이면 신규 매수 보류 — 기존 수익 종목 익절 또는 물타기만 허용

## 포트폴리오 판단 기준 (entry 신호 시 반드시 확인)
- 현금 비중 15% 미만: 추천수량을 목표의 절반 이하로 제한. 근거에 "현금 부족으로 축소 진입" 명시
- 보유 종목 5개 이상: 신규 진입 시 가장 수익률 낮은 기존 종목 정리 고려를 근거에 언급
- 포트 전체 수익률 -5% 이하: 리스크 축소 모드. 신규 매수 최소 수량만 허용
- 포트 전체 수익률 -10% 이하: [홀드] 판단 우선. 매수는 매우 강한 신호(RSI 극과매도 + R/R 3:1 이상)에만 허용
- 물타기 근접 종목 존재 시: 신규 진입 수량 축소하고 해당 사실을 근거에 명시

## 신호 유형별 판단 기준

**entry** (신규 매수 검토)
- R/R 2:1 이상이면 [매수] 긍정 검토
- 시장 전반 하락은 홀드 근거 아님 — 더 좋은 진입가일 수 있음
- 판단: [매수] or [홀드]

**exit** (매도·익절·손절 검토)
- 목표가 도달 → [매도] 원칙. 홀드 연장 시 근거 필수
- 손절가 도달 → [매도] 원칙. 손절가 근접 시 홀드 유혹 금지
- 판단: [매도] or [홀드]

**add — momentum_add** (평단 위 반등 확인 후 추가매수)
- 현재가 > 평단일 때만 유효. momentum_add를 물타기로 재해석 금지
- "금일 1회 물타기" 규칙은 momentum_add에 적용하지 말 것
- 판단: [추가매수(매수)] or [홀드]

**add — averaging_down** (평단 하회 물타기)
- 현재가 < 평단일 때만 유효. 물타기 최대 1회 원칙 적용
- 오늘 신규 진입 종목은 홀드 우선 권고
- 판단: [물타기(매수)] or [홀드]

**both** (미보유 → entry 기준 / 보유 → exit 기준으로 분기)

## 공통 판단 원칙
- 근거 우선순위: 기술적 신호 → 포지션 맥락 → 리스크 관리
- 뉴스/공시: 실적 쇼크·유상증자·수주·거래정지 등 가격 영향 큰 경우만 상위 반영. 일반 기사는 보조 참고만
- 섹터 등락률은 참고 정보일 뿐 홀드 주된 근거로 사용 금지
- [임계값]: 기존 설정 유지가 기본. 구조적 오류일 때만 변경 제안. 기존값 대비 ±3 초과 금지. 허용 필드: rsi_overbought/rsi_oversold_intraday/volume_surge_ratio

## 출력 형식 (반드시 준수)
[매수 or 추가매수(매수) or 물타기(매수) or 매도 or 홀드]
• 근거1: (필수, 1문장)
• 근거2: (선택, 1문장)
• 근거3: (선택, 1문장)
[주문시장] KRX or NXT or SOR (홀드이면 생략. 모의투자는 KRX만 허용)
[주문방식] 시장가 or 지정가 (홀드이면 생략)
[추천수량] N주 (약 XXX만원) — 실질 매수 여력({buy_budget:,}원) 이내, 총 포트폴리오의 7~15% 목표. 현금 부족 시 절반 이하. 실질 매수 여력 0 이하면 신호 강도로 판단: 복합 신호(3가지 이상) 또는 핵심 지표 극과매도(RSI 30 이하·볼린저 하단 -3% 이상)면 현금의 20~30% 이내 소량 매수 허용, 그 외 단순 신호(1~2가지)면 [홀드] (홀드이면 생략)
[전환조건] 홀드 시 매수/매도 전환 트리거 수치로 명시 (홀드가 아니면 생략)
[임계값] 변경 불필요하면 반드시 생략 (홀드 시만, field=value 형식)

마크다운 헤더(#, ##) 사용 금지. 총 250단어 이내."""

    # ── DART 공시 조회 ──
    dart_text = "공시 조회 불가"
    try:
        from worker.clients.dart_client import format_full_context_for_ai, DART_API_KEY
        if DART_API_KEY:
            dart_text = format_full_context_for_ai(signal.stock_code)
    except Exception as e:
        logger.debug(f"DART 공시 조회 실패: {e}")

    # ── 뉴스 조회 (종목 + 섹터 + 매크로) ──
    news_text = "뉴스 조회 불가"
    sector_news_text = ""
    macro_news_text = ""
    try:
        from worker.clients.news_client import (
            format_news_for_ai, get_macro_news_for_ai,
            format_sector_news_for_ai, NAVER_CLIENT_ID,
        )
        if NAVER_CLIENT_ID:
            news_text = format_news_for_ai(signal.stock_name, max_items=5)
            sector_name = sector.get("upjong_nm") if sector else None
            if sector_name:
                sector_news_text = format_sector_news_for_ai(sector_name, max_items=3)
            macro_news_text = get_macro_news_for_ai()
    except Exception as e:
        logger.debug(f"뉴스 조회 실패: {e}")

    # ── 글로벌 지수 조회 ──
    global_indices_text = ""
    try:
        from worker.clients.global_market import format_global_indices_for_ai
        global_indices_text = format_global_indices_for_ai()
    except Exception as e:
        logger.debug(f"글로벌 지수 조회 실패: {e}")

    # ── 동적 유저 프롬프트 (신호별 데이터) ──
    _skipped = []
    _dart_section = f"\n## 최근 공시 (DART)\n{dart_text}" if dart_text != "공시 조회 불가" else (_skipped.append("DART") or "")
    _news_section = f"\n## 최근 뉴스 ({signal.stock_name})\n{news_text}" if news_text != "뉴스 조회 불가" else (_skipped.append("뉴스") or "")
    _sector_news_section = f"\n## 업종 뉴스 ({sector.get('upjong_nm', '')})\n{sector_news_text}" if sector_news_text else ""
    _macro_news_section = f"\n## 거시경제·글로벌 이슈\n{macro_news_text}" if macro_news_text else ""
    _trades_section = f"\n## 최근 매매 이력 (3일)\n{trades_text}" if trades_text != "없음" else (_skipped.append("매매이력") or "")
    _insights_section = f"\n## 최근 AI 판단 성과 (자기 보정용)\n{insights_text}" if insights_text not in ("데이터 부족", "조회 실패") else (_skipped.append("AI성과") or "")
    if _skipped:
        logger.debug(f"[판단 프롬프트] 빈 섹션 제거: {', '.join(_skipped)}")

    _global_line = f"\n글로벌: {global_indices_text}" if global_indices_text else ""

    user_prompt = f"""## 신호 정보
- 종목: {signal.stock_name} ({signal.stock_code}) | 매매 기간: {getattr(signal, 'horizon', '')}
- 신호 유형: {signal_type_label}
- 트리거 조건:
{conditions_text}
- 현재가: {signal.current_price:,}원
- RSI(14일봉): {signal.rsi if signal.rsi else 'N/A'}
- 거래량 배율: {f'{signal.volume_ratio}배' if signal.volume_ratio else 'N/A'}

## 종목 전략 설정
- {rr_text}{add_trigger_text}{_position_text}{_wl_settings_text}{_sector_text}

## 직전 AI 판단 (오늘)
- {last_ai_text}
{hold_condition_text}

## 과거 AI 판단 이력 — {signal.signal_type} 신호 기준 (최근 5건)
{history_text}
{f'## 유사 지표 사례 (RSI·거래량 유사, 최근 90일){chr(10)}{_rag_context_text}' if _rag_context_text else ''}
{_dart_section}
{_news_section}
{_sector_news_section}
{_macro_news_section}

## 차트 분석
{_fmt_chart(signal)}
{_trades_section}

## 보유 현황
- 해당 종목: {holding_detail}
- 현금(주문가능금액): {f'{deposit:,}원' if deposit else '조회 불가'} (현금 비중 {cash_ratio:.1f}%)
- 주식 평가액: {f'{total_eval:,}원' if total_eval else '없음'} | 총 포트폴리오: {f'{total_portfolio:,}원' if total_portfolio else '조회 불가'}
- 보유 종목: {len(holdings)}개 (수익 {len(winning)}개 / 손실 {len(losing)}개)
- 물타기 예비금: 약 {add_reserve:,}원 | 실질 매수 여력: {f'{buy_budget:,}원' if buy_budget > 0 else f'부족 ({buy_budget:,}원)'}
{f'- ⚠️ 물타기 근접 종목: {", ".join(h.get("stock_name","") for h in near_add)} — 해당 종목 현금 배정 우선 검토' if near_add else ''}
- 포트폴리오 전체:
{portfolio_text}

## 시장 환경
{_fmt_index(kospi, '코스피')}
{_fmt_index(kosdaq, '코스닥')}
{_fmt_sector(sector, signal.sector_code)}{_global_line}
{_insights_section}"""

    if _BACKEND == "anthropic":
        response = _client.messages.create(
            model=MODEL,
            max_tokens=800,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text
    else:
        response = _client.chat.completions.create(
            model=MODEL,
            max_tokens=800,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content


def get_dip_buy_opinion(
    stock: dict,
    current_price: int,
    rsi: float | None,
    chart,
    kospi_rate: float,
    kosdaq_rate: float,
    deposit: int = 0,
    buy_budget: int = 0,
    total_portfolio: int = 0,
    holdings: list | None = None,
) -> str:
    """시장 급락 시 반등 매수 후보 평가 — 기술적 신호 없이 종합 판단.
    반환 형식은 get_trade_opinion과 동일 ([매수]/[홀드] + [추천수량])하여 _auto_execute 재활용."""
    holdings = holdings or []
    code = stock.get("code", "")
    name = stock.get("name", code)
    horizon = stock.get("horizon", "")

    dart_text = ""
    try:
        from worker.clients.dart_client import format_full_context_for_ai, DART_API_KEY
        if DART_API_KEY:
            dart_text = format_full_context_for_ai(code)
    except Exception:
        pass

    news_text = ""
    macro_news_text = ""
    try:
        from worker.clients.news_client import (
            format_news_for_ai, get_macro_news_for_ai, NAVER_CLIENT_ID,
        )
        if NAVER_CLIENT_ID:
            news_text = format_news_for_ai(name, max_items=5)
            macro_news_text = get_macro_news_for_ai()
    except Exception:
        pass

    global_indices_text = ""
    try:
        from worker.clients.global_market import format_global_indices_for_ai
        global_indices_text = format_global_indices_for_ai()
    except Exception:
        pass

    # 포트폴리오 현황
    total_eval = total_portfolio - deposit
    cash_ratio = deposit / total_portfolio * 100 if total_portfolio else 0

    # 차트 요약 (기존 _fmt_chart 재활용)
    class _FakeSignal:
        pass
    fake = _FakeSignal()
    fake.chart = chart
    fake.current_price = current_price
    fake.rsi = rsi
    chart_text = _fmt_chart(fake)

    _dart_section = f"\n## 최근 공시 (DART)\n{dart_text}" if dart_text else ""
    _news_section = f"\n## 최근 뉴스 ({name})\n{news_text}" if news_text else ""
    _macro_news_section = f"\n## 거시경제·글로벌 이슈\n{macro_news_text}" if macro_news_text else ""
    _global_line = f" / 글로벌: {global_indices_text}" if global_indices_text else ""

    system_prompt = f"""당신은 개인 투자자의 퀀트 트레이딩 시스템에서 시장 급락 시 반등 매수 후보를 평가하는 AI입니다.
기술적 신호(RSI 과매도, MA 크로스 등)가 발동하지 않은 상태에서도, 시장 전체 급락과 종목의 펀더멘털·차트·뉴스를 종합하여
"지금 담을 만한가"를 판단합니다.

{_TRADING_KNOWLEDGE_ACTIVE}

## 급락 매수 판단 원칙
- 시장 급락은 우량 종목을 싸게 살 기회일 수 있음 — 공포에 동조하지 말 것
- 단, 시장 급락 + 개별 악재(공시/실적 쇼크/섹터 붕괴) 동시면 패스
- 차트상 주요 지지선 근처이거나 RSI가 낮을수록 반등 가능성 높음
- 반등 목표: 최근 고점 또는 피보나치 61.8% 되돌림 수준
- 물타기 여력이 있으면 첫 진입은 보수적으로 (목표 비중의 50~70%)

## 출력 형식 (반드시 준수)
[매수 or 홀드]
• 근거1: (1~2문장)
• 근거2: (1~2문장)
[주문시장] KRX (홀드이면 생략, 모의투자는 KRX만 허용)
[주문방식] 시장가 or 지정가 (홀드이면 생략)
[추천수량] N주 (약 XXX만원) — 총 포트폴리오의 5~10% 기준, 실질 매수 여력({buy_budget:,}원) 초과 금지 (홀드이면 생략)
[전환조건] 홀드 시 매수 전환 조건 명시

마크다운 헤더 사용 금지. 150단어 이내."""

    user_prompt = f"""## 시장 상황
- KOSPI: {kospi_rate:+.2f}% / KOSDAQ: {kosdaq_rate:+.2f}% (급락 진행 중){_global_line}

## 종목 정보
- 종목: {name} ({code}) | 매매 기간: {horizon or '미설정'}
- 현재가: {current_price:,}원
- RSI: {rsi if rsi else 'N/A'}

## 차트 분석
{chart_text}
{_dart_section}
{_news_section}
{_macro_news_section}

## 포트폴리오 상태
- 현금(주문가능금액): {deposit:,}원 (현금 비중 {cash_ratio:.1f}%)
- 주식 평가액: {total_eval:,}원 | 총 포트폴리오: {total_portfolio:,}원
- 실질 매수 여력: {buy_budget:,}원"""

    if _BACKEND == "anthropic":
        response = _client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text
    else:
        response = _client.chat.completions.create(
            model=MODEL,
            max_tokens=400,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content


def judge_position_values(
    stock_code: str,
    stock_name: str,
    avg_price: int,
    quantity: int,
    current_price: int | None = None,
) -> dict | None:
    """매수 체결 후 AI가 포지션 관리값(목표가/손절가/추가매수가) 판단.
    Returns: {"target_price": int, "stop_loss_price": int, "add_buy_price": int,
              "target_reason": str, "stop_loss_reason": str, "add_buy_reason": str} or None
    """
    import json as _json
    from worker.clients.kiwoom_client import KiwoomClient

    try:
        client = KiwoomClient()

        # 현재가 조회
        if not current_price:
            price_data = client.get_current_price(stock_code)
            current_price = _p(price_data.get("cur_prc") or price_data.get("stk_prpr") or price_data.get("prpr"))
        if not current_price:
            current_price = avg_price

        # 90일 일봉 조회
        daily_data = client.get_daily_ohlcv(stock_code, period=90)
        close_prices, high_prices, low_prices = [], [], []
        for d in daily_data:
            cp = _p(d.get("cur_prc"))
            hp = _p(d.get("high_pric"))
            lp = _p(d.get("lwst_pric") or d.get("low_pric"))
            if cp: close_prices.append(cp)
            if hp: high_prices.append(hp)
            if lp: low_prices.append(lp)

        # 기술적 지표 요약
        from worker.indicators import calculate_chart_summary
        chart = calculate_chart_summary(
            close_prices, high_prices, current_price,
            low_prices=low_prices,
        ) if len(close_prices) >= 20 else None

        chart_text = ""
        if chart:
            parts = []
            if chart.ma20: parts.append(f"MA20: {int(chart.ma20):,}")
            if chart.support_level: parts.append(f"지지선: {int(chart.support_level):,}")
            if chart.resistance_level: parts.append(f"저항선: {int(chart.resistance_level):,}")
            if chart.bollinger_upper: parts.append(f"볼린저 상단: {int(chart.bollinger_upper):,}")
            if chart.bollinger_lower: parts.append(f"볼린저 하단: {int(chart.bollinger_lower):,}")
            if chart.trend: parts.append(f"추세: {chart.trend}")
            if hasattr(chart, 'fibonacci') and chart.fibonacci:
                fib = chart.fibonacci
                if isinstance(fib, dict):
                    fib_parts = [f"{k}: {v:,.0f}" for k, v in fib.items() if isinstance(v, (int, float)) and v > 0]
                    if fib_parts:
                        parts.append(f"피보나치: {', '.join(fib_parts[:4])}")
            chart_text = " | ".join(parts)

        # watchlist 설정 조회 (horizon 등)
        from data.db import get_watchlist
        wl_stock = next((s for s in get_watchlist() if s["code"] == stock_code), None)
        horizon = wl_stock.get("horizon", "중기") if wl_stock else "중기"

        system_prompt = """당신은 매수 체결 후 포지션 관리 값을 설정하는 퀀트 트레이딩 AI입니다.
평단가, 차트 지표, 지지/저항선, 피보나치 레벨을 기반으로 합리적인 목표가·손절가·추가매수가를 산출하세요.

## 원칙
- 손절가: 평단가 대비 -5% ~ -10%. 직전 지지선 아래에 설정. 진입 후 변경 금지 원칙이므로 신중하게.
- 목표가: R/R 비율 최소 2:1 이상. 피보나치 확장 127~161% 또는 직전 저항선 근처.
- 추가매수가: 평단가 대비 -8% 근처. 지지선 부근. 물타기 1회 원칙.
- horizon에 따라 목표/손절 폭 조절:
  · 단기(1~2주): 목표가 +5~10%, 손절 -3~5%
  · 중기(1~3개월): 목표가 +10~20%, 손절 -5~8%
  · 장기(3개월+): 목표가 +20% 이상, 손절 -8~10%
- 해당 기간 내에 목표가 도달 가능성이 낮은 종목은 추천수량을 줄이거나 홀드 권고.

## 출력 형식 (JSON만, 설명 없이)
{"target_price": 정수, "target_reason": "근거 한 줄", "stop_loss_price": 정수, "stop_loss_reason": "근거 한 줄", "add_buy_price": 정수, "add_buy_reason": "근거 한 줄"}"""

        user_prompt = f"""## 매수 체결 정보
- 종목: {stock_name} ({stock_code})
- 평단가: {avg_price:,}원
- 수량: {quantity}주
- 현재가: {current_price:,}원
- horizon: {horizon}

## 차트 지표
{chart_text or '데이터 부족'}

## 최근 90일 가격 범위
- 최고가: {max(high_prices[:20]):,}원 (20일)
- 최저가: {min(low_prices[:20]):,}원 (20일)

JSON으로 목표가, 손절가, 추가매수가를 출력하세요."""

        if _BACKEND == "anthropic":
            response = _client.messages.create(
                model=MODEL_MINI,
                max_tokens=300,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = response.content[0].text
        else:
            response = _client.chat.completions.create(
                model=MODEL_MINI,
                max_tokens=300,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = response.choices[0].message.content

        # JSON 파싱 (마크다운 코드블록 제거 + 숫자 내 쉼표 처리)
        import re
        # 마크다운 코드블록 제거
        text_clean = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()
        # 숫자 안의 쉼표 제거 (예: 32,670 → 32670) — JSON 키/값 구분 쉼표는 보존
        text_clean = re.sub(r'(\d),(\d)', r'\1\2', text_clean)
        json_match = re.search(r'\{[\s\S]*?\}', text_clean)
        if json_match:
            try:
                result = _json.loads(json_match.group())
            except _json.JSONDecodeError:
                # 파싱 실패 시 trailing comma 등 정리 후 재시도
                cleaned = re.sub(r',\s*([}\]])', r'\1', json_match.group())
                result = _json.loads(cleaned)
            # 정수 변환
            for k in ("target_price", "stop_loss_price", "add_buy_price"):
                if k in result:
                    result[k] = int(float(str(result[k]).replace(",", "")))
            return result

        logger.warning(f"[{stock_name}] 포지션 AI 판단 JSON 파싱 실패: {text[:200]}")
        return None

    except Exception as e:
        logger.error(f"[{stock_name}] 포지션 AI 판단 실패: {e}")
        return None
