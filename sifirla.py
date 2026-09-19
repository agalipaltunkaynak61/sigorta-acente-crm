from app import SessionLocal, Musteri, Police

def veritabanini_sifirla():
    db = SessionLocal()
    try:
        # Önce poliçeleri silmek gerekir (Müşterilere bağlı oldukları için - Foreign Key)
        silinen_police = db.query(Police).delete()
        
        # Ardından müşterileri siliyoruz
        silinen_musteri = db.query(Musteri).delete()
        
        db.commit()
        print("✅ VERİTABANI TERTEMİZ YAPILDI!")
        print(f"Silinen Poliçe Sayısı: {silinen_police}")
        print(f"Silinen Müşteri Sayısı: {silinen_musteri}")
        
    except Exception as e:
        db.rollback()
        print(f"❌ Bir hata oluştu: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    print("DİKKAT: Veritabanındaki tüm müşteri ve poliçe kayıtları silinecektir!")
    onay = input("Onaylıyor musunuz? (E/H): ")
    
    if onay.lower() == 'e':
        veritabanini_sifirla()
    else:
        print("İşlem iptal edildi. Verileriniz duruyor.")