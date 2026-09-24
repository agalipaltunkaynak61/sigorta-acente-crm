import calendar
import logging
import os
import re
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from database import (  # noqa: F401  (SessionLocal/Musteri/Police/init_db diğer scriptler için de buradan erişilir)
    MUSTERI_ALANLARI, VARSAYILAN_DANISMAN, DosyaDepo, Musteri, Police, SessionLocal, ad_anahtari,
    ascii_buyuk, gelismis_firma_temizle, get_db, init_db, musteri_bul, musteri_kaydet,
    musteri_verisi_temizle, police_bul, police_no_temizle, tarih_coz, veritabanini_temizle, yas_hesapla,
)
from ozel_gunler import bugunku_ozel_gunler, musterinin_bugunku_mesajlari, yaklasan_ozel_gunler
from pdf_parser import ayikla_police_pdf
from whatsapp_gonderici import mesaj_gonder

log = logging.getLogger("crm.app")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DERSLER_DIR = BASE_DIR / "dersler"
DERSLER_DIR.mkdir(exist_ok=True)

# =========================================================================
# 🔴 ASLA BOZULMAYACAK SABİT KURALLAR
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

ACENTE_KOMISYON_ORANLARI = {
    "DASK": 0.15, "Konut ve Eşya Sigortaları": 0.20, "Trafik Sigortası": 0.10,
    "Kasko Sigortası": 0.17, "Ferdi Kaza": 0.25, "İş Yeri": 0.18,
    "Nakliyat": 0.12, "Tamamlayıcı Sağlık Sigortası": 0.20,
    "Tarım": 0.10, "Yat / Denizcilik": 0.12, "Allrisk": 0.12, "Özel Paket / Destek": 0.25
}

# Anahtarlar özelden genele doğru sıralıdır; "anonim"/"a.ş." yalnızca son çare eşleşmesidir.
SIRKET_ESLESMELERI = {
    "türkiye": "Türkiye Sigorta", "turkiye": "Türkiye Sigorta",
    "axa": "Axa Sigorta", "doğa": "Doğa Sigorta", "doga": "Doğa Sigorta",
    "ak sigorta": "Aksigorta", "aksigorta": "Aksigorta", "allianz": "Allianz Sigorta",
    "anadolu": "Anadolu Sigorta", "ankara": "Ankara Sigorta", "bereket": "Bereket Sigorta",
    "bupa": "Bupa Acıbadem Sigorta", "acıbadem": "Bupa Acıbadem Sigorta", "eureko": "Eureko Sigorta",
    "hdi": "HDI Sigorta", "hepiyi": "Hepiyi Sigorta", "koru": "Koru Sigorta",
    "magdeburger": "Magdeburger Sigorta", "mapfre": "Mapfre Sigorta", "neova": "Neova Sigorta",
    "quick": "Quick Sigorta", "ray": "Ray Sigorta", "sompo": "Sompo Sigorta",
    "unico": "Unico Sigorta", "zurich": "Zurich Sigorta",
    "anonim": "Türkiye Sigorta", "a.ş.": "Türkiye Sigorta",
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

# Çapraz satış: sahip olunan branş → önerilecek branşlar
CAPRAZ_SATIS_KURALLARI = {
    "Kasko Sigortası": {
        "firsatlar": ["Tamamlayıcı Sağlık Sigortası", "Konut ve Eşya Sigortaları"],
        "taktik": 'Yüksek Gelir / Risk Bilinci Yüksek: Aracını kaskolatan müşteri harcama yapmaya yatkındır. "Aracınızı güvenceye aldık, peki sağlığınızı ve evinizi?" kurgusuyla yüksek dönüşüm sağlar.'
    },
    "DASK": {
        "firsatlar": ["Konut ve Eşya Sigortaları", "Ferdi Kaza"],
        "taktik": 'Zorunlu Alıcı / Eksiği Tamamlama: DASK sadece binanın kaba yapısını ve düşük limitleri kapsar. "DASK eşyalarınızı kapsamaz" söylemiyle konut/eşya poliçesi satma ihtimali çok yüksektir.'
    },
    "Trafik Sigortası": {
        "firsatlar": ["Kasko Sigortası", "Ferdi Kaza", "Özel Paket / Destek"],
        "taktik": 'Sadece Zorunluluk Satın Alan: Trafik sigortası karşı tarafı korur. Sürücüye "Kendi aracınız ve canınız güvende mi?" teklifiyle kasko, asistan paketleri veya ferdi kaza sunulur.'
    },
    "Tamamlayıcı Sağlık Sigortası": {
        "firsatlar": ["Kasko Sigortası", "Konut ve Eşya Sigortaları", "Ferdi Kaza"],
        "taktik": 'Aile ve Konfor Odaklı: Sağlığına ve ailesine önem veren kitle. Ev ve araç koruma çözümlerine olumlu yanıt verirler.'
    },
    "İş Yeri": {
        "firsatlar": ["Nakliyat", "Allrisk", "Ferdi Kaza"],
        "taktik": 'Ticari / B2B Segment: Ticari işletmelerin mal sevkiyatı için Nakliyat, montaj/proje riskleri için Allrisk ihtiyaçları doğar. Ayrıca iş yeri sahibine şahsi Kasko/TSS çapraz satışı yapılabilir.'
    },
    "Yat / Denizcilik": {
        "firsatlar": ["Kasko Sigortası", "Konut ve Eşya Sigortaları", "Allrisk"],
        "taktik": 'A+ Gelir Grubu: Premium müşteri segmentidir. Lüks araç, gayrimenkul ve özel varlık sigortalarında fiyat hassasiyetleri düşük, kabul oranları yüksektir.'
    },
    "Tarım": {
        "firsatlar": ["Trafik Sigortası", "Ferdi Kaza", "Konut ve Eşya Sigortaları"],
        "taktik": 'Üretici Segmenti: Geçim kaynağını sigortalayan üreticinin araç ve yaşam alanı koruma ihtiyaçlarına odaklanılır.'
    }
}

GENEL_SIRKET_ANAHTARLARI = {"anonim", "a.ş."}


def _eslesme_var(anahtar: str, metin_ascii: str) -> bool:
    k = ascii_buyuk(anahtar)
    if len(k) <= 3:  # axa, hdi, ray, tss, yat: kelime olarak aranır (TRAY, PAYATA gibi yanlış eşleşmeler olmasın)
        return re.search(rf"\b{re.escape(k)}\b", metin_ascii) is not None
    return k in metin_ascii


def standardize_metin(metin, sozluk, zorunlu_liste=None):
    if not metin:
        return "Diğer"
    m = ascii_buyuk(metin).strip()
    for anahtar, deger in sozluk.items():
        if anahtar not in GENEL_SIRKET_ANAHTARLARI and _eslesme_var(anahtar, m):
            return deger
    if sozluk is SIRKET_ESLESMELERI:
        for anahtar in GENEL_SIRKET_ANAHTARLARI:
            if ascii_buyuk(anahtar) in m:
                return sozluk[anahtar]
    for zorunlu in zorunlu_liste or []:
        if ascii_buyuk(zorunlu) in m:
            return zorunlu
    return str(metin).strip().title()


def standart_sirket(metin):
    return standardize_metin(metin, SIRKET_ESLESMELERI, ZORUNLU_SIRKETLER)


def standart_brans(metin):
    return standardize_metin(metin, BRANS_ESLESMELERI, ZORUNLU_BRANSLAR)


def komisyon_orani_getir(brans: str) -> float:
    return ACENTE_KOMISYON_ORANLARI.get(brans, 0.10)


_DANISMAN_ANAHTARI = {ascii_buyuk(d): d for d in ZORUNLU_DANISMANLAR}
_DANISMAN_ANAHTARI[ascii_buyuk("Sezai Karakoç")] = "Sezai Yağcı"


def danisman_bul(deger) -> Optional[str]:
    """Danışman listesindeki karşılığı (büyük/küçük harf, Türkçe karakter farkı gözetmez); yoksa None."""
    return _DANISMAN_ANAHTARI.get(ascii_buyuk(deger).strip())


def danisman_normalize(deger) -> str:
    """Listede olmayan/boş danışman değerleri varsayılan danışmana düşer."""
    return danisman_bul(deger) or VARSAYILAN_DANISMAN


def eski_policeleri_otomatik_temizle(saklama_gunu: int = 90):
    """Bitişinden `saklama_gunu` gün geçmiş poliçeleri siler (ESKI_POLICE_SAKLAMA_GUN=0 → silme)."""
    if saklama_gunu <= 0:
        return
    db = SessionLocal()
    try:
        sinir = date.today() - timedelta(days=saklama_gunu)
        db.query(Police).filter(Police.bitis_tarihi < sinir).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app):
    init_db()
    eski_policeleri_otomatik_temizle(int(os.getenv("ESKI_POLICE_SAKLAMA_GUN", "90")))
    yield


app = FastAPI(title="Altun Kardeşler CRM", version="5.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1024)


# =========================================================================
# Yardımcılar
# =========================================================================
def parse_tarih(val) -> date:
    return tarih_coz(val) or date.today()


def para_coz(val) -> float:
    if val is None or val == "":
        return 0.0
    s = str(val).strip().replace("TL", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def net_komisyon_hesapla(p: Police) -> float:
    if p.komisyon is not None and p.komisyon != 0.0:
        return p.komisyon
    komisyon = (p.prim or 0.0) * komisyon_orani_getir(p.sigorta_turu)
    return komisyon * 0.5 if p.uretim_kaynagi == "Dışarıdan" else komisyon


def uyar_seviyesi(kalan_gun: int) -> str:
    if kalan_gun < 0:
        return "suresi_dolmus"
    if kalan_gun <= 3:
        return "kirmizi"
    if kalan_gun <= 7:
        return "sari"
    return "uzak"


def police_aktif_mi(p: Police, bugun: date) -> bool:
    return p.durum == "aktif" and p.bitis_tarihi >= bugun


def sayfalama(query, limit: int):
    total = query.count()
    return total, (total + limit - 1) // limit


def police_to_out(p: Police) -> dict:
    bugun = date.today()
    kalan = (p.bitis_tarihi - bugun).days if p.bitis_tarihi else 0
    suresi_dolmus = bool((p.bitis_tarihi and p.bitis_tarihi < bugun) or p.durum == "iptal")
    m = p.musteri
    return {
        "id": p.id, "musteri_id": p.musteri_id, "police_no": p.police_no, "sigorta_turu": p.sigorta_turu,
        "sigorta_sirketi": p.sigorta_sirketi, "islem_turu": p.islem_turu or "Yeni",
        "uretim_kaynagi": p.uretim_kaynagi or "Kendi", "baslangic_tarihi": p.baslangic_tarihi,
        "bitis_tarihi": p.bitis_tarihi, "prim": p.prim, "durum": p.durum, "arac_bilgisi": p.arac_bilgisi,
        "varlik_bilgisi": p.varlik_bilgisi, "aciklama": p.aciklama, "pdf_dosya_adi": p.pdf_dosya_adi,
        "kalan_gun": 0 if suresi_dolmus else kalan, "suresi_dolmus": suresi_dolmus,
        "uyar_seviyesi": "suresi_dolmus" if suresi_dolmus else uyar_seviyesi(kalan),
        "musteri_ad": m.ad if m else None, "musteri_soyad": m.soyad if m else None,
        "musteri_telefon": m.telefon if m else None,
        "musteri_portfoy_sorumlusu": m.portfoy_sorumlusu if m else None,
        "komisyon": net_komisyon_hesapla(p),
    }


def musteri_to_dict(m: Musteri) -> dict:
    return {
        "id": m.id, "ad": m.ad, "soyad": m.soyad, "telefon": m.telefon, "email": m.email,
        "tc_kimlik": m.tc_kimlik, "adres": m.adres, "portfoy_sorumlusu": m.portfoy_sorumlusu,
        "dogum_tarihi": m.dogum_tarihi, "meslek": m.meslek, "sirket_sahipleri": m.sirket_sahipleri,
        "medeni_durum": m.medeni_durum, "cocuk_sayisi": m.cocuk_sayisi, "cocuk_yaslari": m.cocuk_yaslari,
        "yas": yas_hesapla(m.dogum_tarihi) if m.dogum_tarihi else m.yas, "emekli_mi": m.emekli_mi,
        "sahip_olunan_araclar": m.sahip_olunan_araclar, "ek_notlar": m.ek_notlar, "musteri_tipi": m.musteri_tipi,
    }


def musteri_payload_hazirla(payload: dict) -> dict:
    veri = dict(payload)
    if "portfoy_sorumlusu" in veri:
        veri["portfoy_sorumlusu"] = danisman_normalize(veri["portfoy_sorumlusu"])
    return veri


# =========================================================================
# Sayfa / sabitler / bakım
# =========================================================================
@app.get("/")
def ana_sayfa():
    index_yolu = BASE_DIR / "index.html"
    if index_yolu.exists():
        return HTMLResponse(index_yolu.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html bulunamadı!</h1>")


@app.get("/vektorel_logo_2.svg")
def serve_logo():
    for ad in ("vektorel_logo_2.svg", "vektorel_logo.svg"):
        if (BASE_DIR / ad).exists():
            return FileResponse(BASE_DIR / ad, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Logo bulunamadı")


@app.get("/api/sabitler")
def api_sabitler():
    return {
        "sirketler": sorted(ZORUNLU_SIRKETLER), "branslar": sorted(ZORUNLU_BRANSLAR),
        "danismanlar": ZORUNLU_DANISMANLAR,
    }


@app.post("/api/sistemi-temizle")
def sistemi_temizle(db: Session = Depends(get_db)):
    """Şirket/branş adlarını standartlaştırır, mükerrer müşteri ve poliçeleri birleştirir."""
    try:
        for p in db.query(Police).all():
            if p.sigorta_sirketi:
                p.sigorta_sirketi = standart_sirket(p.sigorta_sirketi)
            if p.sigorta_turu:
                p.sigorta_turu = standart_brans(p.sigorta_turu)
        for m in db.query(Musteri).all():
            m.portfoy_sorumlusu = danisman_normalize(m.portfoy_sorumlusu)
        db.flush()
        rapor = veritabanini_temizle(db)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "mesaj": (f"Temizlik tamamlandı! {rapor['silinen_kopya_musteri']} mükerrer müşteri birleştirildi, "
                  f"{rapor['silinen_kopya_police']} mükerrer poliçe temizlendi."),
        **rapor,
    }


# =========================================================================
# Pazarlama / çapraz satış
# =========================================================================
@app.get("/api/pazarlama")
def api_pazarlama(db: Session = Depends(get_db)):
    bugun = date.today()
    aktif = (db.query(Police).options(joinedload(Police.musteri))
             .filter(Police.durum == "aktif", Police.bitis_tarihi >= bugun).all())
    musteri_policeleri = {}
    for p in aktif:
        musteri_policeleri.setdefault(p.musteri_id, []).append(p)

    sonuclar = []
    for policeler in musteri_policeleri.values():
        m = policeler[0].musteri
        if not m:
            continue
        sahip = {p.sigorta_turu for p in policeler if p.sigorta_turu}
        eklenen = set()
        for p in policeler:
            kalan_gun = (p.bitis_tarihi - bugun).days
            kural = CAPRAZ_SATIS_KURALLARI.get(p.sigorta_turu)
            if not (0 <= kalan_gun <= 30 and kural) or p.sigorta_turu in eklenen:
                continue
            yeni_firsatlar = [b for b in kural["firsatlar"] if b not in sahip]
            if yeni_firsatlar:
                eklenen.add(p.sigorta_turu)
                sonuclar.append({
                    "musteri_id": m.id, "ad_soyad": f"{m.ad} {m.soyad}".strip(), "telefon": m.telefon or "-",
                    "tetikleyici_police": p.sigorta_turu, "dayanak": p.sigorta_turu,
                    "firsatlar": " + ".join(yeni_firsatlar), "taktik": kural["taktik"],
                    "bitis": p.bitis_tarihi.strftime("%d.%m.%Y"), "kalan_gun": kalan_gun,
                })
    sonuclar.sort(key=lambda x: x["kalan_gun"])
    return sonuclar


# =========================================================================
# Eğitim merkezi
# =========================================================================
DERS_KATEGORILERI = (
    ("Kasko Sigortası", ("KASKO",)),
    ("Tamamlayıcı Sağlık Sigortası", ("TSS", "TAMAMLAYICI")),
    ("Özel Sağlık Sigortası", ("OSS", "ÖSS", "SAĞLIK", "SAGLIK", "ÖZEL")),
    ("Konut Sigortası", ("KONUT", "DASK", "DEPREM")),
    ("İş Yeri Sigortası", ("İŞYERİ", "ISYERI")),
    ("Mühendislik & All Risk", ("ALLRISK", "ALLRİSK", "MÜHENDİSLİK", "INSAAT", "İNŞAAT")),
    ("Nakliyat Sigortası", ("NAKLİYAT", "NAKLIYAT")),
    ("Site & Ortak Alan", ("SİTE", "SITE")),
)


@app.get("/api/dersler/liste")
def api_dersler_liste():
    kategoriler = {ad: [] for ad, _ in DERS_KATEGORILERI}
    for f in DERSLER_DIR.iterdir():
        if not (f.is_file() and f.name.lower().endswith(".pdf")):
            continue
        adi = f.name.upper()
        hedef = next((ad for ad, anahtarlar in DERS_KATEGORILERI if any(a in adi for a in anahtarlar)), "İş Yeri Sigortası")
        kategoriler[hedef].append(f.name)
    return {k: sorted(v) for k, v in kategoriler.items() if v}


@app.get("/dersler/{dosya_adi}")
def get_ders_pdf(dosya_adi: str):
    dosya_adi = urllib.parse.unquote(dosya_adi)
    for f in DERSLER_DIR.iterdir():  # klasör dışına çıkışı (../) engeller; büyük/küçük harf farkını tolere eder
        if f.is_file() and f.name.lower() == dosya_adi.lower():
            return FileResponse(f, media_type="application/pdf")
    raise HTTPException(status_code=404, detail="Not Found")


@app.get("/api/pdf/{dosya_adi}")
def api_pdf_indir(dosya_adi: str, db: Session = Depends(get_db)):
    kayit = db.query(DosyaDepo).filter(DosyaDepo.dosya_adi == dosya_adi).first()
    if not kayit:
        raise HTTPException(status_code=404, detail="PDF bulunamadı")
    return Response(content=kayit.veri, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{urllib.parse.quote(dosya_adi)}"'})


# =========================================================================
# Özet
# =========================================================================
@app.get("/api/ozet")
def api_ozet(db: Session = Depends(get_db)):
    bugun = date.today()
    return {
        "musteri_sayisi": db.query(Musteri).count(),
        "police_sayisi": db.query(Police).count(),
        "aktif_police": db.query(Police).filter(Police.durum == "aktif", Police.bitis_tarihi >= bugun).count(),
        "yaklasan_7_gun": db.query(Police).filter(
            Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=7)).count(),
    }


def _sonraki_dogum_gunu(dogum: date, bugun: date) -> date:
    def yil_icin(y):
        try:
            return dogum.replace(year=y)
        except ValueError:  # 29 Şubat doğumlular artık olmayan yıllarda 28 Şubat kutlar
            return date(y, 2, 28)
    aday = yil_icin(bugun.year)
    return aday if aday >= bugun else yil_icin(bugun.year + 1)


@app.get("/api/dogum-gunleri")
def api_dogum_gunleri(gun: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    bugun = date.today()
    sonuc = []
    for m in db.query(Musteri).filter(Musteri.dogum_tarihi.isnot(None)):
        sonraki = _sonraki_dogum_gunu(m.dogum_tarihi, bugun)
        kalan = (sonraki - bugun).days
        if kalan <= gun:
            sonuc.append({"id": m.id, "ad_soyad": f"{m.ad} {m.soyad}".strip(), "telefon": m.telefon, "kalan_gun": kalan,
                          "yeni_yas": sonraki.year - m.dogum_tarihi.year, "tarih": sonraki.strftime("%d.%m.%Y")})
    return sorted(sonuc, key=lambda x: x["kalan_gun"])


# =========================================================================
# Özel gün / doğum günü kutlama mesajları (WhatsApp altyapısı)
#
# Hesaplama ozel_gunler.py'de, gönderim whatsapp_gonderici.py'dedir. Gerçek bir WhatsApp sağlayıcısı
# (Baileys/Evolution API/WPPConnect) bağlanana kadar gönderim "dry-run" çalışır: mesaj üretilir ve
# loglanır ama iletilmez — WHATSAPP_WEBHOOK_URL ortam değişkeni tanımlanınca otomatik gerçek gönderime döner.
# =========================================================================
@app.get("/api/kutlama/takvim")
def api_kutlama_takvim(gun: int = Query(30, ge=1, le=365)):
    """Önümüzdeki `gun` içindeki milli/dini özel günler (müşteriden bağımsız, genel takvim önizlemesi)."""
    return [{"tarih": og.tarih, "kod": og.kod, "baslik": og.baslik, "tur": og.tur} for og in yaklasan_ozel_gunler(gun)]


@app.get("/api/kutlama/bugun")
def api_kutlama_bugun(db: Session = Depends(get_db)):
    """Bugün doğum günü olan ya da bugüne denk gelen özel gün(ler) bulunan, telefonu kayıtlı müşteriler."""
    ozel_gunler_bugun = bugunku_ozel_gunler()
    sonuc = []
    for m in db.query(Musteri).filter(Musteri.telefon.isnot(None), Musteri.telefon != ""):
        ad_soyad = f"{m.ad} {m.soyad}".strip()
        for mesaj in musterinin_bugunku_mesajlari(ad_soyad, m.dogum_tarihi):
            sonuc.append({"musteri_id": m.id, "ad_soyad": ad_soyad, "telefon": m.telefon, **mesaj})
    return {"ozel_gunler": [{"kod": og.kod, "baslik": og.baslik, "tur": og.tur} for og in ozel_gunler_bugun], "mesajlar": sonuc}


class KutlamaGonderRequest(BaseModel):
    musteri_id: int
    tur: str
    mesaj: str


@app.post("/api/kutlama/gonder")
def api_kutlama_gonder(payload: KutlamaGonderRequest, db: Session = Depends(get_db)):
    """Tek bir müşteriye kutlama mesajını gönderir. Sağlayıcı bağlı değilse yalnızca hazırlanır (dry-run)."""
    m = db.get(Musteri, payload.musteri_id)
    if not m:
        raise HTTPException(404, "Müşteri bulunamadı")
    if not (m.telefon or "").strip():
        raise HTTPException(400, "Bu müşterinin telefon numarası kayıtlı değil")
    sonuc = mesaj_gonder(m.telefon, payload.mesaj)
    return {"basarili": sonuc.basarili, "gonderildi": sonuc.gonderildi, "detay": sonuc.detay}


# =========================================================================
# Müşteriler
# =========================================================================
@app.get("/api/musteriler")
def api_musteri_listele(q: Optional[str] = None, tip: Optional[str] = None, page: int = Query(1, ge=1),
                        limit: int = Query(10, ge=1, le=100000), db: Session = Depends(get_db)):
    query = db.query(Musteri)
    if tip in ("Bireysel", "Kurumsal"):
        query = query.filter(Musteri.musteri_tipi == tip)
    if q and q.strip():
        like = f"%{q.strip()}%"
        kosullar = [Musteri.ad.ilike(like), Musteri.soyad.ilike(like), Musteri.telefon.ilike(like), Musteri.tc_kimlik.ilike(like)]
        anahtar = gelismis_firma_temizle(q)
        if anahtar:  # "sahin" yazınca ŞAHİN de bulunsun
            kosullar.append(Musteri.ad_soyad_anahtar.like(f"%{anahtar}%"))
        query = query.filter(or_(*kosullar))
    total, pages = sayfalama(query, limit)
    musteriler = query.order_by(Musteri.ad).offset((page - 1) * limit).limit(limit).all()

    dagilim = {m.id: {} for m in musteriler}
    bugun = date.today()
    if musteriler:
        for p in db.query(Police.musteri_id, Police.sigorta_turu).filter(
                Police.musteri_id.in_(list(dagilim)), Police.bitis_tarihi >= bugun, Police.durum == "aktif"):
            t = p.sigorta_turu or "Bilinmiyor"
            dagilim[p.musteri_id][t] = dagilim[p.musteri_id].get(t, 0) + 1

    items = [{"id": m.id, "ad": m.ad, "soyad": m.soyad, "telefon": m.telefon, "tc_kimlik": m.tc_kimlik,
              "adres": m.adres, "portfoy_sorumlusu": m.portfoy_sorumlusu, "musteri_tipi": m.musteri_tipi,
              "aktif_police_sayilari": dagilim[m.id]}
             for m in musteriler]
    tip_sayilari = dict(db.query(Musteri.musteri_tipi, func.count(Musteri.id)).group_by(Musteri.musteri_tipi).all())
    return {"items": items, "total": total, "page": page, "limit": limit, "pages": pages, "tip_sayilari": tip_sayilari}


PROFIL_ALANLARI = ("telefon", "email", "adres", "tc_kimlik", "dogum_tarihi", "meslek", "medeni_durum", "emekli_mi",
                   "cocuk_sayisi", "sahip_olunan_araclar")


def profil_oneriler(m: Musteri, sahip: set, araci_var: bool) -> list:
    """Sahip olunan branşlara ve müşteri profiline göre çapraz satış önerileri (tekrarsız, öncelik sıralı)."""
    oneri = {}

    def ekle(brans, neden):
        if brans not in sahip:
            oneri.setdefault(brans, []).append(neden)

    for brans in sahip:
        kural = CAPRAZ_SATIS_KURALLARI.get(brans)
        for f in (kural or {}).get("firsatlar", []):
            ekle(f, f"{brans} sahibi")
    yas = yas_hesapla(m.dogum_tarihi) if m.dogum_tarihi else m.yas
    if araci_var:
        ekle("Kasko Sigortası", "Aracı var")
        ekle("Trafik Sigortası", "Aracı var")
    if m.emekli_mi:
        ekle("Tamamlayıcı Sağlık Sigortası", "Emekli")
        ekle("Ferdi Kaza", "Emekli")
    if m.cocuk_sayisi:
        ekle("Tamamlayıcı Sağlık Sigortası", f"{m.cocuk_sayisi} çocuğu var")
        ekle("Ferdi Kaza", "Çocuklu aile")
    if (m.medeni_durum or "").lower() == "evli":
        ekle("Konut ve Eşya Sigortaları", "Evli / aile")
        ekle("DASK", "Evli / aile")
    if yas and yas >= 50:
        ekle("Tamamlayıcı Sağlık Sigortası", f"{yas} yaşında")
    if "Konut ve Eşya Sigortaları" in sahip:
        ekle("DASK", "Konutu var, DASK zorunlu")
    return [{"brans": b, "nedenler": sorted(set(n))} for b, n in sorted(oneri.items(), key=lambda x: -len(set(x[1])))]


def musteri_karti(m: Musteri, policeler: List[Police]) -> dict:
    bugun = date.today()
    brans_ozeti = {}
    for brans in ZORUNLU_BRANSLAR:
        bp = [p for p in policeler if p.sigorta_turu == brans]
        aktif = sorted((p for p in bp if police_aktif_mi(p, bugun)), key=lambda p: p.bitis_tarihi)
        son = aktif[0] if aktif else (max(bp, key=lambda p: p.bitis_tarihi) if bp else None)
        brans_ozeti[brans] = {
            "aktif_adet": len(aktif), "toplam_adet": len(bp),
            "bitis": son.bitis_tarihi if son else None, "sirket": son.sigorta_sirketi if son else None,
            "kalan_gun": (son.bitis_tarihi - bugun).days if son else None,
        }
    araclar = sorted({p.arac_bilgisi.strip() for p in policeler if p.arac_bilgisi and p.arac_bilgisi.strip()})
    sahip = {b for b, o in brans_ozeti.items() if o["aktif_adet"]}
    dolu = sum(1 for a in PROFIL_ALANLARI if getattr(m, a) not in (None, ""))
    kart = musteri_to_dict(m)
    kart.update({
        "brans_ozeti": brans_ozeti,
        "toplam_aktif": sum(o["aktif_adet"] for o in brans_ozeti.values()),
        "toplam_police": len(policeler),
        "toplam_prim": sum(p.prim or 0 for p in policeler if police_aktif_mi(p, bugun)),
        "police_araclari": araclar,
        "policeler": [police_to_out(p) for p in sorted(policeler, key=lambda p: p.bitis_tarihi, reverse=True)],
        "oneriler": profil_oneriler(m, sahip, bool(araclar or m.sahip_olunan_araclar)),
        "doluluk": round(100 * dolu / len(PROFIL_ALANLARI)),
        "olusturma": m.created_at,
    })
    return kart


@app.get("/api/musteriler/{musteri_id}")
def api_musteri_karti(musteri_id: int, db: Session = Depends(get_db)):
    m = db.get(Musteri, musteri_id)
    if not m:
        raise HTTPException(404, "Müşteri bulunamadı")
    policeler = db.query(Police).options(joinedload(Police.musteri)).filter(Police.musteri_id == m.id).all()
    return musteri_karti(m, policeler)


@app.post("/api/musteriler", status_code=201)
def api_musteri_olustur(payload: dict, db: Session = Depends(get_db)):
    """Aynı TCKN/VKN veya Ad-Soyad varsa yeni kayıt açmaz, mevcut müşteriyi günceller."""
    try:
        m, yeni = musteri_kaydet(db, musteri_payload_hazirla(payload), uzerine_yaz=True)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Bu TCKN/VKN başka bir müşteride kayıtlı.")
    db.refresh(m)
    return {**musteri_to_dict(m), "yeni": yeni}


@app.put("/api/musteriler/{musteri_id}")
def api_musteri_guncelle(musteri_id: int, payload: dict, db: Session = Depends(get_db)):
    m = db.get(Musteri, musteri_id)
    if not m:
        raise HTTPException(404, "Bulunamadı")
    veri = musteri_payload_hazirla({k: v for k, v in payload.items() if k in MUSTERI_ALANLARI})
    temiz = musteri_verisi_temizle(veri)

    yeni_tc = temiz.get("tc_kimlik")
    if yeni_tc:
        cakisan = db.query(Musteri).filter(Musteri.tc_kimlik == yeni_tc, Musteri.id != m.id).first()
        if cakisan:
            raise HTTPException(409, f"Bu TCKN/VKN zaten {cakisan.ad} {cakisan.soyad} adlı müşteride kayıtlı.")
    yeni_ad, yeni_soyad = temiz.get("ad", m.ad), temiz.get("soyad", m.soyad)
    tc_son = yeni_tc or (m.tc_kimlik if "tc_kimlik" not in veri else None)
    anahtar = ad_anahtari(yeni_ad, yeni_soyad)
    for c in db.query(Musteri).filter(Musteri.ad_soyad_anahtar == anahtar, Musteri.id != m.id):
        if not (tc_son and c.tc_kimlik and c.tc_kimlik != tc_son):
            raise HTTPException(409, f"Aynı Ad-Soyad ile başka bir müşteri kartı var (#{c.id}). Mükerrer kayıt açılamaz; "
                                     "önce 'Sistemi Temizle & Birleştir' ile birleştirin.")

    # Boşaltılan alanlar temizlenir (temiz sözlükte yoksa ve payload'da boş geldiyse)
    for k in veri:
        if k in ("ad", "soyad"):
            continue
        setattr(m, k, temiz.get(k))
    for k in ("ad", "soyad"):
        if k in temiz:
            setattr(m, k, temiz[k])
    if not m.portfoy_sorumlusu:
        m.portfoy_sorumlusu = VARSAYILAN_DANISMAN
    if "dogum_tarihi" in temiz and "yas" not in veri:
        m.yas = yas_hesapla(m.dogum_tarihi)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Kayıt mükerrer bir TCKN/VKN üretiyor.")
    db.refresh(m)
    return musteri_to_dict(m)


# =========================================================================
# Poliçeler
# =========================================================================
@app.get("/api/policeler")
def api_police_listele(musteri_id: Optional[int] = None, q: Optional[str] = None, aktif: Optional[str] = None,
                       page: int = Query(1, ge=1), limit: int = Query(10, ge=1, le=100000), db: Session = Depends(get_db)):
    query = db.query(Police).outerjoin(Musteri, Police.musteri_id == Musteri.id).options(joinedload(Police.musteri))
    if musteri_id:
        query = query.filter(Police.musteri_id == musteri_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        kosullar = [Police.police_no.ilike(like), Police.sigorta_sirketi.ilike(like), Police.sigorta_turu.ilike(like),
                    Musteri.ad.ilike(like), Musteri.soyad.ilike(like)]
        if gelismis_firma_temizle(q):
            kosullar.append(Musteri.ad_soyad_anahtar.like(f"%{gelismis_firma_temizle(q)}%"))
        query = query.filter(or_(*kosullar))
    bugun = date.today()
    if aktif == "aktif":
        query = query.filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun)
    elif aktif == "eski":
        query = query.filter((Police.durum == "iptal") | (Police.bitis_tarihi < bugun))
    total, pages = sayfalama(query, limit)
    kayitlar = query.order_by(Police.bitis_tarihi.desc()).offset((page - 1) * limit).limit(limit).all()
    return {"items": [police_to_out(p) for p in kayitlar], "total": total, "page": page, "limit": limit, "pages": pages}


@app.get("/api/policeler/yaklasan")
def api_yaklasan(gun: int = 7, db: Session = Depends(get_db)):
    bugun = date.today()
    kayitlar = (db.query(Police).options(joinedload(Police.musteri))
                .filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=gun)).all())
    return [police_to_out(p) for p in kayitlar]


def _ay_araligi(bugun: date, ay_ofset: int = 0) -> tuple:
    """`ay_ofset` kadar ay sonrasının (0=bu ay, 1=gelecek ay) ilk ve son gününü döndürür."""
    ay_toplam = bugun.month - 1 + ay_ofset
    yil, ay = bugun.year + ay_toplam // 12, ay_toplam % 12 + 1
    ilk_gun = date(yil, ay, 1)
    son_gun = date(yil, ay, calendar.monthrange(yil, ay)[1])
    return ilk_gun, son_gun


def _ay_policelerini_getir(db: Session, ay_ofset: int) -> list:
    ilk_gun, son_gun = _ay_araligi(date.today(), ay_ofset)
    kayitlar = (db.query(Police).options(joinedload(Police.musteri))
                .filter(Police.durum != "iptal", Police.bitis_tarihi >= ilk_gun, Police.bitis_tarihi <= son_gun,
                        Police.sigorta_turu.in_(YENILENEBILIR_BRANSLAR)).all())
    return [police_to_out(p) for p in kayitlar]


@app.get("/api/policeler/bu-ay")
def api_bu_ay(db: Session = Depends(get_db)):
    return _ay_policelerini_getir(db, 0)


@app.get("/api/policeler/gelecek-ay")
def api_gelecek_ay(db: Session = Depends(get_db)):
    return _ay_policelerini_getir(db, 1)


def _musteri_id_coz(payload: dict, db: Session) -> int:
    if payload.get("musteri_id"):
        return int(payload["musteri_id"])
    ad = payload.get("musteri_ad") or payload.get("ad")
    if not (ad and str(ad).strip()):
        raise ValueError("Lütfen önce bir müşteri seçin veya geçerli müşteri Adı/Soyadı girin.")
    m, _ = musteri_kaydet(db, {
        "ad": ad, "soyad": payload.get("musteri_soyad") or payload.get("soyad"),
        "telefon": payload.get("telefon") or payload.get("musteri_telefon"),
        "tc_kimlik": payload.get("tc_kimlik") or payload.get("tckn"),
        "portfoy_sorumlusu": danisman_normalize(payload.get("portfoy_sorumlusu")),
    })
    return m.id


def _zeyil_uygula(ana: Police, islem: str, prim: float, bitis: date):
    oran = komisyon_orani_getir(ana.sigorta_turu) * (0.5 if ana.uretim_kaynagi == "Dışarıdan" else 1)
    if islem == "İptal":
        ana.durum = "iptal"
        if prim == 0:
            toplam_gun = (ana.bitis_tarihi - ana.baslangic_tarihi).days
            kullanilan = (bitis - ana.baslangic_tarihi).days
            if toplam_gun > 0 and kullanilan >= 0:
                prim = (ana.prim or 0) * max(0, min(1, (toplam_gun - kullanilan) / toplam_gun))
        ek, isaret = f" | İPTAL: {bitis.strftime('%d.%m.%Y')} tarihi itibariyle. İade: {prim:.2f} TL", -1
    elif islem == "İadeli Zeyil":
        ek, isaret = f" | İadeli Zeyil: {prim:.2f} TL iade.", -1
    else:
        ek, isaret = f" | Primli Zeyil: {prim:.2f} TL eklendi.", 1
    ana.prim = (ana.prim or 0) + isaret * prim
    ana.komisyon = (ana.komisyon or 0) + isaret * prim * oran
    ana.aciklama = f"{ana.aciklama or ''}{ek}".strip(" |")


@app.post("/api/policeler", status_code=201)
def api_police_olustur(payload: dict, db: Session = Depends(get_db)):
    try:
        baslangic, bitis = parse_tarih(payload.get("baslangic_tarihi")), parse_tarih(payload.get("bitis_tarihi"))
        prim = para_coz(payload.get("prim"))
        islem = payload.get("islem_tipi") or "Yeni"
        kaynak = payload.get("uretim_kaynagi") or "Kendi"
        police_no = police_no_temizle(payload.get("police_no"))
        if not police_no or police_no == "-":
            police_no = f"MANUEL-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
        sirket, brans = standart_sirket(payload.get("sigorta_sirketi")), standart_brans(payload.get("sigorta_turu"))
        mevcut = police_bul(db, police_no)

        if islem in ("İptal", "İadeli Zeyil", "Primli Zeyil"):
            if not mevcut:
                raise ValueError("Bu poliçe numarasına ait ana bir poliçe bulunamadı. İptal/Zeyil işlemi yapılamaz.")
            _zeyil_uygula(mevcut, islem, prim, bitis)
            if payload.get("arac_bilgisi"):
                mevcut.arac_bilgisi = payload["arac_bilgisi"]
            db.commit()
            return police_to_out(mevcut)

        komisyon = prim * komisyon_orani_getir(brans) * (0.5 if kaynak == "Dışarıdan" else 1)
        if mevcut:
            if islem != "Yenileme":
                sahibi = f"{mevcut.musteri.ad} {mevcut.musteri.soyad}".strip() if mevcut.musteri else "başka bir müşteri"
                raise HTTPException(409, f"Bu poliçe numarası zaten kayıtlı ({sahibi}). Aynı numaralı ikinci poliçe açılamaz.")
            # Numarası değişmeyen poliçenin yeni dönemi: mevcut kayıt yeni döneme taşınır
            mevcut.baslangic_tarihi, mevcut.bitis_tarihi, mevcut.prim, mevcut.komisyon = baslangic, bitis, prim, komisyon
            mevcut.durum, mevcut.islem_turu = "aktif", "Yenileme"
            mevcut.aciklama = f"{mevcut.aciklama or ''} | Yenilendi: {baslangic.strftime('%d.%m.%Y')}".strip(" |")
            db.commit()
            return police_to_out(mevcut)

        police = Police(
            musteri_id=_musteri_id_coz(payload, db), police_no=police_no, sigorta_turu=brans, sigorta_sirketi=sirket,
            islem_turu=islem, uretim_kaynagi=kaynak, baslangic_tarihi=baslangic, bitis_tarihi=bitis, prim=prim,
            komisyon=komisyon, durum="aktif", arac_bilgisi=payload.get("arac_bilgisi"), varlik_bilgisi=payload.get("varlik_bilgisi"),
            aciklama=payload.get("aciklama"), pdf_dosya_adi=payload.get("pdf_dosya_adi"),
        )
        db.add(police)
        db.commit()
        db.refresh(police)
        return police_to_out(police)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Bu poliçe numarası zaten kayıtlı.")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/policeler/{police_id}", status_code=204)
def api_police_sil(police_id: int, db: Session = Depends(get_db)):
    p = db.get(Police, police_id)
    if not p:
        raise HTTPException(404, "Poliçe bulunamadı")
    db.delete(p)
    db.commit()
    return Response(status_code=204)


@app.post("/api/upload-parse")
async def api_upload_parse(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"detail": "Yalnızca PDF dosyası yükleyebilirsiniz"})
    icerik = await file.read()
    if not icerik:
        return JSONResponse(status_code=400, content={"detail": "Dosya boş"})

    try:
        ayiklanan = await run_in_threadpool(ayikla_police_pdf, icerik)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    except Exception as e:
        log.exception("PDF okuma hatası")
        return JSONResponse(status_code=500, content={"detail": f"PDF okuma hatası: {e}"})

    dosya_adi = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
    try:
        db.add(DosyaDepo(dosya_adi=dosya_adi, veri=icerik))
        db.commit()
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": f"Dosya veritabanına kaydedilemedi: {e}"})

    ayiklanan["pdf_dosya_adi"] = dosya_adi
    if ayiklanan.get("sigorta_sirketi"):
        ayiklanan["sigorta_sirketi"] = standart_sirket(ayiklanan["sigorta_sirketi"])
    if ayiklanan.get("sigorta_turu"):
        ayiklanan["sigorta_turu"] = standart_brans(ayiklanan["sigorta_turu"])

    m = musteri_bul(db, ayiklanan.get("ad"), ayiklanan.get("soyad"), ayiklanan.get("tckn"))
    if m:
        ayiklanan.update(musteri={"id": m.id, "ad": m.ad, "soyad": m.soyad, "telefon": m.telefon,
                                  "portfoy_sorumlusu": m.portfoy_sorumlusu},
                         musteri_eslesti=True, mesaj=f"Mevcut müşteri bulundu: {m.ad} {m.soyad}")
    else:
        ayiklanan.update(musteri_eslesti=False, mesaj="Yeni müşteri. Lütfen bilgileri kontrol edip kaydedin.")
    ayiklanan.setdefault("telefon", "")
    ayiklanan.setdefault("portfoy_sorumlusu", VARSAYILAN_DANISMAN)
    mevcut = police_bul(db, ayiklanan.get("police_no"))
    ayiklanan["police_mevcut"] = bool(mevcut)
    if mevcut:
        ayiklanan["mesaj"] += f" ⚠ Bu poliçe numarası zaten kayıtlı (#{mevcut.id})."
    return ayiklanan


# =========================================================================
# Finansal raporlama
# =========================================================================
class FinansalRaporRequest(BaseModel):
    baslangic: Optional[str] = None
    bitis: Optional[str] = None
    sirket: Optional[List[str]] = None
    danisman: Optional[List[str]] = None
    branslar: Optional[List[str]] = None


@app.get("/api/finansal/filtreler")
def api_fin_filtreler():
    return {"sirketler": sorted(ZORUNLU_SIRKETLER), "danismanlar": ZORUNLU_DANISMANLAR, "branslar": sorted(ZORUNLU_BRANSLAR)}


@app.post("/api/finansal/rapor")
def api_fin_rapor(payload: FinansalRaporRequest, db: Session = Depends(get_db)):
    query = db.query(Police).join(Musteri, Police.musteri_id == Musteri.id).filter(Police.durum != "iptal")
    if payload.baslangic:
        query = query.filter(Police.baslangic_tarihi >= parse_tarih(payload.baslangic))
    if payload.bitis:
        query = query.filter(Police.baslangic_tarihi <= parse_tarih(payload.bitis))
    if payload.sirket:
        query = query.filter(Police.sigorta_sirketi.in_(payload.sirket))
    if payload.danisman:
        query = query.filter(Musteri.portfoy_sorumlusu.in_(payload.danisman))
    if payload.branslar:
        query = query.filter(Police.sigorta_turu.in_(payload.branslar))

    policeler = query.all()
    brans_dagilimi, sirket_dagilimi = {}, {}
    for p in policeler:
        b, s = p.sigorta_turu or "Diğer", p.sigorta_sirketi or "Diğer"
        brans_dagilimi[b] = brans_dagilimi.get(b, 0) + 1
        sirket_dagilimi[s] = sirket_dagilimi.get(s, 0) + 1
    azalan = lambda d: dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True))
    return {
        "ciro": sum(p.prim or 0.0 for p in policeler),
        "kar": sum(net_komisyon_hesapla(p) for p in policeler),
        "adet": len(policeler),
        "brans_dagilimi": azalan(brans_dagilimi),
        "sirket_dagilimi": azalan(sirket_dagilimi),
    }
