import pandas as pd
import requests
import time
from datetime import datetime
from thefuzz import fuzz
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= AYARLAR =================
API_BASE_URL = "https://sigorta-acente-crm.onrender.com"
EXCEL_DOSYASI = "Police_Arama_2026_09_14_15_46.xls"  # Türkiye Sigorta portal dışa aktarımı (HTML formatlı .xls)
BENZERLIK_ESIGI = 85 
SABIT_SIRKET = "Türkiye Sigorta"
MAX_WORKERS = 5  # Aynı anda atılacak istek sayısı
# ============================================

BRANS_SOZLUGU = {
    "KARAYOLLARI MOTORLU ARAÇLAR ZORUNLU MALİ SORUMLULUK (TRAFİK SİGORTASI)": "Trafik Sigortası",
    "KARA TAŞITLARI İHTİYARİ MALİ MESULİYET": "Trafik Sigortası",
    "AVANTAJLI GENİŞLETİLMİŞ KASKO": "Kasko Sigortası",
    "T_KASKO": "Kasko Sigortası",
    "ZORUNLU DEPREM SİGORTASI-DASK": "DASK",
    "TAMAMLAYICI SAĞLIK": "Tamamlayıcı Sağlık Sigortası",
    "İŞ YERİ EKSTRA": "Kurumsal ve İş Yeri Sigortaları",
    "KAPSAMLI İŞ YERİ": "Kurumsal ve İş Yeri Sigortaları",
    "BİRLEŞİK PAKET (1.0)": "Konut ve Eşya Sigortaları",
}

def sistemden_verileri_cek():
    try:
        print("CRM sisteminden mevcut veriler çekiliyor...")
        musteriler = requests.get(f"{API_BASE_URL}/api/musteriler", timeout=15).json()
        policeler = requests.get(f"{API_BASE_URL}/api/policeler", timeout=15).json()
        mevcut_policeler = [str(p.get("police_no", "")).strip() for p in policeler]
        return musteriler, mevcut_policeler
    except Exception as e:
        print(f"CRM'e bağlanılamadı: {e}")
        return [], []

def musteri_bul_veya_olustur(musteri_isim, musteriler_cache):
    okunan_isim_temiz = str(musteri_isim).upper().strip()
    en_iyi_skor = 0
    eslesen_musteri_id = None
    
    for m in musteriler_cache:
        sistem_isim = f"{m.get('ad', '')} {m.get('soyad', '')}".upper().strip()
        skor = fuzz.token_sort_ratio(okunan_isim_temiz, sistem_isim)
        if skor > en_iyi_skor:
            en_iyi_skor = skor
            eslesen_musteri_id = m.get('id')

    if en_iyi_skor >= BENZERLIK_ESIGI:
        return eslesen_musteri_id

    # Yeni müşteri oluştur
    isim_parcalar = str(musteri_isim).strip().split(" ")
    ad = isim_parcalar[0]
    soyad = " ".join(isim_parcalar[1:]) if len(isim_parcalar) > 1 else ""
    yeni_musteri = {
        "ad": ad, 
        "soyad": soyad, 
        "telefon": "-",         
        "portfoy_sorumlusu": "Atanmadı", 
        "tc_kimlik": None
    }
    try:
        m_res = requests.post(f"{API_BASE_URL}/api/musteriler", json=yeni_musteri, timeout=15)
        if m_res.status_code in [200, 201]:
            olusturulan = m_res.json()
            musteriler_cache.append(olusturulan)
            return olusturulan["id"]
    except Exception as e:
        print(f"   [X] Müşteri oluşturma hatası ({musteri_isim}): {e}")
    return None

def tarih_formatla(tarih_str):
    try:
        # Türkiye Sigorta portalından gelen ISO tarih formatı (örn: 2026-07-10T00:00:00.000+03:00)
        dt = datetime.fromisoformat(str(tarih_str).strip())
        return dt.strftime("%Y-%m-%d")
    except:
        try:
            dt = datetime.strptime(str(tarih_str).strip(), "%d.%m.%Y")
            return dt.strftime("%Y-%m-%d")
        except:
            bugun = datetime.now()
            return bugun.strftime("%Y-%m-%d")

def ana_bransi_bul(urun_adi):
    temiz_urun = str(urun_adi).strip().upper()
    if "KASKO" in temiz_urun: 
        return "Kasko Sigortası"
    if "TRAFİK" in temiz_urun or "ZORUNLU MALİ SORUMLULUK" in temiz_urun: 
        return "Trafik Sigortası"
    if "DASK" in temiz_urun: 
        return "DASK"
    if "SAĞLIK" in temiz_urun: 
        return "Tamamlayıcı Sağlık Sigortası"
    
    if temiz_urun in BRANS_SOZLUGU:
        return BRANS_SOZLUGU[temiz_urun]
        
    return "Diğer"

def poli_isle(row, musteriler, mevcut_policeler):
    police_no = str(int(row['Poliçe No'])).strip()
    if police_no in mevcut_policeler:
        return f"[!] Poliçe No {police_no} zaten sistemde var. Atlandı."
        
    musteri_isim = row['Sigortalı']
    baslangic = tarih_formatla(row['Başlangıç Tarihi'])
    bitis = tarih_formatla(row['Bitiş Tarihi'])
    
    urun_adi = row['Ürün Adı']
    ana_brans = ana_bransi_bul(urun_adi)
    
    # Türkiye Sigorta liste dışa aktarımında prim kolonu bulunmadığı için 0.0 atanır
    toplam_prim = 0.0 
    
    musteri_id = musteri_bul_veya_olustur(musteri_isim, musteriler)
    if not musteri_id:
        return f"[X] Müşteri çözülemediği için poliçe atlandı: {police_no}"

    police_data = {
        "musteri_id": musteri_id,
        "police_no": police_no,
        "sigorta_turu": ana_brans,
        "sigorta_sirketi": SABIT_SIRKET,
        "islem_turu": "Yeni Poliçe",
        "baslangic_tarihi": baslangic,
        "bitis_tarihi": bitis,
        "prim": toplam_prim
    }
    
    try:
        p_res = requests.post(f"{API_BASE_URL}/api/policeler", json=police_data, timeout=15)
        if p_res.status_code in [200, 201]:
            mevcut_policeler.append(police_no)
            return f"[✓] BAŞARILI: {police_no} | {ana_brans} | {urun_adi}"
        else:
            return f"[X] Poliçe eklenemedi ({police_no}): {p_res.text}"
    except Exception as e:
        return f"[X] Bağlantı hatası ({police_no}): {e}"

def botu_calistir():
    musteriler, mevcut_policeler = sistemden_verileri_cek()
    
    print(f"'{EXCEL_DOSYASI}' dosyası okunuyor (HTML tabanlı Excel)...")
    try:
        df = pd.read_html(EXCEL_DOSYASI)[0]
    except Exception as e:
        print(f"Dosya okunurken hata oluştu: {e}")
        return
    
    print(f"Toplam {len(df)} adet poliçe eşzamanlı olarak işlenmeye başlanıyor...\n")

    islenecek_veriler = []
    for _, row in df.iterrows():
        if pd.isnull(row.get('Poliçe No')):
            continue
        islenecek_veriler.append((row, musteriler, mevcut_policeler))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(poli_isle, row, m, m_pol) for row, m, m_pol in islenecek_veriler]
        for future in as_completed(futures):
            print(future.result())

    print("\n🎉 Tüm Türkiye Sigorta Poliçe Verileri Canlı CRM'e Aktarıldı!")

if __name__ == "__main__":
    botu_calistir()