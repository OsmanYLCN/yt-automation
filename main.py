#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import config
import llm_analyzer
import subtitle_burner
import transcript_fetcher
import video_downloader
import video_processor
from utils import (
    check_dependencies,
    format_duration,
    format_timestamp,
    get_logger,
    human_size,
    safe_delete,
    sanitize_filename,
    setup_logging,
)

LOGGER = get_logger("main")

BANNER = r"""
====
   YouTube Shorts Otomasyonu  |  Abacus AI RouteLLM destekli
   Transkript -> Çoklu Klip Analizi -> 9:16 -> Türkçe Altyazı
====
"""


# ----
# Yardımcılar
# ----

def _normalize_url(raw: str) -> str | None:
    """Girilen metni geçerli bir YouTube bağlantısına çevirir."""
    url = (raw or "").strip().strip('"').strip("'")
    if not url or url.startswith("#"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        # Sadece video kimliği girilmiş olabilir
        if len(url) == 11 and "/" not in url:
            return f"https://www.youtube.com/watch?v={url}"
        url = "https://" + url
    if not any(domain in url for domain in ("youtube.com", "youtu.be")):
        LOGGER.warning("YouTube bağlantısı gibi görünmüyor, yine de denenecek: %s", url)
    return url


def collect_urls(args: argparse.Namespace) -> list[str]:
    """Bağlantıları komut satırından, dosyadan veya etkileşimli girişten toplar."""
    raw_urls: list[str] = list(args.urls or [])

    if args.urls_file:
        path = Path(args.urls_file)
        if not path.exists():
            LOGGER.error("Bağlantı dosyası bulunamadı: %s", path)
        else:
            try:
                raw_urls.extend(path.read_text(encoding="utf-8").splitlines())
            except OSError as exc:
                LOGGER.error("Bağlantı dosyası okunamadı: %s", exc)

    if not raw_urls:
        print("\nYouTube bağlantılarını girin (her satıra bir tane).")
        print("Bitirmek için boş satırda Enter'a basın veya Ctrl+D kullanın.\n")
        try:
            while True:
                line = input(f"  Bağlantı {len(raw_urls) + 1}: ").strip()
                if not line:
                    break
                raw_urls.append(line)
        except (EOFError, KeyboardInterrupt):
            print()

    urls: list[str] = []
    for raw in raw_urls:
        url = _normalize_url(raw)
        if url and url not in urls:
            urls.append(url)
    return urls


def _parse_manual_clip(value: str) -> tuple[float, float]:
    """'120-165' veya '2:00-2:45' biçimini (başlangıç, bitiş) saniyeye çevirir."""
    from utils import parse_timestamp

    separator = "-" if "-" in value else ("," if "," in value else None)
    if not separator:
        raise ValueError("Aralık 'başlangıç-bitiş' biçiminde olmalı (örn. 120-165).")
    start_text, _, end_text = value.partition(separator)
    start = parse_timestamp(start_text)
    end = parse_timestamp(end_text)
    if end <= start:
        raise ValueError("Bitiş zamanı başlangıçtan büyük olmalı.")
    return start, end


def _unique_path(directory: Path, stem: str, suffix: str = ".mp4") -> Path:
    """Var olan dosyaların üzerine yazmamak için benzersiz bir yol üretir."""
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def _save_metadata(output_video: Path, payload: dict[str, Any]) -> Path | None:
    """Klip hakkındaki bilgileri (başlık, hashtag, gerekçe) JSON olarak kaydeder."""
    meta_path = output_video.with_suffix(".json")
    try:
        meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return meta_path
    except OSError as exc:
        LOGGER.warning("Meta veri dosyası yazılamadı: %s", exc)
        return None


# ----
# Tek kesit işleme
# ----

def _process_single_clip(
    url: str,
    transcript: dict[str, Any],
    clip: dict[str, Any],
    clip_index: int,
    total_clips: int,
    output_dir: Path,
    args: argparse.Namespace,
    raw_video: Path | None = None,
) -> dict[str, Any]:
    """
    Tek bir kesiti işler: ham dosyadan keser (ya da indirir), 9:16 yapar,
    altyazı gömer ve kaydeder.

    Args:
        raw_video: Önceden indirilmiş ham video dosyası (varsa kesim yapılır,
                   yoksa tam indirme ile devam edilir).

    Returns:
        {'clip_index', 'status': 'ok'|'error', 'output'?, 'error'?, 'clip'?}
    """
    clip_label = f"clip{clip_index}"
    video_id = transcript["video_id"]
    temp_files: list[Path] = []

    try:
        # --- a) Klip al: ham dosyadan kes ya da indir ----
        LOGGER.info(
            "  [Kesit %d/%d] Aralık alınıyor: %s -> %s (%.1f sn)",
            clip_index, total_clips,
            format_timestamp(clip["start_time"]),
            format_timestamp(clip["end_time"]),
            clip["duration"],
        )
        if raw_video is not None:
            # Ham dosya mevcut: sadece FFmpeg ile kes (ağ erişimi yok)
            raw_clip = video_downloader.cut_clip_from_file(
                raw_video,
                clip["start_time"],
                clip["end_time"],
                video_id=f"{video_id}_{clip_label}",
            )
        else:
            # Ham dosya yok (fallback): eski yöntemle tam indir + kes
            raw_clip = video_downloader.download_clip(
                url,
                clip["start_time"],
                clip["end_time"],
                video_id=f"{video_id}_{clip_label}",
            )
        temp_files.append(raw_clip)

        # --- b) 9:16 dikey formata çevir ----
        LOGGER.info("  [Kesit %d/%d] 9:16 (1080x1920) dikey formata dönüştürülüyor...", clip_index, total_clips)
        vertical = video_processor.to_vertical(raw_clip, mode=args.convert_mode)
        temp_files.append(vertical)

        # --- c) Türkçe altyazıyı göm ----
        LOGGER.info("  [Kesit %d/%d] Türkçe altyazılar gömülüyor...", clip_index, total_clips)
        slug = sanitize_filename(clip.get("title_tr") or transcript.get("title") or "shorts")
        output_path = _unique_path(output_dir, f"{slug}_{video_id}_{clip_label}")

        if clip.get("captions"):
            ass_path = config.TEMP_DIR / f"{video_id}_{clip_label}_tr.ass"
            final_video = subtitle_burner.create_and_burn(
                vertical,
                clip["captions"],
                output_path,
                ass_path=ass_path,
                mode=args.subtitle_mode,
            )
            temp_files.append(ass_path)
        else:
            final_video = Path(output_path)
            vertical.replace(final_video)

        # --- Meta veri ----
        metadata = {
            "kaynak_url": url,
            "video_basligi": transcript.get("title"),
            "video_id": video_id,
            "kesit_no": clip_index,
            "toplam_kesit": total_clips,
            "transkript_dili": transcript.get("language"),
            "otomatik_altyazi": transcript.get("is_automatic"),
            "klip_baslangic": clip["start_time"],
            "klip_bitis": clip["end_time"],
            "klip_suresi_sn": clip["duration"],
            "secim_kaynagi": clip.get("source"),
            "turkce_ceviri_yapildi": clip.get("translated"),
            "shorts_basligi": clip.get("title_tr"),
            "hook": clip.get("hook"),
            "gerekce": clip.get("reason"),
            "nis": clip.get("niche"),
            "hashtagler": clip.get("hashtags"),
            "viral_puani": clip.get("score"),
            "altyazi_blok_sayisi": len(clip.get("captions") or []),
            "altyazilar": clip.get("captions"),
            "cikti_dosyasi": str(final_video),
        }
        meta_path = _save_metadata(final_video, metadata)

        LOGGER.info(
            "  [Kesit %d/%d] BAŞARILI | %s (%s)",
            clip_index, total_clips, final_video.name,
            human_size(final_video.stat().st_size),
        )
        return {
            "clip_index": clip_index,
            "status": "ok",
            "output": final_video,
            "metadata": meta_path,
            "clip": clip,
        }

    except Exception as exc:
        LOGGER.error(
            "  [Kesit %d/%d] HATA: %s", clip_index, total_clips, exc
        )
        LOGGER.debug("Ayrıntılı hata:\n%s", traceback.format_exc())
        return {
            "clip_index": clip_index,
            "status": "error",
            "error": str(exc),
            "clip": clip,
        }
    finally:
        if config.CLEANUP_TEMP and not args.keep_temp:
            for path in temp_files:
                safe_delete(path)


# ----
# Hat (pipeline) – Tek URL için
# ----

def process_url(
    url: str,
    index: int,
    total: int,
    analyzer: llm_analyzer.LLMAnalyzer | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """
    Tek bir YouTube bağlantısı için tüm hattı çalıştırır.
    Artık dinamik sayıda kesit üretir.

    Returns:
        {'url', 'status': 'ok'|'error', 'clips_results': [...], 'error'?}
    """
    started = time.time()
    LOGGER.info("=" * 62)
    LOGGER.info("[%d/%d] İşleniyor: %s", index, total, url)
    LOGGER.info("=" * 62)

    temp_files: list[Path] = []
    output_dir = Path(args.output_dir) if args.output_dir else config.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # --- 1) Transkript ----
        LOGGER.info("ADIM 1/3 | Transkript indiriliyor...")
        transcript = transcript_fetcher.fetch_transcript(url)
        if transcript.get("subtitle_file"):
            temp_files.append(Path(transcript["subtitle_file"]))

        # --- 2) Klip seçimi + çeviri ----
        if args.manual_clip:
            start, end = _parse_manual_clip(args.manual_clip)
            LOGGER.info(
                "ADIM 2/3 | Elle belirtilen aralık kullanılıyor: %s -> %s",
                format_timestamp(start), format_timestamp(end),
            )
            captions = llm_analyzer.LLMAnalyzer.build_caption_blocks(transcript, start, end)
            translated = False
            if analyzer is not None and captions:
                try:
                    captions = analyzer.translate_captions(
                        captions, context=transcript.get("title", "")
                    )
                    translated = True
                except llm_analyzer.LLMError as exc:
                    LOGGER.error("Çeviri yapılamadı, orijinal metin kullanılacak: %s", exc)
            for caption in captions:
                caption.setdefault("original", caption["text"])
            plans = [{
                "start_time": round(start, 2),
                "end_time": round(end, 2),
                "duration": round(end - start, 2),
                "title_tr": transcript.get("title", "")[:60],
                "reason": "Kullanıcı tarafından elle belirtilen aralık.",
                "niche": "manuel",
                "hashtags": ["#shorts"],
                "hook": "",
                "score": None,
                "source": "manual",
                "translated": translated,
                "captions": llm_analyzer.LLMAnalyzer._to_relative(captions, start, end - start),
            }]
        elif analyzer is not None:
            LOGGER.info("ADIM 2/3 | RouteLLM ile kesitler seçiliyor ve çevriliyor...")
            try:
                plans = analyzer.analyze(transcript)
            except llm_analyzer.LLMError as exc:
                if not config.ENABLE_HEURISTIC_FALLBACK:
                    raise
                LOGGER.error("LLM analizi başarısız: %s", exc)
                plans = llm_analyzer.heuristic_clips(transcript)
        else:
            LOGGER.info("ADIM 2/3 | LLM devre dışı; sezgisel klip seçimi kullanılıyor...")
            plans = llm_analyzer.heuristic_clips(transcript)

        LOGGER.info(
            "Toplam %d kesit belirlendi. Her biri sırayla işlenecek.", len(plans)
        )

        if args.analyze_only:
            LOGGER.info("--analyze-only verildi; video indirilmeyecek.")
            return {
                "url": url,
                "status": "ok",
                "clips": plans,
                "title": transcript.get("title"),
                "clips_results": [],
                "elapsed": time.time() - started,
            }

        # --- 3) Ham videoyu tek seferlik indir ----
        LOGGER.info("ADIM 3/4 | Ham video bir kez indiriliyor (raw_videos/ klasörüne)...")
        raw_video: Path | None = None
        try:
            raw_video = video_downloader.download_full_video(
                url, video_id=transcript["video_id"]
            )
        except video_downloader.DownloadError as exc:
            LOGGER.warning(
                "Ham video ön indirme başarısız; her kesit ayrı indirilecek: %s", exc
            )

        # --- 4) Her kesiti sırayla işle (kes → 9:16 → altyazı göm) ----
        LOGGER.info("ADIM 4/4 | Kesitler işleniyor (%d adet)...", len(plans))
        clips_results: list[dict[str, Any]] = []

        for clip_idx, clip in enumerate(plans, start=1):
            if not clip.get("captions"):
                LOGGER.warning(
                    "Kesit %d: altyazı yok; video altyazısız üretilecek.", clip_idx
                )
            result = _process_single_clip(
                url=url,
                transcript=transcript,
                clip=clip,
                clip_index=clip_idx,
                total_clips=len(plans),
                output_dir=output_dir,
                args=args,
                raw_video=raw_video,
            )
            clips_results.append(result)

        elapsed = time.time() - started
        ok_count = sum(1 for r in clips_results if r["status"] == "ok")
        LOGGER.info(
            "URL tamamlandı: %d/%d kesit başarılı | %s",
            ok_count, len(clips_results), format_duration(elapsed),
        )
        return {
            "url": url,
            "status": "ok" if ok_count > 0 else "error",
            "clips_results": clips_results,
            "title": transcript.get("title"),
            "elapsed": elapsed,
        }

    except KeyboardInterrupt:
        raise
    except Exception as exc:
        LOGGER.error("HATA | %s -> %s", url, exc)
        LOGGER.debug("Ayrıntılı hata:\n%s", traceback.format_exc())
        return {
            "url": url,
            "status": "error",
            "error": str(exc),
            "clips_results": [],
            "elapsed": time.time() - started,
        }
    finally:
        if config.CLEANUP_TEMP and not args.keep_temp:
            for path in temp_files:
                safe_delete(path)


# ----
# Komut satırı
# ----

def build_parser() -> argparse.ArgumentParser:
    """Komut satırı argümanlarını tanımlar."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="YouTube videolarından otomatik Türkçe altyazılı Shorts üretir (çoklu kesit).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Örnekler:\n"
            "  python main.py\n"
            '  python main.py "https://youtu.be/VIDEO_ID"\n'
            "  python main.py URL1 URL2 --subtitle-mode karaoke\n"
            "  python main.py --urls-file linkler.txt --keep-temp\n"
            '  python main.py URL --manual-clip "2:10-2:55"\n'
        ),
    )
    parser.add_argument("urls", nargs="*", help="Bir veya daha fazla YouTube bağlantısı")
    parser.add_argument("--urls-file", help="Her satırda bir bağlantı bulunan metin dosyası")
    parser.add_argument("--output-dir", help=f"Çıktı klasörü (varsayılan: {config.OUTPUT_DIR})")
    parser.add_argument("--model", help=f"RouteLLM modeli (varsayılan: {config.PRIMARY_MODEL})")
    parser.add_argument(
        "--subtitle-mode",
        choices=["highlight", "karaoke", "sentence"],
        help=f"Altyazı gösterim modu (varsayılan: {config.SUBTITLE_MODE})",
    )
    parser.add_argument(
        "--convert-mode",
        choices=["auto", "center_crop", "blur_pad"],
        help=f"9:16 dönüştürme modu (varsayılan: {config.CONVERT_MODE})",
    )
    parser.add_argument("--min-duration", type=float, help="Minimum klip süresi (sn)")
    parser.add_argument("--max-duration", type=float, help="Maksimum klip süresi (sn)")
    parser.add_argument(
        "--manual-clip",
        help='LLM klip seçimini atlayıp aralığı elle ver: "120-165" veya "2:00-2:45"',
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="LLM'i tamamen devre dışı bırak (sezgisel seçim, ÇEVİRİ YAPILMAZ)",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Sadece analiz yap, video indirme/işleme adımlarını atla",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Ara dosyaları silme")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=f"Log seviyesi (varsayılan: {config.LOG_LEVEL})",
    )
    return parser


def apply_overrides(args: argparse.Namespace) -> None:
    """Komut satırı argümanlarını config üzerine uygular."""
    if args.model:
        config.PRIMARY_MODEL = args.model
    if args.subtitle_mode:
        config.SUBTITLE_MODE = args.subtitle_mode
    if args.convert_mode:
        config.CONVERT_MODE = args.convert_mode
    if args.min_duration:
        config.MIN_CLIP_DURATION = args.min_duration
    if args.max_duration:
        config.MAX_CLIP_DURATION = args.max_duration
    if args.min_duration or args.max_duration:
        config.TARGET_CLIP_DURATION = min(
            max(config.TARGET_CLIP_DURATION, config.MIN_CLIP_DURATION),
            config.MAX_CLIP_DURATION,
        )
    if args.keep_temp:
        config.CLEANUP_TEMP = False
    if args.output_dir:
        config.OUTPUT_DIR = Path(args.output_dir)


def print_summary(results: list[dict[str, Any]]) -> None:
    """İşlem sonuçlarının özetini ekrana basar."""
    total_clips_ok = 0
    total_clips_fail = 0

    print("\n" + "=" * 62)
    print("  ÖZET")
    print("=" * 62)
    print(f"  Toplam bağlantı : {len(results)}")

    successful_urls = [r for r in results if r["status"] == "ok"]
    failed_urls = [r for r in results if r["status"] != "ok"]
    print(f"  Başarılı URL    : {len(successful_urls)}")
    print(f"  Başarısız URL   : {len(failed_urls)}")
    print("-" * 62)

    for result in results:
        clips_results = result.get("clips_results") or []
        ok_clips = [c for c in clips_results if c.get("status") == "ok"]
        fail_clips = [c for c in clips_results if c.get("status") != "ok"]
        total_clips_ok += len(ok_clips)
        total_clips_fail += len(fail_clips)

        if result["status"] == "ok":
            print(f"  [OK] {result['url']}")
            if result.get("title"):
                print(f"       Başlık : {result['title']}")
            print(f"       Kesit  : {len(ok_clips)} başarılı, {len(fail_clips)} başarısız")

            for cr in ok_clips:
                clip = cr.get("clip") or {}
                print(
                    f"       • Kesit {cr['clip_index']}: "
                    f"{format_timestamp(clip.get('start_time', 0))} -> "
                    f"{format_timestamp(clip.get('end_time', 0))} "
                    f"({clip.get('duration', 0):.1f} sn)"
                )
                if clip.get("title_tr"):
                    print(f"         Başlık : {clip['title_tr']}")
                if clip.get("hashtags"):
                    print(f"         Etiket : {' '.join(clip['hashtags'])}")
                if cr.get("output"):
                    print(f"         Dosya  : {cr['output']}")
                if clip.get("score") is not None:
                    print(f"         Puan   : {clip['score']}")
                if clip.get("translated") is False:
                    print("         UYARI  : Türkçe çeviri yapılamadı, altyazılar orijinal dilde.")

            for cr in fail_clips:
                print(f"       • Kesit {cr['clip_index']}: HATA – {cr.get('error')}")

            print(f"       Süre   : {format_duration(result.get('elapsed', 0))}")
        else:
            print(f"  [HATA] {result['url']}")
            print(f"         {result.get('error')}")

    print("-" * 62)
    print(f"  Toplam kesit    : {total_clips_ok + total_clips_fail}")
    print(f"  Başarılı kesit  : {total_clips_ok}")
    print(f"  Başarısız kesit : {total_clips_fail}")
    print("=" * 62 + "\n")


def main(argv: list[str] | None = None) -> int:
    """Programın giriş noktası."""
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    apply_overrides(args)
    config.ensure_directories()

    print(BANNER)

    # Bağımlılık kontrolü
    try:
        check_dependencies()
    except Exception as exc:
        LOGGER.error("Bağımlılık hatası: %s", exc)
        return 2

    urls = collect_urls(args)
    if not urls:
        LOGGER.error("İşlenecek bağlantı bulunamadı.")
        return 1

    LOGGER.info("%d bağlantı işlenecek.", len(urls))
    LOGGER.info("Çıktı klasörü: %s", Path(args.output_dir) if args.output_dir else config.OUTPUT_DIR)

    # LLM istemcisi
    analyzer: llm_analyzer.LLMAnalyzer | None = None
    if args.no_llm:
        LOGGER.warning("--no-llm verildi: klip sezgisel seçilecek ve ÇEVİRİ YAPILMAYACAK.")
    elif not config.api_key_is_configured():
        LOGGER.error(
            "Abacus AI RouteLLM API anahtarı ayarlı değil! config.py içindeki "
            "ABACUS_API_KEY değerini doldurun ya da ABACUS_API_KEY ortam değişkenini tanımlayın."
        )
        if not config.ENABLE_HEURISTIC_FALLBACK:
            return 2
        LOGGER.warning("Sezgisel yedek moda geçiliyor (çeviri yapılmayacak).")
    else:
        try:
            analyzer = llm_analyzer.LLMAnalyzer(models=[args.model] if args.model else None)
        except llm_analyzer.LLMError as exc:
            LOGGER.error("LLM istemcisi hazırlanamadı: %s", exc)
            if not config.ENABLE_HEURISTIC_FALLBACK:
                return 2
            LOGGER.warning("Sezgisel yedek moda geçiliyor (çeviri yapılmayacak).")

    results: list[dict[str, Any]] = []
    try:
        for index, url in enumerate(urls, start=1):
            results.append(process_url(url, index, len(urls), analyzer, args))
    except KeyboardInterrupt:
        LOGGER.warning("İşlem kullanıcı tarafından durduruldu.")

    print_summary(results)

    # En az bir kesit başarılıysa 0, değilse 1 döndür
    any_ok = any(
        cr.get("status") == "ok"
        for r in results
        for cr in (r.get("clips_results") or [])
    )
    return 0 if any_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nİptal edildi.")
        sys.exit(130)
