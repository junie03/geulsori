"""자막 통화 서버 — 번호 배정, 호출/받기/거절/끊기, 자막·입력 글·WebRTC 신호 중계, AI 요청 대행, 일반 전화(Twilio) 중계."""

import asyncio
import os
import random
import secrets
from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import ai  # noqa: E402  (.env를 먼저 읽은 뒤 API 키 확인)

STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="자막 통화")

# 상대에게 그대로 전달하는 메시지 종류
RELAY_TYPES = {"caption", "message", "typing", "rtc"}


class User:
    def __init__(self, ws: WebSocket, number: str, name: str, mode: str):
        self.ws = ws
        self.number = number
        self.name = name
        self.mode = mode  # "caption"(자막 모드) | "voice"(목소리 모드)
        self.peer: "User | None" = None  # 통화 중인 상대
        self.pending: "User | None" = None  # 호출 중(아직 받기 전)인 상대

    def info(self) -> dict:
        return {"number": self.number, "name": self.name, "mode": self.mode}

    async def send(self, msg: dict) -> None:
        try:
            await self.ws.send_json(msg)
        except Exception:
            pass


users: dict[str, User] = {}


def new_number() -> str:
    while True:
        n = "010" + "".join(random.choices("0123456789", k=8))
        if n not in users:
            return n


async def end_call(user: User, reason: str) -> None:
    """통화 또는 호출 중인 상대와의 연결을 정리하고 상대에게 알린다."""
    other = user.peer or user.pending
    user.peer = user.pending = None
    if other:
        other.peer = other.pending = None
        await other.send({"type": "ended", "reason": reason})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    me: User | None = None
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")

            if t == "register":
                number = msg.get("number")
                # 새로고침 시 같은 번호를 다시 쓰고, 이미 사용 중이면 새 번호 배정
                if not (isinstance(number, str) and len(number) == 11 and number.isdigit()) or number in users:
                    number = new_number()
                mode = msg.get("mode") if msg.get("mode") in ("caption", "voice") else "caption"
                me = User(ws, number, (msg.get("name") or "이름 없음")[:20], mode)
                users[number] = me
                await me.send({"type": "registered", "number": number})
                continue

            if me is None:
                await ws.send_json({"type": "error", "message": "먼저 등록이 필요합니다."})
                continue

            if t == "call":
                target = users.get(str(msg.get("to", "")).replace("-", ""))
                if target is None or target is me:
                    await me.send({"type": "error", "message": "연결할 수 없는 번호예요."})
                elif target.peer or target.pending:
                    await me.send({"type": "error", "message": "상대방이 통화 중이에요."})
                else:
                    me.pending, target.pending = target, me
                    await target.send({"type": "incoming", "peer": me.info()})
                    await me.send({"type": "ringing", "peer": target.info()})

            elif t == "phone_call":
                base = os.getenv("PUBLIC_URL") or f"https://{ws.headers.get('host', '')}"
                await start_phone_call(me, str(msg.get("to", "")), base.rstrip("/"))

            elif t == "accept" and me.pending:
                caller = me.pending
                me.pending = caller.pending = None
                me.peer, caller.peer = caller, me
                await caller.send({"type": "connected", "peer": me.info(), "role": "caller"})
                await me.send({"type": "connected", "peer": caller.info(), "role": "callee"})

            elif t == "reject" and me.pending:
                await end_call(me, "rejected")

            elif t == "hangup":
                await end_call(me, "hangup")

            elif t in RELAY_TYPES and me.peer:
                await me.peer.send({**msg, "from": me.number})

    except WebSocketDisconnect:
        pass
    finally:
        if me:
            await end_call(me, "disconnected")
            if users.get(me.number) is me:
                del users[me.number]


# ---------- 일반 전화(Twilio) ----------
# 앱 사용자(자막 모드)가 앱이 없는 일반 전화에 건다. 상대 말은 Twilio 음성 인식으로 자막이 되고,
# 사용자가 보낸 글은 Twilio 음성 합성으로 읽어 준다. 앱 쪽에서는 목소리 모드 상대와 똑같이 보인다.

SAY = 'language="ko-KR" voice="Polly.Seoyeon"'
HANGUP = "<Response><Hangup/></Response>"
_twilio = None


def twilio_enabled() -> bool:
    return all(os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"))


def twilio():
    global _twilio
    if _twilio is None:
        from twilio.rest import Client
        _twilio = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    return _twilio


def to_e164(number: str) -> str | None:
    """010-1234-5678 → +821012345678"""
    digits = "".join(c for c in number if c.isdigit())
    if number.strip().startswith("+") and len(digits) >= 8:
        return "+" + digits
    if digits.startswith("0") and 9 <= len(digits) <= 11:
        return "+82" + digits[1:]
    return None


class PhonePeer:
    """일반 전화 상대. User와 같은 모양(number, name, mode, peer, pending, info, send)이라 end_call·중계를 그대로 쓴다."""

    def __init__(self, user: User, number: str, base: str):
        self.id = secrets.token_urlsafe(12)
        self.number = number
        self.name = "일반 전화"
        self.mode = "phone"
        self.base = base
        self.sid: str | None = None
        self.answered = False
        self.peer: User | None = None
        self.pending: User | None = user

    def info(self) -> dict:
        return {"number": self.number, "name": self.name, "mode": self.mode}

    def url(self, path: str) -> str:
        return f"{self.base}{path}?cid={self.id}"

    def gather(self, say: str = "") -> str:
        """할 말을 읽고, 상대가 말하기를 기다리는 TwiML"""
        s = f"<Say {SAY}>{escape(say)}</Say>" if say else ""
        return (
            f'<Response>{s}<Gather input="speech" language="ko-KR" speechTimeout="auto" timeout="20" '
            f'actionOnEmptyResult="true" action="{escape(self.url("/twilio/gather"))}" '
            f'partialResultCallback="{escape(self.url("/twilio/partial"))}"/></Response>'
        )

    async def send(self, msg: dict) -> None:
        t = msg.get("type")
        try:
            if t == "message" and self.sid and self.answered:
                # 진행 중인 통화의 TwiML을 바꿔 글을 바로 읽어 준다
                await asyncio.to_thread(twilio().calls(self.sid).update, twiml=self.gather(msg["text"]))
            elif t == "ended" and self.sid:
                status = "completed" if self.answered else "canceled"
                await asyncio.to_thread(twilio().calls(self.sid).update, status=status)
        except Exception as e:
            print(f"Twilio 요청 실패: {e}")
        if t == "ended":
            phone_calls.pop(self.id, None)


phone_calls: dict[str, PhonePeer] = {}


async def start_phone_call(me: User, raw: str, base: str) -> None:
    to = to_e164(raw)
    if not twilio_enabled():
        return await me.send({"type": "error", "message": "일반 전화 기능이 꺼져 있어요. 서버 .env에 Twilio 키를 설정하세요."})
    if me.mode != "caption":
        return await me.send({"type": "error", "message": "일반 전화는 자막 모드에서만 걸 수 있어요."})
    if not to:
        return await me.send({"type": "error", "message": "전화번호를 확인해 주세요."})
    if "localhost" in base or "127.0.0.1" in base:
        return await me.send({"type": "error", "message": "일반 전화는 ngrok 같은 공개 주소로 접속했을 때만 걸 수 있어요."})
    if me.peer or me.pending:
        return

    shown = "0" + to[3:] if to.startswith("+82") else to.lstrip("+")
    p = PhonePeer(me, shown, base)
    phone_calls[p.id] = p
    me.pending = p
    await me.send({"type": "ringing", "peer": p.info()})
    try:
        call = await asyncio.to_thread(
            twilio().calls.create,
            to=to, from_=os.getenv("TWILIO_PHONE_NUMBER"),
            url=p.url("/twilio/voice"), status_callback=p.url("/twilio/status"),
        )
        p.sid = call.sid
    except Exception as e:
        phone_calls.pop(p.id, None)
        me.pending = None
        await me.send({"type": "error", "message": f"전화를 걸 수 없어요: {getattr(e, 'msg', e)}"})


async def twilio_request(request: Request) -> tuple[PhonePeer | None, dict]:
    """Twilio가 보낸 요청인지 서명으로 확인하고, 해당 통화와 폼 값을 돌려준다."""
    from twilio.request_validator import RequestValidator

    form = dict(await request.form())
    p = phone_calls.get(request.query_params.get("cid", ""))
    if p:
        url = f"{p.base}{request.url.path}?{request.url.query}"
        sig = request.headers.get("x-twilio-signature", "")
        if not RequestValidator(os.getenv("TWILIO_AUTH_TOKEN")).validate(url, form, sig):
            print(f"[twilio] 서명 불일치: {request.url.path}")
            p = None
    return p, form


def twiml(xml: str) -> Response:
    return Response(xml, media_type="application/xml")


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """상대가 전화를 받았을 때"""
    p, _ = await twilio_request(request)
    if not p or not p.pending:
        return twiml(HANGUP)
    user = p.pending
    user.pending = p.pending = None
    user.peer, p.peer = p, user
    p.answered = True
    await user.send({"type": "connected", "peer": p.info(), "role": "caller"})
    return twiml(p.gather(
        f"안녕하세요. {user.name}님이 자막 통화 서비스로 거신 전화입니다. "
        "상대방은 말씀을 자막으로 보고 글로 답하니, 천천히 말씀해 주세요."
    ))


@app.post("/twilio/partial")
async def twilio_partial(request: Request):
    """상대가 말하는 중간 결과 → 점선 자막"""
    p, form = await twilio_request(request)
    text = (form.get("UnstableSpeechResult") or form.get("StableSpeechResult") or "").strip()
    if p and p.peer and text:
        await p.peer.send({"type": "caption", "text": text, "final": False, "from": p.number})
    return Response(status_code=204)


@app.post("/twilio/gather")
async def twilio_gather(request: Request):
    """상대 말 한 마디가 끝났을 때 → 확정 자막, 다시 듣기"""
    p, form = await twilio_request(request)
    if not p or not p.peer:
        return twiml(HANGUP)
    text = (form.get("SpeechResult") or "").strip()
    if text:
        await p.peer.send({"type": "caption", "text": text, "final": True, "from": p.number})
    return twiml(p.gather())


@app.post("/twilio/status")
async def twilio_status(request: Request):
    """통화가 끝났을 때(상대가 끊음, 안 받음, 실패)"""
    p, form = await twilio_request(request)
    if p:
        phone_calls.pop(p.id, None)
        status = form.get("CallStatus", "")
        reason = "hangup" if p.answered else ("rejected" if status in ("busy", "no-answer", "canceled") else "disconnected")
        await end_call(p, reason)
    return Response(status_code=204)


# ---------- AI API ----------

class Line(BaseModel):
    who: str  # "me" | "peer"
    text: str


class SuggestReq(BaseModel):
    history: list[Line]
    purpose: str = ""


class RefineReq(BaseModel):
    text: str
    history: list[Line] = []


class PrepReq(BaseModel):
    purpose: str


class SummaryReq(BaseModel):
    history: list[Line]
    purpose: str = ""


class ScamReq(BaseModel):
    history: list[Line]


def _require_ai() -> None:
    if not ai.enabled():
        raise HTTPException(503, "AI 기능이 꺼져 있어요. 서버 .env에 ANTHROPIC_API_KEY를 설정하세요.")


@app.get("/api/config")
async def config():
    return {"ai": ai.enabled(), "phone": twilio_enabled()}


@app.post("/api/suggest")
async def suggest(req: SuggestReq):
    _require_ai()
    return {"suggestions": await ai.suggest(req.history, req.purpose)}


@app.post("/api/refine")
async def refine(req: RefineReq):
    _require_ai()
    return {"refined": await ai.refine(req.text, req.history)}


@app.post("/api/scam")
async def scam(req: ScamReq):
    _require_ai()
    return await ai.scam_check(req.history)


@app.post("/api/prep")
async def prep(req: PrepReq):
    _require_ai()
    return await ai.prep(req.purpose)


@app.post("/api/summary")
async def summary(req: SummaryReq):
    _require_ai()
    return await ai.summarize(req.history, req.purpose)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
