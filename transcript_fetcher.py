"""
transcript_fetcher.py
=====================
YouTube videosundan zaman damgalı altyazı (transkript) çeker.

Öncelik sırası:
  1) Elle yazılmış İngilizce altyazılar
  2) Videonun orijinal dilindeki elle yazılmış altyazılar
  3) Otomatik oluşturulmuş İngilizce/orijinal altyazılar

Çıktı olarak {'video_id', 'title', 'duration', 'language', 'segments'} sözlüğü
döner. segments: [{'start': float, 'end': float, 'text': str}, ...]
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Iterable

import yt_dlp

import config
from utils import format_timestamp, get_logger

LOGGER = get_logger("transcript")


class TranscriptError(RuntimeError):
    """Altyazı bulunamadığında veya indirilemediğinde yükseltilir."""


# ---------------------------------------------------------------------------
# yt-dlp yardımcıları
# ---------------------------------------------------------------------------

class _YtdlpLogger:
    """
    yt-dlp'nin kendi çıktısını bizim log sistemimize yönlendirir.
    Hataları biz zaten yakalayıp anlamlı mesaja çevirdiğimiz için
    yt-dlp'nin ham ERROR satırları DEBUG seviyesinde tutulur.
    """

    def debug(self, message: str) -> None:
        LOGGER.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        LOGGER.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        LOGGER.debug("yt-dlp uyarı: %s", message)

    def error(self, message: str) -> None:
        LOGGER.debug("yt-dlp hata: %s", message)


def base_ytdlp_options() -> dict[str, Any]:
    """Tüm yt-dlp çağrılarında kullanılan ortak ayarları döndürür."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "ignoreerrors": False,
        "retries": config.YTDLP_RETRIES,
        "socket_timeout": 30,
        "logger": _YtdlpLogger(),
        "extractor_args": {"youtube": {"player_client": list(config.YTDLP_PLAYER_CLIENTS)}},
    }
    if config.COOKIES_FILE:
        options["cookiefile"] = config.COOKIES_FILE
    if config.COOKIES_FROM_BROWSER:
        options["cookiesfrombrowser"] = (config.COOKIES_FROM_BROWSER,)
    return options


def probe_video_info(url: str) -> dict[str, Any]:
    """
    Videoyu indirmeden meta verisini (başlık, süre, mevcut altyazılar) okur.
    """
    options = base_ytdlp_options()
    options["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise TranscriptError(f"Video bilgileri alınamadı: {_short_error(exc)}") from exc
    except Exception as exc:  # Beklenmeyen hatalar
        raise TranscriptError(f"Video bilgileri alınırken hata: {exc}") from exc

    if not info:
        raise TranscriptError("yt-dlp video bilgisi döndürmedi.")
    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise TranscriptError("Bağlantı bir oynatma listesi ve içi boş.")
        LOGGER.warning("Bağlantı bir oynatma listesi; ilk video kullanılacak.")
        info = entries[0]
    return info


def _short_error(exc: Exception, limit: int = 300) -> str:
    """yt-dlp hata mesajlarını kısaltır."""
    message = str(exc).replace("\n", " ").strip()
    if "Sign in to confirm you" in message:
        return (
            "YouTube bot doğrulaması istiyor. config.py içindeki COOKIES_FILE ayarına "
            "tarayıcıdan aldığınız cookies.txt dosyasının yolunu yazın."
        )
    return message[:limit]


# ---------------------------------------------------------------------------
# Dil seçimi
# ---------------------------------------------------------------------------

def _pick_language(available: Iterable[str], original_language: str | None) -> str | None:
    """
    Mevcut altyazı dilleri arasından en uygun olanı seçer.
    Tercih: config.SUBTITLE_LANG_PREFERENCE > orijinal dil > herhangi bir 'en*' > ilk dil
    """
    langs = list(available)
    if not langs:
        return None

    lower_map = {lang.lower(): lang for lang in langs}

    for preferred in config.SUBTITLE_LANG_PREFERENCE:
        if preferred.lower() in lower_map:
            return lower_map[preferred.lower()]

    if original_language:
        original = original_language.lower()
        if original in lower_map:
            return lower_map[original]
        for lang in langs:
            # "en-US" gibi bölgesel varyantlar
            if lang.lower().split("-")[0] == original.split("-")[0]:
                return lang

    # 'tr-en' gibi çeviri altyazıları elemek için sadece tek parçalı kodlara bak
    simple = [lang for lang in langs if "-" not in lang]
    english = [lang for lang in langs if lang.lower().startswith("en")]
    if english:
        return sorted(english, key=len)[0]
    if simple:
        return simple[0]
    return langs[0]


def _pick_format(entries: list[dict[str, Any]]) -> str:
    """Altyazı formatları arasından tercih edilen ilkini seçer."""
    available = {str(entry.get("ext", "")).lower() for entry in entries}
    for preferred in config.SUBTITLE_FORMAT_PREFERENCE:
        if preferred in available:
            return preferred
    return config.SUBTITLE_FORMAT_PREFERENCE[0]


# ---------------------------------------------------------------------------
# Altyazı dosyası indirme
# ---------------------------------------------------------------------------

def _download_subtitle_file(
    url: str,
    lang: str,
    subtitle_format: str,
    automatic: bool,
    temp_dir: Path,
    video_id: str,
) -> Path:
    """
    Seçilen dildeki altyazı dosyasını temp klasörüne indirir ve yolunu döndürür.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)
    options = base_ytdlp_options()
    options.update(
        {
            "skip_download": True,
            "writesubtitles": not automatic,
            "writeautomaticsub": automatic,
            "subtitleslangs": [lang],
            "subtitlesformat": subtitle_format,
            "outtmpl": {"default": str(temp_dir / "%(id)s.%(ext)s")},
        }
    )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise TranscriptError(f"Altyazı indirilemedi: {_short_error(exc)}") from exc

    # Dosya adı: <id>.<lang>.<ext> - küçük farklılıklara karşı esnek arama yapıyoruz
    patterns = [
        f"{video_id}.{lang}.{subtitle_format}",
        f"{video_id}.{lang}.*",
        f"{video_id}.*",
    ]
    for pattern in patterns:
        matches = sorted(temp_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        matches = [m for m in matches if m.suffix.lower() in (".json3", ".vtt", ".srt", ".ttml", ".srv1")]
        if matches:
            return matches[0]

    raise TranscriptError("Altyazı dosyası indirildi ancak diskte bulunamadı.")


# ---------------------------------------------------------------------------
# Parser'lar
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Altyazı metnini temizler (HTML etiketleri, fazla boşluklar, ses efektleri)."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)          # <c>, <i> gibi etiketler
    text = re.sub(r"\[[^\]]{0,40}\]", " ", text)  # [Müzik], [Applause] gibi notlar
    text = text.replace("\u200b", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_json3(path: Path) -> list[dict[str, Any]]:
    """YouTube json3 altyazı formatını ayrıştırır (en güvenilir zaman damgaları)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptError(f"json3 altyazı okunamadı: {exc}") from exc

    segments: list[dict[str, Any]] = []
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        text = _clean_text("".join(seg.get("utf8", "") for seg in segs))
        if not text:
            continue
        start = float(event.get("tStartMs", 0)) / 1000.0
        duration = float(event.get("dDurationMs") or 0) / 1000.0
        end = start + duration if duration > 0 else start + 2.0
        segments.append({"start": start, "end": end, "text": text})
    return segments


_VTT_TIME = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3})"
)


def _vtt_time_to_seconds(value: str) -> float:
    """'00:01:02.500' veya '01:02.500' biçimini saniyeye çevirir."""
    value = value.replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


def _parse_vtt_or_srt(path: Path) -> list[dict[str, Any]]:
    """WebVTT ve SRT altyazılarını ayrıştırır."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise TranscriptError(f"Altyazı dosyası okunamadı: {exc}") from exc

    segments: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", content)
    for block in blocks:
        match = _VTT_TIME.search(block)
        if not match:
            continue
        start = _vtt_time_to_seconds(match.group("start"))
        end = _vtt_time_to_seconds(match.group("end"))
        lines = block.split("\n")
        text_lines = [line for line in lines if not _VTT_TIME.search(line)]
        text_lines = [line for line in text_lines if not re.match(r"^\s*\d+\s*$", line)]
        text = _clean_text(" ".join(text_lines))
        if text and end > start:
            segments.append({"start": start, "end": end, "text": text})
    return segments


def _parse_subtitle_file(path: Path) -> list[dict[str, Any]]:
    """Uzantıya göre uygun parser'ı çağırır."""
    suffix = path.suffix.lower()
    if suffix == ".json3":
        return _parse_json3(path)
    if suffix in (".vtt", ".srt"):
        return _parse_vtt_or_srt(path)
    # Bilinmeyen formatta yine de VTT/SRT gibi denemeyi tercih ediyoruz
    LOGGER.warning("Bilinmeyen altyazı formatı (%s), VTT/SRT olarak ayrıştırılacak.", suffix)
    return _parse_vtt_or_srt(path)


# ---------------------------------------------------------------------------
# Temizleme / birleştirme
# ---------------------------------------------------------------------------

def _dedupe_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Otomatik altyazılarda sık görülen tekrarları (kayan satırlar) temizler
    ve zaman çakışmalarını düzeltir.
    """
    cleaned: list[dict[str, Any]] = []
    for segment in sorted(segments, key=lambda s: (s["start"], s["end"])):
        text = segment["text"].strip()
        if not text:
            continue
        if cleaned:
            previous = cleaned[-1]
            # Tam tekrar
            if text == previous["text"]:
                previous["end"] = max(previous["end"], segment["end"])
                continue
            # Önceki satırın devamı olan kayan metin
            if text.startswith(previous["text"]) and len(previous["text"]) > 12:
                previous["text"] = text
                previous["end"] = max(previous["end"], segment["end"])
                continue
            if previous["text"].endswith(text) and len(text) > 12:
                continue
            if segment["start"] < previous["end"]:
                previous["end"] = max(previous["start"], segment["start"])
        cleaned.append({"start": segment["start"], "end": max(segment["end"], segment["start"] + 0.4), "text": text})
    return cleaned


_SENTENCE_END = re.compile(r"[.!?…]['\"]?$")


def merge_segments(
    segments: list[dict[str, Any]],
    max_gap: float | None = None,
    max_chars: int | None = None,
    max_duration: float | None = None,
) -> list[dict[str, Any]]:
    """
    Kısa altyazı parçalarını cümle benzeri, okunabilir bloklara birleştirir.
    Cümle sonu noktalama işaretlerinde bölme yapılır.
    """
    max_gap = config.MERGE_MAX_GAP if max_gap is None else max_gap
    max_chars = config.MERGE_MAX_CHARS if max_chars is None else max_chars
    max_duration = config.MERGE_MAX_DURATION if max_duration is None else max_duration

    merged: list[dict[str, Any]] = []
    for segment in segments:
        if not merged:
            merged.append(dict(segment))
            continue

        current = merged[-1]
        gap = segment["start"] - current["end"]
        combined_length = len(current["text"]) + 1 + len(segment["text"])
        combined_duration = segment["end"] - current["start"]

        should_break = (
            gap > max_gap
            or combined_length > max_chars
            or combined_duration > max_duration
            or bool(_SENTENCE_END.search(current["text"]))
        )
        if should_break:
            merged.append(dict(segment))
        else:
            current["text"] = f"{current['text']} {segment['text']}".strip()
            current["end"] = segment["end"]
    return merged


def slice_segments(
    segments: list[dict[str, Any]], start: float, end: float, min_overlap: float = 0.25
) -> list[dict[str, Any]]:
    """
    Verilen zaman aralığına düşen altyazı bloklarını, sınırlara kırpılmış
    şekilde döndürür.
    """
    selected: list[dict[str, Any]] = []
    for segment in segments:
        overlap = min(segment["end"], end) - max(segment["start"], start)
        if overlap <= min_overlap:
            continue
        selected.append(
            {
                "start": max(segment["start"], start),
                "end": min(segment["end"], end),
                "text": segment["text"],
            }
        )
    return selected


def split_for_captions(
    segments: list[dict[str, Any]],
    max_chars: int | None = None,
    max_duration: float | None = None,
) -> list[dict[str, Any]]:
    """
    Uzun altyazı bloklarını ekranda okunabilir küçük parçalara böler.
    Süre, kelime uzunluklarına göre orantılı dağıtılır.
    """
    max_chars = config.CAPTION_MAX_CHARS if max_chars is None else max_chars
    max_duration = config.CAPTION_MAX_DURATION if max_duration is None else max_duration

    result: list[dict[str, Any]] = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        duration = max(0.4, float(segment["end"]) - float(segment["start"]))
        if len(text) <= max_chars and duration <= max_duration:
            result.append({"start": float(segment["start"]), "end": float(segment["end"]), "text": text})
            continue

        words = text.split()
        chunk_count = max(
            1,
            max(
                (len(text) + max_chars - 1) // max_chars,
                int(duration // max_duration) + (1 if duration % max_duration else 0),
            ),
        )
        target = max(1, len(words) // chunk_count)

        chunks: list[list[str]] = []
        current: list[str] = []
        for word in words:
            current.append(word)
            if len(current) >= target and len(chunks) < chunk_count - 1:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        total_chars = sum(len(" ".join(chunk)) for chunk in chunks) or 1
        cursor = float(segment["start"])
        for index, chunk in enumerate(chunks):
            chunk_text = " ".join(chunk)
            share = len(chunk_text) / total_chars
            chunk_end = cursor + duration * share
            if index == len(chunks) - 1:
                chunk_end = float(segment["end"])
            result.append({"start": cursor, "end": max(cursor + 0.4, chunk_end), "text": chunk_text})
            cursor = chunk_end
    return result


def transcript_to_text(segments: list[dict[str, Any]], with_index: bool = False) -> str:
    """
    Transkripti LLM'e gönderilecek zaman damgalı düz metne dönüştürür.
    Örnek satır: [12] [125.4 -> 130.2] Cümle metni
    """
    lines: list[str] = []
    for index, segment in enumerate(segments):
        prefix = f"[{index}] " if with_index else ""
        lines.append(
            f"{prefix}[{segment['start']:.1f} -> {segment['end']:.1f}] {segment['text']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ana giriş noktası
# ---------------------------------------------------------------------------

def fetch_transcript(url: str, temp_dir: Path | None = None) -> dict[str, Any]:
    """
    Videonun zaman damgalı transkriptini indirir ve ayrıştırır.

    Dönüş:
        {
          'video_id': str, 'title': str, 'duration': float, 'url': str,
          'language': str, 'is_automatic': bool,
          'segments': [{'start', 'end', 'text'}],   # birleştirilmiş bloklar
          'raw_segments': [...]                     # ham altyazı parçaları
        }
    """
    temp_dir = Path(temp_dir) if temp_dir else config.TEMP_DIR
    LOGGER.info("Video bilgileri alınıyor...")
    info = probe_video_info(url)

    video_id = info.get("id") or "video"
    title = info.get("title") or "Bilinmeyen Başlık"
    duration = float(info.get("duration") or 0.0)
    original_language = info.get("language")

    LOGGER.info("Video: %s (%s)", title, format_timestamp(duration))

    manual_subs: dict[str, Any] = info.get("subtitles") or {}
    auto_subs: dict[str, Any] = info.get("automatic_captions") or {}

    attempts: list[tuple[str, bool, dict[str, Any]]] = []

    manual_lang = _pick_language(manual_subs.keys(), original_language)
    if manual_lang:
        attempts.append((manual_lang, False, manual_subs[manual_lang]))

    # Otomatik altyazılarda "xx-yy" biçimindeki çeviri sürümlerini eleyip
    # orijinal/İngilizce olanları tercih ediyoruz.
    auto_candidates = {
        lang: entries
        for lang, entries in auto_subs.items()
        if lang.count("-") == 0 or lang.endswith("-orig") or lang.lower() in ("en-us", "en-gb")
    } or auto_subs
    auto_lang = _pick_language(auto_candidates.keys(), original_language)
    if auto_lang:
        attempts.append((auto_lang, True, auto_candidates[auto_lang]))

    if not attempts:
        raise TranscriptError(
            "Bu videoda kullanılabilir altyazı yok. Altyazısı olan bir video deneyin."
        )

    last_error: Exception | None = None
    for lang, automatic, entries in attempts:
        subtitle_format = _pick_format(entries if isinstance(entries, list) else [])
        kind = "otomatik" if automatic else "elle yazılmış"
        LOGGER.info("Altyazı indiriliyor: %s (%s, %s)", lang, kind, subtitle_format)
        try:
            path = _download_subtitle_file(url, lang, subtitle_format, automatic, temp_dir, video_id)
            raw_segments = _parse_subtitle_file(path)
            raw_segments = _dedupe_segments(raw_segments)
            if len(raw_segments) < 3:
                raise TranscriptError(f"Altyazı içeriği çok kısa ({len(raw_segments)} satır).")

            merged = merge_segments(raw_segments)
            LOGGER.info(
                "Transkript hazır: %d ham satır -> %d blok", len(raw_segments), len(merged)
            )
            if duration <= 0 and raw_segments:
                duration = raw_segments[-1]["end"]
            return {
                "video_id": video_id,
                "title": title,
                "duration": duration,
                "url": url,
                "language": lang,
                "is_automatic": automatic,
                "segments": merged,
                "raw_segments": raw_segments,
                "subtitle_file": str(path),
            }
        except Exception as exc:  # Sıradaki adaya geç
            last_error = exc
            LOGGER.warning("%s altyazısı kullanılamadı: %s", lang, exc)

    raise TranscriptError(f"Transkript alınamadı. Son hata: {last_error}")


if __name__ == "__main__":  # Basit manuel test
    import sys

    if len(sys.argv) < 2:
        print("Kullanım: python transcript_fetcher.py <youtube_url>")
        raise SystemExit(1)

    config.ensure_directories()
    data = fetch_transcript(sys.argv[1])
    print(f"\nBaşlık : {data['title']}")
    print(f"Süre   : {format_timestamp(data['duration'])}")
    print(f"Dil    : {data['language']} (otomatik={data['is_automatic']})")
    print(f"Blok   : {len(data['segments'])}\n")
    print(transcript_to_text(data["segments"][:15]))
