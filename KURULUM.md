# Kakao Haber Radarı — GitHub kurulumu

Mac'in açık olmasına gerek yok. GitHub'ın sunucuları saat başı 38 kaynağı
tarayıp sonucu `veri/haberler.json` dosyasına yazıyor; sayfa da onu okuyor.

---

## Bir kerelik kurulum

### 1. Dosyaları repoya koy

Klasördeki her şeyi klonladığın repoya kopyala, sonra:

```
git add .
git commit -m "kakao radar"
git push
```

### 2. Pages'i aç

Repo → **Settings** → **Pages** → Source: **Deploy from a branch** →
Branch: `main`, klasör `/ (root)` → Save.

Bir iki dakika sonra adres hazır olur:
`https://KULLANICI_ADIN.github.io/kakao-radar/`

### 3. Actions'a yazma izni ver

Repo → **Settings** → **Actions** → **General** → en altta
**Workflow permissions** → **Read and write permissions** → Save.

Bu adım atlanırsa toplayıcı çalışır ama sonucu repoya yazamaz.

### 4. İlk taramayı elle başlat

Repo → **Actions** → soldan **Kakao haber topla** → sağda
**Run workflow** → yeşil düğme.

Bir dakika içinde biter. Yeşil tik gördüğünde adresi telefonda aç.

### 5. Telefona ekle

Safari'de adresi aç → Paylaş → **Ana Ekrana Ekle**. Uygulama gibi durur.

---

## Nasıl çalışıyor

```
.github/workflows/topla.yml   saat başı tetikler
        │
        ▼
topla.py                      index.html'den 38 kaynağı okur,
        │                     hepsini çeker, ayrıştırır, süzer,
        │                     başlıkları Türkçe'ye çevirir
        ▼
veri/haberler.json            repoya işlenir
        │
        ▼
index.html (Pages)            JSON'u okur, listeler
```

Kaynak listesi **yalnızca `index.html` içinde** duruyor (`const KAYNAKLAR`).
`topla.py` onu oradan okuyor, kendi kopyasını tutmuyor — yani kaynak
eklediğinde tek bir yeri değiştirmen yetiyor. Push ettiğin anda toplayıcı
kendiliğinden yeniden çalışır.

---

## Neler değişti

**Kazanılan**

- Mac kapalıyken de çalışıyor, telefondan her yerden açılıyor
- CORS ve proxy derdi tamamen bitti — istekleri GitHub yapıyor
- Başlıklar hazır Türkçe geliyor (Fransızca/İspanyolca kaynaklar için önemli)
  `🌐 TR` düğmesiyle orijinaline geçebilirsin
- 38 kaynağın hepsi sunucu tarafında çekildiği için engellenme daha az

**Kaybedilen**

- Alt penceredeki okuyucu (Sade görünüm, video engeli, sayfa çevirisi).
  Bunlar `kakao_sunucu.py`'ye bağlıydı. Artık habere tıklayınca yayıncının
  sayfası yeni sekmede açılıyor.
- "Linkleri kontrol et" ve "Tanı" düğmeleri de sunucuya bağlıydı,
  Pages'te çalışmazlar.

Okuyucuyu geri istersen `kakao_sunucu.py`'yi Mac'te eskisi gibi
çalıştırabilirsin — `index.html` yerel sunucuyu algılayınca eski
davranışına döner. İkisi bir arada çalışır.

---

## Günlük kullanım

Hiçbir şey yapman gerekmiyor. Sayfayı açarsın, veri hazırdır.

Sağ üstteki damga verinin ne kadar taze olduğunu söyler. Veri 3 saatten
eskiyse sayfa sarı bir uyarı gösterir — o zaman Actions sekmesine bakılır.

**Kaynak eklemek:** `index.html` içindeki `KAYNAKLAR` listesine yeni bir
satır ekle, push et. Toplayıcı kendiliğinden çalışır.

**Çeviriyi kapatmak:** `.github/workflows/topla.yml` içinde `CEVIR: "1"`
satırını `"0"` yap.

**Sıklığı değiştirmek:** aynı dosyada `cron` satırı. Örneğin üç saatte bir
için `"0 */3 * * *"`.

---

## Sorun çıkarsa

| Belirti | Sebep / çözüm |
|---|---|
| Actions kırmızı, "hicbir kaynaktan haber gelmedi" | Toplayıcı bilerek hata veriyor. Log'a bak: hepsi zaman aşımına düştüyse geçici, tekrar dene. |
| Actions yeşil ama sayfa güncellenmiyor | Adım 3'teki yazma izni verilmemiş. |
| Sayfa "Taranıyor…" da kalıyor | `veri/haberler.json` henüz yok. Adım 4'ü çalıştır. |
| Bazı kaynaklar hep boş | Bazı siteler bulut IP'lerini engelliyor. Alternatif besleme adresi ekle. |

Yerel olarak denemek için Mac'te:

```
cd kakao-radar
python3 topla.py
python3 -m http.server 8000
```

sonra `http://localhost:8000/`
