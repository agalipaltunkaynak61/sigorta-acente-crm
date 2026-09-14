import os
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text, LargeBinary, create_engine
from sqlalchemy.orm import declarative_base, joinedload, sessionmaker
from sqlalchemy.orm.session import Session

import google.generativeai as genai

# PDF parser dosyan aynı kalmalı
from pdf_parser import ayikla_police_pdf

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# DERSLER KLASÖRÜ
DERSLER_DIR = BASE_DIR / "dersler"
DERSLER_DIR.mkdir(exist_ok=True)

# ==================== VERİ STANDARTLAŞTIRMA SÖZLÜKLERİ ====================
SIRKET_ESLESMELERI = {
    "türkiye sigorta a.ş.": "Türkiye Sigorta",
    "türkiye sigorta anonim şirketi": "Türkiye Sigorta",
    "türkiye": "Türkiye Sigorta",
    "turkiye": "Türkiye Sigorta",
    "axa sigorta a.ş.": "Axa Sigorta",
    "axa": "Axa Sigorta",
    "doğa sigorta a.ş.": "Doğa Sigorta",
    "doğa": "Doğa Sigorta",
    "doga": "Doğa Sigorta",
    "ak sigorta": "Aksigorta",
    "aksigorta": "Aksigorta",
    "hepiyi": "Hepiyi Sigorta",
    "neova": "Neova Sigorta",
    "quick": "Quick Sigorta"
}

BRANS_ESLESMELERI = {
    "trafik sigortası": "Trafik Sigortası",
    "trafik": "Trafik Sigortası",
    "kasko sigortası": "Kasko Sigortası",
    "kasko": "Kasko Sigortası",
    "tamamlayıcı sağlık sigortası": "Tamamlayıcı Sağlık Sigortası",
    "tamamlayıcı": "Tamamlayıcı Sağlık Sigortası",
    "tss": "Tamamlayıcı Sağlık Sigortası",
    "dask": "DASK",
    "deprem": "DASK",
    "kurumsal ve iş yeri sigortaları": "Kurumsal ve İş Yeri Sigortaları",
    "iş yeri": "Kurumsal ve İş Yeri Sigortaları",
    "işyeri": "Kurumsal ve İş Yeri Sigortaları",
    "kurumsal": "Kurumsal ve İş Yeri Sigortaları",
    "konut ve eşya sigortaları": "Konut ve Eşya Sigortaları",
    "konut": "Konut ve Eşya Sigortaları",
    "eşya": "Konut ve Eşya Sigortaları",
    "ferdi kaza sigortaları": "Ferdi Kaza",
    "ferdi kaza": "Ferdi Kaza"
}

def standardize_metin(metin, sozluk):
    """Excel veya PDF'den gelen dağınık metinleri standart formata çevirir"""
    if not metin: return metin
    m_lower = metin.strip().lower()
    
    for key, val in sozluk.items():
        if key in m_lower:
            return val
            
    return metin.strip().title()

# ==================== GEMINI AI YAPILANDIRMASI ====================
API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY.strip())

# ==================== VERİTABANI BAĞLANTISI (SUPABASE POSTGRESQL) ====================
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DB_PATH = BASE_DIR / "acente_crm.db"
    DATABASE_URL = f"sqlite:///{DB_PATH}"

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine_args = {}
if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Musteri(Base):
    __tablename__ = "musteriler"
    id = Column(Integer, primary_key=True, index=True)
    ad = Column(String(100), nullable=False, index=True)
    soyad = Column(String(100), nullable=False, index=True)
    telefon = Column(String(50), nullable=True, index=True)
    email = Column(String(100), nullable=True)
    tc_kimlik = Column(String(50), nullable=True, index=True)
    adres = Column(Text, nullable=True)
    notlar = Column(Text, nullable=True)
    portfoy_sorumlusu = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Police(Base):
    __tablename__ = "policeler"
    id = Column(Integer, primary_key=True, index=True)
    musteri_id = Column(Integer, nullable=False, index=True)
    police_no = Column(String(80), nullable=False, index=True)
    sigorta_turu = Column(String(120), nullable=False, index=True)
    sigorta_sirketi = Column(String(100), nullable=True, index=True)
    islem_turu = Column(String(50), default="Yeni Poliçe")
    baslangic_tarihi = Column(Date, nullable=False)
    bitis_tarihi = Column(Date, nullable=False, index=True)
    prim = Column(Float, nullable=True)
    durum = Column(String(30), default="aktif", index=True)
    arac_bilgisi = Column(String(255), nullable=True) 
    varlik_bilgisi = Column(Text, nullable=True)    
    aciklama = Column(Text, nullable=True)
    pdf_dosya_adi = Column(String(255), nullable=True)

class DosyaDepo(Base):
    __tablename__ = "dosya_depo"
    id = Column(Integer, primary_key=True, index=True)
    dosya_adi = Column(String(255), unique=True, nullable=False)
    veri = Column(LargeBinary, nullable=False)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def eski_policeleri_otomatik_temizle():
    db = SessionLocal()
    try:
        sinir_tarihi = date.today() - timedelta(days=90)
        eski_policeler = db.query(Police).filter(Police.bitis_tarihi < sinir_tarihi).all()
        silinen_sayi = len(eski_policeler)
        if silinen_sayi > 0:
            for p in eski_policeler:
                db.delete(p)
            db.commit()
            print(f"🧹 OTOMATİK TEMİZLİK: Bitiş tarihi 3 aydan eski olan {silinen_sayi} adet poliçe silindi.")
    except Exception as e:
        db.rollback()
        print(f"Eski poliçe otomatik temizleme hatası: {e}")
    finally:
        db.close()

app = FastAPI(title="Altun Kardeşler CRM", version="3.8.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()
    eski_policeleri_otomatik_temizle()

def normalize_string(s: str) -> str:
    if not s: return ""
    s = s.replace("İ", "I").replace("ı", "i").replace("Ş", "S").replace("ş", "s")
    s = s.replace("Ğ", "G").replace("ğ", "g").replace("Ü", "U").replace("ü", "u")
    s = s.replace("Ö", "O").replace("ö", "o").replace("Ç", "C").replace("ç", "c")
    return s.strip().lower()

# ==================== MÜKERRER POLİÇELERİ TEMİZLEME VE TEKLEŞTİRME ROTASI ====================
@app.get("/api/policeleri-temizle-ve-tekliyle")
def policeleri_temizle_ve_tekliyle(db: Session = Depends(get_db)):
    """
    Aynı poliçe numarasına sahip mükerrer kayıtları temizler:
    - Öncelik her zaman branşı 'Diğer', boş veya None OLMAYAN (doğru branşlı) kayıttadır.
    - Kopyaları siler ve her poliçe numarasından sadece 1 tane bırakır.
    """
    try:
        tum_policeler = db.query(Police).all()
        
        police_gruplari = {}
        for p in tum_policeler:
            p_no = str(p.police_no).strip()
            if p_no not in police_gruplari:
                police_gruplari[p_no] = []
            police_gruplari[p_no].append(p)
            
        silinen_sayi = 0
        guncellenen_sayisi = 0
        
        for p_no, p_listesi in police_gruplari.items():
            if len(p_listesi) > 1:
                def siralama_kriteri(police):
                    brans = (police.sigorta_turu or "").strip()
                    is_diger = 1 if (brans in ["Diğer", "", None] or "Özel Sigorta" in brans) else 0
                    return (is_diger, police.id)
                
                p_listesi.sort(key=siralama_kriteri)
                
                korunacak_police = p_listesi[0]
                silinecekler = p_listesi[1:]
                
                if korunacak_police.sigorta_turu in ["Diğer", "", None]:
                    for silinecek in silinecekler:
                        if silinecek.sigorta_turu not in ["Diğer", "", None]:
                            korunacak_police.sigorta_turu = silinecek.sigorta_turu
                            guncellenen_sayisi += 1
                            break
                
                for silinecek in silinecekler:
                    db.delete(silinecek)
                    silinen_sayi += 1
                    
        db.commit()
        return {
            "durum": "Başarılı",
            "mesaj": f"Temizlik tamamlandı! Toplam {silinen_sayi} adet mükerrer poliçe silindi.",
            "guncellenen_brans_sayisi": guncellenen_sayisi
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/eski-policeleri-temizle")
def api_eski_policeleri_temizle():
    db = SessionLocal()
    try:
        sinir_tarihi = date.today() - timedelta(days=90)
        eski_policeler = db.query(Police).filter(Police.bitis_tarihi < sinir_tarihi).all()
        silinen_sayi = len(eski_policeler)
        for p in eski_policeler:
            db.delete(p)
        db.commit()
        return {"mesaj": f"Temizlik başarılı! Bitiş tarihi 3 aydan eski olan {silinen_sayi} eski poliçe sistemden silindi."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/sistemi-temizle")
def sistemi_temizle(db: Session = Depends(get_db)):
    policeler = db.query(Police).all()
    for p in policeler:
        p.sigorta_sirketi = standardize_metin(p.sigorta_sirketi, SIRKET_ESLESMELERI)
        p.sigorta_turu = standardize_metin(p.sigorta_turu, BRANS_ESLESMELERI)
    
    musteriler = db.query(Musteri).all()
    for m in musteriler:
        if m.portfoy_sorumlusu == "Sezai Karakoç":
            m.portfoy_sorumlusu = "Sezai Yağcı"

    db.commit()

    birlesen_sayisi = 0
    musteriler = db.query(Musteri).all()
    isim_gruplari = {}
    
    for m in musteriler:
        anahtar = f"{normalize_string(m.ad)}_{normalize_string(m.soyad)}"
        if anahtar not in isim_gruplari:
            isim_gruplari[anahtar] = []
        isim_gruplari[anahtar].append(m)
        
    for anahtar, m_liste in isim_gruplari.items():
        if len(m_liste) > 1:
            ana_musteri = m_liste[0]
            for kopya_musteri in m_liste[1:]:
                if not ana_musteri.tc_kimlik and kopya_musteri.tc_kimlik:
                    ana_musteri.tc_kimlik = kopya_musteri.tc_kimlik
                if not ana_musteri.telefon and kopya_musteri.telefon:
                    ana_musteri.telefon = kopya_musteri.telefon
                    
                db.query(Police).filter(Police.musteri_id == kopya_musteri.id).update({"musteri_id": ana_musteri.id})
                db.delete(kopya_musteri)
                birlesen_sayisi += 1
                
    db.commit()
    return {"mesaj": f"Temizlik tamamlandı! {birlesen_sayisi} çift müşteri hesabı tek hesapta birleştirildi. Şirket ve branş isimleri standardize edildi."}

@app.get("/dersler/{dosya_adi}")
def get_ders_pdf(dosya_adi: str):
    dosya_adi = urllib.parse.unquote(dosya_adi)
    hedef = DERSLER_DIR / dosya_adi
    if hedef.exists() and hedef.is_file():
        return FileResponse(hedef, media_type="application/pdf")
        
    for f in DERSLER_DIR.iterdir():
        if f.is_file() and f.name.lower() == dosya_adi.lower():
            return FileResponse(f, media_type="application/pdf")
            
    hedef_norm = normalize_string(dosya_adi)
    for f in DERSLER_DIR.iterdir():
        if f.is_file() and normalize_string(f.name) == hedef_norm:
            return FileResponse(f, media_type="application/pdf")
            
    raise HTTPException(status_code=404, detail="Not Found")

def parse_tarih(val) -> date:
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        val = val.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
    return date.today()

def hesapla_komisyon(brans: str, sirket: str, prim: float) -> float:
    if not prim: return 0.0
    b = (brans or "").lower()
    s = (sirket or "").lower()
    
    oran = 0.10
    if "tss" in b or "tamamlay" in b: oran = 0.25
    elif "iş" in b or "is yeri" in b or "isyeri" in b: oran = 0.20
    elif "dask" in b: oran = 0.12
    elif "trafik" in b: oran = 0.10
    elif "kasko" in b: oran = 0.15
    elif "yeşil" in b or "yesil" in b: oran = 0.10
    elif "konut" in b: oran = 0.25
    elif "seyahat" in b or "seyehat" in b: oran = 0.15
    elif "ferdi kaza" in b: oran = 0.30
    elif "allrisk" in b or "inşaat" in b or "insaat" in b: oran = 0.20

    anlasmali = ["türkiye", "turkiye", "axa", "ak", "hepiyi", "neova", "doğa", "doga", "quick"]
    disaridan = True
    for a in anlasmali:
        if a in s:
            disaridan = False
            break
            
    komisyon = prim * oran
    if disaridan:
        komisyon = komisyon / 2.0
    return komisyon

def uyar_seviyesi(kalan_gun: int) -> str:
    if kalan_gun < 0: return "suresi_dolmus"
    if kalan_gun <= 3: return "kirmizi"
    if kalan_gun <= 15: return "sari"
    if kalan_gun <= 30: return "yesil"
    return "uzak"

def police_to_out(p: Police, db_session: Session) -> dict:
    bugun = date.today()
    kalan = (p.bitis_tarihi - bugun).days if p.bitis_tarihi else 0
    suresi_dolmus = (p.bitis_tarihi and p.bitis_tarihi < bugun) or (p.durum == "iptal")
    musteri = db_session.query(Musteri).filter(Musteri.id == p.musteri_id).first()
    komisyon = hesapla_komisyon(p.sigorta_turu, p.sigorta_sirketi, p.prim)
    
    return {
        "id": p.id,
        "musteri_id": p.musteri_id,
        "police_no": p.police_no,
        "sigorta_turu": p.sigorta_turu,
        "sigorta_sirketi": p.sigorta_sirketi,
        "islem_turu": p.islem_turu or "Yeni Poliçe",
        "baslangic_tarihi": p.baslangic_tarihi,
        "bitis_tarihi": p.bitis_tarihi,
        "prim": p.prim,
        "durum": p.durum,
        "arac_bilgisi": p.arac_bilgisi,
        "varlik_bilgisi": p.varlik_bilgisi,
        "aciklama": p.aciklama,
        "pdf_dosya_adi": p.pdf_dosya_adi,
        "kalan_gun": 0 if suresi_dolmus else kalan,
        "suresi_dolmus": suresi_dolmus,
        "uyar_seviyesi": "suresi_dolmus" if suresi_dolmus else uyar_seviyesi(kalan),
        "musteri_ad": musteri.ad if musteri else None,
        "musteri_soyad": musteri.soyad if musteri else None,
        "musteri_telefon": musteri.telefon if musteri else None,
        "musteri_portfoy_sorumlusu": musteri.portfoy_sorumlusu if musteri else None,
        "komisyon": komisyon
    }

@app.get("/")
def ana_sayfa():
    index_yolu = BASE_DIR / "index.html"
    if index_yolu.exists():
        return HTMLResponse(index_yolu.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html bulunamadı!</h1>")

@app.get("/api/ozet")
def api_ozet(db: Session = Depends(get_db)):
    bugun = date.today()
    return {
        "musteri_sayisi": db.query(Musteri).count(),
        "police_sayisi": db.query(Police).count(),
        "aktif_police": db.query(Police).filter(Police.durum == "aktif", Police.bitis_tarihi >= bugun).count(),
        "yaklasan_30_gun": db.query(Police).filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=30)).count()
    }

@app.get("/api/musteriler")
def api_musteri_listele(q: Optional[str] = None, page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100000), db: Session = Depends(get_db)):
    query = db.query(Musteri)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((Musteri.ad.ilike(like)) | (Musteri.soyad.ilike(like)) | (Musteri.telefon.ilike(like)) | (Musteri.tc_kimlik.ilike(like)))
    
    total = query.count()
    musteriler = query.order_by(Musteri.ad).offset((page - 1) * limit).limit(limit).all()
    
    sonuc = []
    bugun = date.today()
    for m in musteriler:
        aktif_policeler = db.query(Police).filter(Police.musteri_id == m.id, Police.bitis_tarihi >= bugun, Police.durum == "aktif").all()
        dagilim = {}
        for p in aktif_policeler:
            t = p.sigorta_turu or "Bilinmiyor"
            dagilim[t] = dagilim.get(t, 0) + 1
        
        sonuc.append({
            "id": m.id, "ad": m.ad, "soyad": m.soyad, "telefon": m.telefon, "tc_kimlik": m.tc_kimlik,
            "adres": m.adres, "portfoy_sorumlusu": m.portfoy_sorumlusu,
            "aktif_police_sayilari": dagilim
        })
        
    return {
        "items": sonuc,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if limit > 0 else 1
    }

@app.get("/api/musteriler/{id}")
def api_musteri_detay(id: int, db: Session = Depends(get_db)):
    m = db.query(Musteri).filter(Musteri.id == id).first()
    if not m: raise HTTPException(404, "Bulunamadı")
    bugun = date.today()
    aktif_policeler = db.query(Police).filter(Police.musteri_id == m.id, Police.bitis_tarihi >= bugun, Police.durum == "aktif").all()
    dagilim = {}
    for p in aktif_policeler:
        t = p.sigorta_turu or "Bilinmiyor"
        dagilim[t] = dagilim.get(t, 0) + 1
    
    return {
        "id": m.id, "ad": m.ad, "soyad": m.soyad, "telefon": m.telefon, "tc_kimlik": m.tc_kimlik,
        "adres": m.adres, "portfoy_sorumlusu": m.portfoy_sorumlusu,
        "aktif_police_sayilari": dagilim
    }

@app.post("/api/musteriler", status_code=201)
def api_musteri_olustur(payload: dict, db: Session = Depends(get_db)):
    tckn = payload.get("tc_kimlik")
    ad = (payload.get("ad") or "").strip().title()
    soyad = (payload.get("soyad") or "").strip().upper()
    danisman = payload.get("portfoy_sorumlusu", "Diğer")
    
    if danisman == "Sezai Karakoç": danisman = "Sezai Yağcı"

    existing = None
    if tckn and "**" not in str(tckn):
        existing = db.query(Musteri).filter(Musteri.tc_kimlik == tckn).first()
    
    if not existing and ad and soyad and "**" not in ad:
        norm_ad, norm_soyad = normalize_string(ad), normalize_string(soyad)
        tum_musteriler = db.query(Musteri).all()
        for m in tum_musteriler:
            if normalize_string(m.ad) == norm_ad and normalize_string(m.soyad) == norm_soyad:
                existing = m
                break

    if existing:
        for k, v in payload.items():
            if v and not str(v).startswith("*"):
                setattr(existing, k, v)
        existing.portfoy_sorumlusu = danisman
        db.commit()
        db.refresh(existing)
        return existing

    payload["ad"] = ad
    payload["soyad"] = soyad
    payload["portfoy_sorumlusu"] = danisman
    
    m = Musteri(**payload)
    db.add(m); db.commit(); db.refresh(m)
    return m

@app.put("/api/musteriler/{id}")
def api_musteri_guncelle(id: int, payload: dict, db: Session = Depends(get_db)):
    m = db.query(Musteri).filter(Musteri.id == id).first()
    if not m: raise HTTPException(404, "Bulunamadı")
    for k, v in payload.items(): setattr(m, k, v)
    if m.portfoy_sorumlusu == "Sezai Karakoç": m.portfoy_sorumlusu = "Sezai Yağcı"
    db.commit(); db.refresh(m)
    return m

@app.delete("/api/musteriler/{id}")
def api_musteri_sil(id: int, db: Session = Depends(get_db)):
    m = db.query(Musteri).filter(Musteri.id == id).first()
    if not m: raise HTTPException(404, "Bulunamadı")
    db.query(Police).filter(Police.musteri_id == id).delete()
    db.delete(m); db.commit()
    return {"ok": True}

@app.get("/api/policeler")
def api_police_listele(musteri_id: Optional[int] = None, q: Optional[str] = None, aktif: Optional[str] = None, page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100000), db: Session = Depends(get_db)):
    query = db.query(Police)
    
    if musteri_id: 
        query = query.filter(Police.musteri_id == musteri_id)
        
    if q or aktif:
        query = query.outerjoin(Musteri, Police.musteri_id == Musteri.id)
        
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (Police.police_no.ilike(like)) | 
            (Police.sigorta_sirketi.ilike(like)) | 
            (Police.sigorta_turu.ilike(like)) |
            (Musteri.ad.ilike(like)) |
            (Musteri.soyad.ilike(like))
        )
        
    bugun = date.today()
    if aktif == 'aktif':
        query = query.filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun)
    elif aktif == 'eski':
        query = query.filter((Police.durum == "iptal") | (Police.bitis_tarihi < bugun))
        
    total = query.count()
    kayitlar = query.order_by(Police.bitis_tarihi.desc()).offset((page - 1) * limit).limit(limit).all()
    
    return {
        "items": [police_to_out(p, db) for p in kayitlar],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if limit > 0 else 1
    }

@app.get("/api/policeler/yaklasan")
def api_yaklasan(gun: int = 30, db: Session = Depends(get_db)):
    bugun = date.today()
    kayitlar = db.query(Police).filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=gun)).all()
    return [police_to_out(p, db) for p in kayitlar]

@app.post("/api/policeler", status_code=201)
def api_police_olustur(payload: dict, db: Session = Depends(get_db)):
    try:
        baslangic = parse_tarih(payload.get("baslangic_tarihi"))
        bitis = parse_tarih(payload.get("bitis_tarihi"))
        prim_deger = float(payload.get("prim") or 0) if payload.get("prim") is not None else None
        police_no = payload.get("police_no")
        islem_turu = payload.get("islem_turu", "Yeni Poliçe")
        arac_yeni = payload.get("arac_bilgisi")
        
        temiz_sirket = standardize_metin(payload.get("sigorta_sirketi"), SIRKET_ESLESMELERI)
        temiz_brans = standardize_metin(payload.get("sigorta_turu"), BRANS_ESLESMELERI)

        ana_police = None
        if police_no and ("plaka" in str(islem_turu).lower() or "zeyil" in str(islem_turu).lower() or "tahakkuk" in str(islem_turu).lower() or "ek" in str(islem_turu).lower()):
            ana_police = db.query(Police).filter(Police.police_no == police_no).first()

        if ana_police:
            if arac_yeni:
                ana_police.arac_bilgisi = arac_yeni
            if prim_deger:
                ana_police.prim = (ana_police.prim or 0) + prim_deger
            
            ek_not = f" | Ek Belge ({islem_turu}): Prim +{prim_deger} TL, Araç: {arac_yeni}"
            ana_police.aciklama = (ana_police.aciklama or "") + ek_not
            if payload.get("pdf_dosya_adi"):
                ana_police.pdf_dosya_adi = payload.get("pdf_dosya_adi")
            
            db.commit()
            db.refresh(ana_police)
            return police_to_out(ana_police, db)

        police = Police(
            musteri_id=payload.get("musteri_id"),
            police_no=police_no,
            sigorta_turu=temiz_brans,
            sigorta_sirketi=temiz_sirket,
            islem_turu=islem_turu,
            baslangic_tarihi=baslangic,
            bitis_tarihi=bitis,
            prim=prim_deger,
            durum=str(payload.get("durum", "aktif")).lower(),
            arac_bilgisi=arac_yeni,
            varlik_bilgisi=payload.get("varlik_bilgisi"),
            aciklama=payload.get("aciklama"),
            pdf_dosya_adi=payload.get("pdf_dosya_adi")
        )
        db.add(police)
        db.commit()
        db.refresh(police)
        return police_to_out(police, db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.put("/api/policeler/{id}")
def api_police_guncelle(id: int, payload: dict, db: Session = Depends(get_db)):
    try:
        p = db.query(Police).filter(Police.id == id).first()
        if not p: raise HTTPException(404, "Poliçe bulunamadı")
        
        for k, v in payload.items():
            if k in ["baslangic_tarihi", "bitis_tarihi"] and v:
                setattr(p, k, parse_tarih(v))
            elif k == "prim":
                p.prim = float(v) if v is not None else None
            elif k == "sigorta_sirketi":
                p.sigorta_sirketi = standardize_metin(v, SIRKET_ESLESMELERI)
            elif k == "sigorta_turu":
                p.sigorta_turu = standardize_metin(v, BRANS_ESLESMELERI)
            elif hasattr(p, k):
                setattr(p, k, v)
        
        db.commit()
        db.refresh(p)
        return police_to_out(p, db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/policeler/{id}")
def api_police_sil(id: int, db: Session = Depends(get_db)):
    p = db.query(Police).filter(Police.id == id).first()
    if not p: raise HTTPException(404, "Bulunamadı")
    db.delete(p); db.commit()
    return {"ok": True}

@app.get("/api/policeler/{id}/pdf")
def api_pdf_goster(id: int, db: Session = Depends(get_db)):
    p = db.query(Police).filter(Police.id == id).first()
    if not p or not p.pdf_dosya_adi:
        raise HTTPException(status_code=404, detail="Bu poliçeye ait PDF bulunamadı.")
    
    dosya = db.query(DosyaDepo).filter(DosyaDepo.dosya_adi == p.pdf_dosya_adi).first()
    if not dosya:
        raise HTTPException(status_code=404, detail="Dosya sunucuda bulunamadı.")
        
    return Response(content=dosya.veri, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{p.pdf_dosya_adi}"'})

@app.post("/api/upload-parse")
async def api_upload_parse(file: UploadFile = File(...)):
    try:
        if not file.filename.lower().endswith(".pdf"):
            return JSONResponse(status_code=400, content={"detail": "Yalnızca PDF dosyası yükleyebilirsiniz"})
        icerik = await file.read()
        if not icerik:
            return JSONResponse(status_code=400, content={"detail": "Dosya boş"})
        
        dosya_adi = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
        
        db = SessionLocal()
        try:
            yeni_dosya = DosyaDepo(dosya_adi=dosya_adi, veri=icerik)
            db.add(yeni_dosya)
            db.commit()
        except Exception as e:
            db.rollback()
            return JSONResponse(status_code=500, content={"detail": f"Dosya veritabanına kaydedilemedi: {str(e)}"})

        ayiklanan = ayikla_police_pdf(icerik)
        ayiklanan["pdf_dosya_adi"] = dosya_adi
        
        if ayiklanan.get("sigorta_sirketi"):
            ayiklanan["sigorta_sirketi"] = standardize_metin(ayiklanan["sigorta_sirketi"], SIRKET_ESLESMELERI)
        if ayiklanan.get("sigorta_turu"):
            ayiklanan["sigorta_turu"] = standardize_metin(ayiklanan["sigorta_turu"], BRANS_ESLESMELERI)

        try:
            tckn = ayiklanan.get("tckn")
            bulunan_musteri = None
            if tckn and "**" not in str(tckn):
                bulunan_musteri = db.query(Musteri).filter(Musteri.tc_kimlik == tckn).first()
            
            if not bulunan_musteri:
                norm_ad = normalize_string(ayiklanan.get("ad"))
                norm_soyad = normalize_string(ayiklanan.get("soyad"))
                tum_musteriler = db.query(Musteri).all()
                for m in tum_musteriler:
                    if normalize_string(m.ad) == norm_ad and normalize_string(m.soyad) == norm_soyad:
                        bulunan_musteri = m
                        break

            if bulunan_musteri:
                ayiklanan["musteri"] = {
                    "id": bulunan_musteri.id,
                    "ad": bulunan_musteri.ad,
                    "soyad": bulunan_musteri.soyad,
                    "telefon": bulunan_musteri.telefon,
                    "portfoy_sorumlusu": bulunan_musteri.portfoy_sorumlusu
                }
                ayiklanan["musteri_eslesti"] = True
                ayiklanan["mesaj"] = f"Mevcut müşteri bulundu: {bulunan_musteri.ad} {bulunan_musteri.soyad} ({bulunan_musteri.portfoy_sorumlusu})."
            else:
                ayiklanan["musteri_eslesti"] = False
                ayiklanan["mesaj"] = "Yeni veya maskeli isim tespit edildi. Lütfen listeden mevcut müşteriyi seçin veya kaydedin."
        finally:
            db.close()

        return ayiklanan
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"PDF okuma hatası: {str(e)}"})

# ==================== AKILLI ASİSTAN (GEMINI 3.5 FLASH LITE) ====================
@app.post("/api/ai-asistan")
async def api_ai_asistan(payload: dict):
    soru = payload.get("soru")
    gecmis = payload.get("gecmis", [])
    
    if not soru:
        raise HTTPException(status_code=400, detail="Soru alanı boş bırakılamaz.")
    
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500, 
            detail="API Anahtarı bulunamadı! Lütfen Render panelinden GOOGLE_API_KEY değişkenini ekleyin."
        )
        
    genai.configure(api_key=api_key.strip())
    bugun_tarihi = datetime.now().strftime("%d %B %Y")
    
    system_instruction = f"""
    Sen sigorta acentelerine teknik danışmanlık veren doğrudan, net ve hızlı bir yapay zekasın. 
    Bugünün tarihi: {bugun_tarihi}.
    
    YASAKLAR: '20 yıllık tecrübeme dayanarak', 'Bir yapay zeka olarak', 'Size yardımcı olmaktan memnuniyet duyarım', 'Uzman bir asistan olarak' gibi saçma, robotik veya laf kalabalığı yapan hiçbir giriş cümlesi KULLANMAYACAKSIN. Doğrudan konuya girip cevabı ver. 
    
    GÜNCELLİK VE KAYNAK KURALLARI (ÇOK ÖNEMLİ):
    1. İnternette arama yaparken her zaman EN YENİ TARİHLİ ve GÜNCEL kaynakları baz al. Sigortacılıkta ve mevzuatta eski tarihli yazılar, mahkeme kararları veya blog yazıları geçersiz olabilir. Arama sonuçlarındaki tarihleri kontrol et ve eski bilgileri eleyip her zaman en son yürürlüğe giren kuralı söyle.
    2. Eğer bir kanun, mevzuat, teminat tutarı veya kural yakın zamanda değişmişse, bunu fark et ve KESİNLİKLE güncel olan durumu (yeni mevzuatı) aktar.
    3. Verdiğin yasal veya finansal bilgilerin kaynağını ve tarihini cevabının içinde mutlaka açıkça belirt (Örn: "1 Temmuz 2026 tarihli Resmî Gazete'de yayımlanan mevzuata göre..." veya "SEDDK'nın son yayınladığı genelgeye göre...").
    4. Konuşma geçmişini dikkate alarak devam sorularına mantıklı yanıtlar ver ve net sayılar kullan.
    """
    
    try:
        model = genai.GenerativeModel(
            model_name='gemini-3.5-flash-lite',
            system_instruction=system_instruction,
            tools='google_search_retrieval'
        )
        
        formatted_history = []
        for msg in gecmis:
            role = "user" if msg.get("role") == "user" else "model"
            formatted_history.append({"role": role, "parts": [msg.get("content")]})
            
        chat = model.start_chat(history=formatted_history)
        response = chat.send_message(soru)
        
        return {"cevap": response.text}
        
    except Exception as e:
        try:
            model = genai.GenerativeModel(
                model_name='gemini-3.5-flash-lite',
                system_instruction=system_instruction
            )
            chat = model.start_chat(history=formatted_history)
            response = chat.send_message(soru)
            return {"cevap": response.text}
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Analiz sırasında hata oluştu: {str(e2)}")