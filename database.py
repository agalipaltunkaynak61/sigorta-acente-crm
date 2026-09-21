"""Veri katmanı: bağlantı, modeller, şema geçişi, mükerrer temizliği ve upsert kuralları.

Tüm modüller (app.py, bot.py, yardımcı scriptler) veritabanına yalnızca buradan erişir.
"""
import difflib
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, LargeBinary,
    String, Text, create_engine, event, func, inspect, text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

log = logging.getLogger("crm.database")

VARSAYILAN_DANISMAN = "Muammer Altunkaynak"

# --------------------------------------------------------------------------
# Bağlantı
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{(BASE_DIR / 'acente_crm.db').as_posix()}"
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SQLITE = DATABASE_URL.startswith("sqlite")
engine_args = {"pool_pre_ping": True}
if SQLITE:
    engine_args["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

if SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_ayarlari(baglanti, _kayit):
        cur = baglanti.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


# --------------------------------------------------------------------------
# Metin / kimlik yardımcıları (tüm modüller ortak kullanır)
# --------------------------------------------------------------------------
_TR_ASCII = str.maketrans("İıŞşĞğÜüÖöÇç", "IISSGGUUOOCC")
_TR_BUYUK = str.maketrans({"i": "İ", "ı": "I"})
_FIRMA_EKLERI = re.compile(r"\b(LTD|STI|SANAYI|SAN|TICARET|TIC|AS|A\.S\.|LIMITED|SIRKETI|VE)\b")


def tr_buyuk(s) -> str:
    """Türkçe büyük harf (i→İ, ı→I) ve fazla boşluk temizliği."""
    return " ".join(str(s or "").split()).translate(_TR_BUYUK).upper()


def ascii_buyuk(s) -> str:
    return str(s or "").translate(_TR_ASCII).upper()


def gelismis_firma_temizle(s) -> str:
    """Karşılaştırma anahtarı: ASCII büyük harf, şirket ekleri ve noktalama atılmış."""
    s = _FIRMA_EKLERI.sub("", ascii_buyuk(s).strip())
    return re.sub(r"[^A-Z0-9]", "", s)


def ad_anahtari(ad, soyad="") -> str:
    return gelismis_firma_temizle(f"{ad or ''} {soyad or ''}")


_FIRMA_GUCLU = {  # tek başına firma göstergesi
    "LTD", "LIMITED", "ANONIM", "SIRKETI", "STI", "SANAYI", "TICARET", "INSAAT", "OTOMOTIV", "MIMARLIK", "MUHENDISLIK",
    "YONETIMI", "DERNEGI", "VAKFI", "KOOPERATIFI", "HOLDING", "GIDA", "TEKSTIL", "TURIZM", "NAKLIYAT", "ORGANIZASYON",
    "TAAHHUT", "ECZANESI", "PAZARLAMA", "ILETISIM", "ELEKTRIK", "MAKINA", "SITE", "LTI", "AS",
}
_FIRMA_IZLERI = (  # boşluksuz adın içinde geçmesi yeterli olan (uzun, kişi adında rastlanmayan) kelimeler
    "LIMITED", "ANONIM", "SIRKET", "SANAYI", "TICARET", "INSAAT", "MIMARLIK", "MUHENDISLIK", "OTOMOTIV", "TEKSTIL",
    "HOLDING", "ORGANIZASYON", "TAAHHUT", "PAZARLAMA", "ISLETME", "BELEDIYE", "BAKANLIG", "HIZMETLERI", "AMBALAJ",
    "SISTEM", "MAKINA", "ELEKTRIK", "YONETIM", "DERNEG", "VAKFI", "KOOPERATIF", "TURIZM", "NAKLIYAT", "ECZANE",
    "ILETISIM", "IDARESI", "BASKANLIGI", "LTDSTI", "LTISTI", "TAAH", "LPG",
)
_GENEL_KELIMELER = ("SANAYI", "SAN", "TICARET", "TIC", "VE", "LIMITED", "LTD", "STI", "SIRKETI", "ANONIM", "AS", "DIS",
                    "INSAAT", "TAAHHUT", "PAZARLAMA")
# Kesik yazılmış son ek ("...LIMITED SIR", "...LIMITED S") da genel ek sayılır.
_KIRPIK = sorted({w[:k] for w in _GENEL_KELIMELER for k in range(1, len(w))}, key=len, reverse=True)
_FIRMA_GENEL_EK = re.compile(r"(?:%s)*(?:%s)?" % ("|".join(_GENEL_KELIMELER), "|".join(_KIRPIK)))


def firma_gibi_mi(ad, soyad="", tckn=None) -> bool:
    """Ad firma/kurum gibi mi? (LTD, A.Ş., Sanayi, İnşaat… ya da 10 haneli VKN)"""
    tc = kimlik_temizle(tckn)
    if tc and len(tc) == 10:
        return True
    ascii_ad = ascii_buyuk(f"{ad or ''} {soyad or ''}")
    if _FIRMA_GUCLU & set(re.findall(r"[A-Z0-9]+", ascii_ad)):
        return True
    sikistirilmis = re.sub(r"[^A-Z0-9]", "", ascii_ad)
    return any(iz in sikistirilmis for iz in _FIRMA_IZLERI) or bool(re.search(r"A\s?\.\s?S", ascii_ad))


def musteri_tipi_belirle(ad, soyad="", tckn=None) -> str:
    return "Kurumsal" if firma_gibi_mi(ad, soyad, tckn) else "Bireysel"


def firma_cekirdegi(ad, soyad="", tckn=None) -> Optional[str]:
    """Firma adının çekirdeği: boşluk/noktalama/harf hataları, kesik ve yasal ekler (Ltd. Şti., Sanayi ve Ticaret…) atılmış hâli.

    'ACRY TEKSTİL İNŞAAT SANAYİ VE TİCARET LİMİTED ŞİRKETİ', '…SANAYİ VETİCARET…' ve 'ACRY TEKSTİL İNŞAATSAN. VE TİC .LTD.ŞTİ.'
    üçü de 'ACRYTEKSTIL' verir. Firma gibi görünmeyen adlar ve 8 karakterden kısa çekirdekler için None.
    """
    if not firma_gibi_mi(ad, soyad, tckn):
        return None
    c = re.sub(r"[^A-Z0-9]", "", ascii_buyuk(f"{ad or ''} {soyad or ''}"))
    for i in range(8, len(c) + 1):
        if _FIRMA_GENEL_EK.fullmatch(c, i):
            return c[:i]
    return None


def firma_ayni_mi(a: str, b: str) -> bool:
    """İki firma çekirdeği aynı firmaya mı ait? Birebir aynı ya da ilk 10 harfi aynı ve %88+ benzer (kısaltma/kesik yazım)."""
    if a == b:
        return True
    if min(len(a), len(b)) < 14 or a[:10] != b[:10]:
        return False
    kisa, uzun = sorted((a, b), key=len)
    if len(kisa) >= 16 and uzun.startswith(kisa):   # AXA gibi kaynaklarda kesilmiş uzun ad
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


def kimlik_temizle(deger) -> Optional[str]:
    """TCKN/VKN'yi rakamlara indirger. Boş, yıldızlı (maskeli) veya geçersiz uzunluk → None."""
    if deger is None:
        return None
    s = str(deger).strip()
    if not s or "*" in s or s.lower() in ("nan", "none", "null", "-"):
        return None
    s = re.sub(r"(?i)(tc\s*kimlik\s*no|vergi\s*kimlik\s*no|vkn|tckn)\s*:?", "", s).strip()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    s = re.sub(r"\D", "", s)
    if len(s) == 9:
        s = s.zfill(10)  # Excel'de başındaki 0'ı kaybeden VKN
    return s if len(s) in (10, 11) else None


def telefon_temizle(deger) -> Optional[str]:
    """Telefonu 05XXXXXXXXX (11 hane) biçimine getirir.

    10 haneli ve 5 ile başlıyorsa (cep) ya da 2/3/4 ile başlıyorsa (sabit hat) başına 0 eklenir; +90/90 ön eki atılır;
    bundan kısa ya da tanınmayan biçimler None (boş) döner. 11 haneden uzun (yurtdışı vb.) numaralar olduğu gibi bırakılır.
    """
    if deger is None:
        return None
    s = str(deger).strip()
    if not s or s.lower() in ("nan", "none", "null", "-"):
        return None
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    d = re.sub(r"\D", "", s)
    if len(d) == 12 and d.startswith("90"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):
        return d
    if len(d) == 10 and d[0] in "2345":
        return "0" + d
    return s if len(d) > 11 else None


def police_no_temizle(deger) -> str:
    s = str(deger if deger is not None else "").strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def tarih_coz(deger) -> Optional[date]:
    if deger is None or deger == "":
        return None
    if isinstance(deger, datetime):
        return deger.date()
    if isinstance(deger, date):
        return deger
    s = str(deger).strip().split("T")[0].split(" ")[0]
    for bicim in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, bicim).date()
        except ValueError:
            continue
    return None


def yas_hesapla(dogum: Optional[date], bugun: Optional[date] = None) -> Optional[int]:
    if not dogum:
        return None
    bugun = bugun or date.today()
    return bugun.year - dogum.year - ((bugun.month, bugun.day) < (dogum.month, dogum.day))


def _bos(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _tam_sayi(v) -> Optional[int]:
    try:
        return None if _bos(v) else int(float(str(v).replace(",", ".")))
    except ValueError:
        return None


def _mantiksal(v) -> Optional[bool]:
    if isinstance(v, bool) or v is None:
        return v
    s = ascii_buyuk(v).strip()
    if s in ("EVET", "E", "TRUE", "1", "VAR"):
        return True
    if s in ("HAYIR", "H", "FALSE", "0", "YOK"):
        return False
    return None


# --------------------------------------------------------------------------
# Modeller
# --------------------------------------------------------------------------
class Musteri(Base):
    __tablename__ = "musteriler"

    id = Column(Integer, primary_key=True, index=True)
    ad = Column(String(100), nullable=False, index=True)
    soyad = Column(String(100), nullable=False, index=True)
    ad_soyad_anahtar = Column(String(220), index=True)  # ad_anahtari(ad, soyad); otomatik doldurulur
    telefon = Column(String(50), index=True)
    email = Column(String(255))
    tc_kimlik = Column(String(50), index=True)  # UNIQUE index: uq_musteriler_tc_kimlik
    adres = Column(Text)
    notlar = Column(Text)
    portfoy_sorumlusu = Column(String(100), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Müşteri kartı alanları
    dogum_tarihi = Column(Date)
    meslek = Column(String(120))
    sirket_sahipleri = Column(Text)
    medeni_durum = Column(String(20))
    cocuk_sayisi = Column(Integer)
    cocuk_yaslari = Column(String(120))
    yas = Column(Integer)
    emekli_mi = Column(Boolean)
    sahip_olunan_araclar = Column(Text)
    ek_notlar = Column(Text)
    musteri_tipi = Column(String(20), index=True)  # 'Bireysel' | 'Kurumsal'

    policeler = relationship("Police", back_populates="musteri")


class Police(Base):
    __tablename__ = "policeler"

    id = Column(Integer, primary_key=True, index=True)
    musteri_id = Column(Integer, ForeignKey("musteriler.id"), nullable=False, index=True)
    police_no = Column(String(80), nullable=False)  # UNIQUE index: uq_policeler_police_no
    sigorta_turu = Column(String(120), nullable=False, index=True)
    sigorta_sirketi = Column(String(150), index=True)
    islem_turu = Column(String(50), default="Yeni")
    uretim_kaynagi = Column(String(50), default="Kendi")
    baslangic_tarihi = Column(Date, nullable=False)
    bitis_tarihi = Column(Date, nullable=False, index=True)
    prim = Column(Float)
    komisyon = Column(Float)
    durum = Column(String(30), default="aktif", index=True)
    arac_bilgisi = Column(String(255))
    varlik_bilgisi = Column(Text)
    aciklama = Column(Text)
    pdf_dosya_adi = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)

    musteri = relationship("Musteri", back_populates="policeler")


class DosyaDepo(Base):
    __tablename__ = "dosya_depo"

    id = Column(Integer, primary_key=True, index=True)
    dosya_adi = Column(String(255), unique=True, nullable=False)
    veri = Column(LargeBinary, nullable=False)


UNIQUE_INDEXLER = (
    ("uq_policeler_police_no", "policeler", "police_no"),
    ("uq_musteriler_tc_kimlik", "musteriler", "tc_kimlik"),
)

MUSTERI_ALANLARI = (
    "ad", "soyad", "telefon", "email", "tc_kimlik", "adres", "notlar", "portfoy_sorumlusu",
    "dogum_tarihi", "meslek", "sirket_sahipleri", "medeni_durum", "cocuk_sayisi",
    "cocuk_yaslari", "yas", "emekli_mi", "sahip_olunan_araclar", "ek_notlar", "musteri_tipi",
)
_METIN_ALANLARI = ("telefon", "email", "adres", "notlar", "meslek", "sirket_sahipleri",
                   "cocuk_yaslari", "sahip_olunan_araclar", "ek_notlar")


@event.listens_for(Musteri, "before_insert")
@event.listens_for(Musteri, "before_update")
def _anahtar_guncelle(_mapper, _conn, m):
    m.ad_soyad_anahtar = ad_anahtari(m.ad, m.soyad)
    if not m.musteri_tipi:
        m.musteri_tipi = musteri_tipi_belirle(m.ad, m.soyad, m.tc_kimlik)


# --------------------------------------------------------------------------
# Müşteri: bul / kaydet (mükerrer engeli burada)
# --------------------------------------------------------------------------
def musteri_verisi_temizle(veri: dict) -> dict:
    """Ham girdiyi (API/Excel/PDF) veritabanı biçimine çevirir. Boş alanlar sözlükten düşer."""
    temiz = {}
    for k in MUSTERI_ALANLARI:
        if k not in veri:
            continue
        v = veri[k]
        if k in ("ad", "soyad"):
            v = tr_buyuk(v)
        elif k == "tc_kimlik":
            v = kimlik_temizle(v)
        elif k == "telefon":
            v = telefon_temizle(v)
        elif k == "dogum_tarihi":
            v = tarih_coz(v)
        elif k in ("cocuk_sayisi", "yas"):
            v = _tam_sayi(v)
        elif k == "emekli_mi":
            v = _mantiksal(v)
        elif k == "musteri_tipi":
            v = None if _bos(v) else ("Kurumsal" if ascii_buyuk(v).startswith("K") else "Bireysel")
        elif k == "medeni_durum":
            v = None if _bos(v) else str(v).strip().capitalize()
        elif k in _METIN_ALANLARI:
            v = None if _bos(v) else str(v).strip()
        elif k == "portfoy_sorumlusu":
            v = None if _bos(v) else str(v).strip()
        if v is not None and v != "":
            temiz[k] = v
    return temiz


def musteri_bul(db, ad="", soyad="", tckn=None) -> Optional[Musteri]:
    """Önce TCKN/VKN, sonra Ad-Soyad. İki tarafta da farklı TCKN varsa farklı kişi sayılır."""
    tc = kimlik_temizle(tckn)
    if tc:
        m = db.query(Musteri).filter(Musteri.tc_kimlik == tc).first()
        if m:
            return m
    anahtar = ad_anahtari(ad, soyad)
    if not anahtar:
        return None
    for m in db.query(Musteri).filter(Musteri.ad_soyad_anahtar == anahtar).order_by(Musteri.id):
        if not (tc and m.tc_kimlik and m.tc_kimlik != tc):
            return m
    cekirdek = firma_cekirdegi(ad, soyad, tc)   # aynı firmanın farklı yazımları (Ltd. Şti. / Limited Şirketi, yazım hataları)
    if cekirdek:
        for m in db.query(Musteri).filter(Musteri.ad_soyad_anahtar.like(cekirdek[:8] + "%")).order_by(Musteri.id):
            diger = firma_cekirdegi(m.ad, m.soyad, m.tc_kimlik)
            if diger and firma_ayni_mi(cekirdek, diger) and not (tc and m.tc_kimlik and m.tc_kimlik != tc):
                return m
    return None


def musteri_kaydet(db, veri: dict, uzerine_yaz: bool = False, varsayilan_danisman: str = VARSAYILAN_DANISMAN):
    """Var olan müşteriyi günceller, yoksa açar. (musteri, yeni_mi) döner; commit çağırana aittir.

    uzerine_yaz=False (Excel içe aktarımı): yalnızca boş alanlar doldurulur.
    uzerine_yaz=True (kullanıcı girişi): gelen dolu alanlar mevcut değerin üstüne yazılır.
    """
    veri = musteri_verisi_temizle(veri)
    m = musteri_bul(db, veri.get("ad"), veri.get("soyad"), veri.get("tc_kimlik"))
    yeni = m is None
    if yeni:
        if not veri.get("ad"):
            raise ValueError("Müşteri adı zorunludur.")
        m = Musteri(ad=veri["ad"], soyad=veri.get("soyad", ""))
        m.portfoy_sorumlusu = varsayilan_danisman
        db.add(m)
    for k, v in veri.items():
        if not yeni and k in ("ad", "soyad"):
            continue  # eşleşen kartın adı başka bir girdiyle yeniden adlandırılmaz (düzeltme kart üzerinden yapılır)
        if yeni or uzerine_yaz or _bos(getattr(m, k)):
            setattr(m, k, _sigdir(Musteri, k, v))
    if m.dogum_tarihi and "yas" not in veri:
        m.yas = yas_hesapla(m.dogum_tarihi)
    db.flush()
    return m, yeni


def police_bul(db, police_no) -> Optional[Police]:
    no = police_no_temizle(police_no)
    return db.query(Police).filter(Police.police_no == no).first() if no else None


# --------------------------------------------------------------------------
# Şema geçişi + mükerrer temizliği + UNIQUE kısıtları
# --------------------------------------------------------------------------
def _eksik_kolonlari_ekle() -> list:
    """Eksik kolonları ekler; PostgreSQL'de eski şemadan kalan dar VARCHAR ve gereksiz NOT NULL kısıtlarını düzeltir (idempotent)."""
    eklenen = []
    insp = inspect(engine)
    for tablo in Base.metadata.sorted_tables:
        if not insp.has_table(tablo.name):
            continue
        mevcut = {c["name"]: c for c in insp.get_columns(tablo.name)}
        for kolon in tablo.columns:
            tip = kolon.type.compile(dialect=engine.dialect)
            if kolon.name not in mevcut:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {tablo.name} ADD COLUMN {"IF NOT EXISTS " if not SQLITE else ""}{kolon.name} {tip}'))
                eklenen.append(f"{tablo.name}.{kolon.name}")
                continue
            if not SQLITE and kolon.nullable and not kolon.primary_key and mevcut[kolon.name].get("nullable") is False:
                try:  # eski şemadan kalan NOT NULL (örn. musteriler.telefon) → boş bırakılabilir yap
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {tablo.name} ALTER COLUMN {kolon.name} DROP NOT NULL"))
                    eklenen.append(f"{tablo.name}.{kolon.name} NOT NULL kaldırıldı")
                except Exception as e:
                    log.error("%s.%s NOT NULL kaldırılamadı: %s", tablo.name, kolon.name, str(e).splitlines()[0][:200])
            hedef = getattr(kolon.type, "length", None)
            eski = getattr(mevcut[kolon.name]["type"], "length", None)
            if not SQLITE and isinstance(kolon.type, String) and hedef and eski and eski < hedef:
                with engine.begin() as conn:  # örn. eski şemada telefon VARCHAR(20)
                    conn.execute(text(f"ALTER TABLE {tablo.name} ALTER COLUMN {kolon.name} TYPE {tip}"))
                eklenen.append(f"{tablo.name}.{kolon.name} genişletildi {eski}→{hedef}")
    if "musteriler.ek_notlar" in eklenen:
        with engine.begin() as conn:  # eski 'notlar' içeriği yeni kartta görünsün
            conn.execute(text("UPDATE musteriler SET ek_notlar = notlar WHERE notlar IS NOT NULL AND notlar <> ''"))
    return eklenen


def _sigdir(tablo, alan, deger):
    """Değeri kolon uzunluğuna kırpar (PostgreSQL uzun değeri reddeder, SQLite sessizce kabul eder)."""
    uzunluk = getattr(tablo.__table__.c[alan].type, "length", None)
    return deger[:uzunluk] if uzunluk and isinstance(deger, str) and len(deger) > uzunluk else deger


def _dolu_mu(v) -> bool:
    return not _bos(v)


def _musterileri_birlestir(db, rapor: dict):
    musteriler = db.query(Musteri).order_by(Musteri.id).all()
    police_sayilari = dict(db.query(Police.musteri_id, func.count(Police.id)).group_by(Police.musteri_id).all())

    for m in musteriler:  # normalizasyon (kayıt bazlı savepoint: tek bozuk satır tüm açılışı çökertmesin)
        yeni = {
            "ad": tr_buyuk(m.ad), "soyad": tr_buyuk(m.soyad), "tc_kimlik": kimlik_temizle(m.tc_kimlik),
            "telefon": telefon_temizle(m.telefon),
            "email": m.email.strip() if _dolu_mu(m.email) else None,
        }
        if not _dolu_mu(m.portfoy_sorumlusu):
            yeni["portfoy_sorumlusu"] = VARSAYILAN_DANISMAN
        if not _dolu_mu(m.musteri_tipi):
            yeni["musteri_tipi"] = musteri_tipi_belirle(yeni["ad"], yeni["soyad"], yeni["tc_kimlik"])
        yeni = {k: _sigdir(Musteri, k, v) for k, v in yeni.items()}
        degisen = {k: v for k, v in yeni.items() if getattr(m, k) != v}
        anahtar = _sigdir(Musteri, "ad_soyad_anahtar", ad_anahtari(yeni["ad"], yeni["soyad"]))
        if not degisen and m.ad_soyad_anahtar == anahtar:
            continue
        try:
            with db.begin_nested():
                for k, v in degisen.items():
                    setattr(m, k, v)
                m.ad_soyad_anahtar = anahtar
                db.flush()
            rapor["duzeltilen_alan"] += len(degisen)
        except Exception as e:
            log.error("Müşteri #%s normalize edilemedi, atlandı: %s", m.id, str(e).splitlines()[0][:200])
            db.expire(m)
            rapor["atlanan_kayit"] += 1

    ebeveyn = {m.id: m.id for m in musteriler}

    def bul(x):
        while ebeveyn[x] != x:
            ebeveyn[x] = ebeveyn[ebeveyn[x]]
            x = ebeveyn[x]
        return x

    def birlestir(ids):
        kok = bul(ids[0])
        for i in ids[1:]:
            ebeveyn[bul(i)] = kok

    tc_gruplari, isim_gruplari = {}, {}
    for m in musteriler:
        if m.tc_kimlik:
            tc_gruplari.setdefault(m.tc_kimlik, []).append(m)
        if m.ad_soyad_anahtar:
            isim_gruplari.setdefault(m.ad_soyad_anahtar, []).append(m)
    for grup in tc_gruplari.values():
        birlestir([m.id for m in grup])
    kovalar = {}   # ilk 10 harf → [(çekirdek, müşteri)]; benzer firmalar aynı kovada karşılaştırılır
    for m in musteriler:
        cekirdek = firma_cekirdegi(m.ad, m.soyad, m.tc_kimlik)
        if cekirdek:
            kovalar.setdefault(cekirdek[:10], []).append((cekirdek, m))
    firma_gruplari = []
    for kova in kovalar.values():
        if len(kova) < 2:
            continue
        atama = list(range(len(kova)))

        def kok(x):
            while atama[x] != x:
                atama[x] = atama[atama[x]]
                x = atama[x]
            return x
        for a in range(len(kova)):
            for b in range(a + 1, len(kova)):
                if firma_ayni_mi(kova[a][0], kova[b][0]):
                    atama[kok(b)] = kok(a)
        kumeler_k = {}
        for idx, (_c, m) in enumerate(kova):
            kumeler_k.setdefault(kok(idx), []).append(m)
        firma_gruplari.extend(kumeler_k.values())
    for gruplar in (isim_gruplari.values(), firma_gruplari):
        for grup in gruplar:
            if len(grup) < 2:
                continue
            tcler = {m.tc_kimlik for m in grup if m.tc_kimlik}
            if len(tcler) <= 1:
                birlestir([m.id for m in grup])
            else:  # aynı isim/firma, farklı TCKN/VKN: farklı kişiler → yalnızca kimliksiz kayıtlar kendi aralarında birleşir
                tcsiz = [m.id for m in grup if not m.tc_kimlik]
                if len(tcsiz) > 1:
                    birlestir(tcsiz)
                rapor["ayni_isim_farkli_tckn"].append({"ad_soyad": f"{grup[0].ad} {grup[0].soyad}".strip(), "tckn": sorted(tcler)})

    kumeler = {}
    for m in musteriler:
        kumeler.setdefault(bul(m.id), []).append(m)

    for uyeler in kumeler.values():
        if len(uyeler) < 2:
            continue
        uyeler.sort(key=lambda m: (-police_sayilari.get(m.id, 0), m.id))
        ana, kopyalar = uyeler[0], uyeler[1:]
        kopya_idleri = [k.id for k in kopyalar]
        alan_degerleri = {}
        for alan in MUSTERI_ALANLARI:
            if alan in ("ad", "soyad", "notlar", "ek_notlar"):
                continue
            if not _dolu_mu(getattr(ana, alan)):
                for k in kopyalar:
                    if _dolu_mu(getattr(k, alan)):
                        alan_degerleri[alan] = getattr(k, alan)
                        break
        for alan in ("notlar", "ek_notlar"):
            parcalar = []
            for m in uyeler:
                t = (getattr(m, alan) or "").strip()
                if t and t not in parcalar:
                    parcalar.append(t)
            if len(parcalar) > 1:
                alan_degerleri[alan] = "\n".join(parcalar)

        db.query(Police).filter(Police.musteri_id.in_(kopya_idleri)).update({"musteri_id": ana.id}, synchronize_session=False)
        for k in kopyalar:
            db.expunge(k)
        db.query(Musteri).filter(Musteri.id.in_(kopya_idleri)).delete(synchronize_session=False)
        db.flush()
        for alan, v in alan_degerleri.items():
            setattr(ana, alan, v)
        rapor["birlesenler"].append(f"{ana.ad} {ana.soyad}".strip() + "  ←  " + " | ".join(f"{k.ad} {k.soyad}".strip() for k in kopyalar))
        rapor["birlesen_musteri_grubu"] += 1
        rapor["silinen_kopya_musteri"] += len(kopyalar)


def _policeleri_tekillestir(db, rapor: dict):
    gruplar = {}
    for p in db.query(Police).order_by(Police.id):
        no = police_no_temizle(p.police_no)
        if not no or no == "-":
            no = f"YOK-{p.id}"  # numarasız kayıtlar birbirinin kopyası değildir
        if p.police_no != no:
            p.police_no = no
            rapor["duzeltilen_alan"] += 1
        gruplar.setdefault(no, []).append(p)

    for no, grup in gruplar.items():
        if len(grup) < 2:
            continue
        grup.sort(key=lambda p: (p.bitis_tarihi or date.min, _dolu_mu(p.pdf_dosya_adi), p.prim is not None, -p.id), reverse=True)
        ana, kopyalar = grup[0], grup[1:]
        for alan in ("arac_bilgisi", "varlik_bilgisi", "pdf_dosya_adi", "aciklama", "sigorta_sirketi"):
            if not _dolu_mu(getattr(ana, alan)):
                for k in kopyalar:
                    if _dolu_mu(getattr(k, alan)):
                        setattr(ana, alan, getattr(k, alan))
                        break
        for k in kopyalar:
            db.delete(k)
        rapor["silinen_kopya_police"] += len(kopyalar)
    db.flush()


def veritabanini_temizle(db) -> dict:
    """Mükerrer müşteri ve poliçeleri birleştirir/temizler. Idempotenttir; commit çağırana aittir."""
    rapor = {"birlesen_musteri_grubu": 0, "silinen_kopya_musteri": 0, "silinen_kopya_police": 0,
             "duzeltilen_alan": 0, "atlanan_kayit": 0, "ayni_isim_farkli_tckn": [], "birlesenler": []}
    _musterileri_birlestir(db, rapor)
    _policeleri_tekillestir(db, rapor)
    return rapor


def degisiklik_var_mi(rapor: dict) -> bool:
    return bool(rapor["silinen_kopya_musteri"] or rapor["silinen_kopya_police"] or rapor["duzeltilen_alan"])


def unique_kisitlari_ekle():
    for ad, tablo, kolon in UNIQUE_INDEXLER:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {ad} ON {tablo} ({kolon})"))
        except Exception as e:  # temizlik sonrası buraya düşmemesi gerekir
            log.error("UNIQUE kısıtı eklenemedi (%s): %s", ad, e)


def sqlite_yedekle() -> Optional[Path]:
    """Yalnızca yerel SQLite için. Yedek, son işlenmiş (commit edilmiş) hâlin kopyasıdır."""
    if not SQLITE:
        return None
    kaynak = Path(DATABASE_URL.replace("sqlite:///", "", 1))
    if not kaynak.exists():
        return None
    hedef = kaynak.with_name(f"{kaynak.name}.yedek-{datetime.now():%Y%m%d-%H%M%S}")
    src, dst = sqlite3.connect(kaynak), sqlite3.connect(hedef)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return hedef


def _temizligi_calistir() -> dict:
    """Temizliği tek transaction'da çalıştırır. PostgreSQL'de eşzamanlı worker'ları advisory lock ile sıraya koyar."""
    db = SessionLocal()
    try:
        if not SQLITE:
            db.execute(text("SELECT pg_advisory_xact_lock(76543210)"))
        rapor = veritabanini_temizle(db)
        if degisiklik_var_mi(rapor):
            if SQLITE:
                log.warning("Temizlik öncesi yedek alındı: %s", sqlite_yedekle())
            db.commit()
            for satir in rapor["birlesenler"][:300]:
                log.warning("BİRLEŞTİ: %s", satir)
            log.warning("Veri temizliği uygulandı: %s", {k: v for k, v in rapor.items() if k not in ("ayni_isim_farkli_tckn", "birlesenler")})
        else:
            db.rollback()
        return rapor
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> dict:
    """Şemayı kurar/günceller, mükerrerleri temizler, en son UNIQUE kısıtlarını ekler.

    Şema adımları başarısız olursa hata verir (uygulama bozuk şemayla çalışmasın); mükerrer temizliği ise
    başarısız olursa loglanıp atlanır, böylece geçici bir veri sorunu uygulamanın açılmasını engellemez.
    """
    Base.metadata.create_all(bind=engine)
    eklenen = _eksik_kolonlari_ekle()
    try:
        rapor = _temizligi_calistir()
    except Exception:
        log.exception("Veri temizliği başarısız oldu; uygulama temizlik yapılmadan başlatılıyor")
        rapor = {"hata": True, "ayni_isim_farkli_tckn": []}
    unique_kisitlari_ekle()
    rapor["eklenen_kolonlar"] = eklenen
    return rapor


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
