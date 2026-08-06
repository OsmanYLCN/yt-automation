# YouTube Shorts Otomasyonu (Faceless) 🎬

Uzun YouTube videolarından, **Abacus AI RouteLLM** yardımıyla en ilgi çekici
30-60 saniyelik bölümü bulup; **9:16 dikey (1080x1920)** formata çeviren ve
üzerine **Türkçe altyazı** gömen tam otomatik bir Python hattı.

```
YouTube linki
   ↓  1) transcript_fetcher.py   → yt-dlp ile zaman damgalı altyazı (transkript)
   ↓  2) llm_analyzer.py         → RouteLLM: en iyi 30-60 sn bölüm + Türkçe çeviri
   ↓  3) video_downloader.py     → yt-dlp: SADECE o zaman aralığını indir
   ↓  4) video_processor.py      → FFmpeg: 9:16 1080x1920 (bozulma yok)
   ↓  5) subtitle_burner.py      → ASS altyazı üret + FFmpeg ile göm
outputs/ klasöründe hazır Shorts .mp4
```

---

## 1. Kurulum

```bash
cd youtube_shorts_automation

# Python bağımlılıkları
pip install -r requirements.txt

# FFmpeg (sistem paketi)
sudo apt-get install -y ffmpeg        # Ubuntu / Debian
# brew install ffmpeg                 # macOS
```

### API anahtarı

`config.py` içindeki `ABACUS_API_KEY` değerini doldurun **veya** ortam
değişkeni tanımlayın (önerilen):

```bash
export ABACUS_API_KEY="s2_xxxxxxxxxxxxxxxxxxxx"
```

### Endpoint hakkında önemli not

Endpoint OpenAI uyumludur ve `config.py` içinde şu şekilde ayarlıdır:

```python
ROUTELLM_BASE_URL = "https://routellm.abacus.ai/v1"          # doğrulanmış, çalışan adres
ROUTELLM_FALLBACK_BASE_URLS = ["https://llmrouter.abacus.ai/api/v1"]
```

`llmrouter.abacus.ai` alan adı bu makineden **DNS'te çözülemedi**; çalışan adres
`routellm.abacus.ai/v1` olarak doğrulandı (`/v1/models` → 200, 149 model).
Bu yüzden birincil adres o yapıldı, orijinal adres yedek listede bırakıldı.
Bağlantı hatası alınırsa `llm_analyzer.py` otomatik olarak yedek adresi dener.

### Model notu

`claude-3-5-sonnet` canlı olarak test edildi ve **çalışıyor**.
`gemini-1.5-pro` ise bu router'da `400 Invalid model` döndürüyor; onun yerine
`gemini-2.5-pro` kullanılmalı (yedek model listesinde bu şekilde ayarlıdır).
Kullanılabilir modelleri listelemek için:

```bash
curl -s https://routellm.abacus.ai/v1/models -H "Authorization: Bearer $ABACUS_API_KEY"
```

---

## 2. Kullanım

```bash
# Etkileşimli mod (linkleri tek tek sorar)
python main.py

# Tek video
python main.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Birden fazla video
python main.py "URL1" "URL2" "URL3"

# Dosyadan link listesi (her satırda bir link)
python main.py --urls-file linkler.txt

# Model ve altyazı stilini değiştir
python main.py "URL" --model gemini-2.5-pro --subtitle-mode karaoke

# LLM'i atla, klip aralığını elle ver (çeviri yine LLM ile yapılır)
python main.py "URL" --manual-clip "2:10-2:55"

# Sadece analiz et, video işleme yapma (hızlı deneme)
python main.py "URL" --analyze-only
```

### Tüm seçenekler

| Seçenek | Açıklama |
|---|---|
| `--urls-file DOSYA` | Her satırda bir YouTube linki bulunan metin dosyası |
| `--output-dir KLASÖR` | Çıktı klasörü (varsayılan `outputs/`) |
| `--model AD` | RouteLLM modeli (`claude-3-5-sonnet`, `claude-sonnet-4-6`, `gemini-2.5-pro`, `gpt-4o`, `route-llm`) |
| `--subtitle-mode` | `highlight` (kelime kelime sarı vurgu), `karaoke`, `sentence` |
| `--convert-mode` | `auto`, `center_crop`, `blur_pad` |
| `--min-duration` / `--max-duration` | Klip süresi sınırları (saniye) |
| `--manual-clip "120-165"` | LLM klip seçimini atla, aralığı elle belirt |
| `--no-llm` | LLM'i tamamen kapat (sezgisel seçim, **çeviri yapılmaz**) |
| `--analyze-only` | Sadece analiz; indirme/işleme yok |
| `--keep-temp` | Ara dosyaları silme (hata ayıklama için) |
| `--log-level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## 3. Dosya yapısı

```
youtube_shorts_automation/
├── config.py               # Tüm ayarlar: API anahtarı, model, yollar, stil
├── main.py                 # Ana orkestratör (komut satırı + etkileşimli)
├── transcript_fetcher.py   # yt-dlp ile zaman damgalı altyazı çekme
├── llm_analyzer.py         # RouteLLM: klip seçimi + cümle cümle Türkçe çeviri
├── video_downloader.py     # Sadece seçilen zaman aralığını indirme
├── video_processor.py      # 9:16 1080x1920 dönüşümü (kırpma / bulanık arka plan)
├── subtitle_burner.py      # ASS altyazı üretimi + FFmpeg ile gömme
├── utils.py                # Loglama, zaman/dosya yardımcıları, FFmpeg sarmalayıcı
├── requirements.txt
├── outputs/                # Hazır Shorts videoları (+ .json meta veri)
├── temp/                   # Ara dosyalar (otomatik temizlenir)
└── logs/                   # shorts_automation.log
```

Her çıktı videosunun yanına aynı adla bir `.json` dosyası yazılır: seçilen
klip aralığı, Türkçe başlık önerisi, hashtag'ler, LLM'in seçim gerekçesi ve
altyazı blokları burada saklanır.

---

## 4. Ayarlar (config.py)

Sık değiştirilen değerler:

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `ROUTELLM_BASE_URL` | `https://routellm.abacus.ai/v1` | OpenAI uyumlu endpoint (doğrulandı) |
| `ROUTELLM_FALLBACK_BASE_URLS` | `https://llmrouter.abacus.ai/api/v1` | Bağlantı hatasında denenen yedek adres |
| `PRIMARY_MODEL` | `claude-3-5-sonnet` | Birincil RouteLLM modeli |
| `FALLBACK_MODELS` | `claude-sonnet-4-6`, `gemini-2.5-pro`, `gpt-4o` | Birincil model hata verirse sırayla denenir |
| `MIN/MAX_CLIP_DURATION` | 30 / 60 sn | Shorts klip süresi sınırları |
| `TARGET_WIDTH/HEIGHT` | 1080 / 1920 | Çıktı çözünürlüğü |
| `CONVERT_MODE` | `auto` | Geniş video → merkezden kırp, dikey video → bulanık arka plan |
| `VERTICAL_FOCUS` | `0.45` | Dikey kırpma odağı (0=üst, 0.5=merkez, 1=alt) |
| `HORIZONTAL_FOCUS` | `0.5` | Yatay kırpma odağı (konuşmacı sağda/solda ise değiştirin) |
| `SUBTITLE_MODE` | `highlight` | Altyazı animasyon modu |
| `SUBTITLE_FONT` | `DejaVu Sans` | Sistemde kurulu olmalı (`fc-list \| grep -i dejavu`) |
| `SUBTITLE_FONT_SIZE` | `82` | 1080x1920 için büyük ve okunaklı |
| `SUBTITLE_MARGIN_V` | `340` | Alttan boşluk → alt-orta konum |
| `SUBTITLE_COLOR_HIGHLIGHT` | sarı | Konuşulan kelimenin rengi |
| `VIDEO_CRF` / `VIDEO_PRESET` | 20 / medium | Kalite–hız dengesi |
| `COOKIES_FILE` | `None` | YouTube bot doğrulaması için `cookies.txt` yolu |

---

## 5. Görüntü neden bozulmuyor?

`video_processor.py` en-boy oranını asla zorlamaz:

- **center_crop** — Yatay videoda (16:9) kaynağın yüksekliği korunur,
  genişlikten `yükseklik × 9/16` kadar bir alan **merkezden** kırpılır ve
  1080x1920'ye ölçeklenir. Konuşmacı ortada olduğu için kadraj korunur.
  `HORIZONTAL_FOCUS` / `VERTICAL_FOCUS` ile odak kaydırılabilir.
- **blur_pad** — Kaynak zaten dikey/kare ise görüntünün tamamı korunur,
  arka plan aynı görüntünün bulanıklaştırılmış hâliyle doldurulur.

---

## 6. Sorun giderme

| Hata | Çözüm |
|---|---|
| `Sign in to confirm you're not a bot` | Tarayıcıdan `cookies.txt` dışa aktarıp `config.COOKIES_FILE` ayarına yolunu yazın ya da `export YTDLP_COOKIES_FILE=/yol/cookies.txt` |
| `Bu videoda kullanılabilir altyazı yok` | Altyazısı (otomatik dahil) olan bir video seçin |
| `API anahtarı ayarlı değil` | `export ABACUS_API_KEY=...` ya da `config.py`'yi düzenleyin |
| Connection error / timeout | Ağ erişimini ve `ROUTELLM_BASE_URL` değerini kontrol edin |
| Altyazıda Türkçe karakter kutu görünüyor | `SUBTITLE_FONT` değerini sistemde kurulu bir fontla değiştirin |
| `ffmpeg bulunamadı` | `sudo apt-get install -y ffmpeg` |

Ayrıntılı hata dökümü için: `python main.py URL --log-level DEBUG --keep-temp`
Loglar `logs/shorts_automation.log` dosyasına da yazılır.

---

## 7. Modülleri tek tek deneme

Her modül kendi başına da çalıştırılabilir:

```bash
python transcript_fetcher.py "URL"              # Transkriptin ilk 15 bloğunu yazdırır
python llm_analyzer.py "URL"                    # Klip planını JSON olarak yazdırır
python video_downloader.py "URL" 120 165        # Sadece 120-165. saniyeyi indirir
python video_processor.py temp/klip.mp4         # 9:16'ya çevirir
python subtitle_burner.py temp/klip_vertical.mp4  # Örnek altyazı gömer
```

### Ağ/API gerektirmeyen testler

`tests/test_llm_mock.py` yerel bir sahte (mock) OpenAI sunucusu başlatır ve
klip seçimi, çeviri hizalama, ASS üretimi gibi mantığı internet olmadan test eder:

```bash
python tests/test_llm_mock.py     # 18/18 test geçmeli
```

---

## 8. Doğrulanmış test durumu

Bu proje bu makinede uçtan uca çalıştırılarak doğrulandı:

- `python tests/test_llm_mock.py` → **18/18 test geçti**
- Tüm modüller `py_compile` ile derlendi → sözdizimi hatası yok
- Gerçek çalıştırma (gerçek RouteLLM API + gerçek YouTube videosu):
  `python main.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --keep-temp`
  → **1 dk 44 sn** içinde `outputs/` altına 1080x1920, 30 fps, 45.0 sn,
  ~31 MB Türkçe altyazılı Shorts videosu üretildi.

> Not: Sunucu IP'lerinde YouTube sık sık "Sign in to confirm you're not a bot"
> doğrulaması ister. Kendi makinenizde genelde sorun olmaz; olursa
> `config.COOKIES_FILE` veya `COOKIES_FROM_BROWSER` ayarını kullanın.
