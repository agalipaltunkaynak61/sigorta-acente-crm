import io
import json
import pdfplumber
from google import genai
from google.genai import types
import os

# Render ortamındaki GCP_API_KEY değişkenini güvenli bir şekilde okuyoruz
GEMINI_API_KEY = os.getenv("GCP_API_KEY") or os.getenv("GCP_API_KEY")

def pdf_metni_al(dosya: bytes) -> str:
    sayfalar = []
    with pdfplumber.open(io.BytesIO(dosya)) as pdf:
        for sayfa in pdf.pages:
            parca = sayfa.extract_text() or ""
            sayfalar.append(parca)
    return "\n".join(sayfalar)

def ayikla_police_pdf(dosya: bytes) -> dict:
    metin = pdf_metni_al(dosya)
    if not metin.strip():
        raise ValueError("PDF dosyasından metin okunamadı!")

    if not GEMINI_API_KEY:
        raise ValueError("API anahtarı bulunamadı! Lütfen Render çevre değişkenlerini kontrol edin.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    Aşağıdaki sigorta poliçesini analiz et ve tam olarak şu alanları içeren geçerli bir JSON nesnesi döndür. Başka hiçbir açıklama yazma.

    Alanlar:
    - ad: Müşterinin adı
    - soyad: Müşterinin soyadı
    - tckn: TC Kimlik veya Vergi No
    - police_no: Poliçe numarası
    - sigorta_sirketi: Sigorta şirketinin tam adı (Örn: AXA SİGORTA A.Ş.)
    - sigorta_turu: Poliçe türü (Örn: Trafik, Kasko, DASK)
    - islem_turu: Poliçenin mahiyeti nedir? Şunlardan biri olmalı: "Yeni Poliçe", "Zeyilname (Ek Sözleşme)", "İptal", "Plaka Değişikliği", "Yenileme"
    - baslangic_tarihi: YYYY-MM-DD formatında başlangıç tarihi
    - bitis_tarihi: YYYY-MM-DD formatında bitiş tarihi
    - net_prim: Sayısal float değer (Örn: 25933.76)
    - brut_prim: Sayısal float değer (Örn: 28469.07)

    Poliçe Metni:
    {metin[:4000]}
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        if not response or not response.text:
            raise ValueError("Yapay zekadan yanıt alınamadı.")
        veri = json.loads(response.text)
    except Exception as e:
        err_str = str(e)
        if "503" in err_str or "UNAVAILABLE" in err_str:
            raise ValueError("Google sunucuları anlık yoğunluk yaşadı. Lütfen birkaç saniye sonra tekrar deneyin.")
        if "429" in err_str or "ResourceExhausted" in err_str or "quota" in err_str.lower():
            raise ValueError("API kota sınırına ulaşıldı. Lütfen kısa bir süre bekleyin.")
        raise ValueError(f"Yapay zeka okuma hatası: {err_str}")

    ad = veri.get("ad") or ""
    soyad = veri.get("soyad") or ""
    brut = veri.get("brut_prim") or veri.get("net_prim") or 0.0

    return {
        "ad_soyad": f"{ad} {soyad}".strip(),
        "ad": ad,
        "soyad": soyad,
        "tckn": veri.get("tckn"),
        "vergi_no": veri.get("tckn") if veri.get("tckn") and len(str(veri.get("tckn"))) == 10 else None,
        "police_no": str(veri.get("police_no") or ""),
        "sigorta_sirketi": veri.get("sigorta_sirketi") or "Bilinmeyen Sigorta",
        "sigorta_turu": veri.get("sigorta_turu") or "Diğer",
        "islem_turu": veri.get("islem_turu") or "Yeni Poliçe",
        "baslangic_tarihi": veri.get("baslangic_tarihi"),
        "bitis_tarihi": veri.get("bitis_tarihi"),
        "net_prim": veri.get("net_prim"),
        "brut_prim": brut,
        "prim": brut,
    }