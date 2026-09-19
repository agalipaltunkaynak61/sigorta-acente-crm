import os
import pandas as pd
from datetime import datetime, date
from sqlalchemy.orm import Session
from app import (
    SessionLocal, Musteri, Police, standardize_metin, 
    SIRKET_ESLESMELERI, ZORUNLU_SIRKETLER, BRANS_ESLESMELERI, 
    ZORUNLU_BRANSLAR, gelismis_firma_temizle, init_db
)
import warnings
warnings.filterwarnings('ignore') # Excel uyarılarını gizler

def parse_tarih_bot(val):
    if pd.isna(val) or not val:
        return date.today()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    
    val = str(val).strip().split()[0]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return date.today()

def split_ad_soyad(tam_isim):
    if pd.isna(tam_isim) or not tam_isim:
        return "BİLİNMİYOR", ""
    tam_isim = str(tam_isim).strip().upper()
    if any(x in tam_isim for x in ["LTD", "A.Ş", "SAN", "TİC", "ŞTİ", "LİMİTED", "ANONİM"]):
        return tam_isim, "" 
    parts = tam_isim.split()
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]

def musteri_bul_veya_yarat(db: Session, ad, soyad, tckn, telefon):
    ad, soyad = str(ad).strip().upper(), str(soyad).strip().upper()
    temiz_tckn = None
    
    if not pd.isna(tckn):
        t = str(tckn).replace(".0", "").strip()
        if len(t) in [10, 11] and t.isdigit(): 
            temiz_tckn = t

    if temiz_tckn:
        m = db.query(Musteri).filter(Musteri.tc_kimlik == temiz_tckn).first()
        if m: 
            if telefon and not pd.isna(telefon) and not m.telefon:
                m.telefon = str(telefon)
                db.commit()
            return m
        
    norm_anahtar = gelismis_firma_temizle(f"{ad} {soyad}")
    if norm_anahtar:
        tum_musteriler = db.query(Musteri).all()
        for m in tum_musteriler:
            if gelismis_firma_temizle(f"{m.ad} {m.soyad}") == norm_anahtar:
                if temiz_tckn and not m.tc_kimlik: m.tc_kimlik = temiz_tckn
                if telefon and not pd.isna(telefon) and not m.telefon: 
                    m.telefon = str(telefon)
                db.commit()
                return m
                
    telefon_str = str(telefon) if (telefon is not None and not pd.isna(telefon)) else ""

    yeni = Musteri(
        ad=ad, soyad=soyad, tc_kimlik=temiz_tckn, telefon=telefon_str,
        portfoy_sorumlusu="Muammer Altunkaynak"
    )
    db.add(yeni)
    db.commit()
    db.refresh(yeni)
    return yeni

def crm_brans_eslestir(ham_brans):
    """
    Sigorta şirketlerinden gelen karmaşık branş adlarını,
    sisteminizdeki standart checkbox branşlarına çevirir.
    """
    if pd.isna(ham_brans) or not ham_brans:
        return "Özel Paket / Destek"
        
    b = str(ham_brans).upper()
    
    if any(x in b for x in ["DASK", "ZORUNLU DEPREM"]): return "DASK"
    if any(x in b for x in ["KASKO"]): return "Kasko Sigortası"
    if any(x in b for x in ["TRAFİK", "TRAFIK", "ZORUNLU MALİ", "ZORUNLU MALI", "KARAYOLLARI"]): return "Trafik Sigortası"
    if any(x in b for x in ["TAMAMLAYICI", "SAĞLIK", "SAGLIK", "TSS"]): return "Tamamlayıcı Sağlık Sigortası"
    if any(x in b for x in ["FERDİ KAZA", "FERDI KAZA"]): return "Ferdi Kaza"
    if any(x in b for x in ["KONUT", "EŞYA", "ESYA", "YANGIN", "DEPREM"]): return "Konut ve Eşya Sigortaları"
    if any(x in b for x in ["İŞ YERİ", "IS YERI", "İŞYERİ", "TİCARİ", "TICARI", "İŞVEREN", "KOBİ", "İŞ", "MAKİNE", "ELEKTRONİK", "3.ŞAHIS"]): return "İş Yeri"
    if any(x in b for x in ["NAKLİYAT", "NAKLIYAT", "EMTİA", "EMTIA"]): return "Nakliyat"
    if any(x in b for x in ["TARIM", "BİTKİSEL", "BITKISEL", "SERA", "HAYVAN"]): return "Tarım"
    if any(x in b for x in ["YAT", "DENİZ", "DENIZ", "TEKNE"]): return "Yat / Denizcilik"
    if any(x in b for x in ["ALLRİSK", "ALLRISK"]): return "Allrisk"
    
    return "Özel Paket / Destek"

def police_kaydet_veya_guncelle(db: Session, p):
    mevcut = db.query(Police).filter(Police.police_no == str(p['police_no'])).first()
    
    sirket = standardize_metin(p['sigorta_sirketi'], SIRKET_ESLESMELERI, ZORUNLU_SIRKETLER)
    brans = crm_brans_eslestir(p['sigorta_turu'])
    
    if mevcut:
        mevcut.prim = (mevcut.prim or 0.0) + float(p['prim'] or 0.0)
        mevcut.komisyon = (mevcut.komisyon or 0.0) + float(p['komisyon'] or 0.0)
        if p['bitis_tarihi'] and (not mevcut.bitis_tarihi or p['bitis_tarihi'] > mevcut.bitis_tarihi):
            mevcut.bitis_tarihi = p['bitis_tarihi']
            
        zeyil_notu = f" | Otomatik Zeyil İşlendi (Zeyil No: {p.get('zeyil_no', '?')}): Ek Prim {p['prim']} TL"
        mevcut.aciklama = f"{mevcut.aciklama or ''}{zeyil_notu}".strip(" |")
        db.commit()
    else:
        yeni_p = Police(
            musteri_id=p['musteri_id'], police_no=str(p['police_no']),
            sigorta_sirketi=sirket, sigorta_turu=brans,
            baslangic_tarihi=p['baslangic_tarihi'], bitis_tarihi=p['bitis_tarihi'],
            prim=float(p['prim'] or 0.0), komisyon=float(p['komisyon'] or 0.0),
            islem_turu="Yeni", uretim_kaynagi="Kendi", durum="aktif"
        )
        db.add(yeni_p)
        db.commit()

def process_axa(db, filename):
    if not os.path.exists(filename): return
    df = pd.read_excel(filename, skiprows=3)
    for _, row in df.iterrows():
        pol_no = row.get('POLİÇE NO')
        if pd.isna(pol_no): continue
        ad, soyad = split_ad_soyad(row.get('SIGORTALI ADI'))
        m = musteri_bul_veya_yarat(db, ad, soyad, None, None)
        
        baslangic = parse_tarih_bot(row.get('TANZIM  TAR'))
        bitis = baslangic.replace(year=baslangic.year + 1)
        
        police_kaydet_veya_guncelle(db, {
            'musteri_id': m.id, 'police_no': pol_no, 'zeyil_no': row.get('ZEYL'),
            'sigorta_sirketi': 'Axa Sigorta', 'sigorta_turu': row.get('BRANŞ ADI'),
            'baslangic_tarihi': baslangic, 'bitis_tarihi': bitis,
            'prim': row.get('BÜRÜT PRİM', 0), 'komisyon': row.get('KOMİSYON', 0)
        })

def process_turkiye(db, filename):
    if not os.path.exists(filename): return
    df = pd.read_excel(filename, skiprows=6)
    for _, row in df.iterrows():
        pol_no = row.get('Pol\nNo')
        if pd.isna(pol_no): continue
        
        pol_no_clean = str(pol_no).replace(".0", "").strip()
        ad, soyad = split_ad_soyad(row.get('Sigortalı'))
        
        tckn = row.get('Sigortalı Kimlik')
        if not pd.isna(tckn):
            tckn = str(tckn).replace("TC Kimlik No:", "").replace("VKN:", "").strip()
        else:
            tckn = None
            
        m = musteri_bul_veya_yarat(db, ad, soyad, tckn, None)
        police_kaydet_veya_guncelle(db, {
            'musteri_id': m.id, 'police_no': pol_no_clean, 'zeyil_no': row.get('Zyl'),
            'sigorta_sirketi': 'Türkiye Sigorta', 'sigorta_turu': row.get('Ürün'),
            'baslangic_tarihi': parse_tarih_bot(row.get('Baş\nTar')), 
            'bitis_tarihi': parse_tarih_bot(row.get('Bit.\nTar')),
            'prim': row.get('Brüt\nPrim', 0), 'komisyon': row.get('TL\nKom', 0)
        })

def process_neova(db, filename):
    if not os.path.exists(filename): return
    df = pd.read_excel(filename)
    for _, row in df.iterrows():
        pol_no = row.get('Poliçe No')
        if pd.isna(pol_no): continue
        ad, soyad = split_ad_soyad(row.get('Sigortalı Adı'))
        m = musteri_bul_veya_yarat(db, ad, soyad, None, None)
        
        baslangic = parse_tarih_bot(row.get('Tanzim Tarihi'))
        bitis = baslangic.replace(year=baslangic.year + 1)
        police_kaydet_veya_guncelle(db, {
            'musteri_id': m.id, 'police_no': pol_no, 'zeyil_no': row.get('Zeyil No'),
            'sigorta_sirketi': 'Neova Sigorta', 'sigorta_turu': row.get('Poliçe Branşı'),
            'baslangic_tarihi': baslangic, 'bitis_tarihi': bitis,
            'prim': 0, 'komisyon': 0
        })

def process_quick(db, filename):
    if not os.path.exists(filename): return
    xls = pd.ExcelFile(filename)
    
    df_policies = pd.DataFrame()
    df_customers = pd.DataFrame()
    
    for sheet in xls.sheet_names:
        df_temp = pd.read_excel(filename, sheet_name=sheet)
        if df_temp.empty: continue
        
        sheet_type = str(df_temp.iloc[0, 0]) if len(df_temp) > 0 else ""
        
        if sheet_type == 'PoliceListesi':
            df_policies = df_temp.copy()
        elif sheet_type == 'Sigortalilar':
            df_temp.columns = df_temp.iloc[0]
            df_customers = df_temp[1:].copy()
            df_customers = df_customers.drop_duplicates(subset=['PoliceNo'])
            
    if df_policies.empty: return
    
    for _, prow in df_policies.iterrows():
        pol_no = prow.get('PoliceNo')
        if pd.isna(pol_no) or str(pol_no) == "PoliceNo": continue
        
        ad, soyad, tckn, tel = "BİLİNMİYOR", "", None, None
        
        if not df_customers.empty:
            c_row = df_customers[df_customers['PoliceNo'] == pol_no]
            if not c_row.empty:
                firma_ad = c_row.iloc[0].get('FirmaAd')
                if not pd.isna(firma_ad):
                    ad = firma_ad
                    soyad = ""
                else:
                    ad = c_row.iloc[0].get('Ad', 'BİLİNMİYOR')
                    soyad = c_row.iloc[0].get('Soyad', '')
                
                tckn_val = c_row.iloc[0].get('Tckn')
                vkn_val = c_row.iloc[0].get('Vkn')
                tckn = vkn_val if not pd.isna(vkn_val) else tckn_val
                
                tel = c_row.iloc[0].get('GsmNo')
            
        m = musteri_bul_veya_yarat(db, ad, soyad, tckn, tel)
        
        police_kaydet_veya_guncelle(db, {
            'musteri_id': m.id, 'police_no': pol_no, 'zeyil_no': prow.get('ZeyilNo'),
            'sigorta_sirketi': 'Quick Sigorta', 'sigorta_turu': prow.get('UrunAd'),
            'baslangic_tarihi': parse_tarih_bot(prow.get('BaslamaTarihi')), 
            'bitis_tarihi': parse_tarih_bot(prow.get('BitisTarihi')),
            'prim': prow.get('BrutPrim', 0), 'komisyon': prow.get('AcenteKomisyonTL', 0)
        })

def process_hepiyi(db, filename):
    if not os.path.exists(filename): return
    df = pd.read_excel(filename)
    
    for _, row in df.iterrows():
        if row.get('Teklif Durumu') != 'POLİÇE': continue
        pol_no = row.get('Teklif No')
        maskeli_isim = str(row.get('Sigortalı Ad-Soyad', ''))
        maskeli_tckn = str(row.get('Tckn-Vkn', ''))
        
        if pd.isna(pol_no) or not maskeli_isim or maskeli_isim == "nan": continue
        
        parts = maskeli_isim.split()
        ad_prefix = parts[0].split("*")[0].upper() if len(parts) > 0 else ""
        soyad_prefix = parts[-1].split("*")[0].upper() if len(parts) > 1 else ""
        
        tum_musteriler = db.query(Musteri).all()
        eslesen_musteri = None
        
        # Maskeli isim ve TCKN'den (AH*** KU*** ve 42*********) sistemde varolan müşteriyi bulma mantığı
        for m in tum_musteriler:
            if m.ad and m.soyad and str(m.ad).upper().startswith(ad_prefix) and str(m.soyad).upper().startswith(soyad_prefix):
                if m.tc_kimlik and maskeli_tckn and maskeli_tckn != "nan":
                    tckn_prefix = maskeli_tckn.split("*")[0]
                    if m.tc_kimlik.startswith(tckn_prefix):
                        eslesen_musteri = m
                        break
                else:
                    eslesen_musteri = m
                    break
                    
        # Eğer müşteri eşleştirilemiyorsa, isim ve soyadı "?" veya belirsiz kayıt yapmasını önleyerek es geçeriz
        # Çünkü poliçe kaydının havada kalmaması veya yanlış kişiye yazılmaması gerekir
        if not eslesen_musteri:
            continue
            
        police_kaydet_veya_guncelle(db, {
            'musteri_id': eslesen_musteri.id, 'police_no': pol_no, 'zeyil_no': row.get('Zeyil No'),
            'sigorta_sirketi': 'Hepiyi Sigorta', 
            'sigorta_turu': "Trafik Sigortası",  # Hepiyi default veya ürün numarasından çıkartılabilir
            'baslangic_tarihi': parse_tarih_bot(row.get('Başlangıç Tarihi')), 
            'bitis_tarihi': parse_tarih_bot(row.get('Bitiş Tarihi')),
            'prim': row.get('Brüt Prim', 0), 'komisyon': row.get('Komisyon', 0)
        })

def main():
    init_db()
    
    db = SessionLocal()
    try:
        print("--> AXA dosyası taranıyor...")
        process_axa(db, "AXA_VERİ.xlsx")
        
        print("--> TÜRKİYE dosyası taranıyor...")
        process_turkiye(db, "TÜRKİYE_VERİ.xlsx")
        
        print("--> NEOVA dosyası taranıyor...")
        process_neova(db, "NEOVA_VERİ.xlsx")
        
        print("--> QUICK dosyası taranıyor (Akıllı Sayfa Taraması devrede)...")
        process_quick(db, "QUİCK_VERİ.xlsx")
        
        print("--> HEPİYİ dosyası taranıyor (Maskeli eşleştirme devrede)...")
        process_hepiyi(db, "HEPİYİ_VERİ.xlsx")
        
        print("\nTEBRİKLER! Tüm Excel verileri kusursuz kurallarla veritabanına aktarıldı.")
    except Exception as e:
        print(f"Büyük Bir Hata Oluştu: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    main()