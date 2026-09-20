"""Poliçe PDF'lerini Gemini ile yapılandırılmış veriye çevirir."""
import io
import json
import os
import re
import time
from datetime import datetime
from functools import lru_cache

import pdfplumber
from google import genai
from google.genai import types

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_METIN_KARAKTER = 60_000   # çok uzun PDF'lerde istem boyutunu sınırlar
MIN_METIN_KARAKTER = 40       # bunun altı "taranmış (görüntü) PDF" sayılır
MAX_DENEME = 3
GECICI_HATALAR = ("429", "resourceexhausted", "quota", "503", "unavailable", "500", "internal", "deadline", "timeout")

PROMPT = """
Aşağıdaki sigorta poliçesini dikkatlice analiz et ve tam olarak şu alanları içeren geçerli bir JSON nesnesi döndür. Başka hiçbir açıklama yazma.

ÖNEMLİ KURALLAR:
1. Belgede "Sigorta Ettiren" (kurum/şirket olabilir) ile "Sigortalı" (gerçek kişi) farklı olabilir. Sen daima "Sigortalı" bölümündeki kişinin adını, soyadını ve TCKN'sini bul.
2. TCKN veya isimlerde yıldız (*) ile gizleme (maskeleme) yapılmışsa (Örn: 1***1***7**), bu değerleri yok sayma, olduğu gibi yıldızlı haliyle al.
3. Poliçenin asıl bilgileri ilk sayfadaki reklamlardan sonra (örneğin 3. veya 4. sayfada) başlıyor olabilir, metnin sonuna kadar dikkatlice tara.
4. Prim tutarlarını (Örn: 23.824,49 TL) temizleyerek sadece sayısal float değere çevir (Örn: 23824.49).

Alanlar:
- ad: Sigortalının adı
- soyad: Sigortalının soyadı
- tckn: TC Kimlik veya Vergi No (Yıldızlıysa yıldızlı haliyle yaz)
- police_no: Poliçe numarası
- sigorta_sirketi: Sigorta şirketinin tam adı (Örn: TÜRKİYE SİGORTA A.Ş.)
- sigorta_turu: Poliçe türü (Örn: Trafik, Kasko, DASK, Konut, TSS, İş Yeri)
- islem_turu: Poliçenin mahiyeti: "Yeni Poliçe", "Zeyilname (Ek Sözleşme)", "İptal", "Plaka Değişikliği", "Yenileme"
- baslangic_tarihi: YYYY-MM-DD formatında başlangıç tarihi
- bitis_tarihi: YYYY-MM-DD formatında bitiş tarihi
- net_prim: Sayısal float değer
- brut_prim: Sayısal float değer
- arac_bilgisi: Eğer poliçe Trafik veya Kasko ise, aracın Plaka, Marka, Model ve Model Yılı bilgilerini yaz (Örn: "34 ABC 123 - Ford Focus 2022"). Araç poliçesi değilse boş bırakın (null).
- varlik_bilgisi: Eğer poliçe DASK veya Konut/İş Yeri ise, adres ve metrekare (m2) bilgilerini yaz (Örn: "Atatürk Mah. No:12 D:3, 110 m2"). Değilse boş bırakın (null).

Poliçe Metni:
"""


def pdf_metni_al(dosya: bytes) -> str:
    with pdfplumber.open(io.BytesIO(dosya)) as pdf:
        return "\n".join(sayfa.extract_text() or "" for sayfa in pdf.pages)


@lru_cache(maxsize=1)
def _istemci(api_key: str):
    return genai.Client(api_key=api_key)


def _icerik_hazirla(dosya: bytes, metin: str) -> list:
    """Metin çıkarılabildiyse metni, taranmış PDF ise PDF'in kendisini modele verir."""
    if len(metin.strip()) >= MIN_METIN_KARAKTER:
        return [PROMPT + metin[:MAX_METIN_KARAKTER]]
    return [types.Part.from_bytes(data=dosya, mime_type="application/pdf"), PROMPT + "(Poliçe ekteki PDF'tedir.)"]


def _hata_mesaji(hata: Exception) -> str:
    s = str(hata)
    if "429" in s or "ResourceExhausted" in s or "quota" in s.lower():
        return "Ücretsiz API dakika sınırı (5 istek/dk) doldu. Lütfen 10-15 saniye bekleyip tekrar deneyin."
    if "503" in s or "UNAVAILABLE" in s:
        return "Google sunucuları yoğun. Birkaç saniye sonra tekrar deneyin."
    return f"Yapay zeka okuma hatası: {s}"


def _modele_sor(icerik: list) -> str:
    api_key = os.getenv("GCP_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("API anahtarı bulunamadı! Lütfen Render çevre değişkenlerini kontrol edin.")
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)

    for deneme in range(1, MAX_DENEME + 1):
        try:
            yanit = _istemci(api_key).models.generate_content(model=MODEL, contents=icerik, config=config)
            if yanit and yanit.text:
                return yanit.text
            hata = ValueError("Yapay zekadan yanıt alınamadı.")
        except Exception as e:
            if not any(h in str(e).lower() for h in GECICI_HATALAR):
                raise ValueError(_hata_mesaji(e))  # geçersiz anahtar, hatalı istek vb.: tekrar denemenin anlamı yok
            hata = e
        if deneme == MAX_DENEME:
            raise ValueError(_hata_mesaji(hata))
        time.sleep(12 if "429" in str(hata) or "quota" in str(hata).lower() else 4)


def _json_coz(metin: str) -> dict:
    temiz = re.sub(r"^```(?:json)?\s*|\s*```$", "", metin.strip())
    try:
        veri = json.loads(temiz)
    except json.JSONDecodeError:
        raise ValueError("Yapay zeka geçerli bir JSON döndürmedi. Lütfen tekrar deneyin.")
    if isinstance(veri, list):
        veri = veri[0] if veri else {}
    if not isinstance(veri, dict):
        raise ValueError("Yapay zeka beklenmeyen bir yanıt döndürdü.")
    return veri


def _sayi(deger):
    """23.824,49 / '23824.49 TL' / 23824.49 → float; çözülemezse None."""
    if deger is None or deger == "":
        return None
    if isinstance(deger, (int, float)):
        return float(deger)
    s = re.sub(r"[^\d,.\-]", "", str(deger))
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _tarih(deger):
    """YYYY-MM-DD'ye normalleştirir; çözülemezse None."""
    if not deger:
        return None
    s = str(deger).strip()[:10]
    for bicim in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, bicim).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _kimlik(deger):
    """Maskeli değerler olduğu gibi kalır; düz değerlerden rakam dışı karakterler atılır."""
    if not deger:
        return None
    s = str(deger).strip()
    return s if "*" in s else (re.sub(r"\D", "", s) or None)


def _islem_tipi(islem_turu, prim) -> str:
    """Yapay zekanın serbest metnini uygulamadaki işlem tiplerine çevirir."""
    s = (islem_turu or "").replace("İ", "i").lower()  # "İptal".lower() Python'da "i̇ptal" olur
    if "iptal" in s:
        return "İptal"
    if "yenileme" in s:
        return "Yenileme"
    if any(k in s for k in ("zeyil", "plaka", "ek sözleşme")):
        return "İadeli Zeyil" if (prim or 0) < 0 else "Primli Zeyil"
    return "Yeni"


def ayikla_police_pdf(dosya: bytes) -> dict:
    try:
        metin = pdf_metni_al(dosya)
    except Exception:
        raise ValueError("PDF dosyası okunamadı; dosya bozuk veya şifreli olabilir.")

    veri = _json_coz(_modele_sor(_icerik_hazirla(dosya, metin)))

    ad, soyad = (veri.get("ad") or "").strip(), (veri.get("soyad") or "").strip()
    tckn = _kimlik(veri.get("tckn"))
    net, brut = _sayi(veri.get("net_prim")), _sayi(veri.get("brut_prim"))
    brut = brut if brut is not None else (net or 0.0)
    islem_turu = veri.get("islem_turu") or "Yeni Poliçe"

    return {
        "ad_soyad": f"{ad} {soyad}".strip(),
        "ad": ad,
        "soyad": soyad,
        "tckn": tckn,
        "vergi_no": tckn if tckn and "*" not in tckn and len(tckn) == 10 else None,
        "police_no": str(veri.get("police_no") or "").strip(),
        "sigorta_sirketi": veri.get("sigorta_sirketi") or "Bilinmeyen Sigorta",
        "sigorta_turu": veri.get("sigorta_turu") or "Diğer",
        "islem_turu": islem_turu,
        "islem_tipi": _islem_tipi(islem_turu, brut),
        "baslangic_tarihi": _tarih(veri.get("baslangic_tarihi")),
        "bitis_tarihi": _tarih(veri.get("bitis_tarihi")),
        "net_prim": net,
        "brut_prim": brut,
        "prim": brut,
        "arac_bilgisi": veri.get("arac_bilgisi"),
        "varlik_bilgisi": veri.get("varlik_bilgisi"),
    }
