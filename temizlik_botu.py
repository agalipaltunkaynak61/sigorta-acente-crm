import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE_URL = "https://sigorta-acente-crm.onrender.com"

# Sunucu kopmalarına ve uyku moduna karşı dayanıklı bağlantı ayarı
session = requests.Session()
retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

def sistemi_temizle():
    print("1. Poliçe Hataları Temizleniyor (Şirket ve Branş)... Lütfen sunucu uyanana kadar bekleyin.")
    try:
        # Timeout süresi 90 saniyeye çıkarıldı
        policeler_req = session.get(f"{API_BASE_URL}/api/policeler", timeout=90)
        policeler = policeler_req.json()
    except Exception as e:
        print(f"[X] Poliçeler çekilirken hata oluştu: {e}")
        return

    for p in policeler:
        degisti = False
        
        # Şirket Adı Standardizasyonu
        sirket = p.get('sigorta_sirketi', '')
        if sirket in ['AXA SİGORTA A.Ş.', 'Axa sigorta', 'Axa Sigorta A.Ş.']:
            p['sigorta_sirketi'] = 'Axa Sigorta'
            degisti = True
        elif sirket in ['TÜRKİYE SİGORTA ANONİM ŞİRKETİ', 'TÜRKİYE SİGORTA A.Ş.']:
            p['sigorta_sirketi'] = 'Türkiye Sigorta'
            degisti = True
        elif sirket == 'Doğa Sigorta A.Ş.':
            p['sigorta_sirketi'] = 'Doğa Sigorta'
            degisti = True
            
        # Branş Adı Standardizasyonu
        brans = p.get('sigorta_turu', '')
        if brans == 'Trafik':
            p['sigorta_turu'] = 'Trafik Sigortası'
            degisti = True
        elif brans == 'Kasko':
            p['sigorta_turu'] = 'Kasko Sigortası'
            degisti = True
        elif brans == 'TSS':
            p['sigorta_turu'] = 'Tamamlayıcı Sağlık Sigortası'
            degisti = True
            
        if degisti:
            try:
                session.put(f"{API_BASE_URL}/api/policeler/{p['id']}", json=p, timeout=30)
                print(f"[+] Düzeltildi: Poliçe {p.get('police_no')}")
            except Exception as e:
                print(f"[-] Poliçe {p.get('police_no')} güncellenemedi: {e}")

    print("\n2. Müşteriler, Danışmanlar ve Çift Kayıtlar Kontrol Ediliyor...")
    try:
        musteriler = session.get(f"{API_BASE_URL}/api/musteriler", timeout=90).json()
    except Exception as e:
        print(f"[X] Müşteriler çekilirken hata oluştu: {e}")
        return

    isim_haritasi = {}
    
    for m in musteriler:
        degisti = False
        
        # Danışman Düzeltme
        if m.get('portfoy_sorumlusu') == 'Sezai Karakoç':
            m['portfoy_sorumlusu'] = 'Sezai Yağcı'
            degisti = True
            
        if degisti:
            session.put(f"{API_BASE_URL}/api/musteriler/{m['id']}", json=m, timeout=30)

        # Çift Müşteri Tespiti
        tam_isim = f"{m.get('ad','')} {m.get('soyad','')}".strip().upper()
        tam_isim = tam_isim.replace('İ','I').replace('Ş','S').replace('Ğ','G').replace('Ü','U').replace('Ö','O').replace('Ç','C')
        
        if tam_isim in isim_haritasi:
            ana_id = isim_haritasi[tam_isim]
            kopya_id = m['id']
            print(f"[!] Çift Müşteri Bulundu: {tam_isim}. Kayıtlar birleştiriliyor...")
            
            for p in policeler:
                if str(p.get('musteri_id')) == str(kopya_id):
                    p['musteri_id'] = ana_id
                    session.put(f"{API_BASE_URL}/api/policeler/{p['id']}", json=p, timeout=30)
            
            # Kopyayı sil
            session.delete(f"{API_BASE_URL}/api/musteriler/{kopya_id}", timeout=30)
        else:
            isim_haritasi[tam_isim] = m['id']

if __name__ == "__main__":
    sistemi_temizle()
    print("Temizlik işlemi tamamlandı!")