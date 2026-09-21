"""Telefon rehberini (.vcf) müşteri kartlarıyla eşleştirip telefon/e-posta bilgilerini ekler.

Kullanım:
    python rehber_aktar.py "C:\\Users\\ahmet\\Downloads\\Mobile Devices\\Kişiler.vcf" --dry-run
    python rehber_aktar.py "C:\\Users\\ahmet\\Downloads\\Mobile Devices\\Kişiler.vcf"

Güvenlik kuralları (yanlış kişiye numara yazmamak için):
  * Parantez içi notlar ve baştaki unvanlar (Av., Dr., TC) isimden ayıklanır; ardından Ad-Soyad kelimeleri BİREBİR
    aynıysa (sıra fark etmez) eşleşir.
  * Müşteri adının ardına açıklama eklenmişse ("Ali Kurukol Erzurumlu", "Aydın Bakırcı Cami") aynı kişi sayılır
    (raporda 'OLASI (uygulandı)').
  * Başka birinin numarası olma ihtimali olanlar ("... Esi", "... Damat", "... Muhasebecisi", müşteri adı parantez
    içinde ya da önünde başka isim var) telefon alanına YAZILMAZ; yalnızca 'Ek Notlar'a "İlişkili rehber kaydı" eklenir.
  * Sistemde aynı isimde birden fazla müşteri ya da rehberde aynı isimde farklı numaralı birden fazla kişi varsa atlanır.
  * Kartta zaten telefon/e-posta varsa üzerine yazılmaz; farklı ikinci numara 'Ek Notlar'a eklenir.
  * Tekrar çalıştırmak güvenlidir (aynı bilgi ikinci kez eklenmez).
"""
import argparse
import csv
import quopri
import re
import sys
from collections import defaultdict
from pathlib import Path

from database import SQLITE, Musteri, SessionLocal, ascii_buyuk, init_db, sqlite_yedekle, telefon_temizle

UNVANLAR = {"TC", "AV", "DR", "DOKTOR", "PROF", "USTA", "HOCA", "MUHTAR", "AVUKAT"}
ILISKI = {"ESI", "ESIM", "DAMAT", "GELIN", "ANNESI", "BABASI", "OGLU", "KIZI", "MUHASEBECISI", "MUHASEBE", "ARKADASI",
          "ARKADAS", "SEKRETERI", "SOFORU", "ORTAGI", "BACANAK", "KAYINCO", "ABLASI", "ABISI", "KARISI", "KOCASI"}


# --------------------------------------------------------------------------
# vCard 2.1 ayrıştırma (QUOTED-PRINTABLE, satır katlama, PHOTO)
# --------------------------------------------------------------------------
def _satirlari_ac(metin: str):
    """QP yumuşak satır sonlarını ('=' ile biten) ve boşlukla başlayan devam satırlarını (PHOTO) birleştirir."""
    cikti, tampon = [], None
    for ham in metin.splitlines():
        if tampon is not None:
            if ham[:1] in (" ", "\t"):          # katlanmış devam satırı
                tampon += ham[1:]
                continue
            if tampon.endswith("=") and "QUOTED-PRINTABLE" in tampon.split(":", 1)[0].upper():
                tampon = tampon[:-1] + ham       # QP yumuşak satır sonu
                continue
            cikti.append(tampon)
        tampon = ham
    if tampon is not None:
        cikti.append(tampon)
    return cikti


def _deger_coz(parametreler: str, deger: str) -> str:
    if "QUOTED-PRINTABLE" in parametreler.upper():
        try:
            return quopri.decodestring(deger.encode("latin-1", "ignore")).decode("utf-8", "replace")
        except Exception:
            return deger
    return deger


def kartlari_oku(yol: Path):
    """[{'fn': ..., 'n': ..., 'tel': [...], 'email': [...]}] döndürür."""
    kartlar, kart = [], None
    for satir in _satirlari_ac(yol.read_text(encoding="utf-8", errors="replace")):
        u = satir.strip().upper()
        if u == "BEGIN:VCARD":
            kart = {"fn": "", "n": "", "tel": [], "email": []}
        elif u == "END:VCARD":
            if kart:
                kartlar.append(kart)
            kart = None
        elif kart is not None and ":" in satir:
            ad_kismi, deger = satir.split(":", 1)
            ad, _, parametreler = ad_kismi.partition(";")
            ad = ad.upper()
            if ad == "FN":
                kart["fn"] = _deger_coz(parametreler, deger).strip()
            elif ad == "N":
                kart["n"] = " ".join(p for p in reversed(_deger_coz(parametreler, deger).split(";")) if p.strip())
            elif ad == "TEL":
                kart["tel"].append(_deger_coz(parametreler, deger).strip())
            elif ad == "EMAIL":
                kart["email"].append(_deger_coz(parametreler, deger).strip().lower())
    return kartlar


# --------------------------------------------------------------------------
# İsim ve telefon normalizasyonu
# --------------------------------------------------------------------------
def kelimeler(ad) -> tuple:
    return tuple(re.findall(r"[A-Z0-9]+", ascii_buyuk(ad)))


def kisi_kelimeleri(ad):
    """Rehber adını (parantez dışı kelimeler, parantez içi kelimeler) olarak ayırır; baştaki unvanlar atılır."""
    ic = " ".join(re.findall(r"\(([^)]*)\)?", ad))
    dis = list(kelimeler(re.sub(r"\([^)]*\)?", " ", ad)))
    while dis and dis[0] in UNVANLAR and len(dis) > 2:
        dis.pop(0)
    return tuple(dis), kelimeler(ic)


def telefonlari_sec(ham_liste):
    """Türkiye cep (05XXXXXXXXX) numaraları önce, sonra sabit hatlar; yabancı/geçersizler atılır. Tekrarsız."""
    temiz = []
    for h in ham_liste:
        t = telefon_temizle(h)
        if t and re.fullmatch(r"0\d{10}", t) and t not in temiz:
            temiz.append(t)
    return sorted(temiz, key=lambda t: not t.startswith("05"))  # sort kararlı: cepler öne


def rehberi_hazirla(kartlar):
    kisiler = []
    for k in kartlar:
        ad = k["fn"] or k["n"]
        dis, ic = kisi_kelimeleri(ad)
        if ad and (len(dis) >= 2 or len(ic) >= 2):   # tek kelimelik kayıtlar ("Annem", "Axa") kişi eşleştirmesi için yetersiz
            kisiler.append({"ad": ad, "dis": dis, "ic": ic, "tel": telefonlari_sec(k["tel"]),
                            "emailler": [e for e in k["email"] if e]})
    return kisiler


def grupla(kisiler):
    """Aynı kelime kümesine sahip rehber kayıtlarını birleştirir (parantez dışı adla)."""
    gruplar = defaultdict(lambda: {"ad": "", "telefonlar": [], "emailler": []})
    for k in kisiler:
        if len(k["dis"]) < 2:
            continue
        g = gruplar[tuple(sorted(k["dis"]))]
        g["ad"] = g["ad"] or k["ad"]
        if k["tel"] and k["tel"] not in g["telefonlar"]:
            g["telefonlar"].append(k["tel"])
        g["emailler"] += [e for e in k["emailler"] if e not in g["emailler"]]
    return gruplar


def not_ekle(m, satir):
    m.ek_notlar = ((m.ek_notlar or "") + ("\n" if m.ek_notlar else "") + satir)[:5000]


def uygula(m, g) -> list:
    """Rehber grubunun bilgilerini karta (yalnızca boş alanlara) işler; yapılan değişikliklerin listesini döndürür."""
    degisiklik = []
    telefonlar = g["telefonlar"][0] if g["telefonlar"] else []
    if telefonlar:
        if not (m.telefon or "").strip():
            m.telefon = telefonlar[0]
            degisiklik.append(f"telefon={telefonlar[0]}")
        ekler = [t for t in telefonlar if t != m.telefon and t not in (m.ek_notlar or "")]
        if ekler:
            not_ekle(m, "Rehberde ek telefon: " + ", ".join(ekler))
            degisiklik.append("ek_telefon=" + ", ".join(ekler))
    if g["emailler"] and not (m.email or "").strip():
        m.email = g["emailler"][0]
        degisiklik.append(f"email={g['emailler'][0]}")
    return degisiklik


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vcf", help=".vcf rehber dosyası")
    ap.add_argument("--dry-run", action="store_true", help="veritabanına yazmadan raporla")
    ap.add_argument("--yedeksiz", action="store_true", help="SQLite yedeği alma")
    ap.add_argument("--rapor", default="rehber_rapor.csv", help="rapor dosyası")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    kartlar = kartlari_oku(Path(args.vcf))
    kisiler = rehberi_hazirla(kartlar)
    gruplar = grupla(kisiler)
    print(f"Veritabanı: {'SQLite (yerel)' if SQLITE else 'PostgreSQL'} | rehber kartı: {len(kartlar)} | kişi grubu: {len(gruplar)}")

    init_db()
    if SQLITE and not args.dry_run and not args.yedeksiz:
        print(f"Yedek alındı: {sqlite_yedekle()}")

    db = SessionLocal()
    rapor, sayac = [], defaultdict(int)
    try:
        indeks = defaultdict(list)
        for m in db.query(Musteri).all():
            kel = kelimeler(f"{m.ad} {m.soyad}")
            if len(kel) >= 2:
                indeks[tuple(sorted(kel))].append(m)

        # 1) Birebir eşleşme
        for anahtar, g in gruplar.items():
            adaylar = indeks.get(anahtar, [])
            if not adaylar:
                continue
            if len(adaylar) > 1:
                sayac["belirsiz_musteri"] += 1
                rapor.append(("ATLANDI", g["ad"], "", f"sistemde aynı isimde {len(adaylar)} müşteri var (TCKN: {', '.join(str(a.tc_kimlik or '-') for a in adaylar)})"))
                continue
            m = adaylar[0]
            ad_soyad = f"{m.ad} {m.soyad}".strip()
            if len({t[0] for t in g["telefonlar"]}) > 1:
                sayac["belirsiz_rehber"] += 1
                rapor.append(("ATLANDI", ad_soyad, "", f"rehberde aynı isimde farklı numaralı {len(g['telefonlar'])} kişi: " + " | ".join(t[0] for t in g["telefonlar"])))
                continue
            degisiklik = uygula(m, g)
            if degisiklik:
                sayac["guncellenen"] += 1
                rapor.append(("GÜNCELLENDİ", ad_soyad, str(m.tc_kimlik or ""), "; ".join(degisiklik)))
            else:
                sayac["degisiklik_yok"] += 1

        # 2) Olası eşleşmeler: aynı kişi (ad + açıklama) → uygula; başka biri olma ihtimali → yalnızca not
        ayni_kisi = defaultdict(list)   # müşteri id → [(grup, müşteri)]
        ilgili = []                     # (müşteri, rehber adı, telefonlar)
        for k in kisiler:
            if not k["tel"] or (len(k["dis"]) >= 2 and tuple(sorted(k["dis"])) in indeks):
                continue
            kume = set(k["dis"])
            bulunan = {m.id: m for kk, ms in indeks.items() if len(kk) >= 2 and set(kk) < kume for m in ms}
            if len(bulunan) == 1:
                m = next(iter(bulunan.values()))
                oz = set(kelimeler(f"{m.ad} {m.soyad}"))
                ayni = set(k["dis"][:len(oz)]) == oz and not (set(k["dis"][len(oz):]) & ILISKI)
                if ayni:
                    g = gruplar[tuple(sorted(k["dis"]))]
                    if not any(x[0] is g for x in ayni_kisi[m.id]):
                        ayni_kisi[m.id].append((g, m))
                else:
                    ilgili.append((m, k["ad"], k["tel"]))
                continue
            if len(k["ic"]) >= 2:   # müşteri adı parantez içinde: "Ozan Göral (ADEM ÇULFA)"
                ic = set(k["ic"])
                bulunan = {m.id: m for kk, ms in indeks.items() if len(kk) >= 2 and set(kk) <= ic for m in ms}
                if len(bulunan) == 1:
                    ilgili.append((next(iter(bulunan.values())), k["ad"], k["tel"]))

        for liste in ayni_kisi.values():
            m = liste[0][1]
            ad_soyad = f"{m.ad} {m.soyad}".strip()
            if len(liste) > 1 or len({t[0] for t in liste[0][0]["telefonlar"]}) > 1:
                sayac["olasi_belirsiz"] += 1
                rapor.append(("OLASI ATLANDI", ad_soyad, str(m.tc_kimlik or ""), "birden fazla rehber kaydı uyuşuyor: " + " | ".join(g["ad"] for g, _ in liste)))
                continue
            degisiklik = uygula(m, liste[0][0])
            if degisiklik:
                sayac["olasi_uygulanan"] += 1
                rapor.append(("OLASI (uygulandı)", ad_soyad, str(m.tc_kimlik or ""), f"rehberde '{liste[0][0]['ad']}' → " + "; ".join(degisiklik)))

        for m, ad, tel in ilgili:
            satir = f"İlişkili rehber kaydı: {ad} → {tel[0]}"
            if satir not in (m.ek_notlar or ""):
                not_ekle(m, satir)
                sayac["iliskili_not"] += 1
                rapor.append(("İLİŞKİLİ (yalnızca not)", f"{m.ad} {m.soyad}".strip(), str(m.tc_kimlik or ""), satir))

        if args.dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    with open(args.rapor, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Durum", "Müşteri", "TCKN", "Ayrıntı"])
        w.writerows(sorted(rapor))
    print(f"Birebir güncellenen: {sayac['guncellenen']} | değişiklik gerekmeyen: {sayac['degisiklik_yok']} | "
          f"belirsiz (sistemde): {sayac['belirsiz_musteri']} | belirsiz (rehberde): {sayac['belirsiz_rehber']}")
    print(f"Olası eşleşme uygulanan: {sayac['olasi_uygulanan']} | olası ama belirsiz: {sayac['olasi_belirsiz']} | "
          f"ilişkili kişi olarak yalnızca not düşülen: {sayac['iliskili_not']}")
    print(f"Rapor: {args.rapor}")
    print("DRY-RUN: hiçbir şey yazılmadı." if args.dry_run else "Tamamlandı.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
