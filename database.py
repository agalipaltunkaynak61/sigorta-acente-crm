import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    event,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# Çevre değişkeninden (Render / Supabase) URL al, yoksa lokal SQLite kullan
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_PATH = Path(__file__).resolve().parent / "acente_crm.db"
    DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Bağlantı argümanları (SQLite için ayrı, PostgreSQL için ayrı)
engine_args = {
    "pool_pre_ping": True,
}

if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {
        "check_same_thread": False,
        "timeout": 30,
    }

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# SQLite için özel ayarlar (Sadece lokalde çalışır, PostgreSQL'de hata vermez)
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _connection_record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


class Musteri(Base):
    __tablename__ = "musteriler"

    id = Column(Integer, primary_key=True, index=True)
    ad = Column(String(100), nullable=False)
    soyad = Column(String(100), nullable=False)
    telefon = Column(String(20), nullable=True)
    email = Column(String(255), nullable=True)
    tc_kimlik = Column(String(20), nullable=True, index=True)
    adres = Column(Text, nullable=True)
    notlar = Column(Text, nullable=True)
    portfoy_sorumlusu = Column(String(100), nullable=True, index=True)  # Çalışan / Danışman Takibi için
    created_at = Column(DateTime, default=datetime.utcnow)

    policeler = relationship(
        "Police",
        back_populates="musteri",
        cascade="all, delete-orphan",
    )


class Police(Base):
    __tablename__ = "policeler"

    id = Column(Integer, primary_key=True, index=True)
    musteri_id = Column(Integer, ForeignKey("musteriler.id"), nullable=False, index=True)
    police_no = Column(String(80), nullable=False, index=True)
    sigorta_turu = Column(String(120), nullable=False, index=True)
    sigorta_sirketi = Column(String(150), nullable=True)
    islem_turu = Column(String(50), nullable=False, default="Yeni Poliçe")
    pdf_dosya_adi = Column(String(255), nullable=True)
    baslangic_tarihi = Column(Date, nullable=False)
    bitis_tarihi = Column(Date, nullable=False, index=True)
    prim = Column(Float, nullable=True)
    net_prim = Column(Float, nullable=True)
    brut_prim = Column(Float, nullable=True)
    durum = Column(String(30), nullable=False, default="aktif")
    aciklama = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    musteri = relationship("Musteri", back_populates="policeler")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)