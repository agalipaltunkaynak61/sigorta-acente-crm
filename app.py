import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, joinedload, sessionmaker
from sqlalchemy.orm.session import Session

from pdf_parser import ayikla_police_pdf

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

DB_PATH = BASE_DIR / "acente_crm.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Musteri(Base):
    __tablename__ = "musteriler"
    id = Column(Integer, primary_key=True, index=True)
    ad = Column(String(100), nullable=False)
    soyad = Column(String(100), nullable=False)
    telefon = Column(String(50), nullable=False)
    email = Column(String(100), nullable=True)
    tc_kimlik = Column(String(50), nullable=True)
    adres = Column(Text, nullable=True)
    notlar = Column(Text, nullable=True)
    portfoy_sorumlusu = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Police(Base):
    __tablename__ = "policeler"
    id = Column(Integer, primary_key=True, index=True)
    musteri_id = Column(Integer, nullable=False)
    police_no = Column(String(80), nullable=False)
    sigorta_turu = Column(String(120), nullable=False)
    sigorta_sirketi = Column(String(100), nullable=True)
    islem_turu = Column(String(50), default="Yeni Poliçe")
    baslangic_tarihi = Column(Date, nullable=False)
    bitis_tarihi = Column(Date, nullable=False)
    prim = Column(Float, nullable=True)
    durum = Column(String(30), default="aktif")
    aciklama = Column(Text, nullable=True)
    pdf_dosya_adi = Column(String(255), nullable=True)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app = FastAPI(title="Altun Kardeşler CRM", version="3.1.0")

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

def parse_tarih(val) -> date:
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        val = val.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
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

@app.get("/Logo.png")
def get_logo():
    logo_yolu = BASE_DIR / "Logo.png"
    if logo_yolu.exists():
        return FileResponse(logo_yolu, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo bulunamadı")

@app.get("/", response_class=HTMLResponse)
def ana_sayfa():
    index_yolu = BASE_DIR / "index.html"
    if index_yolu.exists():
        return index_yolu.read_text(encoding="utf-8")
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
def api_musteri_listele(q: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Musteri)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((Musteri.ad.ilike(like)) | (Musteri.soyad.ilike(like)) | (Musteri.telefon.ilike(like)))
    musteriler = query.all()
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
    return sonuc

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
    if not payload.get("telefon") or not payload.get("portfoy_sorumlusu"):
        raise HTTPException(status_code=400, detail="Telefon numarası ve Portföy Sorumlusu zorunludur!")
    m = Musteri(**payload)
    db.add(m); db.commit(); db.refresh(m)
    return m

@app.put("/api/musteriler/{id}")
def api_musteri_guncelle(id: int, payload: dict, db: Session = Depends(get_db)):
    m = db.query(Musteri).filter(Musteri.id == id).first()
    if not m: raise HTTPException(404, "Bulunamadı")
    for k, v in payload.items(): setattr(m, k, v)
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
def api_police_listele(musteri_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Police)
    if musteri_id: query = query.filter(Police.musteri_id == musteri_id)
    kayitlar = query.all()
    return [police_to_out(p, db) for p in kayitlar]

@app.get("/api/policeler/yaklasan")
def api_yaklasan(gun: int = 30, db: Session = Depends(get_db)):
    bugun = date.today()
    kayitlar = db.query(Police).filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=gun)).all()
    return [police_to_out(p, db) for p in kayitlar]

@app.delete("/api/policeler/{id}")
def api_police_sil(id: int, db: Session = Depends(get_db)):
    p = db.query(Police).filter(Police.id == id).first()
    if not p: raise HTTPException(404, "Bulunamadı")
    db.delete(p); db.commit()
    return {"ok": True}

# *** YENİ EKLENEN PDF GÖRÜNTÜLEME API'Sİ ***
@app.get("/api/policeler/{id}/pdf")
def api_pdf_goster(id: int, db: Session = Depends(get_db)):
    p = db.query(Police).filter(Police.id == id).first()
    if not p or not p.pdf_dosya_adi:
        raise HTTPException(status_code=404, detail="Bu poliçeye ait PDF bulunamadı.")
    
    hedef_yol = UPLOAD_DIR / p.pdf_dosya_adi
    if not hedef_yol.exists():
        # Render ücretsiz sürümde klasörler sunucu uyuyunca sıfırlandığı için bu hata çıkabilir
        raise HTTPException(status_code=404, detail="Dosya sunucuda bulunamadı. (Sistem Render'da uyku moduna geçtiği için dosya silinmiş olabilir)")
        
    return FileResponse(hedef_yol, media_type="application/pdf", filename=p.pdf_dosya_adi)

@app.post("/api/upload-parse")
async def api_upload_parse(file: UploadFile = File(...)):
    try:
        if not file.filename.lower().endswith(".pdf"):
            return JSONResponse(status_code=400, content={"detail": "Yalnızca PDF dosyası yükleyebilirsiniz"})
        icerik = await file.read()
        if not icerik:
            return JSONResponse(status_code=400, content={"detail": "Dosya boş"})
        
        dosya_adi = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
        hedef_yol = UPLOAD_DIR / dosya_adi
        hedef_yol.write_bytes(icerik)

        ayiklanan = ayikla_police_pdf(icerik)
        ayiklanan["pdf_dosya_adi"] = dosya_adi
        ayiklanan["mesaj"] = "Poliçe başarıyla okundu. Müşteri bilgilerini onaylayın."
        return ayiklanan
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"PDF okuma hatası: {str(e)}"})

@app.post("/api/upload-save")
async def api_upload_save(payload: dict, db: Session = Depends(get_db)):
    try:
        telefon = payload.get("telefon")
        danisman = payload.get("portfoy_sorumlusu")
        if not telefon or not danisman:
            raise HTTPException(status_code=400, detail="Telefon numarası ve Portföy Sorumlusu seçilmesi zorunludur!")

        tckn = (payload.get("tc_kimlik") or "").strip() or None
        musteri = None
        
        if payload.get("musteri_id"):
            musteri = db.query(Musteri).filter(Musteri.id == payload.get("musteri_id")).first()
            
        if not musteri and tckn:
            musteri = db.query(Musteri).filter(Musteri.tc_kimlik == tckn).first()

        if not musteri:
            musteri = Musteri(
                ad=payload.get("ad") or "Bilinmiyor",
                soyad=payload.get("soyad") or "Müşteri",
                tc_kimlik=tckn,
                telefon=telefon,
                portfoy_sorumlusu=danisman
            )
            db.add(musteri)
            db.commit()
            db.refresh(musteri)
        else:
            musteri.telefon = telefon
            musteri.portfoy_sorumlusu = danisman
            db.commit()

        baslangic = parse_tarih(payload.get("baslangic_tarihi"))
        bitis = parse_tarih(payload.get("bitis_tarihi"))
        prim_deger = float(payload.get("prim") or 0)

        police = Police(
            musteri_id=musteri.id,
            police_no=payload.get("police_no") or f"PLK-{int(datetime.utcnow().timestamp())}",
            sigorta_turu=payload.get("sigorta_turu") or "Genel",
            sigorta_sirketi=payload.get("sigorta_sirketi") or "Diğer",
            islem_turu=payload.get("islem_turu") or "Yeni Poliçe",
            baslangic_tarihi=baslangic,
            bitis_tarihi=bitis,
            prim=prim_deger,
            aciklama=payload.get("aciklama") or "",
            pdf_dosya_adi=payload.get("pdf_dosya_adi")
        )
        db.add(police)
        db.commit()
        
        return {"ok": True, "mesaj": "Kayıt başarılı"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Kayıt hatası: {str(e)}"})