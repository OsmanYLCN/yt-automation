"""
config.py
=========
YouTube Shorts otomasyon sisteminin tüm ayarları bu dosyada toplanmıştır.
Buradaki değerleri değiştirerek sistemin davranışını (model, çözünürlük,
altyazı stili, klip süresi vb.) kolaylıkla özelleştirebilirsiniz.

API anahtarları ve hassas bilgiler proje kökündeki `.env` dosyasından
okunur. Bu dosya .gitignore'a eklidir ve GitHub'a gönderilmez.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# .env dosyasını yükle (python-dotenv gerektirmez)
# ---------------------------------------------------------------------------
# Proje klasöründeki .env dosyası varsa içindeki KEY=VALUE satırlarını
# os.environ'a ekler — sadece henüz tanımlanmamış değişkenleri doldurur.
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())

# ---------------------------------------------------------------------------
# 1) ABACUS AI ROUTELLM API AYARLARI
# ---------------------------------------------------------------------------

# API anahtarı: önce ortam değişkenleri kontrol edilir.
# Kendi anahtarınızı terminalde ya da bir .env dosyasında tanımlayın:
#   Windows : $env:ABACUS_API_KEY = "s2_xxxxxxxxxxxxxxxx"
#   Linux   : export ABACUS_API_KEY="s2_xxxxxxxxxxxxxxxx"
# Anahtarı doğrudan bu dosyaya YAZMAYIN — git geçmişine girer.
ABACUS_API_KEY: str = (
    os.getenv("ABACUS_ROUTELLM_API_KEY")
    or os.getenv("ABACUS_API_KEY")
    or "BURAYA_API_ANAHTARINIZI_YAZIN"
)

# Abacus AI RouteLLM, OpenAI uyumlu bir arayüz sunar.
# NOT: Canlı olarak doğrulanan çalışan adres "https://routellm.abacus.ai/v1"dir.
# "https://llmrouter.abacus.ai/api/v1" adresi de yedek olarak denenir; birincil
# adres ağ/DNS hatası verirse istemci otomatik olarak yedeğe geçer.
ROUTELLM_BASE_URL: str = os.getenv(
    "ABACUS_ROUTELLM_BASE_URL", "https://routellm.abacus.ai/v1"
)
ROUTELLM_FALLBACK_BASE_URLS: list[str] = [
    "https://llmrouter.abacus.ai/api/v1",
]

# Kullanılacak birincil model. Başarısız olursa FALLBACK_MODELS sırayla denenir.
# Canlı testte çalıştığı doğrulanan model adları:
#   claude-3-5-sonnet, claude-sonnet-4-6, claude-opus-4-5-20251101,
#   gemini-2.5-pro, gemini-2.5-flash, gpt-4o, gpt-5, route-llm (otomatik yönlendirme)
# UYARI: "gemini-1.5-pro" RouteLLM tarafından artık sunulmuyor (400 Invalid model).
#        Gemini için "gemini-2.5-pro" kullanın.
PRIMARY_MODEL: str = os.getenv("ROUTELLM_MODEL", "claude-3-5-sonnet")
FALLBACK_MODELS: list[str] = [
    "claude-sonnet-4-6",
    "gemini-2.5-pro",
    "gpt-4o",
]

# LLM istek ayarları
LLM_TEMPERATURE: float = 0.3           # Daha tutarlı JSON çıktısı için düşük tutuldu
LLM_MAX_TOKENS: int = 4000             # Yanıt için üst sınır
LLM_TIMEOUT: int = 180                 # Saniye
LLM_MAX_RETRIES: int = 3               # Aynı model için deneme sayısı
LLM_RETRY_BACKOFF: float = 2.0         # Denemeler arası bekleme çarpanı (saniye)

# Transkript çok uzunsa parça parça analiz edilir (map-reduce yaklaşımı).
MAX_TRANSCRIPT_CHARS: int = 45_000     # Tek istekte gönderilecek maksimum karakter
MAX_TRANSCRIPT_CHUNKS: int = 6         # En fazla kaç parçaya bölünüp analiz edilecek

# LLM tamamen başarısız olursa sezgisel (heuristic) yedek klip seçimi devreye girsin mi?
# Bu durumda altyazılar Türkçeye çevrilemez, orijinal dilde gömülür ve uyarı verilir.
ENABLE_HEURISTIC_FALLBACK: bool = True


# ---------------------------------------------------------------------------
# 2) KLASÖR YOLLARI
# ---------------------------------------------------------------------------

BASE_DIR: Path = Path(__file__).resolve().parent
OUTPUT_DIR: Path = BASE_DIR / "outputs"    # Hazır Shorts videoları
TEMP_DIR: Path = BASE_DIR / "temp"         # Ara dosyalar (indirme, altyazı, ham klip)
LOG_DIR: Path = BASE_DIR / "logs"          # Log dosyaları

# İşlem bittikten sonra temp klasöründeki ara dosyalar silinsin mi?
CLEANUP_TEMP: bool = True


# ---------------------------------------------------------------------------
# 3) KLİP SEÇİM AYARLARI
# ---------------------------------------------------------------------------

MIN_CLIP_DURATION: float = 30.0   # Saniye - Shorts için minimum süre
MAX_CLIP_DURATION: float = 60.0   # Saniye - Shorts için maksimum süre
TARGET_CLIP_DURATION: float = 45.0  # LLM sınırların dışına çıkarsa hedeflenen süre


# ---------------------------------------------------------------------------
# 4) TRANSKRİPT (ALTYAZI) AYARLARI
# ---------------------------------------------------------------------------

# Tercih sırası: elle yazılmış altyazılar > otomatik altyazılar
SUBTITLE_LANG_PREFERENCE: list[str] = ["en", "en-US", "en-GB", "en-orig"]
SUBTITLE_FORMAT_PREFERENCE: list[str] = ["json3", "vtt", "srt"]

# Ham altyazı parçaları (cue) okunabilir cümlelere birleştirilirken kullanılır
MERGE_MAX_GAP: float = 1.0        # İki cue arası bu süreden kısaysa birleştir (saniye)
MERGE_MAX_CHARS: int = 140        # Birleştirilmiş bloğun maksimum karakter sayısı
MERGE_MAX_DURATION: float = 7.0   # Birleştirilmiş bloğun maksimum süresi (saniye)

# Ekrana basılacak altyazı blokları için (daha kısa, daha okunabilir)
CAPTION_MAX_CHARS: int = 60       # Bir altyazı bloğundaki maksimum karakter
CAPTION_MAX_DURATION: float = 4.0  # Bir altyazı bloğunun maksimum süresi


# ---------------------------------------------------------------------------
# 5) VİDEO / FFMPEG AYARLARI
# ---------------------------------------------------------------------------

TARGET_WIDTH: int = 1080
TARGET_HEIGHT: int = 1920
TARGET_FPS: int = 30

VIDEO_CODEC: str = "libx264"
VIDEO_CRF: int = 20               # Düşük = daha kaliteli, daha büyük dosya
VIDEO_PRESET: str = "medium"      # Son (altyazı gömme) kodlaması: ultrafast ... veryslow
INTERMEDIATE_PRESET: str = "veryfast"  # Ara adımlar (kesme, 9:16) için hız odaklı preset
AUDIO_CODEC: str = "aac"
AUDIO_BITRATE: str = "192k"
PIXEL_FORMAT: str = "yuv420p"     # Tüm oynatıcılarla uyum için

# yt-dlp format seçici (dikey kırpma için 1080p yeterlidir)
YTDLP_FORMAT: str = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"

YTDLP_RETRIES: int = 3

# YouTube bazı sunucu IP'lerinde "Sign in to confirm you're not a bot" hatası verir.
# Bu durumda tarayıcınızdan çerez (cookies.txt) dışa aktarıp yolunu buraya yazın
# ya da COOKIES_FROM_BROWSER değerini "chrome" / "firefox" gibi ayarlayın.
COOKIES_FILE: str | None = os.getenv("YTDLP_COOKIES_FILE") or None
COOKIES_FROM_BROWSER: str | None = os.getenv("YTDLP_COOKIES_BROWSER") or None

# yt-dlp'nin denediği YouTube istemcileri (bot kontrolünü aşmakta yardımcı olur)
YTDLP_PLAYER_CLIENTS: list[str] = ["android_vr", "tv", "web_safari", "default"]

# 9:16 dönüşüm modu:
#   "auto"        -> Geniş videoda merkezden kırpar, dikey videoda bulanık arka plan ekler
#   "center_crop" -> Her zaman merkezden kırpar
#   "blur_pad"    -> Görüntünün tamamını korur, üst/alt boşluğu bulanık arka planla doldurur
CONVERT_MODE: str = "auto"

# Merkezden kırparken dikey odak noktası (0.0 = en üst, 0.5 = tam merkez, 1.0 = en alt).
# Konuşmacının yüzü genelde biraz yukarıda olduğu için 0.45 iyi bir başlangıçtır.
VERTICAL_FOCUS: float = 0.45

# Yatay odak noktası (0.5 = merkez). Konuşmacı sağda/solda ise değiştirin.
HORIZONTAL_FOCUS: float = 0.5

BLUR_BACKGROUND_SIGMA: int = 25   # blur_pad modunda arka plan bulanıklığı


# ---------------------------------------------------------------------------
# 6) ALTYAZI (ASS) STİL AYARLARI
# ---------------------------------------------------------------------------

# Altyazı gösterim modu:
#   "highlight" -> Cümle ekranda durur, konuşulan kelime sarı vurgulanır (viral Shorts stili)
#   "karaoke"   -> ASS \k karaoke etiketleriyle kelimeler sırayla sarıya döner
#   "sentence"  -> Cümle tek parça halinde belirir (fade in/out)
SUBTITLE_MODE: str = "highlight"

# Sistemde kurulu bir font adı olmalı. Kontrol: fc-list | grep -i "font adı"
# DejaVu Sans, Türkçe karakterleri (ı, İ, ş, ğ, ç, ö, ü) tam destekler.
SUBTITLE_FONT: str = "Arial"
SUBTITLE_FONT_SIZE: int = 82            # 1080x1920 karesi için büyük ve okunaklı
SUBTITLE_BOLD: bool = True

# ASS renkleri &HAABBGGRR formatındadır (AA=00 tamamen opak).
SUBTITLE_COLOR_BASE: str = "&H00FFFFFF"       # Beyaz - normal kelimeler
SUBTITLE_COLOR_HIGHLIGHT: str = "&H0000FFFF"  # Sarı  - vurgulanan kelime
SUBTITLE_COLOR_OUTLINE: str = "&H00000000"    # Siyah kontur
SUBTITLE_COLOR_SHADOW: str = "&H96000000"     # Yarı saydam siyah gölge

SUBTITLE_OUTLINE: float = 5.0            # Kontur kalınlığı
SUBTITLE_SHADOW: float = 2.5             # Gölge mesafesi
SUBTITLE_USE_BOX: bool = False           # True ise yazı arkasına opak kutu çizilir
SUBTITLE_BOX_COLOR: str = "&H80000000"   # Kutu rengi (yarı saydam siyah)

SUBTITLE_ALIGNMENT: int = 2              # 2 = alt-orta (lower-center)
SUBTITLE_MARGIN_V: int = 340             # Alttan boşluk (px) - alt-orta bölge
SUBTITLE_MARGIN_H: int = 70              # Sağ/sol boşluk (px)

SUBTITLE_MAX_CHARS_PER_LINE: int = 22    # Satır kaydırma eşiği
SUBTITLE_MAX_LINES: int = 3              # Bir blokta en fazla kaç satır
SUBTITLE_UPPERCASE: bool = False         # True ise altyazılar BÜYÜK HARFE çevrilir
SUBTITLE_POP_EFFECT: bool = True         # Kelime vurgulanırken hafif büyüme animasyonu


# ---------------------------------------------------------------------------
# 7) LOGLAMA
# ---------------------------------------------------------------------------

LOG_LEVEL: str = os.getenv("SHORTS_LOG_LEVEL", "INFO")
LOG_FILE_NAME: str = "shorts_automation.log"


# ---------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    """Gerekli klasörleri (outputs, temp, logs) oluşturur."""
    for directory in (OUTPUT_DIR, TEMP_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def api_key_is_configured() -> bool:
    """API anahtarının gerçekten girilip girilmediğini kontrol eder."""
    return bool(ABACUS_API_KEY) and not ABACUS_API_KEY.startswith("BURAYA_")


def model_candidates() -> list[str]:
    """Denenecek modellerin sırasını (tekrarsız) döndürür."""
    models: list[str] = []
    for model in [PRIMARY_MODEL, *FALLBACK_MODELS]:
        if model and model not in models:
            models.append(model)
    return models
