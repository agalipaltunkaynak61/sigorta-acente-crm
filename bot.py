"""Excel içe aktarımı: AXA, Türkiye, Neova, Quick ve Hepiyi dosyalarını CRM veritabanına aktarır.

Kullanım:
    python bot.py                    # 5 dosyanın hepsini aktarır
    python bot.py --dry-run          # hiçbir şey yazmadan ne olacağını raporlar
    python bot.py --dosya axa,quick  # yalnızca seçilenleri aktarır (axa, turkiye, neova, quick, hepiyi)

Kurallar:
  * Her satır poliçe numarası bazında toplanır (AXA'da bir poliçe onlarca teminat satırıdır; zeyiller net etkiye sayılır).
  * Aynı numaralı poliçe yalnızca bir kez bulunur; tekrar çalıştırmak veriyi çoğaltmaz, aynı değerleri yazar.
  * Müşteri TCKN/VKN veya Ad-Soyad ile eşleştirilir, mükerrer kart açılmaz (database.musteri_kaydet).
  * Hepiyi'deki yıldızlı (maskeli) kişiler yalnızca mevcut müşterilerle TEK ve KESİN eşleşirse aktarılır.
"""
import argparse
import csv
import re
import sys
import warnings
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

from app import danisman_bul, standart_sirket
from database import (
    SQLITE, Musteri, Police, SessionLocal, ascii_buyuk, init_db, kimlik_temizle, musteri_kaydet,
    police_bul, police_no_temizle, sqlite_yedekle, tarih_coz, tr_buyuk,
)

warnings.filterwarnings("ignore")  # openpyxl uyarıları

KLASOR = Path(__file__).resolve().parent
DOSYALAR = {  # anahtar → (dosya adı, okuyucu adı)
    "axa": "AXA_VERİ.xlsx", "turkiye": "TÜRKİYE_VERİ.xlsx", "neova": "NEOVA_VERİ.xlsx",
    "quick": "QUİCK_VERİ.xlsx", "hepiyi": "HEPİYİ_VERİ.xlsx",   # Hepiyi en sonda: mevcut müşterilerle eşleşir
}

# --------------------------------------------------------------------------
# Küçük yardımcılar
# --------------------------------------------------------------------------
def bos_mu(v) -> bool:
    return v is None or (not isinstance(v, (list, tuple)) and pd.isna(v)) or (isinstance(v, str) and not v.strip())


def metin(v):
    return None if bos_mu(v) else " ".join(str(v).split())


def sayi(v) -> float:
    if bos_mu(v):
        return 0.0
    try:
        return float(v) if not isinstance(v, str) else float(v.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def tam_sayi(v) -> int:
    return 0 if bos_mu(v) else int(float(v))


def tarih(v):
    return None if bos_mu(v) else tarih_coz(v)


def bir_yil_sonra(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 Şubat
        return d.replace(year=d.year + 1, day=28)


def baslikli_oku(yol, anahtar, sayfa=0, varsayilan_satir=None) -> pd.DataFrame:
    """Başlık satırını (ilk 15 satırda 'anahtar' sütununu içeren satır) kendisi bulur; bulamazsa
    `varsayilan_satir` (0 tabanlı) kullanılır. Sütun adlarındaki boşluk/satır sonu temizlenir."""
    ham = pd.read_excel(yol, sheet_name=sayfa, header=None, dtype=object)
    satir_no = next((i for i in range(min(len(ham), 15)) if anahtar in [" ".join(str(c).split()) for c in ham.iloc[i]]), varsayilan_satir)
    if satir_no is None:
        raise ValueError(f"'{anahtar}' başlığı bulunamadı: {yol}")
    df = ham.iloc[satir_no + 1:].copy()
    df.columns = [" ".join(str(c).split()) for c in ham.iloc[satir_no]]
    if anahtar not in df.columns:
        raise ValueError(f"{yol}: {satir_no + 1}. satırda '{anahtar}' sütunu yok")
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# Branş eşleştirme
# --------------------------------------------------------------------------
GENEL_BRANS_KURALLARI = (  # (regex, branş) - sıra önemli
    (r"\bDASK\b|ZORUNLU DEPREM", "DASK"),
    (r"KASKO", "Kasko Sigortası"),
    (r"KARAYOLLARI|TRAFIK|YESIL ?KART", "Trafik Sigortası"),
    (r"TAMAMLAYICI|SAGLIK|\bTSS\b", "Tamamlayıcı Sağlık Sigortası"),
    (r"FERDI KAZA", "Ferdi Kaza"),
    (r"KONUT|ESYA|YANGIN", "Konut ve Eşya Sigortaları"),
    (r"IS ?YERI|TICARI|KOBI|ISVEREN|MAKINE|ELEKTRONIK", "İş Yeri"),
    (r"NAKLIYAT|EMTIA|EMTEA", "Nakliyat"),
    (r"TARIM|BITKISEL|\bSERA\b|HAYVAN", "Tarım"),
    (r"\bYAT\b|DENIZ|TEKNE", "Yat / Denizcilik"),
    (r"ALL ?RISK|INSAAT|MONTAJ", "Allrisk"),
)
VARSAYILAN_BRANS = "Özel Paket / Destek"


def brans_eslestir(ham) -> str:
    if bos_mu(ham):
        return VARSAYILAN_BRANS
    b = ascii_buyuk(ham)
    return next((brans for kural, brans in GENEL_BRANS_KURALLARI if re.search(kural, b)), VARSAYILAN_BRANS)


AXA_TARIFE = {  # AXA'da poliçenin ana ürünü teminat satırlarından değil TRF (tarife) kodundan anlaşılır
    "KNT": "Konut ve Eşya Sigortaları", "KAS": "Kasko Sigortası", "TR1": "Trafik Sigortası",
    "ISY": "İş Yeri", "ECZ": "İş Yeri", "OTE": "İş Yeri", "DAS": "DASK", "C01": "Allrisk", "NET": "Nakliyat",
}
TURKIYE_URUN = {  # ürün kodu (Ürün sütununun başındaki sayı) → branş
    "21100": "DASK", "15101": "Trafik Sigortası", "60200": "Trafik Sigortası", "46150": "Trafik Sigortası",
    "86000": "Kasko Sigortası", "86142": "Kasko Sigortası", "86143": "Kasko Sigortası",
    "85270": "Tamamlayıcı Sağlık Sigortası", "37101": "Nakliyat", "85211": "Nakliyat",
    "62200": "İş Yeri", "44150": "İş Yeri", "41150": "Konut ve Eşya Sigortaları", "13100": "Ferdi Kaza",
    "51153": "Tarım", "28100": "Yat / Denizcilik", "28101": "Allrisk",
}
HEPIYI_URUN = {  # Ürün No → branş (komisyon oranlarından çıkarıldı: 301≈%9 Trafik, 351≈%15 Kasko, 600≈TSS)
    "202": "Konut ve Eşya Sigortaları", "301": "Trafik Sigortası", "351": "Kasko Sigortası",
    "352": "Kasko Sigortası", "600": "Tamamlayıcı Sağlık Sigortası", "602": "Tamamlayıcı Sağlık Sigortası",
}
TAM_IPTAL_DURUMLARI = {"MEBDEINDEN IPTAL", "PERTTEN IPTAL", "KISMI IPTAL"}

# --------------------------------------------------------------------------
# Kişi / firma ayrıştırma
# --------------------------------------------------------------------------
FIRMA_KELIMELERI = {
    "LTD", "LIMITED", "ANONIM", "SIRKETI", "STI", "SANAYI", "TICARET", "TIC", "SAN", "AS", "INSAAT", "OTOMOTIV",
    "MIMARLIK", "MUHENDISLIK", "YONETIMI", "SITE", "DERNEGI", "VAKFI", "KOOPERATIFI", "HOLDING", "GIDA", "TEKSTIL",
    "TURIZM", "NAKLIYAT", "ORGANIZASYON", "TAAHHUT", "ECZANESI", "PAZARLAMA", "ILETISIM", "ELEKTRIK", "MAKINA",
}


def firma_mi(tam_ad: str, kimlik=None) -> bool:
    kimlik = kimlik_temizle(kimlik)
    if kimlik and len(kimlik) == 10:
        return True
    return bool(FIRMA_KELIMELERI & set(re.findall(r"[A-Z0-9]+", ascii_buyuk(tam_ad))))


def ad_soyad_ayir(tam_ad, kimlik=None):
    tam_ad = tr_buyuk(tam_ad)
    if firma_mi(tam_ad, kimlik):
        return tam_ad, ""
    parcalar = tam_ad.split()
    return (" ".join(parcalar[:-1]), parcalar[-1]) if len(parcalar) > 1 else (tam_ad, "")


def danisman_cikar(kullanici):
    """'SEZAİ YAĞCI-(A6001242002)' → danışman adı; tanınmazsa None (varsayılan atanır)."""
    return danisman_bul((metin(kullanici) or "").split("-(")[0])


def satir(police_no, sirket, brans, **ek) -> dict:
    """Okuyucuların ürettiği normalize satır."""
    return {
        "police_no": police_no_temizle(police_no), "sirket": sirket, "brans": brans,
        "zeyil": 0, "baslangic": None, "bitis": None, "prim": 0.0, "komisyon": 0.0,
        "iptal": False, "yenileme": False, "ad_soyad": None, "ad": None, "soyad": None, "firma": False,
        "tckn": None, "telefon": None, "email": None, "adres": None, "dogum": None, "plaka": None,
        "danisman": None, "maskeli": False, **ek,
    }


# --------------------------------------------------------------------------
# Excel okuyucuları
# --------------------------------------------------------------------------
def oku_axa(yol):
    df = baslikli_oku(yol, "POLİÇE NO", varsayilan_satir=4)  # AXA: ilk 4 satır rapor başlığı, sütun adları 5. satırda (header=4)
    for r in df.to_dict("records"):
        if bos_mu(r.get("POLİÇE NO")) or bos_mu(r.get("SIGORTALI ADI")):
            continue
        brans = AXA_TARIFE.get(ascii_buyuk(r.get("TRF")).strip()) or brans_eslestir(r.get("BRANŞ ADI"))
        yield satir(r["POLİÇE NO"], "Axa Sigorta", brans, zeyil=tam_sayi(r.get("ZEYL")),
                    ad_soyad=metin(r["SIGORTALI ADI"]), baslangic=tarih(r.get("TANZIM TAR")),
                    prim=sayi(r.get("BÜRÜT PRİM")), komisyon=sayi(r.get("KOMİSYON")))


def oku_turkiye(yol):
    df = baslikli_oku(yol, "Pol No")
    for r in df.to_dict("records"):
        if bos_mu(r.get("Pol No")) or bos_mu(r.get("Sigortalı")):
            continue
        urun = metin(r.get("Ürün")) or ""
        kod = urun.split(" - ")[0].strip()
        iptal_satiri = ascii_buyuk(r.get("İpt / İst")) == "IPTAL"
        isaret = -1 if iptal_satiri else 1  # iptal satırlarında tutarlar pozitif (iade edilen) yazılmış
        kimlik = r.get("Sigortalı Kimlik")
        plaka = metin(r.get("PLAKA No"))
        yield satir(
            r["Pol No"], "Türkiye Sigorta", TURKIYE_URUN.get(kod) or brans_eslestir(urun),
            zeyil=tam_sayi(r.get("Zyl")), ad_soyad=metin(r["Sigortalı"]), tckn=kimlik,
            firma="VERGI" in ascii_buyuk(kimlik), baslangic=tarih(r.get("Baş Tar")), bitis=tarih(r.get("Bit. Tar")),
            prim=isaret * abs(sayi(r.get("TL Brüt P"))), komisyon=isaret * abs(sayi(r.get("TL Kom"))),
            iptal=iptal_satiri and ascii_buyuk(r.get("Durum")) in TAM_IPTAL_DURUMLARI,
            plaka=plaka.upper() if plaka else None, danisman=danisman_cikar(r.get("Oluşturan Kullanıcı")),
        )


def oku_neova(yol):
    df = baslikli_oku(yol, "Poliçe No")
    for r in df.to_dict("records"):
        if bos_mu(r.get("Poliçe No")) or bos_mu(r.get("Sigortalı Adı")):
            continue
        yield satir(r["Poliçe No"], "Neova Sigorta", brans_eslestir(r.get("Poliçe Branşı")),
                    zeyil=tam_sayi(r.get("Zeyil No")), ad_soyad=metin(r["Sigortalı Adı"]),
                    baslangic=tarih(r.get("Tanzim Tarihi")))  # dosyada prim/komisyon/bitiş yok: 1 yıllık varsayılır


def _quick_sayfalari(yol):
    """Quick tek sayfada değil, sayfalara bölünmüş: PoliceListesi / Sigortalilar / SigortaEttirenler.
    Sayfa türü 2. satırın ilk hücresindedir; PoliceListesi'nde başlık 1. satır, diğerlerinde 2. satırdır."""
    sayfalar = {}
    for ad in pd.ExcelFile(yol).sheet_names:
        ham = pd.read_excel(yol, sheet_name=ad, header=None, dtype=object)
        if len(ham) < 2:
            continue
        tur = metin(ham.iloc[1, 0])
        bas = 0 if tur == "PoliceListesi" else 1
        df = ham.iloc[bas + 1:].copy()
        df.columns = [metin(c) or f"_{i}" for i, c in enumerate(ham.iloc[bas])]
        sayfalar.setdefault(tur, []).append(df[df["PoliceNo"].notna() & (df["PoliceNo"] != "PoliceNo")])
    return {t: pd.concat(d) for t, d in sayfalar.items()}


def oku_quick(yol):
    sayfalar = _quick_sayfalari(yol)
    musteriler = {}
    for tur in ("SigortaEttirenler", "Sigortalilar"):  # sigortalı, sigorta ettirenin üstüne yazar
        for m in sayfalar.get(tur, pd.DataFrame()).to_dict("records"):
            musteriler[police_no_temizle(m["PoliceNo"])] = m
    for r in sayfalar["PoliceListesi"].to_dict("records"):
        no = police_no_temizle(r["PoliceNo"])
        m = musteriler.get(no, {})
        firma = metin(m.get("FirmaAd"))
        kimlik = metin(m.get("Vkn")) or metin(m.get("Tckn")) or metin(m.get("IdentityNo"))
        gsm = "".join(filter(None, (metin(m.get("GsmCode")), metin(m.get("GsmNo")))))
        zeyil_adi = ascii_buyuk(r.get("ZeyilAd"))
        yield satir(
            no, "Quick Sigorta", brans_eslestir(r.get("UrunAd")), zeyil=tam_sayi(r.get("ZeyilNo")),
            yenileme=tam_sayi(r.get("YenilemeNo")) > 0,
            ad_soyad=firma or " ".join(filter(None, (metin(m.get("Ad")), metin(m.get("Soyad"))))) or None,
            ad=firma or metin(m.get("Ad")), soyad="" if firma else (metin(m.get("Soyad")) or ""), firma=bool(firma),
            tckn=kimlik, telefon=gsm or None, email=metin(m.get("Email")), adres=metin(m.get("Adres")),
            dogum=tarih(m.get("DogumTarihi")), baslangic=tarih(r.get("BaslamaTarihi")), bitis=tarih(r.get("BitisTarihi")),
            prim=sayi(r.get("BrutPrimTL")), komisyon=sayi(r.get("AcenteKomisyonTL")),
            iptal="IPTAL" in zeyil_adi,
        )


def oku_hepiyi(yol):
    df = baslikli_oku(yol, "Teklif No")
    for r in df.to_dict("records"):
        if ascii_buyuk(r.get("Teklif Durumu")) != "POLICE" or bos_mu(r.get("Teklif No")) or bos_mu(r.get("Sigortalı Ad-Soyad")):
            continue
        isim, tc = metin(r["Sigortalı Ad-Soyad"]), metin(r.get("Tckn-Vkn"))
        plaka = metin(r.get("Plaka"))
        yield satir(
            r["Teklif No"], "Hepiyi Sigorta", HEPIYI_URUN.get(str(tam_sayi(r.get("Ürün No"))), VARSAYILAN_BRANS),
            zeyil=tam_sayi(r.get("Zeyil No")), yenileme=tam_sayi(r.get("Yenileme No")) > 0, ad_soyad=isim,
            tckn=tc, maskeli="*" in isim, baslangic=tarih(r.get("Başlangıç Tarihi")), bitis=tarih(r.get("Bitiş Tarihi")),
            prim=sayi(r.get("Brüt Prim")), komisyon=sayi(r.get("Komisyon")),
            iptal="IPTAL" in ascii_buyuk(r.get("Zeyil Türü")), plaka=plaka.upper() if plaka else None,
        )


OKUYUCULAR = {"axa": oku_axa, "turkiye": oku_turkiye, "neova": oku_neova, "quick": oku_quick, "hepiyi": oku_hepiyi}


# --------------------------------------------------------------------------
# Poliçe bazında toplama
# --------------------------------------------------------------------------
def police_bazinda_topla(satirlar):
    """Satırları poliçe numarasına göre tek kayda indirger.

    * Dönem: zeyil=0 satırlarından en yeni başlangıçlı olan (aynı numara yenilemede tekrar kullanılabilir).
      Bu dönemin başlangıcından sonraki zeyiller de dönemin parçasıdır.
    * Prim/komisyon: dönemdeki satırların işaretli toplamı.
    * İptal: açık iptal satırı VEYA net prim ≤ 0 (negatif zeyil de varsa).
    * Ana (zeyil=0) satır dosyada yoksa (yalnızca zeyil/iptal var) kayıt 'taban_yok' işaretlenir.
    """
    gruplar = defaultdict(list)
    for s in satirlar:
        if s["police_no"] and s["baslangic"]:
            gruplar[s["police_no"]].append(s)

    for no, rows in gruplar.items():
        bazlar = [r for r in rows if r["zeyil"] == 0]
        ana = max(bazlar or rows, key=lambda r: r["baslangic"])
        donem = [r for r in rows if r["baslangic"] >= ana["baslangic"]]
        prim, komisyon = sum(r["prim"] for r in donem), sum(r["komisyon"] for r in donem)
        iptaller = [r for r in donem if r["iptal"]]
        iptal = bool(iptaller) or (prim <= 0.5 and any(r["prim"] < 0 for r in donem))
        bitis = min((r["bitis"] for r in iptaller if r["bitis"]), default=None) or ana["bitis"] or bir_yil_sonra(ana["baslangic"])

        kayit = dict(ana)
        for alan in ("ad_soyad", "ad", "soyad", "tckn", "telefon", "email", "adres", "dogum", "plaka", "danisman"):
            kayit[alan] = next((r[alan] for r in [ana] + donem if not bos_mu(r[alan])), None)
        zeyil_sayisi = sum(1 for r in donem if r["zeyil"] > 0)
        kayit.update(
            bitis=bitis,
            prim=max(prim, 0.0), komisyon=max(komisyon, 0.0), durum="iptal" if iptal else "aktif",
            islem_turu="Yenileme" if (any(r["yenileme"] for r in donem) or len(bazlar) > 1) else "Yeni",
            taban_yok=not any(r["zeyil"] == 0 and r["baslangic"] == ana["baslangic"] for r in donem),
            zeyil_sayisi=zeyil_sayisi,
        )
        yield kayit


# --------------------------------------------------------------------------
# Hepiyi: maskeli kişi eşleştirme
# --------------------------------------------------------------------------
class MaskeEslestirici:
    """'AH*** KU***' + '42*********' → mevcut müşteri. Maske, her kelimenin uzunluğunu ve ilk harflerini korur;
    aday, kelime sayısı, kelime uzunlukları, ilk harfler (ve varsa TCKN'nin ilk rakamları) ile uyuşmalıdır.
    Yalnızca TEK aday kalırsa eşleşme kesin sayılır."""

    def __init__(self, db):
        self.db = db
        self.indeks = defaultdict(list)  # (kelime sayısı, uzunluklar) → [(musteri, kelimeler)]
        for m in db.query(Musteri):
            kelimeler = ascii_buyuk(f"{m.ad} {m.soyad}").split()
            self.indeks[(len(kelimeler), tuple(map(len, kelimeler)))].append((m, kelimeler))
        self.onbellek = {}

    def bul(self, maskeli_isim, maskeli_tc):
        anahtar = (maskeli_isim, maskeli_tc)
        if anahtar not in self.onbellek:
            self.onbellek[anahtar] = self._ara(maskeli_isim, maskeli_tc)
        return self.onbellek[anahtar]

    def _ara(self, isim, tc):
        maske = ascii_buyuk(isim).split()
        oneekler = [k.split("*")[0] for k in maske]
        if sum(map(len, oneekler)) < 4:
            return None, "maske çok kısa (ayırt edilemez)"
        tc_tam = kimlik_temizle(tc)  # maskesiz TCKN/VKN ise doğrudan ara
        if tc_tam:
            m = self.db.query(Musteri).filter(Musteri.tc_kimlik == tc_tam).first()
            return (m, "TCKN") if m else (None, "TCKN/VKN sistemde yok")
        tc_onek = (tc or "").split("*")[0] if tc and "*" in tc else ""
        adaylar = [
            (m, kelimeler) for m, kelimeler in self.indeks.get((len(maske), tuple(map(len, maske))), [])
            if all(k.startswith(o) for k, o in zip(kelimeler, oneekler))
        ]
        dogrulanan = [m for m, _ in adaylar if m.tc_kimlik and len(m.tc_kimlik) == len(tc or "") and m.tc_kimlik.startswith(tc_onek)]
        dogrulanamayan = [m for m, _ in adaylar if not m.tc_kimlik]  # TCKN'si olmayan kartlarda yalnızca isim maskesi doğrulanır
        if len(dogrulanan) == 1:
            return dogrulanan[0], "isim + TCKN öneki"
        if len(dogrulanan) > 1:
            return None, f"belirsiz: {len(dogrulanan)} müşteri uyuşuyor"
        if len(dogrulanamayan) == 1 and len(maske) >= 2:
            return dogrulanamayan[0], "isim (kartta TCKN yok)"
        if len(dogrulanamayan) > 1:
            return None, f"belirsiz: {len(dogrulanamayan)} müşteri uyuşuyor"
        return None, "sistemde eşleşen müşteri yok"


# --------------------------------------------------------------------------
# Aktarım
# --------------------------------------------------------------------------
class Sayac:
    def __init__(self):
        self.satir = self.police = self.yeni_police = self.guncellenen = self.ayni = self.yeni_musteri = self.hata = 0
        self.atlanan = []   # (poliçe no, isim, neden)
        self.eslesme = {}   # Hepiyi maske eşleştirme sonuçları: neden → adet

    def ozet(self):
        return (f"{self.satir} satır → {self.police} poliçe | yeni: {self.yeni_police}, güncellenen: {self.guncellenen}, "
                f"değişmeyen: {self.ayni} | yeni müşteri: {self.yeni_musteri} | atlanan: {len(self.atlanan)} | hata: {self.hata}")


POLICE_ALANLARI = ("sigorta_sirketi", "sigorta_turu", "baslangic_tarihi", "bitis_tarihi", "prim", "komisyon", "durum")


def musteri_belirle(db, k, eslestirici, sayac):
    """Kayıt için müşteriyi bulur/açar. Bulunamazsa (None, neden)."""
    if k["maskeli"]:
        m, neden = eslestirici.bul(k["ad_soyad"], k["tckn"])
        sayac.eslesme[neden] = sayac.eslesme.get(neden, 0) + 1
        return m, neden
    if not (k["ad_soyad"] or k["ad"]):
        return None, "müşteri adı yok"
    ad, soyad = (k["ad"], k["soyad"] or "") if k["ad"] else ad_soyad_ayir(k["ad_soyad"], k["tckn"] if k["firma"] else None)
    m, yeni = musteri_kaydet(db, {
        "ad": ad, "soyad": soyad, "tc_kimlik": k["tckn"], "telefon": k["telefon"], "email": k["email"],
        "adres": k["adres"], "dogum_tarihi": k["dogum"], "portfoy_sorumlusu": k["danisman"],
    })
    sayac.yeni_musteri += yeni
    return m, None


def police_yaz(db, k, musteri, sayac):
    yeni_degerler = {
        "sigorta_sirketi": standart_sirket(k["sirket"]), "sigorta_turu": k["brans"], "baslangic_tarihi": k["baslangic"],
        "bitis_tarihi": k["bitis"], "prim": round(k["prim"], 2), "komisyon": round(k["komisyon"], 2), "durum": k["durum"],
    }
    notlar = []
    if k["taban_yok"]:
        notlar.append("Excel: ana poliçe satırı dosyada yok (yalnızca zeyil/iptal satırı)")
    if k["zeyil_sayisi"]:
        notlar.append(f"Excel: {k['zeyil_sayisi']} zeyil satırı işlendi")

    mevcut = police_bul(db, k["police_no"])
    if mevcut is None:
        db.add(Police(musteri_id=musteri.id, police_no=k["police_no"], islem_turu=k["islem_turu"], uretim_kaynagi="Kendi",
                      arac_bilgisi=k["plaka"], aciklama=" | ".join(notlar) or None, **yeni_degerler))
        sayac.yeni_police += 1
        db.flush()
        return
    if mevcut.sigorta_sirketi and mevcut.sigorta_sirketi != yeni_degerler["sigorta_sirketi"]:
        sayac.atlanan.append((k["police_no"], k["ad_soyad"], f"aynı numara başka şirkette kayıtlı ({mevcut.sigorta_sirketi})"))
        return
    if mevcut.durum == "iptal":  # elle iptal edilen poliçe Excel'den dirilmez
        yeni_degerler["durum"] = "iptal"
    degisti = False
    for alan, v in yeni_degerler.items():
        if getattr(mevcut, alan) != v:
            setattr(mevcut, alan, v)
            degisti = True
    if not mevcut.arac_bilgisi and k["plaka"]:
        mevcut.arac_bilgisi, degisti = k["plaka"], True
    if not mevcut.aciklama and notlar:
        mevcut.aciklama, degisti = " | ".join(notlar), True
    sayac.guncellenen += degisti
    sayac.ayni += not degisti


def dosyayi_aktar(db, anahtar, yol, eslestirici_uret):
    sayac = Sayac()
    satirlar = list(OKUYUCULAR[anahtar](yol))
    sayac.satir = len(satirlar)
    eslestirici = eslestirici_uret() if anahtar == "hepiyi" else None
    kayitlar = list(police_bazinda_topla(satirlar))
    sayac.police = len(kayitlar)
    atlanan_no = {s["police_no"] for s in satirlar if s["police_no"]} - {k["police_no"] for k in kayitlar}
    sayac.atlanan += [(no, "", "başlangıç tarihi okunamadı") for no in sorted(atlanan_no)]

    for k in kayitlar:
        try:
            with db.begin_nested():
                musteri, neden = musteri_belirle(db, k, eslestirici, sayac)
                if musteri is None:
                    sayac.atlanan.append((k["police_no"], k["ad_soyad"], neden))
                    continue
                police_yaz(db, k, musteri, sayac)
        except Exception as e:  # tek satırın hatası tüm aktarımı durdurmasın
            sayac.hata += 1
            sayac.atlanan.append((k["police_no"], k["ad_soyad"], f"HATA: {e}"))
    return sayac


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="veritabanına yazmadan raporla")
    ap.add_argument("--dosya", default=",".join(DOSYALAR), help="virgülle ayrılmış: " + ",".join(DOSYALAR))
    ap.add_argument("--klasor", default=str(KLASOR), help="Excel dosyalarının klasörü")
    ap.add_argument("--yedeksiz", action="store_true", help="SQLite yedeği alma")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    secilen = [a.strip() for a in args.dosya.split(",") if a.strip()]
    bilinmeyen = [a for a in secilen if a not in DOSYALAR]
    if bilinmeyen:
        ap.error(f"bilinmeyen dosya anahtarı: {bilinmeyen}")
    sira = [a for a in DOSYALAR if a in secilen]  # Hepiyi her zaman sonda
    var = {a: Path(args.klasor) / DOSYALAR[a] for a in sira if (Path(args.klasor) / DOSYALAR[a]).exists()}
    for a in sira:
        if a not in var:
            print(f"! {DOSYALAR[a]} bulunamadı, atlanıyor.")
    if not var:
        print("Aktarılacak Excel dosyası yok.")
        return 1

    print(f"Veritabanı: {'SQLite (yerel)' if SQLITE else 'PostgreSQL'}")
    rapor = init_db()
    if rapor.get("hata"):
        print("! Şema/temizlik adımında uyarı var (ayrıntı log'da); aktarım yine de devam ediyor.")
    if not SQLITE and not args.dry_run:
        print("Not: PostgreSQL için otomatik yedek alınmaz; Supabase panelinden yedeğinizi alın.")
    if SQLITE and not args.dry_run and not args.yedeksiz:
        print(f"Yedek alındı: {sqlite_yedekle()}")

    db = SessionLocal()
    sonuclar = {}
    try:
        for anahtar, yol in var.items():
            print(f"--> {yol.name} işleniyor...")
            sonuclar[anahtar] = dosyayi_aktar(db, anahtar, yol, lambda: MaskeEslestirici(db))
            print("    " + sonuclar[anahtar].ozet())
            if sonuclar[anahtar].police == 0:
                print(f"    ! UYARI: {yol.name} dosyasından hiç poliçe okunamadı — sütun adlarını kontrol edin.")
            nedenler = {}
            for _no, _ad, neden in sonuclar[anahtar].atlanan:
                nedenler[neden[:110]] = nedenler.get(neden[:110], 0) + 1
            for neden, adet in sorted(nedenler.items(), key=lambda kv: -kv[1])[:3]:
                print(f"      atlanan · {neden}: {adet}")
            for neden, adet in sorted(sonuclar[anahtar].eslesme.items()):
                print(f"      maske eşleşmesi · {neden}: {adet}")
            if not args.dry_run:
                db.commit()
        if args.dry_run:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    atlananlar = [(DOSYALAR[a], *x) for a, s in sonuclar.items() for x in s.atlanan]
    if atlananlar:
        rapor = Path(args.klasor) / "import_atlanan_satirlar.csv"
        with open(rapor, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Dosya", "Poliçe No", "Sigortalı", "Neden"])
            w.writerows(atlananlar)
        print(f"\nAtlanan {len(atlananlar)} kayıt (özellikle Hepiyi'de eşleşmeyenler) → {rapor.name}")
    print("\n" + ("DRY-RUN: hiçbir şey yazılmadı." if args.dry_run else "Aktarım tamamlandı. Tekrar çalıştırmak güvenlidir (veri çoğalmaz)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
