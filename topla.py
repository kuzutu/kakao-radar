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
import html as html_kacis
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KOK = os.path.dirname(os.path.abspath(__file__))
HTML_YOLU = os.path.join(KOK, "index.html")
CIKTI = os.path.join(KOK, "veri", "haberler.json")
CEVIRI_YOLU = os.path.join(KOK, "veri", "ceviri.json")

AZAMI_GUN = 60           # index.html'deki AZAMI_GUN ile ayni olmali
ZAMAN_ASIMI = 25
ES_ZAMANLI = 8
CEVIR = os.environ.get("CEVIR", "").strip() in ("1", "true", "evet")

# Baslikta/ozette bunlardan biri gecmiyorsa haber alinmaz.
# index.html'deki ayni filtrenin kopyasi.
KONU = re.compile(r"cocoa|cacao|cacau|kakao|chocolate|chocolat|cocobod", re.I)

TARAYICI_KIMLIGI = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

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
        uy = re.search(r"uyari\s*:\s*'((?:[^'\\]|\\.)*)'", t)
        kaynaklar.append({
            "ad": ad.group(1),
            "feeds": feeds,
            "w": float(w.group(1)) if w else 1.0,
            "max": int(mx.group(1)) if mx else 40,
            "uyari": (uy.group(1).replace("\\'", "'") if uy else ""),
        })
    return kaynaklar


# ══════════════════════════════════════════════════════════════════
#  Ag
# ══════════════════════════════════════════════════════════════════

def getir(url, zaman_asimi=ZAMAN_ASIMI):
    istek = urllib.request.Request(url, headers={
        "User-Agent": TARAYICI_KIMLIGI,
        "Accept": "application/json, application/rss+xml, text/xml, */*;q=0.8",
        "Accept-Language": "en,fr;q=0.8,es;q=0.7,pt;q=0.6",
    })
    with urllib.request.urlopen(istek, timeout=zaman_asimi,
                                context=SSL_BAGLAMI) as y:
        ham = y.read()
    for kodlama in ("utf-8", "latin-1"):
        try:
            return ham.decode(kodlama)
        except UnicodeDecodeError:
            continue
    return ham.decode("utf-8", "replace")


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
    esik = datetime.now(timezone.utc) - timedelta(days=AZAMI_GUN)
    son_hata = "adres yok"

    for url in k["feeds"]:
        try:
            metin = getir(url)
        except Exception as e:
            son_hata = ("%s" % e)[:70]
            continue

        bicim = bicim_bul(metin)
        if not bicim:
            son_hata = "besleme tanimsiz (%d bayt)" % len(metin)
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
            if not KONU.search(h["baslik"] + " " + h["ozet"]):
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
            return haberler, [k["ad"], "%d haber" % len(haberler), ms,
                              url, "toplayici", None, elenen]
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
    print("%d kaynak okundu (index.html)" % len(kaynaklar))
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
    print("yazildi: %s" % CIKTI)

    # Tum kaynaklar birden dustuyse bu bir ag/kod sorunudur; Actions'in
    # yesil gorunup bos veri islemesindense hata vermesi daha iyi.
    if calisan == 0:
        print("HATA: hicbir kaynaktan haber gelmedi.")
        sys.exit(1)


if __name__ == "__main__":
    main()
