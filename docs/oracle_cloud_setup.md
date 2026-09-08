# Oracle Cloud Free Tier — 워커 배포 가이드

워커(`worker/main.py`)를 Oracle Cloud Always Free VM에 배포하는 가이드.

## 왜 Oracle Cloud?

- **서울 리전** (한국 IP → 키움 API 호환)
- **영구 무료** (Always Free, 기간 제한 없음)
- VM 1대: 1 OCPU, 1GB RAM (워커 돌리기 충분)

---

## 1단계: Oracle Cloud 가입

1. https://cloud.oracle.com 접속
2. **Start for Free** 클릭
3. **Home Region**: `South Korea North (Chuncheon)` 선택 (가입 후 변경 불가!)
4. 가입 완료 (신용카드 필요하지만 Always Free는 과금 안 됨)

> ⚠️ Home Region을 한국(Chuncheon)으로 반드시 선택. 키움 API가 한국 IP를 요구할 수 있음.

---

## 2단계: VM 인스턴스 생성

1. Oracle Cloud Console → **Compute** → **Instances** → **Create Instance**
2. 설정:

| 항목 | 값 |
|------|-----|
| Name | `quant-worker` |
| Image | **Oracle Linux 8** (또는 Ubuntu 22.04) |
| Shape | **VM.Standard.E2.1.Micro** (Always Free) |
| OCPU | 1 |
| Memory | 1 GB |

3. **Add SSH keys** → 공개키 업로드 또는 자동 생성 (프라이빗 키 다운로드 필수!)
4. **Create** 클릭 → 인스턴스 생성 (2~3분)

---

## 3단계: 네트워크 설정

워커는 **나가는 연결만** 사용 (키움 API, 텔레그램 등). 들어오는 포트 오픈 불필요.

기본 설정으로 아웃바운드 통신이 허용되므로 **추가 설정 없음**.

> SSH 접속용 포트 22는 기본 열려있음.

---

## 4단계: SSH 접속 + 환경 설정

```bash
# SSH 접속 (Windows: PowerShell 또는 Git Bash)
ssh -i <프라이빗키.pem> opc@<퍼블릭IP>

# Ubuntu 이미지인 경우
ssh -i <프라이빗키.pem> ubuntu@<퍼블릭IP>
```

### Python 설치

```bash
# Oracle Linux 8
sudo dnf install python39 python39-pip git -y
sudo alternatives --set python3 /usr/bin/python3.9

# Ubuntu 22.04
sudo apt update && sudo apt install python3.10 python3.10-venv python3-pip git -y
```

### 프로젝트 클론

```bash
cd ~
git clone <레포지토리URL> quant_trading
cd quant_trading
```

### 가상환경 + 패키지 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# requests 패키지 (뉴스/DART 클라이언트에서 사용)
pip install requests
```

### .env 파일 생성

```bash
cp .env.example .env
nano .env   # 또는 vi .env
```

실제 값 입력:
```env
KIWOOM_APP_KEY=실제값
KIWOOM_APP_SECRET=실제값
KIWOOM_ACCOUNT_NO=실제값
KIWOOM_BASE_URL=https://api.kiwoom.com
KIWOOM_ALLOW_TRADE_EXECUTION=false

ANTHROPIC_API_KEY=실제값
TELEGRAM_BOT_TOKEN=실제값
TELEGRAM_CHAT_ID=실제값
DART_API_KEY=실제값
NAVER_CLIENT_ID=실제값
NAVER_CLIENT_SECRET=실제값

AUTO_TRADE=false
```

> ⚠️ 처음에는 `KIWOOM_ALLOW_TRADE_EXECUTION=false`, `AUTO_TRADE=false`로 시작.
> 신호 감지 + 텔레그램 알림만 확인한 후 단계적으로 켜기.

---

## 5단계: 테스트 실행

```bash
cd ~/quant_trading
source .venv/bin/activate

# 장 시간 무관 즉시 테스트
python -m worker.main --test
```

정상 동작 확인 사항:
- 로그에 `워커 시작` 출력
- 텔레그램에 `✅ Quant Trading 워커가 시작되었습니다.` 수신
- 종목별 조건 체크 로그 출력
- `Ctrl+C`로 종료 → 텔레그램에 종료 메시지 수신

---

## 6단계: systemd 서비스 등록 (자동 실행)

VM 재부팅/장애 시 자동 재시작되도록 systemd 서비스를 등록합니다.

### 서비스 파일 생성

```bash
sudo nano /etc/systemd/system/quant-worker.service
```

내용 (Oracle Linux / `opc` 유저 기준):

```ini
[Unit]
Description=Quant Trading Worker
After=network.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/quant_trading
ExecStart=/home/opc/quant_trading/.venv/bin/python -m worker.main
Restart=always
RestartSec=10

# 환경변수
EnvironmentFile=/home/opc/quant_trading/.env

# 로그
StandardOutput=append:/home/opc/quant_trading/logs/worker.log
StandardError=append:/home/opc/quant_trading/logs/worker.log

[Install]
WantedBy=multi-user.target
```

> Ubuntu 이미지인 경우 `User=ubuntu`, 경로도 `/home/ubuntu/...`로 변경.

### 서비스 등록 + 시작

```bash
# logs 디렉토리 확인
mkdir -p ~/quant_trading/logs

# 서비스 등록
sudo systemctl daemon-reload
sudo systemctl enable quant-worker    # 부팅 시 자동 시작
sudo systemctl start quant-worker     # 지금 시작

# 상태 확인
sudo systemctl status quant-worker
```

### 자주 쓰는 명령

```bash
# 로그 실시간 확인
tail -f ~/quant_trading/logs/worker.log

# 서비스 재시작
sudo systemctl restart quant-worker

# 서비스 중지
sudo systemctl stop quant-worker

# 최근 로그 (systemd)
sudo journalctl -u quant-worker -f
```

### 실제 운영 서버(`quant-worker`) 기준 차이

위 예시는 Oracle Linux / `opc` 유저 기준입니다. 현재 운영 중인 VM은 Ubuntu / `ubuntu` 유저이며, 등록된 unit이 예시와 다음과 같이 다릅니다.

| 항목 | 문서 예시 | 실제 `/etc/systemd/system/quant-worker.service` |
|---|---|---|
| `User` | `opc` | `ubuntu` |
| `WorkingDirectory` | `/home/opc/quant_trading` | `/home/ubuntu/quant-trading` (밑줄이 아니라 하이픈) |
| `ExecStart` | `.venv/bin/python -m worker.main` | `/home/ubuntu/quant-trading/.venv/bin/python worker/main.py` |
| `Restart` | `always` / `RestartSec=10` | `on-failure` / `RestartSec=15` |
| `EnvironmentFile` | `.env` 지정 | 미사용 (워커가 직접 `.env` 로드) |
| 기타 | — | `KillMode=control-group`, `TimeoutStopSec=30` |

로그 경로도 실제로는 `/home/ubuntu/quant-trading/logs/worker.log`입니다. 이 문서의 `~/quant_trading/...` 경로 예시를 그대로 복사하지 마십시오.

같은 VM에서 `stock-coach` 백엔드/프론트엔드가 PM2(`pm2-ubuntu.service` -> `stockcoach-api`, `stockcoach-web`)로 함께 운영됩니다. 상세는 `stock-coach` 저장소 README의 운영 실행 섹션을 참고하십시오.

### 현재 자동실행 상태: 비활성 (2026-09-08 기준)

외부 API 키 만료로 정상 동작이 불가능해 워커 자동실행을 중단했습니다.

```bash
sudo systemctl disable --now quant-worker.service   # 부팅 자동실행 해제 + 즉시 중단
```

같은 시점에 stock-coach 쪽 `pm2-ubuntu.service`도 함께 비활성화했습니다.

#### 재개 절차

1. `.env`의 만료된 키를 먼저 갱신합니다. 키움(`KIWOOM_*`)은 공통 필수이고, AI 키는 아래 브랜치별 필요 키를 확인하십시오.
2. 주문 안전장치(`AUTO_TRADE`)가 의도한 값인지 확인합니다.
3. 그 다음 서비스를 다시 켭니다.

```bash
sudo systemctl enable --now quant-worker.service
sudo systemctl status quant-worker.service
tail -f /home/ubuntu/quant-trading/logs/worker.log
```

`Restart=on-failure`이므로 키가 만료된 상태로 enable 하면 15초 간격으로 재시작을 반복하며 로그만 쌓입니다. 키 갱신 전에는 enable 하지 마십시오.

#### 브랜치별 필요 키 (주의)

이 저장소는 판단 엔진이 다른 두 브랜치를 유지합니다. 브랜치를 전환해 운용할 경우 **필요한 AI 키가 달라진다는 점**에 주의하십시오.

| 브랜치 | 판단 설정 (`config/worker.yaml`) | 필수 AI 키 |
|---|---|---|
| `main` (일반) | `use_claude_api: true`, `claude_model` | `ANTHROPIC_API_KEY` |
| `feature/agent-mode` (에이전트) | `use_agent_mode: true`, `judgment_agent_model` / `research_agent_model` | `OPENAI_API_KEY` (OpenAI 호환 API) |

현재 서버에 체크아웃된 브랜치는 `feature/agent-mode`이고 `use_agent_mode: true`이므로, 재개하려면 `OPENAI_API_KEY` 갱신이 필수입니다. `use_agent_mode: false`로 내리면 레거시 경로(`ANTHROPIC_API_KEY`가 있으면 Claude, 없으면 OpenAI 폴백)로 동작합니다.

---

## 7단계: 코드 업데이트 방법

```bash
cd ~/quant_trading
source .venv/bin/activate

# 코드 업데이트
git pull

# 패키지 변경 시
pip install -r requirements.txt

# 워커 재시작
sudo systemctl restart quant-worker
```

---

## 운영 체크리스트

### 일일 확인
- [ ] 텔레그램에 워커 시작 메시지 오는지 (매일 아침 or 서비스 재시작 시)
- [ ] 신호 알림이 정상 발송되는지
- [ ] `tail -f ~/quant_trading/logs/worker.log`에 에러 없는지

### 주간 확인
- [ ] `df -h` — 디스크 여유 공간 (1GB RAM이라 로그 관리 중요)
- [ ] `free -m` — 메모리 사용량
- [ ] 오래된 로그 정리: `ls -la ~/quant_trading/logs/`

### 로그 정리 (자동, 선택사항)

```bash
# crontab에 추가: 7일 이상 된 로그 자동 삭제
crontab -e
```

추가할 줄:
```
0 2 * * 0 find /home/opc/quant_trading/logs -name "*.log.*" -mtime +7 -delete
```

---

## 트러블슈팅

### 워커가 시작 안 됨
```bash
# 수동 실행으로 에러 확인
cd ~/quant_trading && source .venv/bin/activate
python -m worker.main --test
```

### 키움 API 연결 실패
- Oracle Cloud 서울 리전인지 확인 (`curl ifconfig.me` → 한국 IP인지)
- `.env`의 `KIWOOM_BASE_URL`이 정확한지
- 키움 API 키/시크릿이 유효한지

### 메모리 부족 (1GB)
```bash
# 스왑 파일 추가 (2GB)
sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 영구 적용
echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
```

> 1GB RAM에서 워커 + SQLite는 충분하지만, Claude API 호출이 겹치면 스왑이 도움됨.

### DB 파일 위치
```bash
# SQLite DB는 서버에서 자동 생성
ls -la ~/quant_trading/data/trading.db
```

로컬에서 만든 기존 DB를 사용하려면:
```bash
# 로컬 → 서버 복사
scp -i <키.pem> data/trading.db opc@<IP>:~/quant_trading/data/
```

---

## 비용 요약

| 항목 | 비용 |
|------|------|
| Oracle Cloud VM | **무료** (Always Free) |
| 키움 API | **무료** |
| DART API | **무료** |
| 네이버 뉴스 API | **무료** (일 25,000건) |
| 텔레그램 Bot API | **무료** |
| Claude API | **유료** (~$0.003/신호, 프롬프트 캐싱으로 90% 절감) |

실질적으로 **Claude API 비용만** 발생. 하루 신호 10~20건 기준 ~$0.03~0.06/일.
