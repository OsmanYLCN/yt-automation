"""
video_downloader.py
===================
YouTube videosunun SADECE belirtilen zaman aralığını indirir.

yt-dlp'nin `download_ranges` özelliği kullanılır: bu sayede videonun tamamı
indirilmeden yalnızca gereken saniyeler çekilir (bant genişliği ve süre tasarrufu).
`force_keyframes_at_cuts=True` ile kesim noktaları kare hassasiyetinde olur.

Aralık indirme başarısız olursa (bazı formatlarda desteklenmez) yedek olarak
tüm video indirilip FFmpeg ile kesilebilir (config.ALLOW_FULL_DOWNLOAD_FALLBACK).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import download_range_func

import config
from transcript_fetcher import base_ytdlp_options
from utils import (
    FFmpegError,
    format_timestamp,
    get_logger,
    human_size,
    probe_video,
    run_command,
    safe_delete,
)

LOGGER = get_logger("downloader")


class DownloadError(RuntimeError):
    """Video indirme başarısız olduğunda yükseltilir."""


def _find_downloaded_file(temp_dir: Path, stem: str) -> Path | None:
    """Belirtilen ön ada sahip indirilmiş video dosyasını bulur."""
    candidates = [
        path
        for path in temp_dir.glob(f"{stem}*")
        if path.is_file()
        and path.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv")
        and not path.name.endswith(".part")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_size)


def _download(url: str, options: dict[str, Any], description: str) -> None:
    """yt-dlp indirme çağrısını sarmalar ve hataları dönüştürür."""
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).replace("\n", " ")
        if "Sign in to confirm you" in message:
            message = (
                "YouTube bot doğrulaması istedi. config.py içindeki COOKIES_FILE "
                "ayarına cookies.txt yolunu ekleyin."
            )
        raise DownloadError(f"{description} başarısız: {message[:300]}") from exc
    except Exception as exc:
        raise DownloadError(f"{description} sırasında beklenmeyen hata: {exc}") from exc


def download_clip(
    url: str,
    start_time: float,
    end_time: float,
    output_path: str | Path | None = None,
    temp_dir: Path | None = None,
    video_id: str = "clip",
) -> Path:
    """
    Videonun [start_time, end_time] aralığını indirir.

    Args:
        url: YouTube video bağlantısı
        start_time: Başlangıç saniyesi
        end_time: Bitiş saniyesi
        output_path: İstenen çıktı yolu (uzantı yt-dlp tarafından belirlenebilir)
        temp_dir: Ara dosyaların yazılacağı klasör
        video_id: Dosya adı ön eki

    Returns:
        İndirilen klip dosyasının yolu (Path)
    """
    if end_time <= start_time:
        raise DownloadError(f"Geçersiz aralık: {start_time} -> {end_time}")

    temp_dir = Path(temp_dir) if temp_dir else config.TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(output_path).stem if output_path else f"{video_id}_raw"
    target_duration = end_time - start_time

    LOGGER.info(
        "Klip indiriliyor: %s -> %s (%.1f sn)",
        format_timestamp(start_time), format_timestamp(end_time), target_duration,
    )

    # 1) Yalnızca istenen aralığı indirmeyi dene
    options = base_ytdlp_options()
    options.update(
        {
            "format": config.YTDLP_FORMAT,
            "outtmpl": {"default": str(temp_dir / f"{stem}.%(ext)s")},
            "merge_output_format": "mp4",
            "download_ranges": download_range_func(None, [(start_time, end_time)]),
            "force_keyframes_at_cuts": True,
            "overwrites": True,
            "quiet": True,
            "noprogress": True,
        }
    )

    downloaded: Path | None = None
    try:
        _download(url, options, "Aralık indirme")
        downloaded = _find_downloaded_file(temp_dir, stem)
        if downloaded is None:
            raise DownloadError("Aralık indirme sonrası dosya bulunamadı.")
        LOGGER.info("Aralık indirme tamam: %s (%s)", downloaded.name, human_size(downloaded.stat().st_size))
    except DownloadError as exc:
        LOGGER.warning("Aralık indirme başarısız: %s", exc)
        if not config.ALLOW_FULL_DOWNLOAD_FALLBACK:
            raise
        downloaded = _download_full_and_cut(url, start_time, end_time, temp_dir, stem)

    # İndirilen dosyayı doğrula
    try:
        info = probe_video(downloaded)
    except FFmpegError as exc:
        raise DownloadError(f"İndirilen klip doğrulanamadı: {exc}") from exc

    if info["duration"] <= 0.5:
        raise DownloadError(f"İndirilen klip boş görünüyor (süre={info['duration']:.2f} sn).")

    # Süre beklenenden çok farklıysa FFmpeg ile yeniden kes
    if info["duration"] > target_duration + 3.0:
        LOGGER.warning(
            "Klip süresi beklenenden uzun (%.1f sn > %.1f sn), yeniden kesiliyor.",
            info["duration"], target_duration,
        )
        downloaded = _trim_with_ffmpeg(downloaded, 0.0, target_duration, temp_dir, f"{stem}_trim")
        info = probe_video(downloaded)

    LOGGER.info(
        "Klip hazır: %dx%d | %.1f sn | %s ses",
        info["width"], info["height"], info["duration"],
        "var" if info["has_audio"] else "YOK",
    )
    return downloaded


def _download_full_and_cut(
    url: str, start_time: float, end_time: float, temp_dir: Path, stem: str
) -> Path:
    """Yedek yol: videonun tamamını indirip FFmpeg ile istenen aralığı keser."""
    LOGGER.info("Yedek yönteme geçiliyor: video tamamı indirilip kesilecek.")
    full_stem = f"{stem}_full"
    options = base_ytdlp_options()
    options.update(
        {
            "format": config.YTDLP_FORMAT,
            "outtmpl": {"default": str(temp_dir / f"{full_stem}.%(ext)s")},
            "merge_output_format": "mp4",
            "overwrites": True,
            "quiet": True,
            "noprogress": True,
        }
    )
    _download(url, options, "Tam video indirme")

    full_path = _find_downloaded_file(temp_dir, full_stem)
    if full_path is None:
        raise DownloadError("Tam video indirildi ancak dosya bulunamadı.")

    try:
        clip_path = _trim_with_ffmpeg(full_path, start_time, end_time - start_time, temp_dir, stem)
    finally:
        safe_delete(full_path)  # Büyük dosyayı hemen temizle
    return clip_path


def _trim_with_ffmpeg(
    source: Path, start: float, duration: float, temp_dir: Path, stem: str
) -> Path:
    """FFmpeg ile yeniden kodlayarak kare hassasiyetinde kesim yapar."""
    output = temp_dir / f"{stem}.mp4"
    safe_delete(output)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-c:v", config.VIDEO_CODEC,
        "-crf", str(config.VIDEO_CRF),
        "-preset", config.INTERMEDIATE_PRESET,
        "-pix_fmt", config.PIXEL_FORMAT,
        "-c:a", config.AUDIO_CODEC,
        "-b:a", config.AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(output),
    ]
    try:
        run_command(cmd, description="FFmpeg kesme")
    except FFmpegError as exc:
        raise DownloadError(f"Klip kesilemedi: {exc}") from exc

    if not output.exists() or output.stat().st_size == 0:
        raise DownloadError("FFmpeg kesme sonrası çıktı dosyası oluşmadı.")
    return output


if __name__ == "__main__":  # Basit manuel test
    import sys

    if len(sys.argv) < 4:
        print("Kullanım: python video_downloader.py <youtube_url> <başlangıç_sn> <bitiş_sn>")
        raise SystemExit(1)

    config.ensure_directories()
    path = download_clip(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))
    print(f"İndirilen klip: {path}")
