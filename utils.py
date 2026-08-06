"""
utils.py
========
Tüm modüllerin ortak kullandığı yardımcı fonksiyonlar:
loglama kurulumu, zaman biçimlendirme, dosya adı temizleme,
FFmpeg/FFprobe çağrıları ve LLM yanıtından JSON çıkarma.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Sequence

import config

# ---------------------------------------------------------------------------
# LOGLAMA
# ---------------------------------------------------------------------------

_LOG_CONFIGURED = False


class _ColorFormatter(logging.Formatter):
    """Terminalde seviyelere göre renkli log çıktısı üretir."""

    COLORS = {
        "DEBUG": "\033[37m",
        "INFO": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;41m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if sys.stderr.isatty():
            color = self.COLORS.get(record.levelname, "")
            if color:
                return f"{color}{message}{self.RESET}"
        return message


def setup_logging(level: str | None = None) -> logging.Logger:
    """
    Kök logger'ı hem konsola hem de dosyaya yazacak şekilde yapılandırır.
    Birden fazla kez çağrılsa bile tek kurulum yapılır.
    """
    global _LOG_CONFIGURED
    logger = logging.getLogger("shorts")
    if _LOG_CONFIGURED:
        return logger

    log_level = getattr(logging, (level or config.LOG_LEVEL).upper(), logging.INFO)
    logger.setLevel(log_level)
    logger.propagate = False

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(_ColorFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(config.LOG_DIR / config.LOG_FILE_NAME, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
        )
        logger.addHandler(file_handler)
    except OSError as exc:  # Dosyaya yazamazsak sadece konsola devam ederiz
        logger.warning("Log dosyası oluşturulamadı: %s", exc)

    _LOG_CONFIGURED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """Modüle özel alt logger döndürür (örn. 'shorts.transcript')."""
    setup_logging()
    return logging.getLogger(f"shorts.{name}")


LOGGER = get_logger("utils")


# ---------------------------------------------------------------------------
# ZAMAN YARDIMCILARI
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(
    r"^\s*(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2}(?:[.,]\d+)?)\s*$"
)


def parse_timestamp(value: Any) -> float:
    """
    Farklı biçimlerdeki zaman değerlerini saniyeye (float) çevirir.

    Desteklenen biçimler: 90, "90", "90.5", "1:30", "01:30.500", "00:01:30,250"
    Hata durumunda ValueError yükseltir.
    """
    if value is None:
        raise ValueError("Zaman değeri boş olamaz.")
    if isinstance(value, (int, float)):
        return max(0.0, float(value))

    text = str(value).strip()
    if not text:
        raise ValueError("Zaman değeri boş olamaz.")

    match = _TIME_RE.match(text)
    if match:
        hours = int(match.group("h") or 0)
        minutes = int(match.group("m"))
        seconds = float(match.group("s").replace(",", "."))
        return float(hours * 3600 + minutes * 60 + seconds)

    try:
        return max(0.0, float(text.replace(",", ".")))
    except ValueError as exc:
        raise ValueError(f"Zaman değeri anlaşılamadı: {value!r}") from exc


def format_timestamp(seconds: float) -> str:
    """Saniyeyi okunabilir 'HH:MM:SS.mmm' biçimine çevirir."""
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:  # Yuvarlama taşmasını düzelt
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def format_ass_time(seconds: float) -> str:
    """ASS altyazı formatının beklediği 'H:MM:SS.cc' zaman biçimini üretir."""
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:
        centis = 99
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def format_duration(seconds: float) -> str:
    """İnsan tarafından okunabilir süre metni: '1 dk 12 sn'."""
    seconds = max(0.0, float(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 1:
        return f"{int(minutes)} dk {secs:.1f} sn"
    return f"{secs:.1f} sn"


# ---------------------------------------------------------------------------
# DOSYA / METİN YARDIMCILARI
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_length: int = 70) -> str:
    """
    Dosya adı olarak güvenle kullanılabilecek bir metin üretir
    (Türkçe karakterler ASCII karşılıklarına indirgenir).
    """
    if not name:
        return "video"

    replacements = {
        "ı": "i", "İ": "I", "ş": "s", "Ş": "S", "ğ": "g", "Ğ": "G",
        "ç": "c", "Ç": "C", "ö": "o", "Ö": "O", "ü": "u", "Ü": "U",
    }
    for src, dst in replacements.items():
        name = name.replace(src, dst)

    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\s.-]", "", name).strip()
    name = re.sub(r"[\s_]+", "_", name).strip("._-")
    return (name[:max_length].rstrip("._-") or "video")


def extract_json(text: str) -> Any:
    """
    LLM yanıtının içinden JSON nesnesini/dizisini çıkarır.
    Markdown kod bloklarını ve yanıt öncesi/sonrası açıklamaları temizler.
    """
    if not text or not text.strip():
        raise ValueError("LLM boş yanıt döndürdü.")

    cleaned = text.strip()

    # ```json ... ``` bloklarını ayıkla
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    # Doğrudan denemesi
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # İlk '{' ya da '[' ile son eşleşen kapanış arasını dene
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            candidate = cleaned[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                # Sondaki fazla virgülleri temizleyip tekrar dene
                repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    continue

    raise ValueError("LLM yanıtından geçerli JSON çıkarılamadı.")


# ---------------------------------------------------------------------------
# FFMPEG / FFPROBE
# ---------------------------------------------------------------------------

class FFmpegError(RuntimeError):
    """FFmpeg/FFprobe komutları başarısız olduğunda yükseltilir."""


def check_dependencies() -> None:
    """ffmpeg ve ffprobe'un sistemde kurulu olduğunu doğrular."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise FFmpegError(
            f"Şu araçlar sistemde bulunamadı: {', '.join(missing)}. "
            "Kurulum: sudo apt-get install -y ffmpeg"
        )


def run_command(cmd: Sequence[str], description: str = "komut", timeout: int = 3600) -> str:
    """
    Harici bir komutu çalıştırır; hata olursa FFmpegError yükseltir.
    Başarılıysa stdout içeriğini döndürür.
    """
    LOGGER.debug("Çalıştırılıyor (%s): %s", description, " ".join(str(part) for part in cmd))
    try:
        result = subprocess.run(
            [str(part) for part in cmd],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"{description}: komut bulunamadı ({cmd[0]}).") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{description}: {timeout} saniye içinde tamamlanamadı.") from exc

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip().splitlines()[-15:]
        raise FFmpegError(
            f"{description} başarısız (çıkış kodu {result.returncode}):\n"
            + "\n".join(stderr_tail)
        )
    return result.stdout


def probe_video(path: str | Path) -> dict[str, Any]:
    """
    Videonun genişlik, yükseklik, süre, fps ve ses bilgisini ffprobe ile okur.
    """
    path = Path(path)
    if not path.exists():
        raise FFmpegError(f"Video dosyası bulunamadı: {path}")

    raw = run_command(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        description="ffprobe",
        timeout=120,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe çıktısı okunamadı: {exc}") from exc

    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise FFmpegError(f"Dosyada video akışı bulunamadı: {path}")

    duration = 0.0
    for candidate in (video_stream.get("duration"), data.get("format", {}).get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    fps = 30.0
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "30/1"
    try:
        num, _, den = rate.partition("/")
        if den and float(den) != 0:
            fps = float(num) / float(den)
        elif num:
            fps = float(num)
    except (TypeError, ValueError):
        fps = 30.0

    return {
        "path": str(path),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "duration": duration,
        "fps": round(fps, 3) if fps > 0 else 30.0,
        "has_audio": audio_stream is not None,
        "video_codec": video_stream.get("codec_name"),
        "size_bytes": int(data.get("format", {}).get("size") or 0),
    }


def escape_ffmpeg_filter_path(path: str | Path) -> str:
    """
    FFmpeg filtre zincirinde (ass=..., subtitles=...) kullanılacak dosya yolunu
    kaçış karakterleriyle güvenli hale getirir.
    """
    text = str(path)
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    text = text.replace("[", "\\[").replace("]", "\\]")
    text = text.replace(",", "\\,")
    return text


def safe_delete(path: str | Path) -> None:
    """Dosya ya da klasörü hata vermeden siler."""
    target = Path(path)
    try:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
    except OSError as exc:
        LOGGER.debug("Silinemedi (%s): %s", target, exc)


def human_size(num_bytes: int | float) -> str:
    """Bayt değerini okunabilir birime çevirir."""
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
