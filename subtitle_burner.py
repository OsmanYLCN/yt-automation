"""
subtitle_burner.py
==================
Türkçe altyazıları ASS (Advanced SubStation Alpha) formatında üretir ve
FFmpeg + libass ile videonun üzerine kalıcı olarak gömer.

Altyazı stili (config.py üzerinden ayarlanabilir):
  * Büyük, kalın font; siyah kontur + gölge (her arka planda okunur)
  * Alt-orta konum (Alignment=2, MarginV ile yükseklik ayarı)
  * Beyaz temel renk, konuşulan kelimede SARI vurgu (dinamik his)
  * Otomatik satır kaydırma (en fazla 2-3 satır)

Modlar (config.SUBTITLE_MODE):
  * "highlight" : Her kelime için ayrı olay üretilir; o an konuşulan kelime sarı,
                  diğerleri beyaz kalır. (Viral Shorts görünümü)
  * "karaoke"   : Tek olay içinde ASS \\k etiketleriyle ilerleyen vurgu.
  * "sentence"  : Cümle tek parça belirir, hafif fade efektiyle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import config
from utils import (
    FFmpegError,
    escape_ffmpeg_filter_path,
    format_ass_time,
    get_logger,
    human_size,
    probe_video,
    run_command,
    safe_delete,
)

LOGGER = get_logger("subtitle")


class SubtitleError(RuntimeError):
    """Altyazı üretme/gömme adımı başarısız olduğunda yükseltilir."""


# ---------------------------------------------------------------------------
# Metin yardımcıları
# ---------------------------------------------------------------------------

def _escape_ass_text(text: str) -> str:
    """ASS formatında özel anlam taşıyan karakterleri güvenli hale getirir."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", " ")
        .strip()
    )


def wrap_words(words: list[str], max_chars: int, max_lines: int) -> list[list[str]]:
    """
    Kelimeleri satırlara böler. Her satır en fazla max_chars karakter olur.
    max_lines aşılırsa son satıra taşma yapılır (metin kesilmez).
    """
    lines: list[list[str]] = []
    current: list[str] = []
    current_length = 0

    for word in words:
        extra = len(word) + (1 if current else 0)
        if current and current_length + extra > max_chars and len(lines) < max_lines - 1:
            lines.append(current)
            current = [word]
            current_length = len(word)
        else:
            current.append(word)
            current_length += extra
    if current:
        lines.append(current)
    return lines or [[]]


def _distribute_word_times(words: list[str], start: float, end: float) -> list[tuple[float, float]]:
    """
    Bir altyazı bloğunun süresini kelimelere, karakter uzunluklarına
    orantılı olacak şekilde dağıtır.
    """
    total_chars = sum(max(1, len(word)) for word in words) or 1
    duration = max(0.2, end - start)
    times: list[tuple[float, float]] = []
    cursor = start
    for index, word in enumerate(words):
        share = max(1, len(word)) / total_chars
        word_end = cursor + duration * share
        if index == len(words) - 1:
            word_end = end
        times.append((cursor, max(cursor + 0.08, word_end)))
        cursor = word_end
    return times


# ---------------------------------------------------------------------------
# ASS dosyası üretimi
# ---------------------------------------------------------------------------

def _ass_header(video_width: int, video_height: int) -> str:
    """ASS dosyasının [Script Info] ve [V4+ Styles] bölümlerini üretir."""
    border_style = 3 if config.SUBTITLE_USE_BOX else 1
    back_colour = config.SUBTITLE_BOX_COLOR if config.SUBTITLE_USE_BOX else config.SUBTITLE_COLOR_SHADOW
    bold = -1 if config.SUBTITLE_BOLD else 0

    style = ",".join(
        str(field)
        for field in [
            "Shorts",                          # Name
            config.SUBTITLE_FONT,              # Fontname
            config.SUBTITLE_FONT_SIZE,         # Fontsize
            config.SUBTITLE_COLOR_BASE,        # PrimaryColour
            config.SUBTITLE_COLOR_HIGHLIGHT,   # SecondaryColour (karaoke vurgusu)
            config.SUBTITLE_COLOR_OUTLINE,     # OutlineColour
            back_colour,                       # BackColour
            bold, 0, 0, 0,                     # Bold, Italic, Underline, StrikeOut
            100, 100,                          # ScaleX, ScaleY
            0, 0,                              # Spacing, Angle
            border_style,                      # BorderStyle
            config.SUBTITLE_OUTLINE,           # Outline
            config.SUBTITLE_SHADOW,            # Shadow
            config.SUBTITLE_ALIGNMENT,         # Alignment (2 = alt-orta)
            config.SUBTITLE_MARGIN_H,          # MarginL
            config.SUBTITLE_MARGIN_H,          # MarginR
            config.SUBTITLE_MARGIN_V,          # MarginV
            1,                                 # Encoding
        ]
    )

    return f"""[Script Info]
; YouTube Shorts otomasyonu tarafindan olusturuldu
Title: Shorts Turkce Altyazi
ScriptType: v4.00+
WrapStyle: 2
PlayResX: {video_width}
PlayResY: {video_height}
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _dialogue(start: float, end: float, text: str, layer: int = 0) -> str:
    """Tek bir ASS Dialogue satırı üretir."""
    return (
        f"Dialogue: {layer},{format_ass_time(start)},{format_ass_time(end)},"
        f"Shorts,,0,0,0,,{text}"
    )


def _build_highlight_events(caption: dict[str, Any]) -> list[str]:
    """
    "highlight" modu: cümle ekranda sabit kalır, o an konuşulan kelime
    sarıya döner ve hafifçe büyür.
    """
    words = caption["text"].split()
    if not words:
        return []

    lines = wrap_words(words, config.SUBTITLE_MAX_CHARS_PER_LINE, config.SUBTITLE_MAX_LINES)
    timings = _distribute_word_times(words, float(caption["start"]), float(caption["end"]))

    base_color = config.SUBTITLE_COLOR_BASE.replace("&H00", "&H").rstrip("&") + "&"
    highlight_color = config.SUBTITLE_COLOR_HIGHLIGHT.replace("&H00", "&H").rstrip("&") + "&"

    events: list[str] = []
    global_index = 0
    word_positions: list[tuple[int, int]] = []  # (satır no, satır içi sıra)
    for line_index, line in enumerate(lines):
        for position in range(len(line)):
            word_positions.append((line_index, position))

    for word_index, (start, end) in enumerate(timings):
        if word_index >= len(word_positions):
            break
        active_line, active_position = word_positions[word_index]

        rendered_lines: list[str] = []
        for line_index, line in enumerate(lines):
            parts: list[str] = []
            for position, word in enumerate(line):
                safe_word = _escape_ass_text(word)
                if config.SUBTITLE_UPPERCASE:
                    safe_word = safe_word.upper()
                if line_index == active_line and position == active_position:
                    if config.SUBTITLE_POP_EFFECT:
                        parts.append(
                            f"{{\\c{highlight_color}\\fscx112\\fscy112}}{safe_word}"
                            f"{{\\c{base_color}\\fscx100\\fscy100}}"
                        )
                    else:
                        parts.append(f"{{\\c{highlight_color}}}{safe_word}{{\\c{base_color}}}")
                else:
                    parts.append(safe_word)
            rendered_lines.append(" ".join(parts))

        text = "\\N".join(rendered_lines)
        events.append(_dialogue(start, end, f"{{\\c{base_color}}}{text}"))
        global_index += 1

    return events


def _build_karaoke_events(caption: dict[str, Any]) -> list[str]:
    """
    "karaoke" modu: tek olay, ASS \\k etiketleriyle kelimeler sırayla
    ikincil renkten (sarı) birincil renge geçer.
    """
    words = caption["text"].split()
    if not words:
        return []

    start = float(caption["start"])
    end = float(caption["end"])
    timings = _distribute_word_times(words, start, end)
    lines = wrap_words(words, config.SUBTITLE_MAX_CHARS_PER_LINE, config.SUBTITLE_MAX_LINES)

    highlight_color = config.SUBTITLE_COLOR_HIGHLIGHT.replace("&H00", "&H").rstrip("&") + "&"

    word_index = 0
    rendered_lines: list[str] = []
    for line in lines:
        parts: list[str] = []
        for word in line:
            word_start, word_end = timings[min(word_index, len(timings) - 1)]
            centis = max(5, int(round((word_end - word_start) * 100)))
            safe_word = _escape_ass_text(word)
            if config.SUBTITLE_UPPERCASE:
                safe_word = safe_word.upper()
            parts.append(f"{{\\k{centis}}}{safe_word}")
            word_index += 1
        rendered_lines.append(" ".join(parts))

    text = "\\N".join(rendered_lines)
    return [_dialogue(start, end, f"{{\\2c{highlight_color}}}{text}")]


def _build_sentence_events(caption: dict[str, Any]) -> list[str]:
    """"sentence" modu: cümle tek parça, kısa fade efektiyle belirir."""
    words = caption["text"].split()
    if not words:
        return []
    lines = wrap_words(words, config.SUBTITLE_MAX_CHARS_PER_LINE, config.SUBTITLE_MAX_LINES)
    rendered = "\\N".join(
        " ".join(
            (_escape_ass_text(word).upper() if config.SUBTITLE_UPPERCASE else _escape_ass_text(word))
            for word in line
        )
        for line in lines
    )
    return [_dialogue(float(caption["start"]), float(caption["end"]), f"{{\\fad(120,120)}}{rendered}")]


def generate_ass(
    captions: Iterable[dict[str, Any]],
    output_path: str | Path,
    video_width: int | None = None,
    video_height: int | None = None,
    mode: str | None = None,
    clip_duration: float | None = None,
) -> Path:
    """
    Altyazı bloklarından ASS dosyası üretir.

    Args:
        captions: [{'start': sn, 'end': sn, 'text': 'Türkçe metin'}, ...]
                  Zamanlar klibin başlangıcına GÖRELİ olmalıdır.
        output_path: Yazılacak .ass dosyası
        video_width/height: Çözünürlük (varsayılan: config hedef çözünürlüğü)
        mode: highlight | karaoke | sentence
        clip_duration: Verilirse altyazılar bu süreye kırpılır
    """
    video_width = video_width or config.TARGET_WIDTH
    video_height = video_height or config.TARGET_HEIGHT
    mode = (mode or config.SUBTITLE_MODE).lower()

    builders = {
        "highlight": _build_highlight_events,
        "karaoke": _build_karaoke_events,
        "sentence": _build_sentence_events,
    }
    if mode not in builders:
        LOGGER.warning("Bilinmeyen altyazı modu '%s'; 'highlight' kullanılacak.", mode)
        mode = "highlight"
    builder = builders[mode]

    prepared: list[dict[str, Any]] = []
    for caption in captions:
        try:
            start = max(0.0, float(caption["start"]))
            end = float(caption["end"])
        except (KeyError, TypeError, ValueError) as exc:
            LOGGER.warning("Altyazı bloğu atlandı (geçersiz zaman): %s", exc)
            continue
        text = str(caption.get("text") or "").strip()
        if not text:
            continue
        if clip_duration:
            end = min(end, float(clip_duration))
        if end - start < 0.2:
            continue
        prepared.append({"start": start, "end": end, "text": text})

    if not prepared:
        raise SubtitleError("Geçerli altyazı bloğu bulunamadı; ASS dosyası üretilemedi.")

    # Üst üste binen blokları düzelt (bir sonraki başlarken öncekini kapat)
    for index in range(len(prepared) - 1):
        if prepared[index]["end"] > prepared[index + 1]["start"]:
            prepared[index]["end"] = max(
                prepared[index]["start"] + 0.2, prepared[index + 1]["start"] - 0.02
            )

    events: list[str] = []
    for caption in prepared:
        try:
            events.extend(builder(caption))
        except Exception as exc:
            LOGGER.warning("Altyazı bloğu işlenemedi ('%s'): %s", caption["text"][:40], exc)

    if not events:
        raise SubtitleError("ASS olayları (Dialogue) üretilemedi.")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = _ass_header(video_width, video_height) + "\n".join(events) + "\n"
        output.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise SubtitleError(f"ASS dosyası yazılamadı: {exc}") from exc

    LOGGER.info(
        "ASS altyazı üretildi: %s (%d blok, %d olay, mod=%s)",
        output.name, len(prepared), len(events), mode,
    )
    return output


# ---------------------------------------------------------------------------
# Videoya gömme
# ---------------------------------------------------------------------------

def burn_subtitles(
    video_path: str | Path,
    ass_path: str | Path,
    output_path: str | Path,
    fonts_dir: str | None = None,
) -> Path:
    """
    ASS altyazıyı FFmpeg (libass) ile videoya kalıcı olarak gömer.

    Returns:
        Çıktı videosunun yolu
    """
    video = Path(video_path)
    subtitle = Path(ass_path)
    output = Path(output_path)

    if not video.exists():
        raise SubtitleError(f"Video bulunamadı: {video}")
    if not subtitle.exists():
        raise SubtitleError(f"ASS dosyası bulunamadı: {subtitle}")

    output.parent.mkdir(parents=True, exist_ok=True)
    safe_delete(output)

    try:
        info = probe_video(video)
    except FFmpegError as exc:
        raise SubtitleError(f"Video okunamadı: {exc}") from exc

    safe_subtitle = escape_ffmpeg_filter_path(subtitle)
    ass_filter = f"ass=filename='{safe_subtitle}'"
    if fonts_dir:
        safe_fonts = escape_ffmpeg_filter_path(fonts_dir)
        ass_filter += f":fontsdir='{safe_fonts}'"

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-vf", ass_filter,
        "-c:v", config.VIDEO_CODEC,
        "-crf", str(config.VIDEO_CRF),
        "-preset", config.VIDEO_PRESET,
        "-pix_fmt", config.PIXEL_FORMAT,
    ]
    if info["has_audio"]:
        cmd += ["-c:a", config.AUDIO_CODEC, "-b:a", config.AUDIO_BITRATE]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(output)]

    LOGGER.info("Altyazılar videoya gömülüyor...")
    try:
        run_command(cmd, description="FFmpeg altyazı gömme")
    except FFmpegError as exc:
        raise SubtitleError(f"Altyazı gömme başarısız: {exc}") from exc

    if not output.exists() or output.stat().st_size == 0:
        raise SubtitleError("Altyazı gömme sonrası çıktı dosyası oluşmadı.")

    result = probe_video(output)
    LOGGER.info(
        "Altyazılı video hazır: %s (%dx%d, %.1f sn, %s)",
        output.name, result["width"], result["height"], result["duration"],
        human_size(result["size_bytes"]),
    )
    return output


def create_and_burn(
    video_path: str | Path,
    captions: list[dict[str, Any]],
    output_path: str | Path,
    ass_path: str | Path | None = None,
    mode: str | None = None,
) -> Path:
    """
    Kolaylık fonksiyonu: ASS üret + videoya göm.
    Altyazı üretilemezse video altyazısız olarak kopyalanır (işlem durmaz).
    """
    video = Path(video_path)
    info = probe_video(video)
    ass_file = Path(ass_path) if ass_path else config.TEMP_DIR / f"{video.stem}.ass"

    try:
        generate_ass(
            captions,
            ass_file,
            video_width=info["width"],
            video_height=info["height"],
            mode=mode,
            clip_duration=info["duration"],
        )
    except SubtitleError as exc:
        LOGGER.error("Altyazı üretilemedi (%s); video altyazısız kaydedilecek.", exc)
        output = Path(output_path)
        run_command(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-c", "copy", "-movflags", "+faststart", str(output)],
            description="Altyazısız kopyalama",
        )
        return output

    return burn_subtitles(video, ass_file, output_path)


if __name__ == "__main__":  # Basit manuel test
    import sys

    if len(sys.argv) < 2:
        print("Kullanım: python subtitle_burner.py <video_yolu>")
        raise SystemExit(1)

    config.ensure_directories()
    demo_captions = [
        {"start": 0.2, "end": 3.0, "text": "Hayatını değiştirecek tek bir karar var"},
        {"start": 3.1, "end": 6.4, "text": "Bugün başlamazsan yarın da başlamayacaksın"},
        {"start": 6.5, "end": 10.0, "text": "Şimdi harekete geç ve asla vazgeçme"},
    ]
    demo_out = config.OUTPUT_DIR / "altyazi_testi.mp4"
    print(create_and_burn(sys.argv[1], demo_captions, demo_out))
