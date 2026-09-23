from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, ForeignKey, String, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


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

DEFAULT_THRESHOLD = 0.05
THRESHOLD_KEY = "sticky_threshold"
BACKFILL_KEY = "sticky_backfilled"


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


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(80))


class StickyRecord(Base):
    """可疑册：一旦写入，门槛与差值按检出当时冻结，之后不再改变。"""

    __tablename__ = "sticky_records"
    __table_args__ = (
        UniqueConstraint("prev_reading_id", "curr_reading_id", name="uq_sticky_pair"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    prev_reading_id: Mapped[int] = mapped_column(ForeignKey("readings.id"))
    curr_reading_id: Mapped[int] = mapped_column(ForeignKey("readings.id"))
    prev_ch4: Mapped[float] = mapped_column(Float)
    curr_ch4: Mapped[float] = mapped_column(Float)
    diff: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
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
    row = db.get(Setting, THRESHOLD_KEY)
    return float(row.value) if row is not None else DEFAULT_THRESHOLD


def sticky_pair(db: Session, prev: Reading, curr: Reading, threshold: float) -> dict | None:
    diff = abs(curr.ch4_pct - prev.ch4_pct)
    if diff >= threshold:
        return None
    return {
        "site": curr.site,
        "prev_reading_id": prev.id,
        "curr_reading_id": curr.id,
        "prev_ch4": prev.ch4_pct,
        "curr_ch4": curr.ch4_pct,
        "diff": round(diff, 4),
        "threshold": threshold,
        "detected_at": curr.created_at,
    }


def live_suspicious(db: Session, threshold: float) -> list[dict]:
    """现场可疑页：始终按当前门槛对全部读数重新计算，不落档。"""
    rows = db.query(Reading).order_by(Reading.site, Reading.id).all()
    last: dict[str, Reading] = {}
    pairs: list[dict] = []
    for row in rows:
        prev = last.get(row.site)
        if prev is not None:
            pair = sticky_pair(db, prev, row, threshold)
            if pair is not None:
                pairs.append(pair)
        last[row.site] = row
    pairs.sort(key=lambda p: p["curr_reading_id"], reverse=True)
    return pairs


def archive_pair(db: Session, prev: Reading, curr: Reading, threshold: float) -> StickyRecord | None:
    """落可疑册：冻结检出当时的门槛与差值。同一对只入册一次。"""
    diff = abs(curr.ch4_pct - prev.ch4_pct)
    if diff >= threshold:
        return None
    exists = (
        db.query(StickyRecord)
        .filter(
            StickyRecord.prev_reading_id == prev.id,
            StickyRecord.curr_reading_id == curr.id,
        )
        .first()
    )
    if exists is not None:
        return exists
    record = StickyRecord(
        site=curr.site,
        prev_reading_id=prev.id,
        curr_reading_id=curr.id,
        prev_ch4=prev.ch4_pct,
        curr_ch4=curr.ch4_pct,
        diff=round(diff, 4),
        threshold=threshold,
        detected_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.commit()
    return record


def serialize_record(record: StickyRecord) -> dict:
    return {
        "id": record.id,
        "site": record.site,
        "prev_reading_id": record.prev_reading_id,
        "curr_reading_id": record.curr_reading_id,
        "prev_ch4": record.prev_ch4,
        "curr_ch4": record.curr_ch4,
        "diff": record.diff,
        "threshold": record.threshold,
        "detected_at": record.detected_at.isoformat(),
    }


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
        # 功能首次上线：把已存在的同测点相邻对按当时门槛补入可疑册，仅执行一次
        if db.get(Setting, BACKFILL_KEY) is None:
            threshold = get_threshold(db)
            archived = {
                (r.prev_reading_id, r.curr_reading_id)
                for r in db.query(StickyRecord).all()
            }
            last: dict[str, Reading] = {}
            for row in db.query(Reading).order_by(Reading.site, Reading.id).all():
                prev = last.get(row.site)
                if prev is not None and (prev.id, row.id) not in archived:
                    diff = abs(row.ch4_pct - prev.ch4_pct)
                    if diff < threshold:
                        db.add(
                            StickyRecord(
                                site=row.site,
                                prev_reading_id=prev.id,
                                curr_reading_id=row.id,
                                prev_ch4=prev.ch4_pct,
                                curr_ch4=row.ch4_pct,
                                diff=round(diff, 4),
                                threshold=threshold,
                                detected_at=datetime.now(timezone.utc),
                            )
                        )
                last[row.site] = row
            db.add(Setting(key=BACKFILL_KEY, value="1"))
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
                "created_at": r.created_at.isoformat(),
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
        threshold = get_threshold(db)
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
            .filter(Reading.site == row.site, Reading.id < row.id)
            .order_by(Reading.id.desc())
            .first()
        )
        if prev is not None:
            record = archive_pair(db, prev, row, threshold)
            if record is not None:
                sticky = serialize_record(record)
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
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
def update_threshold(body: ThresholdIn, _user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Setting, THRESHOLD_KEY)
        if row is None:
            row = Setting(key=THRESHOLD_KEY, value=str(body.threshold))
            db.add(row)
        else:
            row.value = str(body.threshold)
        db.commit()
        return {"threshold": body.threshold}
    finally:
        db.close()


@app.get("/api/sticky/live")
def sticky_live(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        threshold = get_threshold(db)
        pairs = live_suspicious(db, threshold)
        return {"threshold": threshold, "pairs": pairs}
    finally:
        db.close()


@app.get("/api/sticky/archive")
def sticky_archive(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(StickyRecord).order_by(StickyRecord.id.desc()).all()
        return [serialize_record(r) for r in rows]
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
