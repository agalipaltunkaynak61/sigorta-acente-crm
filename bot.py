import pandas as pd
import requests
import time
from datetime import datetime, timedelta
from thefuzz import fuzz
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= AYARLAR =================
API_BASE_URL = "https://sigorta-acente-crm.onrender.com"
EXCEL_DOSYASI = "axa_liste.xlsx"
BENZERLIK_ESIGI = 85 
SABIT_SIRKET = "Axa Sigorta"
MAX_WORKERS = 5  # Aynı anda atılacak istek sayısı (Sunucuyu yormamak için ideal)
# ============================================

BRANS_SOZLUGU = {
    "ZORUNLU MALİ SORUMLULUK": "Trafik Sigortası",
    "İHTİYARİ MALİ SORUMLULUK (TRAFIK)": "Trafik Sigortası",
    "AXA ASSISTANCE - TRAFİK": "Trafik Sigortası",
    "KASKO": "Kasko Sigortası",
    "FERDİ KAZA": "Ferdi Kaza Sigortaları",
    "MOTORLU ARACA BAĞLI HUKUKSAL KORUMA": "Hukuksal Koruma Sigortaları",
    "SAĞLIK": "Özel Sağlık Sigortası",
    "TAMAMLAYICI SAĞLIK": "Tamamlayıcı Sağlık Sigortası",
    "DASK": "DASK",
    "YANGIN": "Konut ve Eşya Sigortaları",
    "İŞYERİ": "Kurumsal ve İş Yeri Sigortaları",
    "ELEKTRONİK CİHAZ İLK ATEŞ": "Kurumsal ve İş Yeri Sigortaları",
    "HIRSIZLIK": "Konut ve Eşya Sigortaları"
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
        if isinstance(tarih_str, datetime):
            baslangic = tarih_str
        else:
            baslangic = datetime.strptime(str(tarih_str).strip(), "%d.%m.%Y")
        bitis = baslangic + timedelta(days=365)
        return baslangic.strftime("%Y-%m-%d"), bitis.strftime("%Y-%m-%d")
    except:
        bugun = datetime.now()
        return bugun.strftime("%Y-%m-%d"), (bugun + timedelta(days=365)).strftime("%Y-%m-%d")

def ana_bransi_bul(branslar_listesi):
    branslar_str = " ".join([str(b).upper() for b in branslar_listesi])
    if "KASKO" in branslar_str: return "Kasko Sigortası"
    if "ZORUNLU MALİ SORUMLULUK" in branslar_str: return "Trafik Sigortası"
    if "DASK" in branslar_str: return "DASK"
    
    for brans in branslar_listesi:
        temiz_brans = str(brans).strip().upper()
        if temiz_brans in BRANS_SOZLUGU:
            return BRANS_SOZLUGU[temiz_brans]
    return "Diğer"

def poli_isle(police_no, grup, musteriler, mevcut_policeler):
    if police_no in mevcut_policeler:
        return f"[!] Poliçe No {police_no} zaten sistemde var. Atlandı."
        
    musteri_isim = grup['SIGORTALI ADI'].iloc[0]
    tanzim_tar = grup['TANZIM  TAR'].iloc[0]
    baslangic, bitis = tarih_formatla(tanzim_tar)
    toplam_prim = float(grup['BÜRÜT PRİM'].sum())
    brans_listesi = grup['BRANŞ ADI'].dropna().tolist()
    ana_brans = ana_bransi_bul(brans_listesi)
    
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
            return f"[✓] BAŞARILI: {police_no} | {ana_brans} | {toplam_prim:.2f} TL"
        else:
            return f"[X] Poliçe eklenemedi ({police_no}): {p_res.text}"
    except Exception as e:
        return f"[X] Bağlantı hatası ({police_no}): {e}"

def botu_calistir():
    musteriler, mevcut_policeler = sistemden_verileri_cek()
    
    print(f"'{EXCEL_DOSYASI}' dosyası okunuyor...")
    df = pd.read_excel(EXCEL_DOSYASI, sheet_name="Police_Kontrol_Listesi", header=4)
    
    gruplanmis = list(df.groupby('POLİÇE NO'))
    print(f"Toplam {len(gruplanmis)} adet benzersiz poliçe eşzamanlı olarak işlenmeye başlanıyor...\n")

    islenecek_veriler = []
    for police_no, grup in gruplanmis:
        if pd.isnull(police_no):
            continue
        police_no_str = str(int(police_no))
        islenecek_veriler.append((police_no_str, grup, musteriler, mevcut_policeler))

    # ThreadPoolExecutor ile aynı anda çoklu istek atarak hızı katlayalım
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(poli_isle, p_no, g, musteriler, m_pol) for p_no, g, musteriler, m_pol in islenecek_veriler]
        for future in as_completed(futures):
            print(future.result())

    print("\n🎉 Tüm AXA Poliçe Verileri Çok Daha Hızlı Bir Şekilde Canlı CRM'e Aktarıldı!")

if __name__ == "__main__":
    botu_calistir()