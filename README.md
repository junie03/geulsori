# 자막 통화 — 청각장애인을 위한 AI 자막·음성 변환 통화 서비스

인공지능서비스개발II 11조 팀 프로젝트 (기획서 6장 시스템 구조 기준)

## 기능
- **통화**: 11자리 번호 배정, 발신·수신·거절·취소·끊기, 통화 시간 표시
- **자막 모드 ↔ 목소리 모드**: 상대 말은 실시간 자막(중간 → 확정)으로, 입력한 글은 음성으로 전달
- **목소리 모드끼리**: 자막 없이 양방향 일반 음성 통화(WebRTC), 음소거
- **접근성**: 전화가 오면 진동 + 화면 깜빡임(진동은 안드로이드 크롬), 글자 크기 조절, 빠른 문장, 입력 중 표시, 중요 정보(전화번호·금액·날짜·시간·인원) 강조
- **AI 보조(Claude)**: 통화 준비, 답변 3개 추천, 짧은 말 다듬기, 통화 요약·목적 달성 판단
- **보이스피싱 경고**: 의심 표현을 바로 표시하고 AI가 대화 전체로 판단(주의/위험), 위험이면 빨간 경고 + 진동
- **일반 전화(Twilio, 실험 중)**: 앱이 없는 휴대폰에 자막 통화로 걸기. 체험 계정에서는 받는 순간 끊기는 문제가 있어 유료 계정에서 확인 예정

## 구조
| 경로 | 내용 |
|---|---|
| `server/main.py` | FastAPI 서버: 번호 배정, 호출·받기·거절·끊기, WebSocket(`/ws`) 중계(자막·입력 글·입력 중 표시·WebRTC 신호), AI REST API, Twilio 일반 전화 중계(`/twilio/*`) |
| `server/ai.py` | Claude API: 통화 준비(`/api/prep`), 답변 추천(`/api/suggest`), 짧은 말 다듬기(`/api/refine`), 요약·목적 달성(`/api/summary`), 보이스피싱 판단(`/api/scam`) |
| `static/index.html` | 프론트엔드 단일 파일: 설정·홈(키패드)·발신/수신·통화·종료 화면, STT/TTS(Web Speech API), 음성 통화(WebRTC), 중요 정보 강조, 보이스피싱 경고 |

## 실행
```bash
pip install -r requirements.txt
cp .env.example .env   # ANTHROPIC_API_KEY 입력 (없으면 AI 기능만 꺼짐), Twilio 키는 선택
python -m uvicorn server.main:app --port 8000
```
- PC 크롬에서 `http://localhost:8000` 접속 (localhost는 마이크 허용됨)
- 휴대폰 테스트: `ngrok http 8000`으로 만든 HTTPS 주소로 접속 (휴대폰 브라우저는 HTTPS에서만 마이크 허용)
- 일반 전화는 Twilio 키 3개가 있고 ngrok 같은 공개 HTTPS 주소로 접속했을 때만 켜짐

## 배포 (Render)
- 주소: **https://geulsori.onrender.com** (HTTPS라 휴대폰에서도 마이크 사용 가능)
- `main` 브랜치에 push하면 Render가 자동으로 다시 배포한다. 설정은 [`render.yaml`](render.yaml)에 있다.
- 처음 만들 때: Render 대시보드 → New → Blueprint → 이 저장소 선택. 또는 Web Service를 만들고 아래처럼 입력
  - Build Command: `pip install -r requirements.txt`
  - Start Command: `uvicorn server.main:app --host 0.0.0.0 --port $PORT`
- `ANTHROPIC_API_KEY`는 Render 대시보드 → Environment에서 직접 입력한다(저장소에 넣지 않음).
- 무료 플랜은 15분간 접속이 없으면 잠들고, 다음 접속 때 깨어나는 데 약 1분 걸린다. 시연 1~2분 전에 주소를 미리 열어 둔다.
- 서버를 다시 시작하면 메모리에 있던 번호·접속 정보가 초기화된다.

## WebSocket 메시지
| 보냄 | 받음 |
|---|---|
| `register {name, mode, number?}` | `registered {number}` |
| `call {to}` | 발신자 `ringing {peer}` / 수신자 `incoming {peer}` |
| `phone_call {to}` (일반 전화) | `ringing {peer}` → `connected` / `error {message}` |
| `accept` / `reject` / `hangup` | `connected {peer, role}` / `ended {reason}` |
| `caption {text, final}` · `message {text}` · `typing {on}` · `rtc {data}` | 상대에게 그대로 중계 (+`from`) |
