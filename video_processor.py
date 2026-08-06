"""
video_processor.py
==================
İndirilen yatay klibi 9:16 dikey Shorts formatına (1080x1920) dönüştürür.

ÖNEMLİ: Görüntü asla yatay/dikey olarak sıkıştırılıp gerilmez (en-boy oranı
korunur). İki strateji kullanılır:

  * center_crop : Geniş (yatay) videolarda merkezden 9:16 alan kırpılır.
                  Konuşmacı genelde merkezde/ortada durduğu için framing korunur.
                  VERTICAL_FOCUS / HORIZONTAL_FOCUS ile odak kaydırılabilir.
  * blur_pad    : Zaten dikey ya da kareye yakın videolarda görüntünün tamamı
                  korunur; üst/alt boşluk bulanıklaştırılmış arka planla doldurulur.

config.CONVERT_MODE = "auto" ise uygun strateji otomatik seçilir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from utils import (
    FFmpegError,
    get_logger,
    human_size,
    probe_video,
    run_command,
    safe_delete,
)

LOGGER = get_logger("processor")


class ProcessingError(RuntimeError):
    """Video dönüştürme adımı başarısız olduğunda yükseltilir."""


def _even(value: float) -> int:
    """H.264 için boyutların çift sayı olması gerekir."""
    number = int(round(value))
    return number - (number % 2)


def build_vertical_filter(
    src_width: int,
    src_height: int,
    mode: str | None = None,
    target_width: int | None = None,
    target_height: int | None = None,
) -> tuple[str, str]:
    """
    Kaynak çözünürlüğe göre uygun FFmpeg filtre zincirini üretir.

    Returns:
        (filter_complex_string, kullanılan_mod)
    """
    target_width = target_width or config.TARGET_WIDTH
    target_height = target_height or config.TARGET_HEIGHT
    mode = (mode or config.CONVERT_MODE).lower()

    if src_width <= 0 or src_height <= 0:
        raise ProcessingError(f"Geçersiz kaynak çözünürlük: {src_width}x{src_height}")

    target_aspect = target_width / target_height          # 0.5625
    source_aspect = src_width / src_height

    if mode == "auto":
        # Kaynak 9:16'dan genişse kırpmak en iyi sonucu verir,
        # değilse görüntüyü kaybetmemek için bulanık arka plan kullanılır.
        mode = "center_crop" if source_aspect > target_aspect * 1.02 else "blur_pad"
        LOGGER.info("Otomatik mod seçimi: %s (kaynak oran=%.3f)", mode, source_aspect)

    fps_filter = f"fps={config.TARGET_FPS}"

    if mode == "center_crop":
        if source_aspect > target_aspect:
            # Yatay video: genişlikten kırp
            crop_w = _even(min(src_width, src_height * target_aspect))
            crop_h = _even(src_height)
            offset_x = _even(max(0, (src_width - crop_w) * config.HORIZONTAL_FOCUS))
            offset_y = 0
        else:
            # Dikey/kare video: yükseklikten kırp
            crop_w = _even(src_width)
            crop_h = _even(min(src_height, src_width / target_aspect))
            offset_x = 0
            offset_y = _even(max(0, (src_height - crop_h) * config.VERTICAL_FOCUS))

        chain = (
            f"[0:v]{fps_filter},"
            f"crop={crop_w}:{crop_h}:{offset_x}:{offset_y},"
            f"scale={target_width}:{target_height}:flags=lanczos,"
            f"setsar=1[vout]"
        )
        LOGGER.info(
            "Merkez kırpma: %dx%d -> crop %dx%d @(%d,%d) -> %dx%d",
            src_width, src_height, crop_w, crop_h, offset_x, offset_y,
            target_width, target_height,
        )
        return chain, mode

    if mode == "blur_pad":
        chain = (
            f"[0:v]{fps_filter},split=2[bg][fg];"
            f"[bg]scale={target_width}:{target_height}:force_original_aspect_ratio=increase:flags=fast_bilinear,"
            f"crop={target_width}:{target_height},"
            f"gblur=sigma={config.BLUR_BACKGROUND_SIGMA},"
            f"eq=brightness=-0.08:saturation=1.1[bgout];"
            f"[fg]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease:flags=lanczos[fgout];"
            f"[bgout][fgout]overlay=(W-w)/2:(H-h)/2:format=auto,"
            f"setsar=1[vout]"
        )
        LOGGER.info(
            "Bulanık arka plan: %dx%d -> %dx%d (görüntünün tamamı korunuyor)",
            src_width, src_height, target_width, target_height,
        )
        return chain, mode

    raise ProcessingError(
        f"Bilinmeyen dönüştürme modu: {mode}. "
        "Geçerli değerler: auto, center_crop, blur_pad"
    )


def to_vertical(
    input_path: str | Path,
    output_path: str | Path | None = None,
    mode: str | None = None,
) -> Path:
    """
    Videoyu 9:16 (1080x1920) dikey formata dönüştürür.

    Args:
        input_path: Kaynak video
        output_path: Çıktı yolu (verilmezse temp klasörüne '<ad>_vertical.mp4')
        mode: "auto" | "center_crop" | "blur_pad" (verilmezse config kullanılır)

    Returns:
        Dönüştürülmüş videonun yolu
    """
    source = Path(input_path)
    if not source.exists():
        raise ProcessingError(f"Kaynak video bulunamadı: {source}")

    try:
        info = probe_video(source)
    except FFmpegError as exc:
        raise ProcessingError(f"Kaynak video okunamadı: {exc}") from exc

    output = Path(output_path) if output_path else config.TEMP_DIR / f"{source.stem}_vertical.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    safe_delete(output)

    filter_complex, used_mode = build_vertical_filter(info["width"], info["height"], mode=mode)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", config.AUDIO_CODEC, "-b:a", config.AUDIO_BITRATE, "-ac", "2"]
    else:
        LOGGER.warning("Kaynakta ses akışı yok; sessiz video üretilecek.")
    cmd += [
        "-c:v", config.VIDEO_CODEC,
        "-crf", str(config.VIDEO_CRF),
        "-preset", config.INTERMEDIATE_PRESET,
        "-pix_fmt", config.PIXEL_FORMAT,
        "-movflags", "+faststart",
        str(output),
    ]

    LOGGER.info("9:16 dönüşümü başlıyor (mod=%s)...", used_mode)
    try:
        run_command(cmd, description="FFmpeg 9:16 dönüşümü")
    except FFmpegError as exc:
        raise ProcessingError(f"9:16 dönüşümü başarısız: {exc}") from exc

    if not output.exists() or output.stat().st_size == 0:
        raise ProcessingError("Dönüştürme sonrası çıktı dosyası oluşmadı.")

    result = probe_video(output)
    if (result["width"], result["height"]) != (config.TARGET_WIDTH, config.TARGET_HEIGHT):
        LOGGER.warning(
            "Beklenen çözünürlük %dx%d, oluşan %dx%d",
            config.TARGET_WIDTH, config.TARGET_HEIGHT, result["width"], result["height"],
        )
    LOGGER.info(
        "Dikey video hazır: %dx%d | %.1f sn | %s",
        result["width"], result["height"], result["duration"], human_size(result["size_bytes"]),
    )
    return output


def analyze_source(input_path: str | Path) -> dict[str, Any]:
    """Kaynak videonun bilgilerini ve önerilen dönüşüm modunu döndürür."""
    info = probe_video(input_path)
    _, mode = build_vertical_filter(info["width"], info["height"])
    info["recommended_mode"] = mode
    return info


if __name__ == "__main__":  # Basit manuel test
    import sys

    if len(sys.argv) < 2:
        print("Kullanım: python video_processor.py <video_yolu> [mod]")
        raise SystemExit(1)

    config.ensure_directories()
    result_path = to_vertical(sys.argv[1], mode=sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"Dikey video: {result_path}")
