from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify

DEFAULT_STICKY_THRESHOLD = 0.05
STICKY_THRESHOLD_KEY = "sticky_threshold"


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StickyLedgerEntry(Base):
    """可疑册：检出即冻结，之后改门槛不动旧行。"""

    __tablename__ = "sticky_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    threshold: Mapped[float] = mapped_column(Float)
    prev_reading_id: Mapped[int] = mapped_column()
    curr_reading_id: Mapped[int] = mapped_column(unique=True)
    diff: Mapped[float] = mapped_column(Float)
    created_by: Mapped[str] = mapped_column(String(64))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ThresholdIn(BaseModel):
    threshold: float = Field(ge=0)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


def get_threshold(db: Session) -> float:
    row = db.get(AppSetting, STICKY_THRESHOLD_KEY)
    return row.value if row is not None else DEFAULT_STICKY_THRESHOLD


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    sticky = None
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        prev = (
            db.query(Reading)
            .filter(Reading.site == row.site, Reading.id != row.id)
            .order_by(Reading.created_at.desc(), Reading.id.desc())
            .first()
        )
        if prev is not None:
            threshold = get_threshold(db)
            diff = round(abs(row.ch4_pct - prev.ch4_pct), 6)
            if diff < threshold:
                detected_at = datetime.now(timezone.utc)
                db.add(
                    StickyLedgerEntry(
                        site=row.site,
                        threshold=threshold,
                        prev_reading_id=prev.id,
                        curr_reading_id=row.id,
                        diff=diff,
                        created_by=user["username"],
                        detected_at=detected_at,
                    )
                )
                db.commit()
                sticky = {
                    "site": row.site,
                    "prev_reading_id": prev.id,
                    "curr_reading_id": row.id,
                    "threshold": threshold,
                    "diff": diff,
                    "detected_at": detected_at.isoformat(),
                }
        payload = {
            "id": row.id,
            "site": row.site,
            "ch4_pct": row.ch4_pct,
            "level": row.level,
            "note": row.note,
            "sticky": sticky,
        }
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.get("/api/sticky/threshold")
def read_threshold(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        return {"threshold": get_threshold(db)}
    finally:
        db.close()


@app.put("/api/sticky/threshold")
def update_threshold(body: ThresholdIn, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        row = db.get(AppSetting, STICKY_THRESHOLD_KEY)
        if row is None:
            row = AppSetting(key=STICKY_THRESHOLD_KEY, value=body.threshold, updated_by=user["username"], updated_at=now)
            db.add(row)
        else:
            row.value = body.threshold
            row.updated_by = user["username"]
            row.updated_at = now
        db.commit()
        return {"threshold": body.threshold}
    finally:
        db.close()


def _sticky_pairs(db: Session, threshold: float) -> list[dict]:
    """按测点时间顺序取相邻两笔，浓度差绝对值小于当前门槛即为现场可疑。"""

    rows = db.query(Reading).order_by(Reading.site.asc(), Reading.created_at.asc(), Reading.id.asc()).all()
    by_site: dict[str, list[Reading]] = {}
    for r in rows:
        by_site.setdefault(r.site, []).append(r)
    pairs = []
    for site, items in by_site.items():
        for prev, curr in zip(items, items[1:]):
            diff = round(abs(curr.ch4_pct - prev.ch4_pct), 6)
            if diff < threshold:
                pairs.append(
                    {
                        "site": site,
                        "prev_reading_id": prev.id,
                        "curr_reading_id": curr.id,
                        "prev_ch4_pct": prev.ch4_pct,
                        "curr_ch4_pct": curr.ch4_pct,
                        "diff": diff,
                        "prev_created_at": prev.created_at.isoformat(),
                        "curr_created_at": curr.created_at.isoformat(),
                    }
                )
    pairs.sort(key=lambda p: (p["curr_created_at"], p["curr_reading_id"]), reverse=True)
    return pairs


@app.get("/api/sticky/live")
def sticky_live(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        threshold = get_threshold(db)
        return {"threshold": threshold, "pairs": _sticky_pairs(db, threshold)}
    finally:
        db.close()


@app.get("/api/sticky/ledger")
def sticky_ledger(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        readings = {r.id: r for r in db.query(Reading).all()}
        entries = db.query(StickyLedgerEntry).order_by(StickyLedgerEntry.id.desc()).all()
        result = []
        for e in entries:
            prev = readings.get(e.prev_reading_id)
            curr = readings.get(e.curr_reading_id)
            result.append(
                {
                    "id": e.id,
                    "site": e.site,
                    "threshold": e.threshold,
                    "prev_reading_id": e.prev_reading_id,
                    "curr_reading_id": e.curr_reading_id,
                    "prev_ch4_pct": prev.ch4_pct if prev is not None else None,
                    "curr_ch4_pct": curr.ch4_pct if curr is not None else None,
                    "diff": e.diff,
                    "created_by": e.created_by,
                    "detected_at": e.detected_at.isoformat(),
                }
            )
        return {"entries": result}
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
