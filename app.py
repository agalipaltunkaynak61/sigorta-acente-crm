import os
import urllib.parse
import calendar
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, List

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text, LargeBinary, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.orm.session import Session

from pdf_parser import ayikla_police_pdf

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DERSLER_DIR = BASE_DIR / "dersler"
DERSLER_DIR.mkdir(exist_ok=True)

# =========================================================================
# 🔴 ASLA BOZULMAYACAK SABİT KURALLAR (KULLANICI TALEBİ)
# =========================================================================

ZORUNLU_SIRKETLER = [
    "Aksigorta", "Allianz Sigorta", "Anadolu Sigorta", "Ankara Sigorta", "Axa Sigorta",
    "Bereket Sigorta", "Bupa Acıbadem Sigorta", "Doğa Sigorta", "Eureko Sigorta",
    "HDI Sigorta", "Hepiyi Sigorta", "Koru Sigorta", "Magdeburger Sigorta",
    "Mapfre Sigorta", "Neova Sigorta", "Quick Sigorta", "Ray Sigorta", "Sompo Sigorta",
    "Türkiye Sigorta", "Unico Sigorta", "Zurich Sigorta"
]

ZORUNLU_DANISMANLAR = [
    "Muammer Altunkaynak", "İhsan Berat Altunkaynak", "Sezai Yağcı",
    "Serhat Altunkaynak", "Ahmet Galip Altunkaynak"
]

ZORUNLU_BRANSLAR = [
    "DASK", "Konut ve Eşya Sigortaları", "Trafik Sigortası", "Kasko Sigortası",
    "Ferdi Kaza", "İş Yeri", "Nakliyat", "Tamamlayıcı Sağlık Sigortası",
    "Tarım", "Yat / Denizcilik", "Allrisk", "Özel Paket / Destek"
]

YENILENEBILIR_BRANSLAR = [
    "Trafik Sigortası", "Kasko Sigortası", "Tamamlayıcı Sağlık Sigortası", 
    "DASK", "Konut ve Eşya Sigortaları", "İş Yeri", "Ferdi Kaza"
]

SIRKET_ESLESMELERI = {
    "türkiye": "Türkiye Sigorta", "turkiye": "Türkiye Sigorta", "anonim": "Türkiye Sigorta", "a.ş.": "Türkiye Sigorta",
    "axa": "Axa Sigorta", "doğa": "Doğa Sigorta", "doga": "Doğa Sigorta",
    "ak sigorta": "Aksigorta", "aksigorta": "Aksigorta", "allianz": "Allianz Sigorta",
    "anadolu": "Anadolu Sigorta", "ankara": "Ankara Sigorta", "bereket": "Bereket Sigorta",
    "bupa": "Bupa Acıbadem Sigorta", "acıbadem": "Bupa Acıbadem Sigorta", "eureko": "Eureko Sigorta",
    "hdi": "HDI Sigorta", "hepiyi": "Hepiyi Sigorta", "koru": "Koru Sigorta",
    "magdeburger": "Magdeburger Sigorta", "mapfre": "Mapfre Sigorta", "neova": "Neova Sigorta",
    "quick": "Quick Sigorta", "ray": "Ray Sigorta", "sompo": "Sompo Sigorta",
    "unico": "Unico Sigorta", "zurich": "Zurich Sigorta"
}

BRANS_ESLESMELERI = {
    "trafik": "Trafik Sigortası", "kasko": "Kasko Sigortası", 
    "tamamlayıcı": "Tamamlayıcı Sağlık Sigortası", "tss": "Tamamlayıcı Sağlık Sigortası",
    "dask": "DASK", "deprem": "DASK",
    "iş yeri": "İş Yeri", "kurumsal": "İş Yeri", "işyeri": "İş Yeri",
    "konut": "Konut ve Eşya Sigortaları", "eşya": "Konut ve Eşya Sigortaları",
    "ferdi kaza": "Ferdi Kaza", "nakliyat": "Nakliyat", "tarım": "Tarım",
    "yat": "Yat / Denizcilik", "denizcilik": "Yat / Denizcilik",
    "allrisk": "Allrisk", "özel": "Özel Paket / Destek", "destek": "Özel Paket / Destek"
}

def standardize_metin(metin, sozluk, zorunlu_liste=None):
    if not metin: return "Diğer"
    m_lower = metin.strip().lower()
    for key, val in sozluk.items():
        if key in m_lower: return val
    if zorunlu_liste:
        for zorunlu in zorunlu_liste:
            if zorunlu.lower() in m_lower: return zorunlu
    return metin.strip().title()

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
    try: yield db
    finally: db.close()

def eski_policeleri_otomatik_temizle():
    db = SessionLocal()
    try:
        sinir_tarihi = date.today() - timedelta(days=90)
        eski_policeler = db.query(Police).filter(Police.bitis_tarihi < sinir_tarihi).all()
        if len(eski_policeler) > 0:
            for p in eski_policeler: db.delete(p)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

app = FastAPI(title="Altun Kardeşler CRM", version="3.9.1")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()
    eski_policeleri_otomatik_temizle()

def normalize_string(s: str) -> str:
    if not s: return ""
    s = s.upper().replace("İ", "I").replace("I", "I").replace("Ş", "S").replace("Ğ", "G").replace("Ü", "U").replace("Ö", "O").replace("Ç", "C")
    return s.strip()

def gelismis_firma_temizle(s: str) -> str:
    if not s: return ""
    s = normalize_string(s)
    s = re.sub(r'\b(LTD|STI|SANAYI|SAN|TICARET|TIC|AS|A\.S\.|LIMITED|SIRKETI|VE)\b', '', s)
    s = re.sub(r'[^A-Z0-9]', '', s)
    return s

# --- YENİ EKLENEN ENDPOINTLER ---
@app.get("/vektorel_logo_2.svg")
def serve_logo():
    logo_path = BASE_DIR / "vektorel_logo_2.svg"
    if logo_path.exists():
        return FileResponse(logo_path, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Logo bulunamadı")

@app.get("/api/sabitler")
def api_sabitler():
    return {
        "sirketler": sorted(ZORUNLU_SIRKETLER),
        "branslar": sorted(ZORUNLU_BRANSLAR)
    }
# --------------------------------

@app.get("/api/sistemi-temizle")
def sistemi_temizle(db: Session = Depends(get_db)):
    try:
        policeler = db.query(Police).all()
        for p in policeler:
            if p.sigorta_sirketi: p.sigorta_sirketi = standardize_metin(p.sigorta_sirketi, SIRKET_ESLESMELERI, ZORUNLU_SIRKETLER)
            if p.sigorta_turu: p.sigorta_turu = standardize_metin(p.sigorta_turu, BRANS_ESLESMELERI, ZORUNLU_BRANSLAR)
        
        musteriler = db.query(Musteri).all()
        for m in musteriler:
            if m.ad: m.ad = m.ad.strip().upper() 
            if m.soyad: m.soyad = m.soyad.strip().upper() 
            if not m.portfoy_sorumlusu or m.portfoy_sorumlusu not in ZORUNLU_DANISMANLAR:
                m.portfoy_sorumlusu = "Muammer Altunkaynak"

        db.commit()

        birlesen_sayisi = 0
        tum_musteriler = db.query(Musteri.id, Musteri.ad, Musteri.soyad, Musteri.tc_kimlik, Musteri.telefon).all()
        gruplar = {}
        for m in tum_musteriler:
            anahtar = gelismis_firma_temizle(f"{m.ad} {m.soyad}")
            if not anahtar: continue
            if anahtar not in gruplar: gruplar[anahtar] = []
            gruplar[anahtar].append(m.id)

        for anahtar, m_ids in gruplar.items():
            if len(m_ids) > 1:
                ana_id = m_ids[0]
                kopya_ids = m_ids[1:]
                for kopya_id in kopya_ids:
                    db.query(Police).filter(Police.musteri_id == kopya_id).update({"musteri_id": ana_id})
                    db.query(Musteri).filter(Musteri.id == kopya_id).delete()
                    birlesen_sayisi += 1

        db.commit()
        return {"mesaj": f"Temizlik tamamlandı! {birlesen_sayisi} adet mükerrer hesap başarıyla birleştirildi."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/pazarlama")
def api_pazarlama(db: Session = Depends(get_db)):
    bugun = date.today()
    aktif_policeler = db.query(Police).filter(Police.durum == 'aktif', Police.bitis_tarihi >= bugun).all()
    
    musteri_dict = {}
    for p in aktif_policeler:
        if p.musteri_id not in musteri_dict: musteri_dict[p.musteri_id] = []
        musteri_dict[p.musteri_id].append(p)
        
    musteriler = {m.id: m for m in db.query(Musteri).filter(Musteri.id.in_(musteri_dict.keys())).all()}
    
    sonuclar = {
        "dask_konut": [], "arac_konut": [], "arac_tss": [],
        "konut_ferdi": [], "tss_oss": [], "tss_kasko": []
    }
    
    for m_id, policeler in musteri_dict.items():
        m = musteriler.get(m_id)
        if not m: continue
        branslar = set(p.sigorta_turu for p in policeler if p.sigorta_turu)
        
        def get_expiring(b_types):
            for p in policeler:
                if p.sigorta_turu in b_types and (p.bitis_tarihi - bugun).days <= 30: return p
            return None

        ad_soyad = f"{m.ad} {m.soyad}".strip()
        tel = m.telefon or "-"

        if "DASK" in branslar and "Konut ve Eşya Sigortaları" not in branslar:
            p = get_expiring(["DASK"])
            if p: sonuclar["dask_konut"].append({"musteri_id": m.id, "ad_soyad": ad_soyad, "telefon": tel, "dayanak": p.sigorta_turu, "bitis": p.bitis_tarihi.strftime("%d.%m.%Y")})
                
        if ("Kasko Sigortası" in branslar or "Trafik Sigortası" in branslar) and "Konut ve Eşya Sigortaları" not in branslar:
            p = get_expiring(["Kasko Sigortası", "Trafik Sigortası"])
            if p: sonuclar["arac_konut"].append({"musteri_id": m.id, "ad_soyad": ad_soyad, "telefon": tel, "dayanak": p.sigorta_turu, "bitis": p.bitis_tarihi.strftime("%d.%m.%Y")})

        if ("Kasko Sigortası" in branslar or "Trafik Sigortası" in branslar) and "Tamamlayıcı Sağlık Sigortası" not in branslar:
            p = get_expiring(["Kasko Sigortası", "Trafik Sigortası"])
            if p: sonuclar["arac_tss"].append({"musteri_id": m.id, "ad_soyad": ad_soyad, "telefon": tel, "dayanak": p.sigorta_turu, "bitis": p.bitis_tarihi.strftime("%d.%m.%Y")})

        if "Konut ve Eşya Sigortaları" in branslar and "Ferdi Kaza" not in branslar:
            p = get_expiring(["Konut ve Eşya Sigortaları"])
            if p: sonuclar["konut_ferdi"].append({"musteri_id": m.id, "ad_soyad": ad_soyad, "telefon": tel, "dayanak": p.sigorta_turu, "bitis": p.bitis_tarihi.strftime("%d.%m.%Y")})

        if "Tamamlayıcı Sağlık Sigortası" in branslar and "Özel Sağlık Sigortası" not in branslar:
            p = get_expiring(["Tamamlayıcı Sağlık Sigortası"])
            if p: sonuclar["tss_oss"].append({"musteri_id": m.id, "ad_soyad": ad_soyad, "telefon": tel, "dayanak": p.sigorta_turu, "bitis": p.bitis_tarihi.strftime("%d.%m.%Y")})
                
        if "Tamamlayıcı Sağlık Sigortası" in branslar and "Kasko Sigortası" not in branslar:
            p = get_expiring(["Tamamlayıcı Sağlık Sigortası"])
            if p: sonuclar["tss_kasko"].append({"musteri_id": m.id, "ad_soyad": ad_soyad, "telefon": tel, "dayanak": p.sigorta_turu, "bitis": p.bitis_tarihi.strftime("%d.%m.%Y")})
                    
    return sonuclar

@app.get("/api/dersler/liste")
def api_dersler_liste():
    kategoriler = {
        "Kasko Sigortası": [], 
        "Tamamlayıcı Sağlık Sigortası": [], 
        "Özel Sağlık Sigortası": [], 
        "Konut Sigortası": [], 
        "İş Yeri Sigortası": [],
        "Mühendislik & All Risk": [],
        "Nakliyat Sigortası": [],
        "Site & Ortak Alan": []
    }
    
    if not DERSLER_DIR.exists(): return kategoriler
    for f in DERSLER_DIR.iterdir():
        if f.is_file() and f.name.lower().endswith(".pdf"):
            fname = f.name.upper()
            
            if "KASKO" in fname: 
                kategoriler["Kasko Sigortası"].append(f.name)
            elif "TSS" in fname or "TAMAMLAYICI" in fname: 
                kategoriler["Tamamlayıcı Sağlık Sigortası"].append(f.name)
            elif "OSS" in fname or "ÖSS" in fname or "SAĞLIK" in fname or "SAGLIK" in fname or "ÖZEL" in fname: 
                kategoriler["Özel Sağlık Sigortası"].append(f.name)
            elif "KONUT" in fname or "DASK" in fname or "DEPREM" in fname: 
                kategoriler["Konut Sigortası"].append(f.name)
            elif "İŞYERİ" in fname or "ISYERI" in fname: 
                kategoriler["İş Yeri Sigortası"].append(f.name)
            elif "ALLRISK" in fname or "ALLRİSK" in fname or "MÜHENDİSLİK" in fname or "INSAAT" in fname or "İNŞAAT" in fname: 
                kategoriler["Mühendislik & All Risk"].append(f.name)
            elif "NAKLİYAT" in fname or "NAKLIYAT" in fname: 
                kategoriler["Nakliyat Sigortası"].append(f.name)
            elif "SİTE" in fname or "SITE" in fname: 
                kategoriler["Site & Ortak Alan"].append(f.name)
            else:
                kategoriler["İş Yeri Sigortası"].append(f.name)
                
    return {k: sorted(v) for k, v in kategoriler.items() if len(v) > 0}

@app.get("/dersler/{dosya_adi}")
def get_ders_pdf(dosya_adi: str):
    dosya_adi = urllib.parse.unquote(dosya_adi)
    hedef = DERSLER_DIR / dosya_adi
    if hedef.exists() and hedef.is_file(): return FileResponse(hedef, media_type="application/pdf")
    for f in DERSLER_DIR.iterdir():
        if f.is_file() and f.name.lower() == dosya_adi.lower(): return FileResponse(f, media_type="application/pdf")
    raise HTTPException(status_code=404, detail="Not Found")

def parse_tarih(val) -> date:
    if isinstance(val, date): return val
    if isinstance(val, str):
        val = val.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try: return datetime.strptime(val, fmt).date()
            except ValueError: continue
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
    if disaridan: komisyon = komisyon / 2.0
    return komisyon

def uyar_seviyesi(kalan_gun: int) -> str:
    if kalan_gun < 0: return "suresi_dolmus"
    if kalan_gun <= 3: return "kirmizi"
    if kalan_gun <= 7: return "sari"
    return "uzak"

def police_to_out(p: Police, db_session: Session) -> dict:
    bugun = date.today()
    kalan = (p.bitis_tarihi - bugun).days if p.bitis_tarihi else 0
    suresi_dolmus = (p.bitis_tarihi and p.bitis_tarihi < bugun) or (p.durum == "iptal")
    musteri = db_session.query(Musteri).filter(Musteri.id == p.musteri_id).first()
    komisyon = hesapla_komisyon(p.sigorta_turu, p.sigorta_sirketi, p.prim)
    return {
        "id": p.id, "musteri_id": p.musteri_id, "police_no": p.police_no, "sigorta_turu": p.sigorta_turu,
        "sigorta_sirketi": p.sigorta_sirketi, "islem_turu": p.islem_turu or "Yeni Poliçe", "baslangic_tarihi": p.baslangic_tarihi,
        "bitis_tarihi": p.bitis_tarihi, "prim": p.prim, "durum": p.durum, "arac_bilgisi": p.arac_bilgisi,
        "varlik_bilgisi": p.varlik_bilgisi, "aciklama": p.aciklama, "pdf_dosya_adi": p.pdf_dosya_adi,
        "kalan_gun": 0 if suresi_dolmus else kalan, "suresi_dolmus": suresi_dolmus,
        "uyar_seviyesi": "suresi_dolmus" if suresi_dolmus else uyar_seviyesi(kalan),
        "musteri_ad": musteri.ad if musteri else None, "musteri_soyad": musteri.soyad if musteri else None,
        "musteri_telefon": musteri.telefon if musteri else None,
        "musteri_portfoy_sorumlusu": musteri.portfoy_sorumlusu if musteri else None, "komisyon": komisyon
    }

@app.get("/")
def ana_sayfa():
    index_yolu = BASE_DIR / "index.html"
    if index_yolu.exists(): return HTMLResponse(index_yolu.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html bulunamadı!</h1>")

@app.get("/api/ozet")
def api_ozet(db: Session = Depends(get_db)):
    bugun = date.today()
    return {
        "musteri_sayisi": db.query(Musteri).count(),
        "police_sayisi": db.query(Police).count(),
        "aktif_police": db.query(Police).filter(Police.durum == "aktif", Police.bitis_tarihi >= bugun).count(),
        "yaklasan_7_gun": db.query(Police).filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=7)).count()
    }

@app.get("/api/musteriler")
def api_musteri_listele(q: Optional[str] = None, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100000), db: Session = Depends(get_db)):
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
            "adres": m.adres, "portfoy_sorumlusu": m.portfoy_sorumlusu, "aktif_police_sayilari": dagilim
        })
    return {"items": sonuc, "total": total, "page": page, "limit": limit, "pages": (total + limit - 1) // limit if limit > 0 else 1}

@app.post("/api/musteriler", status_code=201)
def api_musteri_olustur(payload: dict, db: Session = Depends(get_db)):
    tckn = payload.get("tc_kimlik")
    ad = (payload.get("ad") or "").strip().upper() 
    soyad = (payload.get("soyad") or "").strip().upper() 
    
    danisman = payload.get("portfoy_sorumlusu")
    if not danisman or str(danisman).strip().lower() in ["", "atanmadı", "null", "none"]:
        danisman = "Muammer Altunkaynak"
    elif danisman == "Sezai Karakoç": 
        danisman = "Sezai Yağcı"
    
    if danisman not in ZORUNLU_DANISMANLAR:
        danisman = "Muammer Altunkaynak"

    existing = None
    if tckn and "**" not in str(tckn):
        existing = db.query(Musteri).filter(Musteri.tc_kimlik == tckn).first()
    
    if not existing and ad and soyad and "**" not in ad:
        norm_anahtar = gelismis_firma_temizle(f"{ad} {soyad}")
        tum_musteriler = db.query(Musteri).all()
        for m in tum_musteriler:
            if gelismis_firma_temizle(f"{m.ad} {m.soyad}") == norm_anahtar:
                existing = m; break

    if existing:
        for k, v in payload.items():
            if v and not str(v).startswith("*"): setattr(existing, k, v)
        existing.portfoy_sorumlusu = danisman
        db.commit(); db.refresh(existing)
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
    
    if m.ad: m.ad = m.ad.strip().upper()
    if m.soyad: m.soyad = m.soyad.strip().upper()
    
    if not m.portfoy_sorumlusu or str(m.portfoy_sorumlusu).strip().lower() in ["", "atanmadı", "null"]:
        m.portfoy_sorumlusu = "Muammer Altunkaynak"
    elif m.portfoy_sorumlusu == "Sezai Karakoç": 
        m.portfoy_sorumlusu = "Sezai Yağcı"
        
    if m.portfoy_sorumlusu not in ZORUNLU_DANISMANLAR:
        m.portfoy_sorumlusu = "Muammer Altunkaynak"
        
    db.commit(); db.refresh(m)
    return m

@app.get("/api/policeler")
def api_police_listele(musteri_id: Optional[int] = None, q: Optional[str] = None, aktif: Optional[str] = None, page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100000), db: Session = Depends(get_db)):
    query = db.query(Police)
    if musteri_id: query = query.filter(Police.musteri_id == musteri_id)
    if q or aktif: query = query.outerjoin(Musteri, Police.musteri_id == Musteri.id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((Police.police_no.ilike(like)) | (Police.sigorta_sirketi.ilike(like)) | (Police.sigorta_turu.ilike(like)) | (Musteri.ad.ilike(like)) | (Musteri.soyad.ilike(like)))
        
    bugun = date.today()
    if aktif == 'aktif': query = query.filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun)
    elif aktif == 'eski': query = query.filter((Police.durum == "iptal") | (Police.bitis_tarihi < bugun))
        
    total = query.count()
    kayitlar = query.order_by(Police.bitis_tarihi.desc()).offset((page - 1) * limit).limit(limit).all()
    
    return {"items": [police_to_out(p, db) for p in kayitlar], "total": total, "page": page, "limit": limit, "pages": (total + limit - 1) // limit if limit > 0 else 1}

@app.get("/api/policeler/yaklasan")
def api_yaklasan(gun: int = 7, db: Session = Depends(get_db)):
    bugun = date.today()
    kayitlar = db.query(Police).filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=gun)).all()
    return [police_to_out(p, db) for p in kayitlar]

@app.get("/api/policeler/bu-ay")
def api_bu_ay(db: Session = Depends(get_db)):
    bugun = date.today()
    ilk_gun = bugun.replace(day=1)
    son_gun_sayisi = calendar.monthrange(bugun.year, bugun.month)[1]
    son_gun = bugun.replace(day=son_gun_sayisi)
    kayitlar = db.query(Police).filter(
        Police.durum != "iptal", 
        Police.bitis_tarihi >= ilk_gun, 
        Police.bitis_tarihi <= son_gun,
        Police.sigorta_turu.in_(YENILENEBILIR_BRANSLAR)
    ).all()
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
        
        temiz_sirket = standardize_metin(payload.get("sigorta_sirketi"), SIRKET_ESLESMELERI, ZORUNLU_SIRKETLER)
        temiz_brans = standardize_metin(payload.get("sigorta_turu"), BRANS_ESLESMELERI, ZORUNLU_BRANSLAR)

        ana_police = None
        if police_no and ("plaka" in str(islem_turu).lower() or "zeyil" in str(islem_turu).lower() or "tahakkuk" in str(islem_turu).lower() or "ek" in str(islem_turu).lower()):
            ana_police = db.query(Police).filter(Police.police_no == police_no).first()

        if ana_police:
            if arac_yeni: ana_police.arac_bilgisi = arac_yeni
            if prim_deger: ana_police.prim = (ana_police.prim or 0) + prim_deger
            ana_police.aciklama = (ana_police.aciklama or "") + f" | Ek Belge ({islem_turu}): Prim +{prim_deger} TL, Araç: {arac_yeni}"
            if payload.get("pdf_dosya_adi"): ana_police.pdf_dosya_adi = payload.get("pdf_dosya_adi")
            db.commit(); db.refresh(ana_police)
            return police_to_out(ana_police, db)

        police = Police(
            musteri_id=payload.get("musteri_id"), police_no=police_no, sigorta_turu=temiz_brans, sigorta_sirketi=temiz_sirket,
            islem_turu=islem_turu, baslangic_tarihi=baslangic, bitis_tarihi=bitis, prim=prim_deger,
            durum=str(payload.get("durum", "aktif")).lower(), arac_bilgisi=arac_yeni, varlik_bilgisi=payload.get("varlik_bilgisi"),
            aciklama=payload.get("aciklama"), pdf_dosya_adi=payload.get("pdf_dosya_adi")
        )
        db.add(police); db.commit(); db.refresh(police)
        return police_to_out(police, db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/upload-parse")
async def api_upload_parse(file: UploadFile = File(...)):
    try:
        if not file.filename.lower().endswith(".pdf"): return JSONResponse(status_code=400, content={"detail": "Yalnızca PDF dosyası yükleyebilirsiniz"})
        icerik = await file.read()
        if not icerik: return JSONResponse(status_code=400, content={"detail": "Dosya boş"})
        
        dosya_adi = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
        db = SessionLocal()
        try:
            yeni_dosya = DosyaDepo(dosya_adi=dosya_adi, veri=icerik)
            db.add(yeni_dosya); db.commit()
        except Exception as e:
            db.rollback(); return JSONResponse(status_code=500, content={"detail": f"Dosya veritabanına kaydedilemedi: {str(e)}"})

        ayiklanan = ayikla_police_pdf(icerik)
        ayiklanan["pdf_dosya_adi"] = dosya_adi
        if ayiklanan.get("sigorta_sirketi"): ayiklanan["sigorta_sirketi"] = standardize_metin(ayiklanan["sigorta_sirketi"], SIRKET_ESLESMELERI, ZORUNLU_SIRKETLER)
        if ayiklanan.get("sigorta_turu"): ayiklanan["sigorta_turu"] = standardize_metin(ayiklanan["sigorta_turu"], BRANS_ESLESMELERI, ZORUNLU_BRANSLAR)

        try:
            tckn = ayiklanan.get("tckn")
            bulunan_musteri = None
            if tckn and "**" not in str(tckn): bulunan_musteri = db.query(Musteri).filter(Musteri.tc_kimlik == tckn).first()
            if not bulunan_musteri:
                norm_anahtar = gelismis_firma_temizle(f"{ayiklanan.get('ad', '')} {ayiklanan.get('soyad', '')}")
                tum_musteriler = db.query(Musteri).all()
                for m in tum_musteriler:
                    if gelismis_firma_temizle(f"{m.ad} {m.soyad}") == norm_anahtar:
                        bulunan_musteri = m; break

            if bulunan_musteri:
                ayiklanan["musteri"] = {"id": bulunan_musteri.id, "ad": bulunan_musteri.ad, "soyad": bulunan_musteri.soyad, "telefon": bulunan_musteri.telefon, "portfoy_sorumlusu": bulunan_musteri.portfoy_sorumlusu}
                ayiklanan["musteri_eslesti"] = True
                ayiklanan["mesaj"] = f"Mevcut müşteri bulundu: {bulunan_musteri.ad} {bulunan_musteri.soyad}"
            else:
                ayiklanan["musteri_eslesti"] = False
                ayiklanan["mesaj"] = "Yeni müşteri. Lütfen bilgileri kontrol edip kaydedin."
        finally:
            db.close()
        return ayiklanan
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"PDF okuma hatası: {str(e)}"})

class FinansalRaporRequest(BaseModel):
    baslangic: Optional[str] = None
    bitis: Optional[str] = None
    sirket: Optional[str] = None
    danisman: Optional[str] = None
    branslar: Optional[List[str]] = None

@app.get("/api/finansal/filtreler")
def api_fin_filtreler(db: Session = Depends(get_db)):
    return {
        "sirketler": sorted(ZORUNLU_SIRKETLER),
        "danismanlar": ZORUNLU_DANISMANLAR,
        "branslar": sorted(ZORUNLU_BRANSLAR)
    }

@app.post("/api/finansal/rapor")
def api_fin_rapor(payload: FinansalRaporRequest, db: Session = Depends(get_db)):
    query = db.query(Police).join(Musteri, Police.musteri_id == Musteri.id).filter(Police.durum != "iptal")

    if payload.baslangic:
        query = query.filter(Police.baslangic_tarihi >= parse_tarih(payload.baslangic))
    if payload.bitis:
        query = query.filter(Police.baslangic_tarihi <= parse_tarih(payload.bitis))
    if payload.sirket:
        query = query.filter(Police.sigorta_sirketi == payload.sirket)
    if payload.danisman:
        query = query.filter(Musteri.portfoy_sorumlusu == payload.danisman)
    if payload.branslar and len(payload.branslar) > 0:
        query = query.filter(Police.sigorta_turu.in_(payload.branslar))

    policeler = query.all()

    ciro = sum(p.prim or 0 for p in policeler)
    kar = sum(hesapla_komisyon(p.sigorta_turu, p.sigorta_sirketi, p.prim) for p in policeler)
    adet = len(policeler)

    brans_dagilimi = {}
    sirket_dagilimi = {}
    for p in policeler:
        b = p.sigorta_turu or "Diğer"
        s = p.sigorta_sirketi or "Diğer"
        brans_dagilimi[b] = brans_dagilimi.get(b, 0) + 1
        sirket_dagilimi[s] = sirket_dagilimi.get(s, 0) + 1

    return {
        "ciro": ciro, 
        "kar": kar, 
        "adet": adet,
        "brans_dagilimi": dict(sorted(brans_dagilimi.items(), key=lambda item: item[1], reverse=True)),
        "sirket_dagilimi": dict(sorted(sirket_dagilimi.items(), key=lambda item: item[1], reverse=True))
    }