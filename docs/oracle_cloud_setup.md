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
