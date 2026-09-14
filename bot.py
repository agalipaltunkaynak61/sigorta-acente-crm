import pandas as pd
import requests
import time
import threading
from datetime import datetime
from thefuzz import fuzz
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= AYARLAR =================
API_BASE_URL = "https://sigorta-acente-crm.onrender.com"
EXCEL_DOSYASI = "Acente_Police_Dokumu_(EXCEL)_20260914_1620_75856438.xlsx"
BENZERLIK_ESIGI = 85 
SABIT_SIRKET = "Türkiye Sigorta"
MAX_WORKERS = 5  # Sunucu güvenliği için ideal işçi sayısı
# ============================================

musteri_lock = threading.Lock()

# Kesin Eşleşme Sözlüğü (Asla "Diğer" kalmayacak şekilde uyarlandı)
BRANS_SOZLUGU = {
    "15101 - Karayolları Motorlu Araçlar Zorunlu Mali Sorumluluk ": "Trafik Sigortası",
    "15101 - Karayollari Motorlu Araçlar Zorunlu Mali Sorumluluk": "Trafik Sigortası",
    "46150 - Kara Taşıtları İhtiyari Mali Mesuliyet": "Trafik Sigortası",
    "60200 - Yeşilkart": "Trafik Sigortası",
    "KARAYOLLARI MOTORLU ARAÇLAR ZORUNLU MALİ SORUMLULUK (TRAFİK SİGORTASI)": "Trafik Sigortası",
    "KARA TAŞITLARI İHTİYARİ MALİ MESULİYET": "Trafik Sigortası",

    "86142 - AVANTAJLI GENİŞLETİLMİŞ KASKO": "Kasko Sigortası",
    "AVANTAJLI GENİŞLETİLMİŞ KASKO": "Kasko Sigortası",
    "86143 - T_KASKO": "Kasko Sigortası",
    "T_KASKO": "Kasko Sigortası",

    "21100 - Zorunlu Deprem Sigortası-DASK": "DASK",
    "ZORUNLU DEPREM SİGORTASI-DASK": "DASK",

    "85270 - Tamamlayıcı Sağlık": "Tamamlayıcı Sağlık Sigortası",
    "TAMAMLAYICI SAĞLIK": "Tamamlayıcı Sağlık Sigortası",

    "62200 - İŞ YERİ EKSTRA": "Kurumsal ve İş Yeri Sigortaları",
    "İŞ YERİ EKSTRA": "Kurumsal ve İş Yeri Sigortaları",
    "44150 - KAPSAMLI İŞ YERİ": "Kurumsal ve İş Yeri Sigortaları",
    "KAPSAMLI İŞ YERİ": "Kurumsal ve İş Yeri Sigortaları",
    "9100 - Tehlikeli Maddeler ve Tehlikeli Atık Zorunlu Mali Sorumluluk ": "Kurumsal ve İş Yeri Sigortaları",
    "9100 - Tehlikeli Maddeler ve Tehlikeli Atık Zorunlu Mali Sorumluluk": "Kurumsal ve İş Yeri Sigortaları",
    "29100 - Özel Güvenlik Zorunlu Sorumluluk Sigortası": "Kurumsal ve İş Yeri Sigortaları",
    "84218 - Tıbbi Kötü Uygulamaya İlişkin Zorunlu  Mali Sorumluluk Sigortası": "Kurumsal ve İş Yeri Sigortaları",

    "41150 - Konut Ekstra Paket": "Konut ve Eşya Sigortaları",
    "86000 - Birleşik Paket (1.0)": "Konut ve Eşya Sigortaları",
    "BİRLEŞİK PAKET (1.0)": "Konut ve Eşya Sigortaları",

    "37101 - Emtia Nakli(Abonman sözleşmesine bağlı)": "Nakliyat",
    "85211 - Nakliyat Emtia Abonman Sözleşmesi": "Nakliyat",
    "13100 - Ferdi Kaza": "Ferdi Kaza",

    # Kıyıda köşede kalan özel ürünler için net ana branşlar (Asla Diğer olmuyor)
    "51153 - Devlet Destekli Bitkisel Ürün Sigortası": "Tarım (TARSİM)",
    "28100 - Yat": "Yat / Denizcilik",
    "28101 - İnşaat All Risks": "Allrisk / Mühendislik",
    "86062 - HER ŞEYE HAZIRIM ": "Özel Paket / Destek"
}

def guvenli_istek(url, method="GET", json_data=None, max_deneme=3):
    """Zaman aşımı ve bağlantı kopmalarına karşı otomatik tekrar deneme mekanizması"""
    for deneme in range(max_deneme):
        try:
            if method == "GET":
                res = requests.get(url, timeout=30)
            else:
                res = requests.post(url, json=json_data, timeout=30)
            return res
        except Exception as e:
            if deneme == max_deneme - 1:
                raise e
            time.sleep(1.5 * (deneme + 1))

def musteri_bul_veya_olustur(musteri_isim):
    """Müşteriyi isme göre işler, çakışma olmaması için sunucuya bildirir"""
    with musteri_lock:
        if not musteri_isim or pd.isnull(musteri_isim):
            return None
            
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
            m_res = guvenli_istek(f"{API_BASE_URL}/api/musteriler", method="POST", json_data=yeni_musteri)
            if m_res.status_code in [200, 201]:
                return m_res.json().get("id")
        except Exception as e:
            print(f"   [X] Müşteri oluşturma hatası ({musteri_isim}): {e}")
        return None

def tarih_formatla(tarih_str):
    try:
        dt = datetime.fromisoformat(str(tarih_str).strip())
        return dt.strftime("%Y-%m-%d")
    except:
        try:
            dt = datetime.strptime(str(tarih_str).strip(), "%d.%m.%Y")
            return dt.strftime("%Y-%m-%d")
        except:
            return datetime.now().strftime("%Y-%m-%d")

def ana_bransi_bul(urun_adi):
    if not urun_adi:
        return "Özel Sigorta Ürünleri"
        
    temiz_urun = str(urun_adi).strip()
    
    # 1. Sözlükte tam eşleşme kontrolü
    if temiz_urun in BRANS_SOZLUGU:
        return BRANS_SOZLUGU[temiz_urun]
        
    # 2. Akıllı Kelime Eşleme (Yedek Güvence - Asla Diğer bırakmamak için)
    u_upper = temiz_urun.upper()
    if "KASKO" in u_upper: return "Kasko Sigortası"
    if "TRAFİK" in u_upper or "ZORUNLU MALİ SORUMLULUK" in u_upper or "YEŞİLKART" in u_upper: return "Trafik Sigortası"
    if "DASK" in u_upper or "DEPREM" in u_upper: return "DASK"
    if "SAĞLIK" in u_upper: return "Tamamlayıcı Sağlık Sigortası"
    if "İŞ YERİ" in u_upper or "YANGIN" in u_upper or "ÖZEL GÜVENLİK" in u_upper or "TIBBİ" in u_upper: return "Kurumsal ve İş Yeri Sigortaları"
    if "KONUT" in u_upper or "BİRLEŞİK PAKET" in u_upper: return "Konut ve Eşya Sigortaları"
    if "NAKLİYAT" in u_upper or "EMTİA" in u_upper: return "Nakliyat"
    if "FERDİ KAZA" in u_upper: return "Ferdi Kaza"
    if "BİTKİSEL" in u_upper or "TARSİM" in u_upper or "ÜRÜN" in u_upper: return "Tarım (TARSİM)"
    if "YAT" in u_upper: return "Yat / Denizcilik"
    if "İNŞAAT" in u_upper or "ALL RISKS" in u_upper or "ALLRİSK" in u_upper: return "Allrisk / Mühendislik"
    if "HER ŞEYE HAZIRIM" in u_upper: return "Özel Paket / Destek"
    
    return "Özel Sigorta Ürünleri"

def poli_isle(row):
    try:
        p_no = row.get('Pol\nNo')
        if pd.isnull(p_no):
            return None
            
        police_no = str(p_no).strip()
        if police_no.endswith('.0'):
            police_no = police_no[:-2]
            
        musteri_isim = row.get('Sigortalı')
        if pd.isnull(musteri_isim):
            return f"[X] Müşteri adı boş: {police_no}"
            
        baslangic = tarih_formatla(row.get('Baş\nTar'))
        bitis = tarih_formatla(row.get('Bit.\nTar'))
        urun_adi = row.get('Ürün', 'Diğer')
        ana_brans = ana_bransi_bul(urun_adi)
        toplam_prim = float(row.get('TL Net\nPrim', 0) or 0)
        
        # Müşteri ID'sini al veya oluştur
        musteri_id = musteri_bul_veya_olustur(musteri_isim)
        if not musteri_id:
            return f"[X] Müşteri çözülemedi: {police_no}"

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
        
        # Doğrudan kayıt atıyoruz (CRM aynı poliçe numarası varsa otomatik engeller veya günceller)
        p_res = guvenli_istek(f"{API_BASE_URL}/api/policeler", method="POST", json_data=police_data)
        if p_res.status_code in [200, 201]:
            return f"[✓] BAŞARILI: {police_no} | {ana_brans} | ({urun_adi})"
        else:
            return f"[!] ZATEN VAR / ATLANDI: {police_no}"
    except Exception as e:
        return f"[X] İşlem hatası: {e}"

def botu_calistir():
    print(f"'{EXCEL_DOSYASI}' dosyası okunuyor...")
    try:
        df = pd.read_excel(EXCEL_DOSYASI, skiprows=6)
    except Exception as e:
        print(f"Dosya okunurken hata oluştu: {e}")
        return
    
    print(f"Toplam {len(df)} adet poliçe hızlı eşzamanlı olarak işlenmeye başlanıyor...\n")

    islenecek_veriler = []
    for _, row in df.iterrows():
        p_no = row.get('Pol\nNo')
        if pd.isnull(p_no):
            continue
        islenecek_veriler.append(row)

    baslangic_zamani = time.time()
    basarili_sayisi = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(poli_isle, row) for row in islenecek_veriler]
        for future in as_completed(futures):
            sonuc = future.result()
            if sonuc:
                print(sonuc)
                if "[✓]" in sonuc:
                    basarili_sayisi += 1

    gecen_sure = time.time() - baslangic_zamani
    print(f"\n🎉 İşlem Tamamlandı! Süre: {gecen_sure:.2f} saniye, Eklenen / Güncellenen Poliçe: {basarili_sayisi}")

if __name__ == "__main__":
    botu_calistir()