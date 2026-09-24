"""WhatsApp mesaj gönderme altyapısı — sağlayıcıdan bağımsız, ileride kolayca takılabilir.

Şu an sisteme bağlı gerçek bir WhatsApp sağlayıcısı (Baileys, Evolution API, WPPConnect vb.) YOKTUR.
Bu modül, o bağlantı kurulana kadar sistemin güvenle çalışmasını sağlayan bir iskelettir:

  * `WhatsAppGonderici`: her sağlayıcının uyması gereken soyut arayüz (`gonder(telefon, mesaj)`).
  * `KonsolGonderici`: hiçbir sağlayıcı yokken kullanılan varsayılan — mesajı GÖNDERMEZ, yalnızca loglar
    ve `SON_KUYRUK` listesine ekler (arayüzde/raporda gösterebilmek için).
  * `WebhookGonderici`: `WHATSAPP_WEBHOOK_URL` ortam değişkeni tanımlıysa kullanılır. Evolution API,
    WPPConnect ve çoğu Baileys sarmalayıcısı basit bir "şuraya POST at" webhook'u sağlar; URL'yi ortam
    değişkenine yazmanız yeterlidir, kod değişikliği gerekmez.

Yeni bir sağlayıcıya geçmek için: o sağlayıcının SDK'sını çağıran bir `WhatsAppGonderici` alt sınıfı
yazıp dosyanın sonundaki `gonderici_olustur()` fonksiyonuna bir dal eklemeniz yeterlidir; geri kalan kod
(app.py'deki uçlar, ozel_gunler.py'deki mesaj üretimi) değişmeden çalışmaya devam eder.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

log = logging.getLogger("crm.whatsapp")

SON_KUYRUK_BOYUTU = 200
SON_KUYRUK: deque = deque(maxlen=SON_KUYRUK_BOYUTU)  # UI/rapor için: en son hazırlanan/gönderilen mesajlar


@dataclass
class GonderimSonucu:
    basarili: bool
    gonderildi: bool          # False ise mesaj yalnızca hazırlandı/loglandı, gerçekten iletilmedi
    detay: str = ""
    telefon: str = ""
    mesaj: str = ""
    zaman: datetime = field(default_factory=datetime.utcnow)


class WhatsAppGonderici(ABC):
    """Her WhatsApp sağlayıcısının uyması gereken arayüz."""

    @abstractmethod
    def gonder(self, telefon: str, mesaj: str) -> GonderimSonucu: ...


class KonsolGonderici(WhatsAppGonderici):
    """Sağlayıcı bağlanana kadar varsayılan: mesajı gerçekten göndermez, yalnızca kuyruğa/loga yazar."""

    def gonder(self, telefon: str, mesaj: str) -> GonderimSonucu:
        sonuc = GonderimSonucu(
            basarili=True, gonderildi=False, telefon=telefon, mesaj=mesaj,
            detay="Gerçek WhatsApp sağlayıcısı bağlı değil (WHATSAPP_WEBHOOK_URL tanımlı değil); mesaj yalnızca hazırlandı.",
        )
        log.info("WhatsApp (dry-run) → %s: %s", telefon, mesaj.replace("\n", " ")[:120])
        SON_KUYRUK.append(sonuc)
        return sonuc


class WebhookGonderici(WhatsAppGonderici):
    """Evolution API / WPPConnect / Baileys sarmalayıcıları gibi basit webhook alan sağlayıcılar için.

    POST {"telefon": "905xxxxxxxxx", "mesaj": "..."} gönderir; `WHATSAPP_WEBHOOK_TOKEN` tanımlıysa
    Authorization: Bearer <token> başlığı eklenir. Sağlayıcınızın beklediği gövde farklıysa bu sınıfı
    kopyalayıp alanları (telefon/mesaj anahtar adları) kendi API'nize göre uyarlayın.
    """

    def __init__(self, webhook_url: str, token: Optional[str] = None, zaman_asimi: int = 10):
        self.webhook_url, self.token, self.zaman_asimi = webhook_url, token, zaman_asimi

    def gonder(self, telefon: str, mesaj: str) -> GonderimSonucu:
        govde = json.dumps({"telefon": telefon, "mesaj": mesaj}).encode("utf-8")
        istek = urllib.request.Request(self.webhook_url, data=govde, method="POST",
                                       headers={"Content-Type": "application/json"})
        if self.token:
            istek.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(istek, timeout=self.zaman_asimi) as yanit:
                sonuc = GonderimSonucu(basarili=200 <= yanit.status < 300, gonderildi=True, telefon=telefon,
                                       mesaj=mesaj, detay=f"HTTP {yanit.status}")
        except Exception as e:
            log.error("WhatsApp webhook hatası (%s): %s", telefon, e)
            sonuc = GonderimSonucu(basarili=False, gonderildi=False, telefon=telefon, mesaj=mesaj, detay=str(e))
        SON_KUYRUK.append(sonuc)
        return sonuc


def gonderici_olustur() -> WhatsAppGonderici:
    url = os.getenv("WHATSAPP_WEBHOOK_URL")
    if url:
        return WebhookGonderici(url, os.getenv("WHATSAPP_WEBHOOK_TOKEN"))
    return KonsolGonderici()


GONDERICI = gonderici_olustur()


def mesaj_gonder(telefon: str, mesaj: str) -> GonderimSonucu:
    """Uygulama genelinde tek çağrı noktası: app.py ve ileride kurulacak zamanlayıcı bunu kullanır."""
    return GONDERICI.gonder(telefon, mesaj)
