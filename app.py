import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, joinedload, sessionmaker
from sqlalchemy.orm.session import Session

from pdf_parser import ayikla_police_pdf

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# --- Veritabanı Kurulumu ---
DB_PATH = BASE_DIR / "acente_crm.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Musteri(Base):
    __tablename__ = "musteriler"
    id = Column(Integer, primary_key=True, index=True)
    ad = Column(String(100), nullable=False)
    soyad = Column(String(100), nullable=False)
    telefon = Column(String(50), nullable=False)
    email = Column(String(100), nullable=True)
    tc_kimlik = Column(String(50), nullable=True)
    adres = Column(Text, nullable=True)
    notlar = Column(Text, nullable=True)
    portfoy_sorumlusu = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Police(Base):
    __tablename__ = "policeler"
    id = Column(Integer, primary_key=True, index=True)
    musteri_id = Column(Integer, nullable=False)
    police_no = Column(String(80), nullable=False)
    sigorta_turu = Column(String(120), nullable=False)
    sigorta_sirketi = Column(String(100), nullable=True)
    islem_turu = Column(String(50), default="Yeni Poliçe")
    baslangic_tarihi = Column(Date, nullable=False)
    bitis_tarihi = Column(Date, nullable=False)
    prim = Column(Float, nullable=True)
    durum = Column(String(30), default="aktif")
    aciklama = Column(Text, nullable=True)
    pdf_dosya_adi = Column(String(255), nullable=True)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app = FastAPI(title="Altun Kardeşler CRM", version="3.0.6")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()

def parse_tarih(val) -> date:
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        val = val.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
    return date.today()

def hesapla_komisyon(brans: str, sirket: str, prim: float) -> float:
    if not prim: return 0.0
    b = (brans or "").lower()
    s = (sirket or "").lower()
    
    oran = 0.10
    if "tss" in b or "tamamlay" in b: oran = 0.25
    elif "iş" in b or "is yeri" in b or "isyeri" in b: oran = 0.20
    elif "dask" in b: oran = 0.12
    elif "trafik" in b: oran = 0.10
    elif "kasko" in b: oran = 0.15
    elif "yeşil" in b or "yesil" in b: oran = 0.10
    elif "konut" in b: oran = 0.25
    elif "seyahat" in b or "seyehat" in b: oran = 0.15
    elif "ferdi kaza" in b: oran = 0.30
    elif "allrisk" in b or "inşaat" in b or "insaat" in b: oran = 0.20
    
    anlasmali = ["türkiye", "turkiye", "axa", "ak", "hepiyi", "neova", "doğa", "doga", "quick"]
    disaridan = True
    for a in anlasmali:
        if a in s:
            disaridan = False
            break
            
    komisyon = prim * oran
    if disaridan:
        komisyon = komisyon / 2.0
    return komisyon

def uyar_seviyesi(kalan_gun: int) -> str:
    if kalan_gun < 0: return "suresi_dolmus"
    if kalan_gun <= 3: return "kirmizi"
    if kalan_gun <= 15: return "sari"
    if kalan_gun <= 30: return "yesil"
    return "uzak"

def police_to_out(p: Police, db_session: Session) -> dict:
    bugun = date.today()
    kalan = (p.bitis_tarihi - bugun).days if p.bitis_tarihi else 0
    suresi_dolmus = (p.bitis_tarihi and p.bitis_tarihi < bugun) or (p.durum == "iptal")
    musteri = db_session.query(Musteri).filter(Musteri.id == p.musteri_id).first()
    komisyon = hesapla_komisyon(p.sigorta_turu, p.sigorta_sirketi, p.prim)
    
    return {
        "id": p.id,
        "musteri_id": p.musteri_id,
        "police_no": p.police_no,
        "sigorta_turu": p.sigorta_turu,
        "sigorta_sirketi": p.sigorta_sirketi,
        "islem_turu": p.islem_turu or "Yeni Poliçe",
        "baslangic_tarihi": p.baslangic_tarihi,
        "bitis_tarihi": p.bitis_tarihi,
        "prim": p.prim,
        "durum": p.durum,
        "aciklama": p.aciklama,
        "pdf_dosya_adi": p.pdf_dosya_adi,
        "kalan_gun": 0 if suresi_dolmus else kalan,
        "suresi_dolmus": suresi_dolmus,
        "uyar_seviyesi": "suresi_dolmus" if suresi_dolmus else uyar_seviyesi(kalan),
        "musteri_ad": musteri.ad if musteri else None,
        "musteri_soyad": musteri.soyad if musteri else None,
        "musteri_telefon": musteri.telefon if musteri else None,
        "musteri_portfoy_sorumlusu": musteri.portfoy_sorumlusu if musteri else None,
        "komisyon": komisyon
    }

@app.get("/Logo.png")
def get_logo():
    logo_yolu = BASE_DIR / "Logo.png"
    if logo_yolu.exists():
        return FileResponse(logo_yolu, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo bulunamadı")

@app.get("/", response_class=HTMLResponse)
def ana_sayfa():
    return """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Altun Kardeşler CRM</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            brand: { 
              black: "#0a0a0a", 
              dark: "#141414", 
              panel: "#1e1e1e",
              gold: "#D4AF37", 
              goldhover: "#AA8C2C",
              light: "#f3f4f6"
            },
          },
        },
      },
    };
  </script>
  <style>
    ::-webkit-scrollbar { width: 8px; }
    ::-webkit-scrollbar-track { background: #1e1e1e; }
    ::-webkit-scrollbar-thumb { background: #D4AF37; border-radius: 4px; }
  </style>
</head>
<body class="bg-brand-light text-slate-800 min-h-screen">
  <div class="flex min-h-screen">
    <aside class="w-64 bg-brand-black text-white flex flex-col shrink-0 border-r border-brand-gold/20 shadow-2xl">
      <div class="px-6 py-8 border-b border-white/10 text-center">
        <img src="/Logo.png" alt="Altun Kardeşler Logo" class="w-24 mx-auto mb-3 object-contain" onerror="this.style.display='none'">
        <h1 class="text-xl font-bold text-brand-gold tracking-wider">ALTUN</h1>
        <p class="text-xs uppercase tracking-widest text-slate-400 mt-1">KARDEŞLER CRM</p>
      </div>
      <nav class="p-3 space-y-1 flex-1 mt-4">
        <button data-view="dashboard" class="nav-btn w-full text-left px-4 py-3 rounded-lg bg-brand-gold/10 text-brand-gold font-bold transition-all">Ana Ekran</button>
        <button data-view="musteriler" class="nav-btn w-full text-left px-4 py-3 rounded-lg hover:bg-white/5 transition-all text-white">Müşteriler</button>
        <button data-view="policeler" class="nav-btn w-full text-left px-4 py-3 rounded-lg hover:bg-white/5 transition-all text-white">Poliçeler</button>
        <button data-view="finansal" class="nav-btn w-full text-left px-4 py-3 rounded-lg hover:bg-white/5 transition-all text-white border border-transparent hover:border-brand-gold/30">Finansal Analiz</button>
      </nav>
      <p class="px-6 py-4 text-xs text-brand-gold/50 text-center">Altun Kardeşler Sigorta v3</p>
    </aside>

    <main class="flex-1 p-6 md:p-8 bg-slate-50 overflow-y-auto max-h-screen">
      <!-- DASHBOARD -->
      <section id="view-dashboard">
        <div class="mb-6">
          <h2 class="text-2xl font-bold text-brand-black">Hoş Geldiniz</h2>
          <p class="text-sm text-slate-500 mt-1">Bitişi yaklaşan poliçeler (0–3 gün kırmızı · 3–15 gün sarı · 15–30 gün yeşil)</p>
        </div>
        <div id="ozet-kartlar" class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6"></div>
        <div id="yaklasan-liste" class="space-y-3"></div>
      </section>

      <!-- MÜŞTERİLER -->
      <section id="view-musteriler" class="hidden">
        <div class="flex flex-wrap items-center justify-between gap-3 mb-6">
          <h2 class="text-2xl font-bold text-brand-black">Müşteriler</h2>
          <div class="flex gap-2">
            <input id="musteri-ara" type="search" placeholder="Ad, telefon, TCKN…" class="border border-slate-300 rounded-lg px-3 py-2 text-sm w-72 focus:outline-none focus:border-brand-gold" />
            <button onclick="musteriFormu()" class="bg-brand-black text-brand-gold px-4 py-2 rounded-lg text-sm font-semibold hover:bg-brand-dark shadow-md">Yeni Müşteri</button>
          </div>
        </div>
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
          <table class="w-full text-sm">
            <thead class="bg-brand-black text-left text-brand-gold">
              <tr>
                <th class="px-4 py-4">Ad Soyad</th>
                <th class="px-4 py-4">Telefon</th>
                <th class="px-4 py-4">Danışman</th>
                <th class="px-4 py-4">TC / Vergi No</th>
                <th class="px-4 py-4 text-right">İşlemler</th>
              </tr>
            </thead>
            <tbody id="musteri-tbody" class="divide-y divide-slate-100"></tbody>
          </table>
        </div>
      </section>

      <!-- POLİÇELER -->
      <section id="view-policeler" class="hidden">
        <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
          <h2 class="text-2xl font-bold text-brand-black">Poliçeler</h2>
        </div>
        <label id="pdf-drop" class="mb-6 flex flex-col items-center justify-center gap-2 border-2 border-dashed border-brand-gold/50 bg-white rounded-xl p-8 text-center cursor-pointer hover:border-brand-gold">
          <input id="pdf-input" type="file" accept="application/pdf" class="hidden" />
          <p class="font-bold text-brand-black text-lg">PDF poliçe sürükleyin veya tıklayın</p>
          <p class="text-sm text-slate-500">Yapay zeka okuduktan sonra telefon ve danışman sorulacaktır.</p>
          <p id="pdf-durum" class="text-sm text-brand-gold hidden font-semibold mt-2"></p>
        </label>
        <div class="flex border-b border-slate-200 mb-4 gap-6 text-sm font-semibold">
          <button onclick="policeSekmeSec('aktif')" id="btn-sekme-aktif" class="pb-3 border-b-2 border-brand-gold text-brand-black">Aktif Poliçeler</button>
          <button onclick="policeSekmeSec('eski')" id="btn-sekme-eski" class="pb-3 border-b-2 border-transparent text-slate-400">Süresi Dolanlar</button>
        </div>
        <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
          <table class="w-full text-sm">
            <thead class="bg-brand-black text-left text-brand-gold">
              <tr>
                <th class="px-4 py-4">Poliçe No</th>
                <th class="px-4 py-4">Müşteri</th>
                <th class="px-4 py-4">Şirket / Branş</th>
                <th class="px-4 py-4">Bitiş</th>
                <th class="px-4 py-4">Prim</th>
                <th class="px-4 py-4 text-right">İşlemler</th>
              </tr>
            </thead>
            <tbody id="police-tbody" class="divide-y divide-slate-100"></tbody>
          </table>
        </div>
      </section>

      <!-- FİNANSAL ANALİZ -->
      <section id="view-finansal" class="hidden">
        <div class="mb-6">
          <h2 class="text-2xl font-bold text-brand-black">Finansal Analiz & Raporlama</h2>
          <p class="text-sm text-slate-500 mt-1">Şirket, çalışan ve branş bazlı detaylı kar ve ciro analizi.</p>
        </div>

        <!-- Filtre Paneli -->
        <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200 mb-6 space-y-4">
          <div class="grid grid-cols-1 md:grid-cols-5 gap-4 items-end">
            <div>
              <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Başlangıç</label>
              <input type="date" id="fin-start" class="w-full border rounded-lg px-3 py-2 text-sm focus:border-brand-gold" />
            </div>
            <div>
              <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Bitiş</label>
              <input type="date" id="fin-end" class="w-full border rounded-lg px-3 py-2 text-sm focus:border-brand-gold" />
            </div>
            <div>
              <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Şirket</label>
              <select id="fin-sirket" class="w-full border rounded-lg px-3 py-2 text-sm focus:border-brand-gold">
                <option value="">Tümü</option>
                <option value="Türkiye">Türkiye</option>
                <option value="Axa">Axa</option>
                <option value="Ak">Ak</option>
                <option value="Hepiyi">Hepiyi</option>
                <option value="Neova">Neova</option>
                <option value="Doğa">Doğa</option>
                <option value="Quick">Quick</option>
                <option value="Diğer">Diğer (Dışarıdan)</option>
              </select>
            </div>
            <div>
              <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Çalışan / Danışman</label>
              <select id="fin-sorumlu" class="w-full border rounded-lg px-3 py-2 text-sm focus:border-brand-gold">
                <option value="">Tümü</option>
                <option value="Muammer Altunkaynak">Muammer Altunkaynak</option>
                <option value="İhsan Berat Altunkaynak">İhsan Berat Altunkaynak</option>
                <option value="Serhat Altunkaynak">Serhat Altunkaynak</option>
                <option value="Ahmet Galip Altunkaynak">Ahmet Galip Altunkaynak</option>
                <option value="Sezai Karakoç">Sezai Karakoç</option>
                <option value="Diğer">Diğer</option>
              </select>
            </div>
            <div>
              <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Sigorta Branşı</label>
              <select id="fin-brans" class="w-full border rounded-lg px-3 py-2 text-sm focus:border-brand-gold">
                <option value="">Tümü</option>
                <option value="TSS">TSS</option>
                <option value="İş Yeri">İş Yeri</option>
                <option value="DASK">DASK</option>
                <option value="Trafik">Trafik</option>
                <option value="Kasko">Kasko</option>
                <option value="Yeşilkart">Yeşilkart</option>
                <option value="Konut">Konut</option>
                <option value="Seyahat Sağlık">Seyahat Sağlık</option>
                <option value="Ferdi Kaza">Ferdi Kaza</option>
                <option value="Allrisk İnşaat">Allrisk İnşaat</option>
              </select>
            </div>
          </div>

          <div class="pt-3 border-t flex items-center gap-2">
            <input type="checkbox" id="fin-compare-mode" onchange="toggleCompareMode()" class="w-4 h-4 text-brand-gold rounded border-slate-300">
            <label for="fin-compare-mode" class="text-sm font-semibold text-slate-700">Başka bir tarih aralığıyla karşılaştır (Geçen yılla kıyaslama vb.)</label>
          </div>

          <div id="fin-compare-dates" class="hidden grid-cols-1 md:grid-cols-5 gap-4 pt-2">
            <div>
              <label class="block text-xs font-bold text-sky-600 uppercase mb-1">Karşılaştırma Başlangıç</label>
              <input type="date" id="fin-start2" class="w-full border border-sky-300 bg-sky-50 rounded-lg px-3 py-2 text-sm" />
            </div>
            <div>
              <label class="block text-xs font-bold text-sky-600 uppercase mb-1">Karşılaştırma Bitiş</label>
              <input type="date" id="fin-end2" class="w-full border border-sky-300 bg-sky-50 rounded-lg px-3 py-2 text-sm" />
            </div>
          </div>

          <div class="flex justify-end pt-2">
            <button onclick="calistirFinansalRapor()" class="bg-brand-black text-brand-gold px-6 py-2.5 rounded-lg text-sm font-bold shadow-md hover:bg-brand-dark">Raporu Oluştur</button>
          </div>
        </div>

        <!-- Ana Metrikler -->
        <div id="fin-metrics" class="grid grid-cols-1 md:grid-cols-3 gap-5 mb-6 hidden">
          <div class="bg-brand-black text-white rounded-xl p-6 shadow-lg border border-brand-gold/30">
            <p class="text-xs text-brand-gold font-bold uppercase tracking-wider mb-1">Toplam Üretim (Ciro)</p>
            <p id="fin-ciro" class="text-3xl font-bold">₺0,00</p>
            <p id="fin-ciro-diff" class="text-xs mt-2 hidden"></p>
          </div>
          <div class="bg-gradient-to-br from-brand-gold to-brand-goldhover text-brand-black rounded-xl p-6 shadow-lg">
            <p class="text-xs font-black uppercase tracking-wider opacity-80 mb-1">Tahmini Net Komisyon Karı</p>
            <p id="fin-kar" class="text-3xl font-black">₺0,00</p>
            <p id="fin-kar-diff" class="text-xs mt-2 font-bold hidden"></p>
          </div>
          <div class="bg-white rounded-xl p-6 shadow-lg border border-slate-200">
            <p class="text-xs text-slate-500 font-bold uppercase tracking-wider mb-1">Toplam Poliçe Adedi</p>
            <p id="fin-adet" class="text-3xl font-bold text-brand-black">0 Adet</p>
            <p id="fin-adet-diff" class="text-xs mt-2 hidden"></p>
          </div>
        </div>

        <!-- Detay Tabloları -->
        <div id="fin-details" class="grid grid-cols-1 md:grid-cols-2 gap-6 hidden">
          <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
            <div class="bg-slate-50 px-5 py-3 border-b font-bold text-sm text-brand-black">Branşlara Göre Dağılım</div>
            <div class="p-4 max-h-80 overflow-y-auto" id="fin-brans-liste"></div>
          </div>
          <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
            <div class="bg-slate-50 px-5 py-3 border-b font-bold text-sm text-brand-black">Şirketlere Göre Dağılım</div>
            <div class="p-4 max-h-80 overflow-y-auto" id="fin-sirket-liste"></div>
          </div>
        </div>
      </section>
    </main>
  </div>

  <!-- MODAL -->
  <div id="modal" class="hidden fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto border-t-4 border-brand-gold">
      <h3 id="modal-baslik" class="text-xl font-bold mb-4 text-brand-black"></h3>
      <form id="modal-form" class="space-y-4"></form>
      <p id="modal-hata" class="hidden text-sm font-semibold text-red-600 bg-red-50 p-3 rounded-lg mt-4"></p>
      <div class="flex justify-end gap-3 mt-6 pt-4 border-t">
        <button type="button" onclick="document.getElementById('modal').classList.add('hidden')" class="px-4 py-2 border rounded-lg text-sm font-medium">Kapat</button>
        <button type="button" id="modal-kaydet" class="px-4 py-2 bg-brand-black text-brand-gold font-bold rounded-lg text-sm shadow-md">Kaydet</button>
      </div>
    </div>
  </div>

  <script>
    const DANISMANLAR = ["Muammer Altunkaynak", "İhsan Berat Altunkaynak", "Serhat Altunkaynak", "Ahmet Galip Altunkaynak", "Sezai Karakoç", "Diğer"];
    let musteriCache = [], policeCache = [], aktifSekme = 'aktif';

    document.querySelectorAll(".nav-btn").forEach(btn => {
      btn.addEventListener("click", () => goster(btn.dataset.view));
    });

    function goster(ad) {
      ["dashboard", "musteriler", "policeler", "finansal"].forEach(v => {
        document.getElementById("view-" + v).classList.toggle("hidden", v !== ad);
      });
      document.querySelectorAll(".nav-btn").forEach(b => {
        if(b.dataset.view === ad) {
          b.classList.add("bg-brand-gold/10", "text-brand-gold", "font-bold");
          b.classList.remove("text-white", "hover:bg-white/5");
        } else {
          b.classList.remove("bg-brand-gold/10", "text-brand-gold", "font-bold");
          b.classList.add("text-white", "hover:bg-white/5");
        }
      });
      if(ad === "dashboard") yukleDashboard();
      if(ad === "musteriler") yukleMusteriler();
      if(ad === "policeler") yuklePoliceler();
      if(ad === "finansal") yukleFinansalHazirlik();
    }

    async function api(path, opts) {
      const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
      if(!res.ok) {
        let msg = "İstek başarısız";
        try { const j = await res.json(); msg = j.detail || msg; } catch(_){}
        throw new Error(msg);
      }
      return res.status === 204 ? null : res.json();
    }

    async function yukleDashboard() {
      const ozet = await api("/api/ozet");
      document.getElementById("ozet-kartlar").innerHTML = [
        ["Müşteri", ozet.musteri_sayisi], ["Poliçe", ozet.police_sayisi], ["Aktif", ozet.aktif_police], ["30 Gün", ozet.yaklasan_30_gun]
      ].map(([t,v])=>`<div class="bg-white border rounded-xl p-4 border-l-4 border-brand-gold shadow-sm"><p class="text-xs text-slate-400 font-bold">${t}</p><p class="text-2xl font-black">${v}</p></div>`).join("");
      
      const liste = await api("/api/policeler/yaklasan");
      document.getElementById("yaklasan-liste").innerHTML = liste.length === 0 ? '<p class="text-slate-500">Yaklaşan poliçe yok.</p>' :
        liste.map(p => `<div class="bg-white border p-4 rounded-xl flex justify-between items-center shadow-sm"><div><b>${p.police_no}</b> - ${p.sigorta_turu} (${p.sigorta_sirketi||''})</div><span class="bg-red-500 text-white text-xs px-2 py-1 rounded font-bold">${p.kalan_gun} gün</span></div>`).join("");
    }

    async function yukleMusteriler(q="") {
      musteriCache = await api("/api/musteriler" + (q ? "?q="+q : ""));
      document.getElementById("musteri-tbody").innerHTML = musteriCache.map(m => `
        <tr class="hover:bg-brand-gold/5">
          <td class="px-4 py-3 font-bold">${m.ad} ${m.soyad}</td>
          <td class="px-4 py-3">${m.telefon || '—'}</td>
          <td class="px-4 py-3"><span class="bg-slate-100 px-2 py-1 rounded text-xs font-bold">${m.portfoy_sorumlusu || 'Atanmadı'}</span></td>
          <td class="px-4 py-3">${m.tc_kimlik || '—'}</td>
          <td class="px-4 py-3 text-right"><button onclick="musteriSil(${m.id})" class="text-red-600 text-xs font-bold">Sil</button></td>
        </tr>`).join("");
    }

    async function yuklePoliceler() {
      policeCache = await api("/api/policeler");
      renderPoliceListesi();
    }

    function renderPoliceListesi() {
      const filtrelenmis = policeCache.filter(p => aktifSekme === 'aktif' ? !p.suresi_dolmus : p.suresi_dolmus);
      document.getElementById("police-tbody").innerHTML = filtrelenmis.length === 0 ? '<tr><td colspan="6" class="p-4 text-center text-slate-400">Kayıt yok</td></tr>' :
        filtrelenmis.map(p => `
          <tr class="hover:bg-brand-gold/5">
            <td class="px-4 py-3 font-bold">${p.police_no}</td>
            <td class="px-4 py-3">${p.musteri_ad||''} ${p.musteri_soyad||''}</td>
            <td class="px-4 py-3">${p.sigorta_sirketi||''} <span class="text-xs bg-brand-black text-brand-gold px-1 rounded">${p.sigorta_turu}</span></td>
            <td class="px-4 py-3">${p.bitis_tarihi}</td>
            <td class="px-4 py-3 font-bold">₺${p.prim||0}</td>
            <td class="px-4 py-3 text-right"><button onclick="policeSil(${p.id})" class="text-red-600 text-xs font-bold">Sil</button></td>
          </tr>`).join("");
    }

    function policeSekmeSec(sekme) {
      aktifSekme = sekme;
      renderPoliceListesi();
    }

    function danismanSelect(secili) {
      return `<label class="block text-sm font-bold mb-1">Portföy Sorumlusu / Danışman <span class="text-brand-gold">*</span></label>
              <select name="portfoy_sorumlusu" required class="w-full border rounded-lg p-2.5 text-sm bg-slate-50 focus:border-brand-gold">
                ${DANISMANLAR.map(d=>`<option ${d===secili?'selected':''}>${d}</option>`).join("")}
              </select>`;
    }

    function musteriFormu(id) {
      const m = id ? musteriCache.find(x=>x.id===id) : {};
      document.getElementById("modal-baslik").textContent = id ? "Müşteri Düzenle" : "Yeni Müşteri";
      document.getElementById("modal-form").innerHTML = `
        <label class="block text-sm font-bold mb-1">Ad *</label><input name="ad" value="${m.ad||''}" required class="w-full border rounded-lg p-2.5 text-sm" />
        <label class="block text-sm font-bold mb-1">Soyad *</label><input name="soyad" value="${m.soyad||''}" required class="w-full border rounded-lg p-2.5 text-sm" />
        <label class="block text-sm font-bold mb-1">Telefon Numarası *</label><input name="telefon" type="tel" value="${m.telefon||''}" required class="w-full border rounded-lg p-2.5 text-sm" placeholder="05XXXXXXXXX" />
        ${danismanSelect(m.portfoy_sorumlusu)}
      `;
      document.getElementById("modal-kaydet").onclick = async () => {
        const form = document.getElementById("modal-form");
        if(!form.checkValidity()) { form.reportValidity(); return; }
        const fd = new FormData(form);
        const body = Object.fromEntries(fd.entries());
        await api(id ? "/api/musteriler/"+id : "/api/musteriler", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
        document.getElementById("modal").classList.add("hidden");
        yukleMusteriler();
      };
      document.getElementById("modal").classList.remove("hidden");
    }

    async function musteriSil(id) { if(confirm("Silinsin mi?")) { await api("/api/musteriler/"+id, {method:"DELETE"}); yukleMusteriler(); } }
    async function policeSil(id) { if(confirm("Silinsin mi?")) { await api("/api/policeler/"+id, {method:"DELETE"}); yuklePoliceler(); } }

    async function pdfGonder(file) {
      const durum = document.getElementById("pdf-durum");
      durum.classList.remove("hidden");
      durum.textContent = "Yapay Zeka Poliçeyi Okuyor...";
      try {
        const fd = new FormData(); fd.append("file", file);
        const res = await fetch("/api/upload-parse", { method: "POST", body: fd });
        const text = await res.text();
        let veri;
        try { veri = JSON.parse(text); } catch(err) { throw new Error("Sunucu yanıtı: " + text.substring(0, 80)); }
        if(!res.ok) throw new Error(veri.detail || "Okuma hatası");
        
        durum.textContent = "✓ Poliçe okundu. Lütfen müşteri bilgilerini onaylayın.";
        
        document.getElementById("modal-baslik").textContent = "Poliçe Müşteri Bilgileri Onayı";
        document.getElementById("modal-form").innerHTML = `
          <div class="bg-amber-50 border border-amber-200 text-amber-900 p-3 rounded-lg text-xs font-semibold mb-3">
            Yapay Zeka Okudu: <b>${veri.ad} ${veri.soyad}</b> (TC: ${veri.tckn || 'Yok'})<br>Poliçe No: ${veri.police_no}
          </div>
          <input type="hidden" name="pdf_dosya_adi" value="${veri.pdf_dosya_adi}">
          <input type="hidden" name="police_no" value="${veri.police_no}">
          <input type="hidden" name="sigorta_turu" value="${veri.sigorta_turu}">
          <input type="hidden" name="sigorta_sirketi" value="${veri.sigorta_sirketi}">
          <input type="hidden" name="islem_turu" value="${veri.islem_turu}">
          <input type="hidden" name="baslangic_tarihi" value="${veri.baslangic_tarihi}">
          <input type="hidden" name="bitis_tarihi" value="${veri.bitis_tarihi}">
          <input type="hidden" name="prim" value="${veri.brut_prim || veri.net_prim || 0}">
          
          <label class="block text-sm font-bold mb-1">Müşteri Adı *</label><input name="ad" value="${veri.ad || ''}" required class="w-full border rounded-lg p-2.5 text-sm" />
          <label class="block text-sm font-bold mb-1">Müşteri Soyadı *</label><input name="soyad" value="${veri.soyad || ''}" required class="w-full border rounded-lg p-2.5 text-sm" />
          <label class="block text-sm font-bold mb-1">TC / Vergi No</label><input name="tc_kimlik" value="${veri.tckn || ''}" class="w-full border rounded-lg p-2.5 text-sm" />
          <label class="block text-sm font-bold mb-1">Telefon Numarası (Zorunlu) *</label><input name="telefon" type="tel" required class="w-full border rounded-lg p-2.5 text-sm" placeholder="05XXXXXXXXX" />
          ${danismanSelect("")}
        `;
        
        document.getElementById("modal-kaydet").onclick = async () => {
          const form = document.getElementById("modal-form");
          if(!form.checkValidity()) { form.reportValidity(); return; }
          const formVeri = Object.fromEntries(new FormData(form).entries());
          
          const kayitRes = await fetch("/api/upload-save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(formVeri)
          });
          const kayitVeri = await kayitRes.json();
          if(!kayitRes.ok) { alert(kayitVeri.detail || "Kayıt hatası"); return; }
          
          document.getElementById("modal").classList.add("hidden");
          durum.textContent = "✓ Poliçe başarıyla kaydedildi!";
          goster("policeler");
          yuklePoliceler();
        };
        
        document.getElementById("modal").classList.remove("hidden");

      } catch(e) {
        durum.textContent = "Hata: " + e.message;
        alert("Hata: " + e.message);
      }
    }
    
    const drop = document.getElementById("pdf-drop");
    const inp = document.getElementById("pdf-input");
    drop.addEventListener("click", () => inp.click());
    inp.addEventListener("change", (e) => { if(e.target.files[0]) pdfGonder(e.target.files[0]); inp.value=""; });

    // --- Finansal Analiz Fonksiyonları ---
    function toggleCompareMode() {
      const isChecked = document.getElementById("fin-compare-mode").checked;
      const compDiv = document.getElementById("fin-compare-dates");
      if (isChecked) compDiv.classList.remove("hidden");
      else compDiv.classList.add("hidden");
    }

    async function yukleFinansalHazirlik() {
      if(!policeCache.length) policeCache = await api("/api/policeler");
    }

    function hesaplaFiltreliVeri(startDate, endDate, sirket, sorumlu, brans) {
      let filtered = policeCache;
      
      if(startDate) { const d = new Date(startDate); filtered = filtered.filter(p => new Date(p.baslangic_tarihi) >= d); }
      if(endDate) { const d = new Date(endDate); filtered = filtered.filter(p => new Date(p.baslangic_tarihi) <= d); }
      
      if(sirket) {
        if(sirket === "Diğer") {
          const anlasmali = ["türkiye", "turkiye", "axa", "ak", "hepiyi", "neova", "doğa", "doga", "quick"];
          filtered = filtered.filter(p => {
            const s = (p.sigorta_sirketi || "").toLowerCase();
            return !anlasmali.some(a => s.includes(a));
          });
        } else {
          filtered = filtered.filter(p => p.sigorta_sirketi && p.sigorta_sirketi.toLowerCase().includes(sirket.toLowerCase()));
        }
      }

      if(sorumlu) {
        if(sorumlu === "Diğer") {
          filtered = filtered.filter(p => !DANISMANLAR.includes(p.musteri_portfoy_sorumlusu));
        } else {
          filtered = filtered.filter(p => p.musteri_portfoy_sorumlusu === sorumlu);
        }
      }

      if(brans) filtered = filtered.filter(p => p.sigorta_turu && p.sigorta_turu.toLowerCase().includes(brans.toLowerCase()));

      let totalCiro = 0;
      let totalKar = 0;
      let totalAdet = filtered.length;
      let bransDagilimi = {};
      let sirketDagilimi = {};

      filtered.forEach(p => {
        if(p.prim) totalCiro += p.prim;
        if(p.komisyon) totalKar += p.komisyon;
        
        let b = (p.sigorta_turu || "Bilinmiyor").toUpperCase();
        if(!bransDagilimi[b]) bransDagilimi[b] = {adet: 0, ciro: 0, kar: 0};
        bransDagilimi[b].adet += 1;
        bransDagilimi[b].ciro += p.prim || 0;
        bransDagilimi[b].kar += p.komisyon || 0;

        let s = (p.sigorta_sirketi || "Bilinmiyor").toUpperCase();
        if(!sirketDagilimi[s]) sirketDagilimi[s] = {adet: 0, ciro: 0, kar: 0};
        sirketDagilimi[s].adet += 1;
        sirketDagilimi[s].ciro += p.prim || 0;
        sirketDagilimi[s].kar += p.komisyon || 0;
      });

      return { totalCiro, totalKar, totalAdet, bransDagilimi, sirketDagilimi };
    }

    function para(n) {
      if (n == null || n === "") return "—";
      return Number(n).toLocaleString("tr-TR", { style: "currency", currency: "TRY" });
    }

    function diffHTML(val1, val2, formatPara=false) {
      if(!val2 && val2 !== 0) return "";
      const diff = val1 - val2;
      if (diff === 0) return `<span class="text-slate-400">Değişim yok</span>`;
      const isPos = diff > 0;
      const color = isPos ? "text-emerald-500" : "text-red-500";
      const sign = isPos ? "▲" : "▼";
      const txt = formatPara ? para(Math.abs(diff)) : Math.abs(diff);
      return `<span class="${color} font-bold">${sign} ${txt}</span> <span class="text-slate-400 font-normal">fark</span>`;
    }

    function renderDagilimTable(dagilimObj, elementId) {
      const arr = Object.entries(dagilimObj).sort((a,b) => b[1].kar - a[1].kar);
      if(arr.length === 0) {
        document.getElementById(elementId).innerHTML = '<p class="text-slate-400 italic text-sm">Veri bulunamadı.</p>';
        return;
      }
      let html = '<table class="w-full text-sm"><thead class="text-left text-slate-500 border-b"><tr><th class="pb-2">İsim</th><th class="pb-2">Adet</th><th class="pb-2 text-right">Kar</th></tr></thead><tbody class="divide-y divide-slate-100">';
      arr.forEach(([isim, vals]) => {
        html += `<tr><td class="py-2 font-bold text-brand-black">${isim}</td><td class="py-2 font-medium">${vals.adet}</td><td class="py-2 text-right text-brand-gold font-bold">${para(vals.kar)}</td></tr>`;
      });
      html += '</tbody></table>';
      document.getElementById(elementId).innerHTML = html;
    }

    async function calistirFinansalRapor() {
      if(!policeCache.length) policeCache = await api("/api/policeler");

      const sDate = document.getElementById("fin-start").value;
      const eDate = document.getElementById("fin-end").value;
      const sirket = document.getElementById("fin-sirket").value;
      const sorumlu = document.getElementById("fin-sorumlu").value;
      const brans = document.getElementById("fin-brans").value;

      const v1 = hesaplaFiltreliVeri(sDate, eDate, sirket, sorumlu, brans);
      
      document.getElementById("fin-metrics").classList.remove("hidden");
      document.getElementById("fin-details").classList.remove("hidden");

      document.getElementById("fin-ciro").textContent = para(v1.totalCiro);
      document.getElementById("fin-kar").textContent = para(v1.totalKar);
      document.getElementById("fin-adet").textContent = v1.totalAdet + " Adet";

      renderDagilimTable(v1.bransDagilimi, "fin-brans-liste");
      renderDagilimTable(v1.sirketDagilimi, "fin-sirket-liste");

      const compareMode = document.getElementById("fin-compare-mode").checked;
      const elCiroDiff = document.getElementById("fin-ciro-diff");
      const elKarDiff = document.getElementById("fin-kar-diff");
      const elAdetDiff = document.getElementById("fin-adet-diff");

      if(compareMode) {
        const sDate2 = document.getElementById("fin-start2").value;
        const eDate2 = document.getElementById("fin-end2").value;
        const v2 = hesaplaFiltreliVeri(sDate2, eDate2, sirket, sorumlu, brans);
        
        elCiroDiff.innerHTML = diffHTML(v1.totalCiro, v2.totalCiro, true);
        elKarDiff.innerHTML = diffHTML(v1.totalKar, v2.totalKar, true);
        elAdetDiff.innerHTML = diffHTML(v1.totalAdet, v2.totalAdet, false);
        
        elCiroDiff.classList.remove("hidden");
        elKarDiff.classList.remove("hidden");
        elAdetDiff.classList.remove("hidden");
      } else {
        elCiroDiff.classList.add("hidden");
        elKarDiff.classList.add("hidden");
        elAdetDiff.classList.add("hidden");
      }
    }

    yukleDashboard();
  </script>
</body>
</html>
"""

# --- API Endpoints ---
@app.get("/api/ozet")
def api_ozet(db: Session = Depends(get_db)):
    bugun = date.today()
    return {
        "musteri_sayisi": db.query(Musteri).count(),
        "police_sayisi": db.query(Police).count(),
        "aktif_police": db.query(Police).filter(Police.durum == "aktif", Police.bitis_tarihi >= bugun).count(),
        "yaklasan_30_gun": db.query(Police).filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=30)).count()
    }

@app.get("/api/musteriler")
def api_musteri_listele(q: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Musteri)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((Musteri.ad.ilike(like)) | (Musteri.soyad.ilike(like)) | (Musteri.telefon.ilike(like)))
    return query.all()

@app.post("/api/musteriler", status_code=201)
def api_musteri_olustur(payload: dict, db: Session = Depends(get_db)):
    if not payload.get("telefon") or not payload.get("portfoy_sorumlusu"):
        raise HTTPException(status_code=400, detail="Telefon numarası ve Portföy Sorumlusu zorunludur!")
    m = Musteri(**payload)
    db.add(m); db.commit(); db.refresh(m)
    return m

@app.put("/api/musteriler/{id}")
def api_musteri_guncelle(id: int, payload: dict, db: Session = Depends(get_db)):
    m = db.query(Musteri).filter(Musteri.id == id).first()
    if not m: raise HTTPException(404, "Bulunamadı")
    for k, v in payload.items(): setattr(m, k, v)
    db.commit(); db.refresh(m)
    return m

@app.delete("/api/musteriler/{id}")
def api_musteri_sil(id: int, db: Session = Depends(get_db)):
    m = db.query(Musteri).filter(Musteri.id == id).first()
    if not m: raise HTTPException(404, "Bulunamadı")
    db.delete(m); db.commit()
    return {"ok": True}

@app.get("/api/policeler")
def api_police_listele(db: Session = Depends(get_db)):
    kayitlar = db.query(Police).all()
    return [police_to_out(p, db) for p in kayitlar]

@app.get("/api/policeler/yaklasan")
def api_yaklasan(db: Session = Depends(get_db)):
    bugun = date.today()
    kayitlar = db.query(Police).filter(Police.durum != "iptal", Police.bitis_tarihi >= bugun, Police.bitis_tarihi <= bugun + timedelta(days=30)).all()
    return [police_to_out(p, db) for p in kayitlar]

@app.delete("/api/policeler/{id}")
def api_police_sil(id: int, db: Session = Depends(get_db)):
    p = db.query(Police).filter(Police.id == id).first()
    if not p: raise HTTPException(404, "Bulunamadı")
    db.delete(p); db.commit()
    return {"ok": True}

@app.post("/api/upload-parse")
async def api_upload_parse(file: UploadFile = File(...)):
    try:
        if not file.filename.lower().endswith(".pdf"):
            return JSONResponse(status_code=400, content={"detail": "Yalnızca PDF dosyası yükleyebilirsiniz"})
        icerik = await file.read()
        if not icerik:
            return JSONResponse(status_code=400, content={"detail": "Dosya boş"})
        
        dosya_adi = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
        hedef_yol = UPLOAD_DIR / dosya_adi
        hedef_yol.write_bytes(icerik)

        ayiklanan = ayikla_police_pdf(icerik)
        ayiklanan["pdf_dosya_adi"] = dosya_adi
        return ayiklanan
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"PDF okuma hatası: {str(e)}"})

@app.post("/api/upload-save")
async def api_upload_save(payload: dict, db: Session = Depends(get_db)):
    try:
        telefon = payload.get("telefon")
        danisman = payload.get("portfoy_sorumlusu")
        if not telefon or not danisman:
            raise HTTPException(status_code=400, detail="Telefon numarası ve Portföy Sorumlusu seçilmesi zorunludur!")

        tckn = (payload.get("tc_kimlik") or "").strip() or None
        musteri = None
        if tckn:
            musteri = db.query(Musteri).filter(Musteri.tc_kimlik == tckn).first()

        if not musteri:
            musteri = Musteri(
                ad=payload.get("ad") or "Bilinmiyor",
                soyad=payload.get("soyad") or "Müşteri",
                tc_kimlik=tckn,
                telefon=telefon,
                portfoy_sorumlusu=danisman
            )
            db.add(musteri)
            db.commit()
            db.refresh(musteri)
        else:
            musteri.telefon = telefon
            musteri.portfoy_sorumlusu = danisman
            db.commit()

        baslangic = parse_tarih(payload.get("baslangic_tarihi"))
        bitis = parse_tarih(payload.get("bitis_tarihi"))
        prim_deger = float(payload.get("prim") or 0)

        police = Police(
            musteri_id=musteri.id,
            police_no=payload.get("police_no") or f"PLK-{int(datetime.utcnow().timestamp())}",
            sigorta_turu=payload.get("sigorta_turu") or "Genel",
            sigorta_sirketi=payload.get("sigorta_sirketi") or "Diğer",
            islem_turu=payload.get("islem_turu") or "Yeni Poliçe",
            baslangic_tarihi=baslangic,
            bitis_tarihi=bitis,
            prim=prim_deger,
            pdf_dosya_adi=payload.get("pdf_dosya_adi")
        )
        db.add(police)
        db.commit()
        
        return {"ok": True, "mesaj": "Kayıt başarılı"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Kayıt hatası: {str(e)}"})