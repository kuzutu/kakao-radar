#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
topla.py — Kakao Haber Radari toplayici

kakao_sunucu.py'nin yerini alir. Fark: bu bir SUNUCU degil, bir BETIK.
Bir kez calisir, 38 kaynagi gezer, sonucu veri/haberler.json dosyasina
yazar ve biter. GitHub Actions bunu saatte bir calistirir; index.html de
JSON'u okur. Boylece Mac'in acik olmasi gerekmez.

Kaynak listesi index.html icindeki KAYNAKLAR dizisinden okunur.
Tek dogru kaynak orasidir — burada kopyasi TUTULMAZ, ikisi ayrisamaz.

Kullanim:
    python3 topla.py              # topla, cevirme
    CEVIR=1 python3 topla.py      # topla + basliklari Turkce'ye cevir
"""

import concurrent.futures
import gzip
import html as html_kacis
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KOK = os.path.dirname(os.path.abspath(__file__))
HTML_YOLU = os.path.join(KOK, "index.html")
CIKTI = os.path.join(KOK, "veri", "haberler.json")
CEVIRI_YOLU = os.path.join(KOK, "veri", "ceviri.json")
MAKALE_YOLU = os.path.join(KOK, "veri", "makaleler.json")
# Kesifle bulunan calisan besleme adresleri. Bunlar onbelleklenmezse
# her tur ana sayfadan yeniden aranir; ana sayfa o an agirsa kaynak
# sessizce duser ve calisan kaynak sayisi turdan tura oynar.
KESIF_YOLU = os.path.join(KOK, "veri", "kesif.json")

AZAMI_GUN = 60           # index.html'deki AZAMI_GUN ile ayni olmali
ZAMAN_ASIMI = 25
ES_ZAMANLI = 10
CEVIR = os.environ.get("CEVIR", "").strip() in ("1", "true", "evet")
# Makale govdesini cekip sadelestirme (reklamsiz okuyucu icin)
MAKALE = os.environ.get("MAKALE", "1").strip() in ("1", "true", "evet")
MAKALE_AZAMI = 120        # bir calismada en fazla kac YENI makale
MAKALE_UZUNLUK = 9000     # cikarilan metin bu kadar karakterle sinirli

# Baslikta/ozette bunlardan biri gecmiyorsa haber alinmaz.
KONU = re.compile(r"cocoa|cacao|cacau|kakao|chocolate|chocolat|cocobod", re.I)

# ...ANCAK zaten yalnizca kakao yayini yapan kaynaklarda bu filtre
# zararli. CocoaRadar 15 haber getirip hepsi elenmisti: basliklarinda
# "cocoa" gecmiyordu ("Ghana's harvest begins" gibi). Bu kaynaklarda
# her haber alinir.
KONU_MUAF = {
    "ICCO", "CIGHCI · Fildişi-Gana", "ECA · Avrupa öğütme", "CocoaRadar",
    "The Cocoa Post", "CocoaIntel", "World Cocoa Foundation",
    "Anecacao · EC", "Intl Cocoa Initiative",
}

TARAYICI_KIMLIGI = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# 403 alinca denenen ikinci kimlik. Bazi yayincilar tarayici kimligini
# engelliyor ama besleme okuyucularina izin veriyor.
OKUYUCU_KIMLIGI = "KakaoHaberRadari/1.0 (+RSS reader)"

# GitHub Actions'in Ubuntu imajinda kok sertifikalar dolu gelir, ama
# betik Mac'te de elle calistirilabilsin diye certifi'yi deniyoruz.
try:
    import certifi
    SSL_BAGLAMI = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_BAGLAMI = ssl.create_default_context()


# ══════════════════════════════════════════════════════════════════
#  Kaynak listesini index.html'den cikar
# ══════════════════════════════════════════════════════════════════

def kaynaklari_oku():
    """index.html icindeki const KAYNAKLAR=[...] blogunu ayristirir."""
    sayfa = open(HTML_YOLU, encoding="utf-8").read()

    yer = sayfa.find("const KAYNAKLAR")
    kose = sayfa.find("[", yer) if yer >= 0 else -1
    if kose < 0:
        raise SystemExit("index.html icinde KAYNAKLAR listesi bulunamadi.")

    derinlik = 0
    blok = ""
    for j in range(kose, len(sayfa)):
        if sayfa[j] == "[":
            derinlik += 1
        elif sayfa[j] == "]":
            derinlik -= 1
            if derinlik == 0:
                blok = sayfa[kose:j + 1]
                break

    kaynaklar = []
    # Her kaynak bir {..} nesnesi; feeds icinde [...] var, o yuzden
    # ic ice parantezleri de kabul eden bir kalip gerekiyor.
    for gov in re.finditer(r"\{(?:[^{}\[\]]|\[[^\[\]]*\])*\}", blok):
        t = gov.group(0)
        ad = re.search(r"ad\s*:\s*'([^']*)'", t)
        if not ad:
            continue
        feeds = re.findall(r"'(https?://[^']+)'", t)
        if not feeds:
            continue
        w = re.search(r"\bw\s*:\s*([\d.]+)", t)
        mx = re.search(r"\bmax\s*:\s*(\d+)", t)
        gn = re.search(r"\bgun\s*:\s*(\d+)", t)
        uy = re.search(r"uyari\s*:\s*'((?:[^'\\]|\\.)*)'", t)
        kaynaklar.append({
            "ad": ad.group(1),
            "feeds": feeds,
            "w": float(w.group(1)) if w else 1.0,
            "max": int(mx.group(1)) if mx else 40,
            # Bazi kaynaklar seyrek yayin yapiyor (Anecacao, kurum
            # bildirileri). Onlarda 60 gunluk pencere her seyi eliyor.
            # 'gun' verilmisse o kaynak icin pencere genisler.
            "gun": int(gn.group(1)) if gn else AZAMI_GUN,
            "uyari": (uy.group(1).replace("\\'", "'") if uy else ""),
        })
    return kaynaklar


# ══════════════════════════════════════════════════════════════════
#  Ag
# ══════════════════════════════════════════════════════════════════

def _tek_deneme(url, zaman_asimi, sayfa, kimlik):
    basliklar = {
        "User-Agent": kimlik,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8,es;q=0.7,pt;q=0.6",
        "Connection": "close",
    }
    if sayfa:
        basliklar["Accept"] = ("text/html,application/xhtml+xml,"
                               "application/xml;q=0.9,*/*;q=0.8")
        try:
            p = urllib.parse.urlparse(url)
            basliklar["Referer"] = "%s://%s/" % (p.scheme, p.hostname)
        except Exception:
            pass
    else:
        basliklar["Accept"] = ("application/rss+xml, application/atom+xml, "
                               "application/xml;q=0.9, application/json;q=0.9, "
                               "text/xml;q=0.9, */*;q=0.5")

    istek = urllib.request.Request(url, headers=basliklar)
    with urllib.request.urlopen(istek, timeout=zaman_asimi,
                                context=SSL_BAGLAMI) as y:
        ham = y.read()
        kodlama = (y.headers.get("Content-Encoding") or "").lower()

    if "gzip" in kodlama:
        try:
            ham = gzip.decompress(ham)
        except Exception:
            pass
    elif "deflate" in kodlama:
        try:
            ham = zlib.decompress(ham, -zlib.MAX_WBITS)
        except Exception:
            try:
                ham = zlib.decompress(ham)
            except Exception:
                pass

    for kod in ("utf-8", "latin-1"):
        try:
            return ham.decode(kod)
        except UnicodeDecodeError:
            continue
    return ham.decode("utf-8", "replace")


def getir(url, zaman_asimi=ZAMAN_ASIMI, sayfa=False):
    """sayfa=True ise HTML bekleniyor demektir, basliklar ona gore ayarlanir.

       Iki onemli davranis:

       1. GZIP: Accept-Encoding gonderiyoruz ve gelen govdeyi ELIMIZLE
          aciyoruz. urllib bunu kendisi yapmaz; bazi sunucular istenmese
          de gzip gonderiyor ve sikistirilmis bayt yigini "besleme
          tanimsiz" olarak dusuyordu. ICCO, FCC, Ghana News Agency ve
          World Cocoa Foundation bu yuzden hic gelmiyordu.

       2. TEKRAR: 5xx, zaman asimi ve DNS hatalari genelde geciciydi
          (AIP bir turda 500 verip digerinde calisiyordu). Bir kez daha
          deneniyor. 403'te ise sade bir okuyucu kimligiyle tekrar
          deneniyor — bazi yayincilar tarayici kimligini engelliyor."""
    son = None
    for deneme in (1, 2):
        try:
            return _tek_deneme(url, zaman_asimi, sayfa, TARAYICI_KIMLIGI)
        except urllib.error.HTTPError as e:
            son = e
            if e.code == 403:
                try:
                    return _tek_deneme(url, zaman_asimi, sayfa, OKUYUCU_KIMLIGI)
                except Exception as e2:
                    son = e2
                break                      # 403 tekrarla duzelmez
            if e.code < 500:
                break                      # 404 gibi kalici hatalar
        except Exception as e:
            son = e                        # zaman asimi, DNS, baglanti
        if deneme == 1:
            time.sleep(1.5)
    raise son if son else OSError("bilinmeyen hata")


# Yayincilarin bir kismi bulut IP'lerini engelliyor: GitHub'in
# sunucusundan bakinca 403 donuyor ya da besleme yerine bot kontrol
# sayfasi geliyor. Ayni adres Mac'ten sorunsuz aciliyor. Cozum:
# dogrudan olmadiysa herkese acik bir vekil uzerinden tekrar denemek.
# (kakao_sunucu.py'nin tarayici tarafinda yaptigi seyin aynisi.)
VEKILLER = [
    # Kendi Cloudflare Worker'imiz. Ucretsiz katman gunde 100.000 istek;
    # bu tur saat basi ~40 istek yapiyor. Kamu vekillerinin aksine kota
    # yemiyor ve zaman asimina dusmuyor.
    ("worker",     lambda u: "https://kakao-vekil.coffeeryan2018.workers.dev/?u="
                             + urllib.parse.quote(u, safe="")),
    ("allorigins", lambda u: "https://api.allorigins.win/raw?url="
                             + urllib.parse.quote(u, safe="")),
    ("codetabs",   lambda u: "https://api.codetabs.com/v1/proxy/?quest="
                             + urllib.parse.quote(u, safe="")),
]


VEKIL_ZAMAN_ASIMI = 15          # vekil basina sert sinir
VEKIL_ARDISIK_HATA_SINIRI = 8   # ust uste bu kadar hata -> tur boyu kapat

_vekil_ardisik_hata = 0
_vekil_kapali = False
_vekil_kilit = threading.Lock()


def vekille_getir(url, zaman_asimi=VEKIL_ZAMAN_ASIMI):
    """Adresi vekiller uzerinden dener. Ilk isleyeni dondurur.

       Iki davranis:

       1. DEVRE KESICI: ucretsiz vekiller (allorigins, codetabs) kotaya
          takilinca hepsi birden duser. 19 kaynagi sirayla denemek 20+
          dakika bosa bekleme demek. Ust uste 3 basarisizliktan sonra
          vekil o tur boyunca kapatilir.

       2. HATA RAPORU: eskiden 'son' her donguste ustune yaziliyordu,
          bu yuzden tanida hep sadece son vekilin (codetabs) hatasi
          gorunuyordu; allorigins'in neden dustugu hic bilinmiyordu.
          Artik ikisi de raporlaniyor."""
    global _vekil_ardisik_hata, _vekil_kapali

    with _vekil_kilit:
        if _vekil_kapali:
            raise OSError("vekil devre disi (ardisik %d hata)"
                          % VEKIL_ARDISIK_HATA_SINIRI)

    hatalar, vekil_suclu = [], False
    for ad, f in VEKILLER:
        try:
            sonuc = _tek_deneme(f(url), zaman_asimi, False, TARAYICI_KIMLIGI)
            with _vekil_kilit:
                _vekil_ardisik_hata = 0
            return sonuc
        except urllib.error.HTTPError as e:
            hatalar.append("%s: %s" % (ad, ("%s" % e)[:35]))
            # 404/403/500 vekilden DEGIL hedeften geliyor: vekil calisti,
            # istegi tasidi, cevabi aktardi. Devre kesiciyi bunlarla
            # doldurmak gercek adaylari ac birakiyor. Yalnizca vekilin
            # kendi cokusu sayilir: 502/520/522/526 ve baglanti hatalari.
            if e.code in (502, 520, 521, 522, 523, 524, 526):
                vekil_suclu = True
        except Exception as e:
            hatalar.append("%s: %s" % (ad, ("%s" % e)[:35]))
            vekil_suclu = True      # zaman asimi, DNS, baglanti kopmasi

    if vekil_suclu:
        with _vekil_kilit:
            _vekil_ardisik_hata += 1
            if _vekil_ardisik_hata >= VEKIL_ARDISIK_HATA_SINIRI:
                _vekil_kapali = True
                print("  !! vekiller kapatildi (ust uste %d gercek hata)"
                      % _vekil_ardisik_hata)
    raise OSError(" | ".join(hatalar) if hatalar else "vekil yok")


FEED_KALIBI = re.compile(
    r'<link[^>]+(?:type=["\'](?:application/(?:rss|atom)\+xml)["\'][^>]*'
    r'href=["\']([^"\']+)["\']|href=["\']([^"\']+)["\'][^>]*'
    r'type=["\']application/(?:rss|atom)\+xml["\'])', re.I)



# Yayincilarin bir kismi beslemesini <link rel="alternate"> ile ilan
# etmiyor: besleme VAR ama kesif goremiyor. Etiket bulunamazsa bu
# yaygin yollar sirayla denenir. Hepsi 404 verse bile ucuz — getir()
# 404'te tekrar denemeden cikiyor.
YAYGIN_YOLLAR = [
    "/feed/",
    "/rss/",
    "/rss.xml",
    "/feed.xml",
    "/index.xml",
    "/?feed=rss2",
    "/wp-json/wp/v2/posts?per_page=20&orderby=date&order=desc"
    "&_fields=title,link,date,excerpt",
]


def feed_kesfet(ornek_url):
    """Yapilandirilmis adreslerin hepsi 404 verdiyse, sitenin ana
       sayfasindan besleme adresini bulmayi dener. Yayincilar besleme
       yolunu degistirdiginde (COCOBOD, Hedgepoint, Fratmat, B&FT bu
       durumda) elle adres tahmin etmek yerine siteye kendisi sordurur.

       Iki asama: once <link rel="alternate"> etiketi, o yoksa
       YAYGIN_YOLLAR listesi."""
    try:
        p = urllib.parse.urlparse(ornek_url)
        ana = "%s://%s/" % (p.scheme, p.hostname)
    except Exception:
        return []

    bulunan = []
    try:
        sayfa = getir(ana, 20, sayfa=True)
    except Exception:
        sayfa = ""

    for m in FEED_KALIBI.finditer(sayfa or ""):
        y = html_kacis.unescape(m.group(1) or m.group(2) or "").strip()
        if not y:
            continue
        y = urllib.parse.urljoin(ana, y)
        if y not in bulunan:
            bulunan.append(y)

    if bulunan:
        return bulunan[:3]

    # Etiket yok — yaygin yollari aday olarak dondur. Dogrulamayi
    # kaynak_tara zaten yapiyor (bicim_bul ile).
    return [urllib.parse.urljoin(ana, y.lstrip("/")) for y in YAYGIN_YOLLAR]


def kesif_onbellek_oku():
    try:
        with open(KESIF_YOLU, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


_KESIF = kesif_onbellek_oku()
_KESIF_KILIT = threading.Lock()
_KESIF_DEGISTI = False


def kesif_yaz():
    if not _KESIF_DEGISTI:
        return
    try:
        os.makedirs(os.path.dirname(KESIF_YOLU), exist_ok=True)
        with open(KESIF_YOLU, "w", encoding="utf-8") as f:
            json.dump(_KESIF, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception as e:
        print("  kesif onbellegi yazilamadi: %s" % e)


def kesif_kaydet(ad, url):
    global _KESIF_DEGISTI
    with _KESIF_KILIT:
        if _KESIF.get(ad) != url:
            _KESIF[ad] = url
            _KESIF_DEGISTI = True


def kesif_unut(ad):
    global _KESIF_DEGISTI
    with _KESIF_KILIT:
        if ad in _KESIF:
            del _KESIF[ad]
            _KESIF_DEGISTI = True


def bicim_bul(metin):
    if not metin:
        return None
    b = metin.strip()
    if b[:1] == "[":
        try:
            return "wp" if isinstance(json.loads(b), list) else None
        except Exception:
            return None
    if "<item" in metin or "<entry" in metin:
        return "rss"
    return None


# ══════════════════════════════════════════════════════════════════
#  Ayristirma
# ══════════════════════════════════════════════════════════════════

ETIKET = re.compile(r"<[^>]+>")
BOSLUK = re.compile(r"\s+")


def temiz(t):
    if not t:
        return ""
    t = t.replace("<![CDATA[", "").replace("]]>", "")
    t = ETIKET.sub(" ", t)
    t = html_kacis.unescape(t)
    return BOSLUK.sub(" ", t).strip()


def tarih_coz(metin):
    if not metin:
        return None
    metin = metin.strip()
    try:
        d = parsedate_to_datetime(metin)          # RFC 822 (pubDate)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(metin.replace("Z", "+00:00"))  # ISO 8601
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def ozet_ise_yarar(ozet, baslik):
    if not ozet:
        return False
    s = ozet.strip()
    if len(s) < 25:
        return False
    if re.search(r"\bhref\s*=|news\.google\.com", s, re.I):
        return False
    a = re.sub(r"[^a-z0-9]", "", s.lower())
    b = re.sub(r"[^a-z0-9]", "", (baslik or "").lower())
    if b and (b in a or a in b):
        return False
    return True


def _etiket(parca, ad):
    m = re.search(r"<" + ad + r"[^>]*>([\s\S]*?)</" + ad + r">", parca, re.I)
    return temiz(m.group(1)) if m else ""


def rss_ayristir(x):
    out = []
    if "<item" in x:
        parcalar = re.split(r"<item[\s>]", x, flags=re.I)[1:]
    else:
        parcalar = re.split(r"<entry[\s>]", x, flags=re.I)[1:]
    for it in parcalar:
        bas = _etiket(it, "title")
        if not bas:
            continue
        m = re.search(r"<link[^>]*>([\s\S]*?)</link>", it, re.I)
        lnk = temiz(m.group(1)) if m else ""
        if not lnk:
            a = re.search(r"<link[^>]*href=[\"']([^\"']+)[\"']", it, re.I)
            lnk = html_kacis.unescape(a.group(1)) if a else ""
        trh = (_etiket(it, "pubDate") or _etiket(it, "published")
               or _etiket(it, "updated") or _etiket(it, "dc:date"))
        ozet = _etiket(it, "description") or _etiket(it, "summary")
        out.append({
            "baslik": bas,
            "link": lnk,
            "tarih": tarih_coz(trh),
            "ozet": ozet[:260] if ozet_ise_yarar(ozet, bas) else "",
        })
    return out


def wp_ayristir(metin):
    out = []
    for o in json.loads(metin):
        if not isinstance(o, dict):
            continue
        bas = temiz((o.get("title") or {}).get("rendered", ""))
        if not bas:
            continue
        ozet = temiz((o.get("excerpt") or {}).get("rendered", ""))
        out.append({
            "baslik": bas,
            "link": o.get("link", ""),
            "tarih": tarih_coz(o.get("date") or ""),
            "ozet": ozet[:260] if ozet_ise_yarar(ozet, bas) else "",
        })
    return out


# ══════════════════════════════════════════════════════════════════
#  Bir kaynagi tara
# ══════════════════════════════════════════════════════════════════

def kaynak_tara(k):
    t0 = time.time()
    esik = datetime.now(timezone.utc) - timedelta(days=k.get("gun") or AZAMI_GUN)
    muaf = k["ad"] in KONU_MUAF
    son_hata = "adres yok"

    # Sirayla uc yol denenir:
    #   1. adresler dogrudan
    #   2. site kapali/tasinmis ise ana sayfadan besleme kesfi
    #   3. hala olmadiysa herkese acik vekil uzerinden (IP engeli icin)
    adresler = [(u, "dogrudan") for u in k["feeds"]]
    # Gecen turda kesifle bulunmus calisan adres varsa once o denenir.
    _onbellekli = _KESIF.get(k["ad"])
    if _onbellekli and _onbellekli not in k["feeds"]:
        adresler.insert(0, (_onbellekli, "onbellek"))
    kesfedildi = False
    vekil_denendi = False

    def sonraki_yol():
        """Adres listesi bitince bir sonraki kurtarma yolunu kurar."""
        nonlocal kesfedildi, vekil_denendi
        if not kesfedildi:
            kesfedildi = True
            # Kesif eskiden yalnizca feeds[0]'in alan adina bakiyordu.
            # Agence Ecofin gibi kaynaklarda calisan besleme LISTEDEKI
            # BASKA bir alan adinda (agenceecofin.com yerine
            # ecofinagency.com). Artik listedeki her farkli alan adi
            # sirayla deneniyor.
            bulunan, gorulen = [], set()
            for u in k["feeds"]:
                try:
                    h = urllib.parse.urlparse(u).hostname
                except Exception:
                    continue
                if not h or h in gorulen:
                    continue
                gorulen.add(h)
                for x in feed_kesfet(u):
                    if x not in bulunan:
                        bulunan.append(x)
                if bulunan:
                    break
            if bulunan:
                return [(u, "kesif") for u in bulunan]
        if not vekil_denendi:
            vekil_denendi = True
            return [(u, "vekil") for u in k["feeds"][:2]]
        return []

    while True:
        if not adresler:
            adresler = sonraki_yol()
            if not adresler:
                break
        url, yol = adresler.pop(0)
        try:
            metin = vekille_getir(url) if yol == "vekil" else getir(url)
        except Exception as e:
            son_hata = ("%s" % e)[:70]
            if yol == "onbellek":
                kesif_unut(k["ad"])   # bayat adres, kesif tekrar arasin
            continue

        bicim = bicim_bul(metin)
        if not bicim:
            son_hata = "besleme tanimsiz (%d bayt, %s)" % (len(metin), yol)
            continue

        try:
            ham = rss_ayristir(metin) if bicim == "rss" else wp_ayristir(metin)
        except Exception as e:
            son_hata = "ayristirilamadi: %s" % ("%s" % e)[:50]
            continue

        haberler, elenen = [], 0
        for h in ham:
            if len(haberler) >= k["max"]:
                break
            if h["tarih"] and h["tarih"] < esik:
                elenen += 1
                continue
            # Google News yonlendirmesi listeye hic girmez (link acilmiyor)
            if re.search(r"news\.google\.com|/rss/articles/", h["link"] or ""):
                continue
            if not muaf and not KONU.search(h["baslik"] + " " + h["ozet"]):
                continue
            try:
                alan = urllib.parse.urlparse(h["link"]).hostname or k["ad"]
                alan = alan.replace("www.", "", 1)
            except Exception:
                alan = k["ad"]
            haberler.append({
                "baslik": h["baslik"],
                "link": h["link"],
                "tarih": h["tarih"].isoformat() if h["tarih"] else None,
                "ozet": h["ozet"],
                "kaynak": k["ad"],
                "grup": k["ad"],
                "alan": alan,
                "w": k["w"],
                "kaynakUyari": k["uyari"],
            })

        ms = int((time.time() - t0) * 1000)
        if haberler:
            # Kesifle bulunan adres calistiysa kaydet ki gelecek turlar
            # ana sayfayi yeniden taramasin.
            if yol in ("kesif", "onbellek"):
                kesif_kaydet(k["ad"], url)
            return haberler, [k["ad"], "%d haber" % len(haberler), ms,
                              url, yol, None, elenen]
        if muaf:
            son_hata = ("%d kayit var ama hepsi %d gunden eski"
                        % (len(ham), k.get("gun") or AZAMI_GUN))
        else:
            son_hata = "konuya uyan haber yok (%d kayit tarandi)" % len(ham)

    return [], [k["ad"], son_hata, int((time.time() - t0) * 1000),
                "", "", None, 0]


# ══════════════════════════════════════════════════════════════════
#  Ceviri (istege bagli) — CEVIR=1 ile acilir
# ══════════════════════════════════════════════════════════════════

#  Iki ayri uc deneniyor. Ilki bulut IP'lerinde kota yiyebiliyor;
#  o durumda ikinciye duser. Basliklar TEK TEK degil TOPLU gonderilir
#  (10'ar 10'ar) — hem cok daha hizli, hem kota yeme ihtimali dusuk.

def _yaniti_coz(j):
    """Iki ucun yapisi FARKLI, ayni ayristirici ikisini de karsilamali:
         gtx      -> [[["cevrilmis","orijinal",...], [...]], ...]
                     yani j[0] bir LISTE LISTESI, her p[0] bir parca
         clients5 -> [["cevrilmis","en"]]
                     yani j[0] duz bir METIN listesi, ilki cevirinin tamami

       Onceki surumde ikisi ayirt edilmiyordu: clients5'te p[0] parcanin
       degil METNIN ilk HARFINI veriyordu ve basliklar "Ke", "Ge" gibi
       iki harfe dusuyordu (ikinci harf "en" dil kodundan geliyordu)."""
    if not isinstance(j, list) or not j:
        raise ValueError("bos yanit")
    ilk = j[0]
    if isinstance(ilk, str):
        return ilk
    if isinstance(ilk, list):
        if ilk and all(isinstance(p, str) for p in ilk):
            return ilk[0]                      # clients5
        return "".join(p[0] for p in ilk       # gtx
                       if isinstance(p, list) and p and isinstance(p[0], str))
    raise ValueError("tanimsiz yapi")


def _makul(kaynak, cevrilmis):
    """Ceviri kaynakla kiyaslanabilir uzunlukta mi? Bozuk ayristirmadan
       gelen kirpik metinler ('Ke') buradan gecemez."""
    if not cevrilmis or not cevrilmis.strip():
        return False
    return len(cevrilmis.strip()) >= max(6, len(kaynak) * 0.35)


def _uc_gtx(metin):
    return _yaniti_coz(json.loads(getir(
        "https://translate.googleapis.com/translate_a/single"
        "?client=gtx&sl=auto&tl=tr&dt=t&q=" + urllib.parse.quote(metin), 25)))


def _uc_clients5(metin):
    return _yaniti_coz(json.loads(getir(
        "https://clients5.google.com/translate_a/t"
        "?client=dict-chrome-ex&sl=auto&tl=tr&q="
        + urllib.parse.quote(metin), 25)))


UCLAR = [("gtx", _uc_gtx), ("clients5", _uc_clients5)]


def ceviri_onbellek_oku():
    """Onbellegi okur ve bozuk kayitlari ELER. Boylece eski surumun
       yazdigi kirpik ceviriler kendiliginden temizlenir."""
    try:
        ham = json.load(open(CEVIRI_YOLU, encoding="utf-8"))
    except Exception:
        return {}
    temiz = {a: b for a, b in ham.items() if _makul(a, b)}
    atilan = len(ham) - len(temiz)
    if atilan:
        print("  onbellekten %d bozuk ceviri atildi" % atilan)
    return temiz


def cevir_metin(metin, durum):
    """Calisan ilk ucu kullanir ve onu hatirlar."""
    sira = sorted(UCLAR, key=lambda u: 0 if u[0] == durum.get("iyi") else 1)
    son = None
    for ad, f in sira:
        try:
            c = f(metin)
            if c and c.strip():
                durum["iyi"] = ad
                return c
            son = "%s: bos yanit" % ad
        except Exception as e:
            son = "%s: %s" % (ad, ("%s" % e)[:60])
    raise OSError(son or "bilinmeyen")


def basliklari_cevir(haberler):
    """Yalnizca onbellekte OLMAYAN basliklari cevirir. Onbellek repoya
       islendigi icin her calismada sifirdan cevrilmez."""
    onbellek = ceviri_onbellek_oku()
    yeni = [h["baslik"] for h in haberler if h["baslik"] not in onbellek]
    durum = {}
    hata_ornegi = ""
    cevrilen = 0

    # 10'ar 10'ar, satir basiyla ayirarak gonder
    for i in range(0, len(yeni), 10):
        grup = yeni[i:i + 10]
        try:
            sonuc = cevir_metin("\n".join(grup), durum).split("\n")
        except Exception as e:
            hata_ornegi = hata_ornegi or ("%s" % e)
            sonuc = []

        tamam = (len(sonuc) == len(grup)
                 and all(_makul(a, b) for a, b in zip(grup, sonuc)))
        if tamam:
            for a, b in zip(grup, sonuc):
                onbellek[a] = b.strip()
                cevrilen += 1
        else:
            # Toplu ceviri tutmadi: bu grubu tek tek dene
            for a in grup:
                try:
                    c = cevir_metin(a, durum)
                    if _makul(a, c):
                        onbellek[a] = c.strip()
                        cevrilen += 1
                    else:
                        hata_ornegi = hata_ornegi or ("kirpik ceviri: %r" % c[:40])
                except Exception as e:
                    hata_ornegi = hata_ornegi or ("%s" % e)
                time.sleep(0.2)
        time.sleep(0.3)

    for h in haberler:
        h["baslikTR"] = onbellek.get(h["baslik"], "")

    # Onbellegi buda: sadece bu taramada gorulen basliklar kalsin
    gorulen = {h["baslik"] for h in haberler}
    onbellek = {a: b for a, b in onbellek.items() if a in gorulen}
    os.makedirs(os.path.dirname(CEVIRI_YOLU), exist_ok=True)
    with open(CEVIRI_YOLU, "w", encoding="utf-8") as f:
        json.dump(onbellek, f, ensure_ascii=False, indent=0, sort_keys=True)

    print("  ceviri: %d yeni baslik, %d cevrildi, onbellekte %d kayit"
          % (len(yeni), cevrilen, len(onbellek)))
    if durum.get("iyi"):
        print("  ceviri ucu: %s" % durum["iyi"])
    if hata_ornegi:
        print("  ceviri hatasi (ornek): %s" % hata_ornegi)
    if yeni and cevrilen == 0:
        print("  UYARI: hicbir baslik cevrilemedi. Liste orijinal dilde")
        print("         gosterilecek; makaleler yine de Turkce acilir.")


# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
#  Makale govdesi: cek, sadelestir, cevir
# ══════════════════════════════════════════════════════════════════
#  kakao_sunucu.py'nin "Sade gorunum"unun yerini alir. Fark: is okuma
#  aninda degil TOPLAMA aninda yapiliyor. Cikan temiz metin JSON'a
#  yazildigi icin telefonda ne proxy ne de reklam var.

def makale_onbellek_oku():
    try:
        return json.load(open(MAKALE_YOLU, encoding="utf-8"))
    except Exception:
        return {}


def makale_cikar(link):
    """Sayfayi ceker, reklam/menu/altbilgiyi atar, paragraflari dondurur."""
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        sayfa = getir(link, 25, sayfa=True)
    except Exception:
        return None
    try:
        metin = trafilatura.extract(
            sayfa, include_comments=False, include_tables=False,
            favor_precision=True, no_fallback=False)
    except Exception:
        metin = None
    if not metin:
        return None

    paragraflar = [p.strip() for p in metin.split("\n") if len(p.strip()) > 40]
    if not paragraflar:
        return None

    # Toplam uzunlugu sinirla — cok uzun makale hem JSON'u sisiriyor
    # hem de ceviri kotasini hizla tuketiyor.
    out, toplam = [], 0
    for p in paragraflar:
        if toplam + len(p) > MAKALE_UZUNLUK:
            break
        out.append(p)
        toplam += len(p)
    return out or None


def paragraflari_cevir(paragraflar, durum):
    """Paragraflari ~1200 karakterlik obeklerde cevirir. Bir obek
       basarisiz olursa o obegin paragraflari orijinal kalir."""
    cevrili, obek, uzunluk = [], [], 0

    def obegi_bosalt():
        nonlocal obek, uzunluk
        if not obek:
            return
        try:
            sonuc = cevir_metin("\n".join(obek), durum).split("\n")
        except Exception:
            sonuc = []
        if len(sonuc) == len(obek) and all(
                _makul(a, b) for a, b in zip(obek, sonuc)):
            cevrili.extend(s.strip() for s in sonuc)
        else:
            cevrili.extend(obek)          # cevrilemedi: orijinali koru
        obek, uzunluk = [], 0
        time.sleep(0.3)

    for p in paragraflar:
        if uzunluk + len(p) > 1200 and obek:
            obegi_bosalt()
        obek.append(p)
        uzunluk += len(p)
    obegi_bosalt()
    return cevrili


def makaleleri_hazirla(haberler):
    onbellek = makale_onbellek_oku()
    yeni = [h for h in haberler
            if h["link"] and h["link"] not in onbellek][:MAKALE_AZAMI]

    print("  %d haberin %d tanesi yeni, govdeleri cekiliyor..."
          % (len(haberler), len(yeni)))

    # Cekme is agirlikli degil ag agirlikli — paralel yapilabilir
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        govdeler = list(ex.map(lambda h: makale_cikar(h["link"]), yeni))

    cekilen = sum(1 for g in govdeler if g)
    print("  %d/%d makale govdesi cikarildi" % (cekilen, len(yeni)))

    durum, cevrilen = {}, 0
    for h, govde in zip(yeni, govdeler):
        if not govde:
            onbellek[h["link"]] = {"p": [], "pTR": [], "hata": "govde cikarilamadi"}
            continue
        kayit = {"p": govde, "pTR": []}
        if CEVIR:
            try:
                tr = paragraflari_cevir(govde, durum)
                if tr != govde:
                    kayit["pTR"] = tr
                    cevrilen += 1
            except Exception:
                pass
        onbellek[h["link"]] = kayit

    if CEVIR:
        print("  %d makale Turkce'ye cevrildi" % cevrilen)

    # Listede olmayan makaleleri at — dosya sonsuza kadar buyumesin
    gorulen = {h["link"] for h in haberler}
    onbellek = {a: b for a, b in onbellek.items() if a in gorulen}

    os.makedirs(os.path.dirname(MAKALE_YOLU), exist_ok=True)
    with open(MAKALE_YOLU, "w", encoding="utf-8") as f:
        json.dump(onbellek, f, ensure_ascii=False, indent=0, sort_keys=True)

    var = sum(1 for v in onbellek.values() if v.get("p"))
    print("  makale dosyasi: %d kayit (%d okunabilir)" % (len(onbellek), var))
    for h in haberler:
        h["okunur"] = bool(onbellek.get(h["link"], {}).get("p"))


# ══════════════════════════════════════════════════════════════════

def tekille(haberler):
    """Ayni haber birden cok kaynakta cikiyor. Baslik normalize edilip
       tekrarlar atilir — index.html'deki ayni kural."""
    gor, out = set(), []
    for h in haberler:
        a = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,30}$", "", h["baslik"].lower())
        a = re.sub(r"[^a-z0-9]", "", a)[:55]
        if len(a) >= 12:
            if a in gor:
                continue
            gor.add(a)
        h["baslik"] = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,32}$", "",
                             h["baslik"]).strip() or h["baslik"]
        out.append(h)
    return out


def main():
    t0 = time.time()
    kaynaklar = kaynaklari_oku()
    print("%d kaynak okundu (index.html) · SURUM 2026-09-01-c" % len(kaynaklar))
    print("-" * 62)

    hepsi, rapor = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=ES_ZAMANLI) as ex:
        for haberler, satir in ex.map(kaynak_tara, kaynaklar):
            hepsi.extend(haberler)
            rapor.append(satir)
            isaret = "OK " if haberler else "-- "
            print("  %s %-28s %s" % (isaret, satir[0][:28], satir[1]))

    hepsi = tekille(hepsi)
    hepsi.sort(key=lambda h: h["tarih"] or "", reverse=True)
    hepsi = hepsi[:400]

    if CEVIR:
        print("-" * 62)
        basliklari_cevir(hepsi)

    if MAKALE:
        print("-" * 62)
        makaleleri_hazirla(hepsi)

    calisan = sum(1 for r in rapor if re.match(r"\d+ haber", r[1]))
    veri = {
        "uretim": datetime.now(timezone.utc).isoformat(),
        "kaynakSayisi": len(kaynaklar),
        "calisanKaynak": calisan,
        "haberSayisi": len(hepsi),
        "cevirili": CEVIR,
        "haberler": hepsi,
        "rapor": rapor,
    }

    os.makedirs(os.path.dirname(CIKTI), exist_ok=True)
    with open(CIKTI, "w", encoding="utf-8") as f:
        json.dump(veri, f, ensure_ascii=False, indent=1)

    print("-" * 62)
    print("%d haber · %d/%d kaynak · %.1f sn" %
          (len(hepsi), calisan, len(kaynaklar), time.time() - t0))
    kesif_yaz()
    if _KESIF:
        print("kesif onbellegi (%d kaynak):" % len(_KESIF))
        for ad in sorted(_KESIF):
            print("   %-28s %s" % (ad[:28], _KESIF[ad]))
    print("yazildi: %s" % CIKTI)

    # Tum kaynaklar birden dustuyse bu bir ag/kod sorunudur; Actions'in
    # yesil gorunup bos veri islemesindense hata vermesi daha iyi.
    if calisan == 0:
        print("HATA: hicbir kaynaktan haber gelmedi.")
        sys.exit(1)


if __name__ == "__main__":
    main()
