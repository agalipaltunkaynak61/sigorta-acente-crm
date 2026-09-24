"""Dini/milli özel günleri hesaplar ve müşteriye özel kutlama mesajı üretir.

Hicri takvime bağlı günler (kandiller, Ramazan/Kurban Bayramı, arife günleri) `hijridate` kütüphanesiyle
her yıl için otomatik hesaplanır — sabit bir tarih listesi değildir, 2026'dan sonraki yıllarda da çalışır.

ÖNEMLİ - HASSASİYET NOTU:
Hicri takvim güneş takvimiyle birebir örtüşmez; Diyanet'in resmî ilan ettiği tarih (ay gözlemine göre)
burada kullanılan aritmetik hesaplamadan çok nadiren 1 gün farklı çıkabilir. Kandil geceleri, ilgili hicri
günün bir önceki akşamı (gece) olarak hesaplanır — Türkiye'deki yaygın takvim uygulamalarının kuralı budur.
Bir tarihte sapma fark ederseniz `OZEL_GUN_DUZELTME` sözlüğüne o günün doğru tarihini elle ekleyebilirsiniz.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from hijridate import Gregorian, Hijri

# --------------------------------------------------------------------------
# Elle düzeltme: Diyanet'in resmî ilan ettiği tarih, hesaplanandan farklıysa
# buraya {(yıl, "gun_kodu"): date(...)} olarak eklenir; hesaplanan tarihin önüne geçer.
# Örnek: {(2027, "kurban_1"): date(2027, 5, 15)}
# --------------------------------------------------------------------------
OZEL_GUN_DUZELTME: dict = {}


@dataclass(frozen=True)
class OzelGun:
    tarih: date
    kod: str            # "berat", "ramazan_1", "23_nisan" gibi sabit anahtar (düzeltme/loglama için)
    baslik: str          # "Berat Kandili" gibi görünen ad
    tur: str             # mesaj şablonunu seçen kategori


# --------------------------------------------------------------------------
# Sabit (Miladi) milli / resmî günler — her yıl aynı ay-gün
# --------------------------------------------------------------------------
MILLI_GUNLER = (
    (1, 1, "milli_yeni_yil", "Yılbaşı"),
    (4, 23, "23_nisan", "23 Nisan Ulusal Egemenlik ve Çocuk Bayramı"),
    (5, 1, "1_mayis", "1 Mayıs Emek ve Dayanışma Günü"),
    (5, 19, "19_mayis", "19 Mayıs Atatürk'ü Anma, Gençlik ve Spor Bayramı"),
    (7, 15, "15_temmuz", "15 Temmuz Demokrasi ve Millî Birlik Günü"),
    (8, 30, "30_agustos", "30 Ağustos Zafer Bayramı"),
    (10, 29, "29_ekim", "29 Ekim Cumhuriyet Bayramı"),
)

# --------------------------------------------------------------------------
# Hicri takvime bağlı sabit günler: (hicri ay, hicri gün, kod, başlık, tür)
# tür: "kandil" (gece = hicri gün - 1) | "ramazan_baslangici"
# --------------------------------------------------------------------------
_HICRI_SABIT = (
    (8, 15, "berat", "Berat Kandili", "kandil"),
    (7, 27, "mirac", "Miraç Kandili", "kandil"),
    (3, 12, "mevlid", "Mevlid Kandili", "kandil"),
    (9, 27, "kadir", "Kadir Gecesi", "kandil"),
    (9, 1, "ramazan_baslangic", "Ramazan Ayı", "ramazan_baslangici"),
)

_KANDIL_MARGIN_YIL = 1  # Gregoryen yıl sınırını aşan hicri günleri de yakalamak için ileri/geri taranan hicri yıl payı


def _hicri_gun_miladi(hicri_yil: int, ay: int, gun: int) -> Optional[date]:
    try:
        return Hijri(hicri_yil, ay, gun).to_gregorian()
    except ValueError:
        return None  # bazı hicri yıllarda ay 29 günle biter, gün=30 geçersiz olabilir


def _duzelt(yil: int, kod: str, hesaplanan: Optional[date]) -> Optional[date]:
    return OZEL_GUN_DUZELTME.get((yil, kod), hesaplanan)


def _regaip_kandili(hicri_yil: int) -> Optional[date]:
    """Regaip Kandili: Receb ayının ilk Cuma gecesi (ilk Cuma'dan bir önceki akşam)."""
    receb_1 = _hicri_gun_miladi(hicri_yil, 7, 1)
    if not receb_1:
        return None
    ilk_cuma = receb_1 + timedelta(days=(4 - receb_1.weekday()) % 7)  # Python: Pazartesi=0 … Cuma=4
    return ilk_cuma - timedelta(days=1)


def _bayram_gunleri(hicri_yil: int) -> list:
    """[(kod, başlık, tarih)] — Ramazan Bayramı (3 gün) + Arife, Kurban Bayramı (4 gün) + Arife."""
    sonuc = []
    ramazan_1 = _hicri_gun_miladi(hicri_yil, 10, 1)
    if ramazan_1:
        sonuc.append(("ramazan_arife", "Ramazan Bayramı Arifesi", ramazan_1 - timedelta(days=1)))
        for i in range(3):
            etiket = "Ramazan Bayramınız" if i == 0 else f"Ramazan Bayramı {i + 1}. Gün"
            sonuc.append((f"ramazan_{i + 1}", etiket, ramazan_1 + timedelta(days=i)))
    kurban_1 = _hicri_gun_miladi(hicri_yil, 12, 10)
    if kurban_1:
        sonuc.append(("kurban_arife", "Kurban Bayramı Arifesi", kurban_1 - timedelta(days=1)))
        for i in range(4):
            etiket = "Kurban Bayramınız" if i == 0 else f"Kurban Bayramı {i + 1}. Gün"
            sonuc.append((f"kurban_{i + 1}", etiket, kurban_1 + timedelta(days=i)))
    return sonuc


def _yil_icin_hicri_gunler(hicri_yil: int) -> list:
    gunler = []
    for ay, gun, kod, baslik, tur in _HICRI_SABIT:
        tarih = _duzelt(hicri_yil, kod, _hicri_gun_miladi(hicri_yil, ay, gun))
        if tur == "kandil" and tarih:
            tarih -= timedelta(days=1)
        if tarih:
            gunler.append(OzelGun(tarih, kod, baslik, tur))
    regaip = _duzelt(hicri_yil, "regaip", _regaip_kandili(hicri_yil))
    if regaip:
        gunler.append(OzelGun(regaip, "regaip", "Regaip Kandili", "kandil"))
    for kod, baslik, tarih in _bayram_gunleri(hicri_yil):
        tarih = _duzelt(hicri_yil, kod, tarih)
        if tarih:
            tur = "arife" if "arife" in kod else "dini_bayram"
            gunler.append(OzelGun(tarih, kod, baslik, tur))
    return gunler


def ozel_gunler_araliginda(baslangic: date, bitis: date) -> list:
    """[baslangic, bitis] aralığındaki tüm milli + hicri özel günleri tarihe göre sıralı döndürür."""
    sonuc = [
        OzelGun(date(yil, ay, gun), kod, baslik, "milli_bayram")
        for yil in range(baslangic.year, bitis.year + 1)
        for ay, gun, kod, baslik in MILLI_GUNLER
        if baslangic <= date(yil, ay, gun) <= bitis
    ]
    hicri_bas = Gregorian(baslangic.year, 1, 1).to_hijri().year
    hicri_son = Gregorian(bitis.year, 12, 31).to_hijri().year
    for hicri_yil in range(hicri_bas - _KANDIL_MARGIN_YIL, hicri_son + _KANDIL_MARGIN_YIL + 1):
        for og in _yil_icin_hicri_gunler(hicri_yil):
            if baslangic <= og.tarih <= bitis:
                sonuc.append(og)
    tekil = {(og.kod, og.tarih): og for og in sonuc}  # hicri yıl taşmasından gelebilecek kopyaları eler
    return sorted(tekil.values(), key=lambda x: x.tarih)


def bugunku_ozel_gunler(bugun: Optional[date] = None) -> list:
    bugun = bugun or date.today()
    return ozel_gunler_araliginda(bugun, bugun)


def yaklasan_ozel_gunler(gun: int = 14, bugun: Optional[date] = None) -> list:
    bugun = bugun or date.today()
    return ozel_gunler_araliginda(bugun, bugun + timedelta(days=gun))


# --------------------------------------------------------------------------
# Mesaj üretimi
# --------------------------------------------------------------------------
IMZA = "ALTUN KARDEŞLER Sigorta"

_SABLONLAR = {
    "dogum_gunu": "Sayın {ad}, doğum gününüzü en içten dileklerimizle kutlarız. Nice sağlıklı, mutlu ve huzurlu yıllara! 🎂\n\n{imza}",
    "kandil": "Sayın {ad}, {baslik}'niz mübarek olsun. Bu mübarek gecenin siz ve sevdiklerinize hayırlar getirmesini dileriz. 🌙\n\n{imza}",
    "ramazan_baslangici": "Sayın {ad}, Ramazan ayınız mübarek olsun. Bu ayın bereketinin evinize ve işinize huzur getirmesini dileriz. 🌙\n\n{imza}",
    "arife": "Sayın {ad}, {baslik} hayırlı olsun. Yarın idrak edeceğimiz bayramın hepimize sağlık ve huzur getirmesini dileriz.\n\n{imza}",
    "dini_bayram": "Sayın {ad}, {baslik} kutlu olsun! Bayramınızı sevdiklerinizle birlikte sağlık ve mutluluk içinde geçirmenizi dileriz. 🌙🕌\n\n{imza}",
    "milli_bayram": "Sayın {ad}, {baslik} kutlu olsun! 🇹🇷\n\n{imza}",
}


def kutlama_mesaji(ad_soyad: str, ozel_gun: OzelGun) -> str:
    sablon = _SABLONLAR.get(ozel_gun.tur, _SABLONLAR["milli_bayram"])
    return sablon.format(ad=ad_soyad.strip(), baslik=ozel_gun.baslik, imza=IMZA)


def dogum_gunu_mesaji(ad_soyad: str) -> str:
    return _SABLONLAR["dogum_gunu"].format(ad=ad_soyad.strip(), imza=IMZA)


# --------------------------------------------------------------------------
# Müşteri bazlı: bugün hangi kutlamalar tetikleniyor?
# --------------------------------------------------------------------------
def musteri_dogum_gunu_bugun_mu(dogum_tarihi: Optional[date], bugun: Optional[date] = None) -> bool:
    if not dogum_tarihi:
        return False
    bugun = bugun or date.today()
    return (dogum_tarihi.month, dogum_tarihi.day) == (bugun.month, bugun.day)


def musterinin_bugunku_mesajlari(ad_soyad: str, dogum_tarihi: Optional[date], bugun: Optional[date] = None) -> list:
    """Bir müşteri için bugün gönderilecek mesajları üretir: [{"tur","baslik","mesaj"}]."""
    bugun = bugun or date.today()
    mesajlar = []
    if musteri_dogum_gunu_bugun_mu(dogum_tarihi, bugun):
        mesajlar.append({"tur": "dogum_gunu", "baslik": "Doğum Günü", "mesaj": dogum_gunu_mesaji(ad_soyad)})
    for og in bugunku_ozel_gunler(bugun):
        mesajlar.append({"tur": og.tur, "baslik": og.baslik, "mesaj": kutlama_mesaji(ad_soyad, og)})
    return mesajlar
