"""
llm_analyzer.py
====
Zaman damgalı transkripti Abacus AI RouteLLM (OpenAI uyumlu API) üzerinden
analiz eder ve şunları döndürür:

  • Videonun uzunluğuna ve içerik kalitesine göre dinamik sayıda
    viral potansiyeli yüksek kesit (start_time, end_time, title_tr, hashtags …)
  • Her kesitin cümle cümle Türkçe çevirisi (altyazı olarak gömülmek üzere)

Transkript çok uzunsa parçalara bölünüp her parçadan adaylar çıkarılır,
ardından adaylar arasından en iyileri seçilir (map-reduce yaklaşımı).
"""

from __future__ import annotations

import json
import time
from typing import Any

import config
import transcript_fetcher as tf
from utils import extract_json, format_timestamp, get_logger, parse_timestamp

LOGGER = get_logger("llm")

try:
    import openai
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "openai kütüphanesi bulunamadı. Kurulum: pip install openai"
    ) from exc

# Bağlantı/DNS hataları: bu durumda aynı adreste ısrar etmeyip yedek adrese geçilir
CONNECTION_ERRORS: tuple[type[Exception], ...] = tuple(
    error
    for error in (
        getattr(openai, "APIConnectionError", None),
        getattr(openai, "APITimeoutError", None),
    )
    if error is not None
) or (ConnectionError,)

# Model adı geçersizse tekrar denemenin anlamı yok, sıradaki modele geçilir
MODEL_ERRORS: tuple[type[Exception], ...] = tuple(
    error
    for error in (
        getattr(openai, "NotFoundError", None),
        getattr(openai, "BadRequestError", None),
    )
    if error is not None
) or (ValueError,)


class LLMError(RuntimeError):
    """LLM çağrıları başarısız olduğunda yükseltilir."""


# ----
# Prompt şablonları
# ----

SYSTEM_PROMPT = (
    "Sen viral YouTube Shorts ve TikTok içerikleri üreten deneyimli bir video editörü "
    "ve sosyal medya stratejistisin. Uzun videoların transkriptlerini inceleyip, tek "
    "başına izlendiğinde bile anlamlı, merak uyandıran ve duygusal etkisi yüksek "
    "bölümleri bulmakta ustasın. Ayrıca akıcı, doğal ve dikkat çekici Türkçe çeviriler "
    "yaparsın. SADECE istenen JSON formatında yanıt verirsin; açıklama, yorum veya "
    "markdown kod bloğu eklemezsin."
)

MULTI_CLIP_SELECTION_PROMPT = """Aşağıda bir YouTube videosunun zaman damgalı transkripti var.
Her satırın formatı: [başlangıç_saniye -> bitiş_saniye] metin

GÖREV: Bu videodan YouTube Shorts olarak yayınlanabilecek, her biri {min_dur:.0f}-{max_dur:.0f}
saniye uzunluğunda, viral potansiyeli yüksek TÜM mantıklı bölümleri seç.

DİNAMİK KESİT SAYISI KURALLARI:
- Sabit bir kesit sınırı YOK. Videonun uzunluğuna ve içeriğin kalitesine göre karar ver.
- Genel kılavuz: ~15 dk video → 2-4 kesit, ~30 dk → 3-6 kesit, ~60 dk → 4-7 kesit,
  ~120 dk → 6-9 kesit. Bunlar sadece örnektir; asıl belirleyici içerik kalitesidir.
- Tekrarlayan, sıkıcı, çok genel veya viral potansiyeli düşük yerler ELENMELİ.
- Kesitler birbiriyle ÖRTÜŞMEMELİ (overlap olmamalı).
- Az sayıda güçlü kesit, çok sayıda zayıf kesitten her zaman iyidir.

SEÇİM KRİTERLERİ (önem sırasına göre):
1. Her bölüm kendi içinde tam ve anlaşılır olmalı; cümlenin ortasında başlamamalı/bitmemeli.
2. İlk 3 saniye güçlü bir "hook" (kanca) içermeli: çarpıcı bir iddia, soru veya duygusal cümle.
3. Motive edici, öğretici, şaşırtıcı veya çok net bir mesaj taşıyan bölümler tercih edilir.
4. Genel giriş/çıkış, sponsor, selamlama, "abone olun" gibi kısımlardan kaçın.
5. Süre kesinlikle {min_dur:.0f} ile {max_dur:.0f} saniye arasında olmalı.

VİDEO BAŞLIĞI: {title}
VİDEO SÜRESİ: {duration:.0f} saniye

TRANSKRİPT:
{transcript}

SADECE şu JSON'u döndür (bir dizi/array olmalı):
[
  {{
    "start_time": <başlangıç saniyesi, ondalıklı sayı>,
    "end_time": <bitiş saniyesi, ondalıklı sayı>,
    "hook": "<bölümün ilk çarpıcı cümlesi (orijinal dilde)>",
    "reason": "<bu bölümü neden seçtiğini Türkçe 1-2 cümleyle açıkla>",
    "niche": "<içeriğin nişi: motivasyon, girişimcilik, psikoloji, spor vb.>",
    "title_tr": "<Shorts için Türkçe, dikkat çekici, en fazla 60 karakterlik başlık>",
    "hashtags": ["#etiket1", "#etiket2", "#etiket3"],
    "score": <0-100 arası viral olma potansiyeli>
  }}
]"""

CHUNK_CANDIDATES_PROMPT = """Aşağıda uzun bir videonun transkriptinin bir PARÇASI var.
Her satırın formatı: [başlangıç_saniye -> bitiş_saniye] metin

GÖREV: Bu parçadaki viral potansiyeli yüksek, kendi içinde anlamlı {min_dur:.0f}-{max_dur:.0f}
saniyelik TÜM güçlü bölümleri bul. Birden fazla olabilir; tekrarlayan, sıkıcı yerler elensin.
Kesitler birbiriyle ÖRTÜŞMEMELİ.

TRANSKRİPT PARÇASI:
{transcript}

SADECE şu JSON'u döndür (bir dizi/array olmalı):
[
  {{
    "start_time": <saniye>,
    "end_time": <saniye>,
    "reason": "<Türkçe kısa gerekçe>",
    "score": <0-100>
  }}
]"""

TRANSLATION_PROMPT = """Aşağıda bir YouTube Shorts videosunun altyazı blokları var.
Her blok "index | metin" formatında verilmiştir.

GÖREV: Her bloğu ayrı ayrı, akıcı ve doğal Türkçeye çevir.

KURALLAR:
1. Blok sayısını ASLA değiştirme; her index için tam olarak bir çeviri döndür.
2. Blokları birleştirme veya bölme; sırayı koru.
3. Çeviri kısa ve vurucu olsun (altyazı olarak ekranda okunacak). Mümkünse blok başına
   en fazla 8-10 kelime kullan.
4. Kelime kelime birebir çeviri yapma; anlamı ve tonu koruyan doğal Türkçe kullan.
5. Argo/deyimleri Türkçedeki karşılıklarıyla ver. Küfürleri yumuşat.
6. Noktalama işaretlerini sade tut; büyük harfle bağırma.
7. Bloklar art arda konuşulan cümlelerdir, bağlamı göz önünde bulundur.

BAĞLAM (video konusu): {context}

BLOKLAR:
{blocks}

SADECE şu JSON'u döndür:
{{"translations": [{{"index": 0, "tr": "Türkçe çeviri"}}, {{"index": 1, "tr": "..."}}]}}"""


# ----
# Eski tek-klip promptları (geriye dönük uyumluluk / manual-clip modu için korunuyor)
# ----

CLIP_SELECTION_PROMPT = """Aşağıda bir YouTube videosunun zaman damgalı transkripti var.
Her satırın formatı: [başlangıç_saniye -> bitiş_saniye] metin

GÖREV: Bu videodan YouTube Shorts olarak yayınlanacak, {min_dur:.0f}-{max_dur:.0f} saniye
uzunluğunda TEK bir bölüm seç.

SEÇİM KRİTERLERİ (önem sırasına göre):
1. Bölüm kendi içinde tam ve anlaşılır olmalı; cümlenin ortasında başlamamalı/bitmemeli.
2. İlk 3 saniye güçlü bir "hook" (kanca) içermeli: çarpıcı bir iddia, soru veya duygusal cümle.
3. Motive edici, öğretici, şaşırtıcı veya çok net bir mesaj taşıyan bölümler tercih edilir.
4. Genel giriş/çıkış, sponsor, selamlama, "abone olun" gibi kısımlardan kaçın.
5. Süre kesinlikle {min_dur:.0f} ile {max_dur:.0f} saniye arasında olmalı.

VİDEO BAŞLIĞI: {title}
VİDEO SÜRESİ: {duration:.0f} saniye

TRANSKRİPT:
{transcript}

SADECE şu JSON'u döndür:
{{
  "start_time": <başlangıç saniyesi, ondalıklı sayı>,
  "end_time": <bitiş saniyesi, ondalıklı sayı>,
  "hook": "<bölümün ilk çarpıcı cümlesi (orijinal dilde)>",
  "reason": "<bu bölümü neden seçtiğini Türkçe 1-2 cümleyle açıkla>",
  "niche": "<içeriğin nişi: motivasyon, girişimcilik, psikoloji, spor vb.>",
  "title_tr": "<Shorts için Türkçe, dikkat çekici, en fazla 60 karakterlik başlık>",
  "hashtags": ["#etiket1", "#etiket2", "#etiket3"],
  "score": <0-100 arası viral olma potansiyeli>
}}"""

CHUNK_CANDIDATE_PROMPT = """Aşağıda uzun bir videonun transkriptinin bir PARÇASI var.
Her satırın formatı: [başlangıç_saniye -> bitiş_saniye] metin

GÖREV: Bu parçadaki en ilgi çekici, kendi içinde anlamlı {min_dur:.0f}-{max_dur:.0f} saniyelik
bölümü bul ve viral potansiyelini puanla.

TRANSKRİPT PARÇASI:
{transcript}

SADECE şu JSON'u döndür:
{{
  "start_time": <saniye>,
  "end_time": <saniye>,
  "reason": "<Türkçe kısa gerekçe>",
  "score": <0-100>
}}"""


# ----
# Ana sınıf
# ----

class LLMAnalyzer:
    """Abacus AI RouteLLM üzerinden klip seçimi ve çeviri yapan analiz sınıfı."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        models: list[str] | None = None,
    ) -> None:
        self.api_key = api_key or config.ABACUS_API_KEY
        self.models = models or config.model_candidates()

        # Birincil adres + yedek adresler (biri ağ hatası verirse diğeri denenir)
        self.base_urls: list[str] = []
        for candidate in [base_url or config.ROUTELLM_BASE_URL, *config.ROUTELLM_FALLBACK_BASE_URLS]:
            if candidate and candidate not in self.base_urls:
                self.base_urls.append(candidate)
        self.base_url = self.base_urls[0]

        if not self.api_key or self.api_key.startswith("BURAYA_"):
            raise LLMError(
                "Abacus AI RouteLLM API anahtarı ayarlı değil. config.py içindeki "
                "ABACUS_API_KEY değerini doldurun veya ABACUS_API_KEY ortam "
                "değişkenini tanımlayın."
            )

        self._clients: dict[str, Any] = {}
        # İstemcinin kurulabildiğini baştan doğrula
        self._client_for(self.base_url)

        LOGGER.info(
            "RouteLLM istemcisi hazır | endpoint=%s | modeller=%s",
            self.base_url, ", ".join(self.models),
        )

    def _client_for(self, base_url: str) -> Any:
        """Verilen adres için OpenAI istemcisini oluşturur/önbellekten döndürür."""
        if base_url not in self._clients:
            try:
                self._clients[base_url] = OpenAI(
                    api_key=self.api_key,
                    base_url=base_url,
                    timeout=config.LLM_TIMEOUT,
                    max_retries=0,  # Yeniden denemeyi kendimiz yönetiyoruz
                )
            except Exception as exc:
                raise LLMError(f"RouteLLM istemcisi oluşturulamadı ({base_url}): {exc}") from exc
        return self._clients[base_url]

    # ----
    # Düşük seviye API çağrısı
    # ----

    def _chat(self, prompt: str, max_tokens: int | None = None) -> str:
        """
        RouteLLM'e sohbet isteği gönderir. Modeller ve denemeler arasında
        otomatik geçiş/yeniden deneme yapar.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        last_error: Exception | None = None

        for base_url in self.base_urls:
            client = self._client_for(base_url)
            switch_endpoint = False

            for model in self.models:
                for attempt in range(1, config.LLM_MAX_RETRIES + 1):
                    try:
                        LOGGER.debug(
                            "LLM isteği | endpoint=%s | model=%s | deneme=%d",
                            base_url, model, attempt,
                        )
                        response = client.chat.completions.create(
                            model=model,
                            messages=messages,
                            temperature=config.LLM_TEMPERATURE,
                            max_tokens=max_tokens or config.LLM_MAX_TOKENS,
                        )
                        if not response.choices:
                            raise LLMError("LLM yanıtında 'choices' boş döndü.")
                        content = (response.choices[0].message.content or "").strip()
                        if not content:
                            raise LLMError("LLM boş içerik döndürdü.")
                        if base_url != self.base_url:
                            LOGGER.info("Yedek endpoint kullanıldı: %s", base_url)
                        LOGGER.debug(
                            "LLM yanıtı alındı (%d karakter, model=%s)", len(content), model
                        )
                        return content

                    except CONNECTION_ERRORS as exc:
                        # Ağ/DNS sorunu: bu adreste diğer modelleri denemek anlamsız
                        last_error = exc
                        LOGGER.warning(
                            "Endpoint'e ulaşılamadı (%s): %s", base_url, str(exc)[:160]
                        )
                        switch_endpoint = True
                        break

                    except MODEL_ERRORS as exc:
                        # Model adı geçersiz/desteklenmiyor: tekrar denemeden sıradakine geç
                        last_error = exc
                        LOGGER.warning(
                            "Model '%s' kullanılamadı (%s), sıradaki model deneniyor.",
                            model, str(exc)[:160],
                        )
                        break

                    except Exception as exc:
                        last_error = exc
                        LOGGER.warning(
                            "LLM isteği başarısız (model=%s, deneme=%d/%d): %s",
                            model, attempt, config.LLM_MAX_RETRIES, str(exc)[:200],
                        )
                        if attempt < config.LLM_MAX_RETRIES:
                            time.sleep(config.LLM_RETRY_BACKOFF * attempt)

                if switch_endpoint:
                    break

            if switch_endpoint and base_url != self.base_urls[-1]:
                LOGGER.info("Yedek endpoint deneniyor...")

        raise LLMError(f"Tüm endpoint/model kombinasyonları başarısız. Son hata: {last_error}")

    def _chat_json(self, prompt: str, max_tokens: int | None = None) -> Any:
        """Yanıtı JSON olarak ayrıştırır; başarısız olursa bir kez daha dener."""
        content = self._chat(prompt, max_tokens=max_tokens)
        try:
            return extract_json(content)
        except ValueError as exc:
            LOGGER.warning("JSON ayrıştırma hatası: %s. Model tekrar uyarılıyor.", exc)
            retry_prompt = (
                prompt
                + "\n\nÖNEMLİ: Önceki yanıt geçersizdi. SADECE geçerli JSON döndür, "
                "başka hiçbir metin ekleme."
            )
            content = self._chat(retry_prompt, max_tokens=max_tokens)
            return extract_json(content)

    # ----
    # Çoklu klip seçimi (yeni – dinamik sayıda kesit)
    # ----

    def select_clips(self, transcript_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Transkriptten viral potansiyeli yüksek TÜM mantıklı kesitleri seçer.
        Kesit sayısını videonun uzunluğuna ve içerik kalitesine göre LLM belirler.
        Transkript uzunsa parçalı analiz yapar.
        """
        segments: list[dict[str, Any]] = transcript_data["segments"]
        title = transcript_data.get("title", "")
        duration = float(transcript_data.get("duration") or 0.0)
        full_text = tf.transcript_to_text(segments)

        if len(full_text) <= config.MAX_TRANSCRIPT_CHARS:
            LOGGER.info(
                "Transkript tek istekte analiz edilecek (%d karakter).", len(full_text)
            )
            raw = self._chat_json(
                MULTI_CLIP_SELECTION_PROMPT.format(
                    min_dur=config.MIN_CLIP_DURATION,
                    max_dur=config.MAX_CLIP_DURATION,
                    title=title,
                    duration=duration,
                    transcript=full_text,
                ),
                max_tokens=config.LLM_MAX_TOKENS,
            )
        else:
            raw = self._select_clips_chunked(segments, title, duration)

        # Ham sonucu listeye dönüştür
        raw_clips = self._ensure_list(raw)
        if not raw_clips:
            raise LLMError("LLM hiçbir kesit adayı döndürmedi.")

        # Her kesiti normalize et
        clips: list[dict[str, Any]] = []
        for idx, raw_clip in enumerate(raw_clips):
            try:
                clip = self._normalize_clip(raw_clip, segments, duration)
                clips.append(clip)
            except LLMError as exc:
                LOGGER.warning("Kesit %d normalize edilemedi, atlanıyor: %s", idx + 1, exc)

        if not clips:
            raise LLMError("Normalize edilen hiçbir geçerli kesit bulunamadı.")

        # Örtüşen kesitleri ele
        clips = self._remove_overlaps(clips)

        # Skora göre sırala (yüksekten düşüğe)
        clips.sort(key=lambda c: float(c.get("score") or 0), reverse=True)

        LOGGER.info(
            "Toplamda %d kesit seçildi (video süresi: %s).",
            len(clips), format_timestamp(duration),
        )
        return clips

    def _select_clips_chunked(
        self, segments: list[dict[str, Any]], title: str, duration: float
    ) -> list[dict[str, Any]]:
        """Uzun transkriptler için: parça parça adayları topla, birleştir."""
        chunks = self._chunk_segments(segments)
        LOGGER.info("Transkript uzun; %d parça halinde analiz edilecek.", len(chunks))

        all_candidates: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_text = tf.transcript_to_text(chunk)
            try:
                result = self._chat_json(
                    CHUNK_CANDIDATES_PROMPT.format(
                        min_dur=config.MIN_CLIP_DURATION,
                        max_dur=config.MAX_CLIP_DURATION,
                        transcript=chunk_text,
                    ),
                    max_tokens=config.LLM_MAX_TOKENS,
                )
                candidates = self._ensure_list(result)
                for cand in candidates:
                    if isinstance(cand, dict) and cand.get("start_time") is not None:
                        cand["_chunk"] = index
                        all_candidates.append(cand)
                LOGGER.info(
                    "Parça %d/%d → %d aday bulundu.", index, len(chunks), len(candidates)
                )
            except Exception as exc:
                LOGGER.warning("Parça %d analiz edilemedi: %s", index, str(exc)[:200])

        if not all_candidates:
            raise LLMError("Hiçbir transkript parçasından aday klip alınamadı.")

        # Tüm adaylara varsayılan meta veri ekle (chunk promptunda yok)
        for cand in all_candidates:
            cand.setdefault("niche", "genel")
            cand.setdefault("title_tr", title[:60])
            cand.setdefault("hashtags", ["#shorts", "#motivasyon"])
            cand.setdefault("hook", "")

        LOGGER.info(
            "Tüm parçalardan toplam %d aday toplandı.", len(all_candidates)
        )
        return all_candidates

    @staticmethod
    def _ensure_list(raw: Any) -> list[dict[str, Any]]:
        """LLM çıktısını her durumda bir listeye dönüştürür."""
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            # Tek obje döndüyse listeye sar
            # Bazen {"clips": [...]} gibi sarmalı olabiliyor
            for key in ("clips", "results", "segments", "items"):
                if key in raw and isinstance(raw[key], list):
                    return [item for item in raw[key] if isinstance(item, dict)]
            return [raw]
        return []

    @staticmethod
    def _remove_overlaps(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Örtüşen kesitlerden düşük puanlıyı çıkarır."""
        if len(clips) <= 1:
            return clips

        # Başlangıç zamanına göre sırala
        sorted_clips = sorted(clips, key=lambda c: c["start_time"])
        kept: list[dict[str, Any]] = [sorted_clips[0]]

        for clip in sorted_clips[1:]:
            prev = kept[-1]
            # Örtüşme kontrolü: mevcut klibin başlangıcı öncekinin bitişinden önceyse
            if clip["start_time"] < prev["end_time"] - 1.0:
                # Daha yüksek puanlıyı tut
                prev_score = float(prev.get("score") or 0)
                curr_score = float(clip.get("score") or 0)
                if curr_score > prev_score:
                    kept[-1] = clip
                    LOGGER.info(
                        "Örtüşen kesit: %s-%s (puan=%.0f) yerine %s-%s (puan=%.0f) tercih edildi.",
                        format_timestamp(prev["start_time"]),
                        format_timestamp(prev["end_time"]),
                        prev_score,
                        format_timestamp(clip["start_time"]),
                        format_timestamp(clip["end_time"]),
                        curr_score,
                    )
                else:
                    LOGGER.info(
                        "Örtüşen kesit atlandı: %s-%s (puan=%.0f)",
                        format_timestamp(clip["start_time"]),
                        format_timestamp(clip["end_time"]),
                        curr_score,
                    )
            else:
                kept.append(clip)

        return kept

    @staticmethod
    def _chunk_segments(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Segmentleri karakter bütçesine göre parçalara böler."""
        budget = max(4000, config.MAX_TRANSCRIPT_CHARS)
        total_chars = sum(len(s["text"]) + 26 for s in segments)
        chunk_count = min(
            config.MAX_TRANSCRIPT_CHUNKS,
            max(2, (total_chars + budget - 1) // budget),
        )
        per_chunk = max(1, len(segments) // chunk_count)
        return [segments[i : i + per_chunk] for i in range(0, len(segments), per_chunk)][
            : config.MAX_TRANSCRIPT_CHUNKS
        ]

    def _normalize_clip(
        self, raw: Any, segments: list[dict[str, Any]], duration: float
    ) -> dict[str, Any]:
        """
        LLM'in döndürdüğü klip bilgisini doğrular:
        süre sınırlarına oturtur ve cümle sınırlarına yuvarlar.
        """
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if not isinstance(raw, dict):
            raise LLMError(f"LLM beklenmeyen bir yapı döndürdü: {type(raw).__name__}")

        try:
            start = parse_timestamp(raw.get("start_time"))
            end = parse_timestamp(raw.get("end_time"))
        except ValueError as exc:
            raise LLMError(f"Klip zamanları okunamadı: {exc}") from exc

        if end <= start:
            raise LLMError(f"Geçersiz klip aralığı: {start} -> {end}")

        limit = duration if duration > 0 else (segments[-1]["end"] if segments else end)

        # Cümle sınırlarına yuvarla (yarım cümleyle başlamasın)
        overlapping = [s for s in segments if s["end"] > start + 0.3 and s["start"] < end - 0.3]
        if overlapping:
            start = min(start, overlapping[0]["start"])
            end = max(end, overlapping[-1]["end"])

        # Süre sınırlarını uygula
        clip_duration = end - start
        if clip_duration > config.MAX_CLIP_DURATION:
            # Cümle bütünlüğünü koruyarak sondan kısalt
            trimmed_end = start + config.MAX_CLIP_DURATION
            fitting = [s for s in overlapping if s["end"] <= trimmed_end]
            if fitting and (fitting[-1]["end"] - start) >= config.MIN_CLIP_DURATION:
                end = fitting[-1]["end"]
            else:
                end = trimmed_end
            LOGGER.info("Klip çok uzundu, %s saniyeye kısaltıldı.", f"{end - start:.1f}")
        elif clip_duration < config.MIN_CLIP_DURATION:
            needed = config.TARGET_CLIP_DURATION - clip_duration
            end = min(limit, end + needed)
            if (end - start) < config.MIN_CLIP_DURATION:
                start = max(0.0, end - config.TARGET_CLIP_DURATION)
            LOGGER.info("Klip çok kısaydı, %s saniyeye uzatıldı.", f"{end - start:.1f}")

        start = max(0.0, start)
        if limit > 0:
            end = min(end, limit)
        if end - start < 5.0:
            raise LLMError("Klip aralığı kullanılamayacak kadar kısa.")

        hashtags = raw.get("hashtags") or ["#shorts"]
        if isinstance(hashtags, str):
            hashtags = [tag.strip() for tag in hashtags.replace(",", " ").split() if tag.strip()]

        clip = {
            "start_time": round(start, 2),
            "end_time": round(end, 2),
            "duration": round(end - start, 2),
            "hook": str(raw.get("hook") or "").strip(),
            "reason": str(raw.get("reason") or "").strip(),
            "niche": str(raw.get("niche") or "genel").strip(),
            "title_tr": str(raw.get("title_tr") or "").strip(),
            "hashtags": [str(tag) for tag in hashtags][:8],
            "score": raw.get("score"),
            "source": "llm",
        }
        LOGGER.info(
            "Seçilen klip: %s -> %s (%.1f sn) | niş=%s | puan=%s",
            format_timestamp(clip["start_time"]),
            format_timestamp(clip["end_time"]),
            clip["duration"],
            clip["niche"],
            clip["score"],
        )
        if clip["reason"]:
            LOGGER.info("Gerekçe: %s", clip["reason"])
        return clip

    # ----
    # Eski tek-klip seçimi (geriye dönük uyumluluk – manual-clip modu için)
    # ----

    def select_clip(self, transcript_data: dict[str, Any]) -> dict[str, Any]:
        """
        Transkriptten en iyi 30-60 saniyelik TEK bölümü seçer.
        (Geriye dönük uyumluluk – select_clips kullanılması önerilir.)
        """
        clips = self.select_clips(transcript_data)
        return clips[0]  # En yüksek puanlı olan zaten ilk sırada

    # ----
    # Çeviri
    # ----

    def translate_captions(
        self, captions: list[dict[str, Any]], context: str = ""
    ) -> list[dict[str, Any]]:
        """
        Altyazı bloklarını cümle cümle Türkçeye çevirir.
        Zaman damgaları orijinal altyazıdan korunur (senkron bozulmaz).
        """
        if not captions:
            return []

        translated: list[dict[str, Any]] = [dict(caption) for caption in captions]
        batch_size = 40  # Çok uzun isteklerde model blok atlayabiliyor

        for offset in range(0, len(translated), batch_size):
            batch = translated[offset : offset + batch_size]
            blocks = "\n".join(
                f"{index} | {caption['text']}" for index, caption in enumerate(batch)
            )
            try:
                raw = self._chat_json(
                    TRANSLATION_PROMPT.format(context=context or "genel", blocks=blocks)
                )
                mapping = self._parse_translations(raw, len(batch))
            except Exception as exc:
                LOGGER.error("Çeviri başarısız (blok %d-%d): %s", offset, offset + len(batch), exc)
                raise LLMError(f"Altyazı çevirisi yapılamadı: {exc}") from exc

            missing = 0
            for index, caption in enumerate(batch):
                turkish = mapping.get(index, "").strip()
                if not turkish:
                    missing += 1
                    turkish = caption["text"]  # Çeviri gelmediyse orijinali koru
                caption["original"] = caption["text"]
                caption["text"] = turkish
            if missing:
                LOGGER.warning("%d blok için çeviri gelmedi, orijinal metin korundu.", missing)

        LOGGER.info("%d altyazı bloğu Türkçeye çevrildi.", len(translated))
        return translated

    @staticmethod
    def _parse_translations(raw: Any, expected: int) -> dict[int, str]:
        """LLM çeviri yanıtını {index: metin} sözlüğüne dönüştürür."""
        items: list[Any]
        if isinstance(raw, dict):
            items = raw.get("translations") or raw.get("subtitles") or raw.get("items") or []
            if not items:
                # {"0": "metin", "1": "metin"} biçimi
                mapping: dict[int, str] = {}
                for key, value in raw.items():
                    try:
                        mapping[int(key)] = str(value)
                    except (TypeError, ValueError):
                        continue
                if mapping:
                    return mapping
        elif isinstance(raw, list):
            items = raw
        else:
            raise LLMError("Çeviri yanıtı beklenen formatta değil.")

        mapping = {}
        for position, item in enumerate(items):
            if isinstance(item, dict):
                index = item.get("index", item.get("id", position))
                text = item.get("tr") or item.get("text") or item.get("translation") or ""
            else:
                index, text = position, str(item)
            try:
                mapping[int(index)] = str(text)
            except (TypeError, ValueError):
                continue

        if not mapping:
            raise LLMError("Çeviri yanıtından hiçbir blok okunamadı.")
        if len(mapping) < expected:
            LOGGER.warning("Beklenen %d blok, gelen %d blok.", expected, len(mapping))
        return mapping

    # ----
    # Tam analiz (çoklu kesit)
    # ----

    def analyze(self, transcript_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Tam analiz: çoklu klip seçimi + her kesitin Türkçe altyazıları.

        Dönüş: Liste (list) — her eleman bir kesit sözlüğü:
            {
              'start_time', 'end_time', 'duration', 'title_tr', 'reason',
              'niche', 'hashtags', 'translated': bool,
              'captions': [{'start','end','text','original'}]  # klip başına göre göreli
            }
        """
        clips = self.select_clips(transcript_data)
        context = f"{transcript_data.get('title', '')}"

        for clip_index, clip in enumerate(clips, start=1):
            LOGGER.info(
                "Kesit %d/%d için altyazılar hazırlanıyor: %s -> %s",
                clip_index, len(clips),
                format_timestamp(clip["start_time"]),
                format_timestamp(clip["end_time"]),
            )
            captions = self.build_caption_blocks(
                transcript_data, clip["start_time"], clip["end_time"]
            )

            if not captions:
                LOGGER.warning(
                    "Kesit %d: aralıkta altyazı bloğu bulunamadı; altyazısız olacak.",
                    clip_index,
                )
                clip["captions"] = []
                clip["translated"] = False
                continue

            clip_context = f"{context} | niş: {clip.get('niche', 'genel')}"
            try:
                captions = self.translate_captions(captions, context=clip_context)
                clip["translated"] = True
            except LLMError as exc:
                LOGGER.error(
                    "Kesit %d: çeviri yapılamadı, orijinal altyazılar kullanılacak: %s",
                    clip_index, exc,
                )
                for caption in captions:
                    caption["original"] = caption["text"]
                clip["translated"] = False

            clip["captions"] = self._to_relative(
                captions, clip["start_time"], clip["duration"]
            )

        LOGGER.info("Analiz tamamlandı: %d kesit hazır.", len(clips))
        return clips

    # ----
    # Eski tek-klip analyze (geriye dönük uyumluluk)
    # ----

    def analyze_single(self, transcript_data: dict[str, Any]) -> dict[str, Any]:
        """
        Eski tek-klip analyze davranışı (geriye dönük uyumluluk).
        Yeni kodda analyze() kullanılması önerilir.
        """
        clips = self.analyze(transcript_data)
        return clips[0]

    @staticmethod
    def build_caption_blocks(
        transcript_data: dict[str, Any], start: float, end: float
    ) -> list[dict[str, Any]]:
        """Seçilen aralığın altyazı bloklarını (mutlak zamanlı) hazırlar."""
        source = transcript_data.get("raw_segments") or transcript_data["segments"]
        window = tf.slice_segments(tf.merge_segments(source), start, end)
        return tf.split_for_captions(window)

    @staticmethod
    def _to_relative(
        captions: list[dict[str, Any]], clip_start: float, clip_duration: float
    ) -> list[dict[str, Any]]:
        """Mutlak zamanları klip başlangıcına göre göreli hale getirir."""
        relative: list[dict[str, Any]] = []
        for caption in captions:
            start = max(0.0, float(caption["start"]) - clip_start)
            end = min(clip_duration, float(caption["end"]) - clip_start)
            if end - start < 0.25:
                continue
            relative.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": caption["text"].strip(),
                    "original": str(caption.get("original", "")).strip(),
                }
            )
        return relative


# ----
# LLM erişilemediğinde kullanılan sezgisel yedek
# ----

def heuristic_clips(transcript_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    LLM'e ulaşılamadığında devreye giren yedek klip seçimi.
    Videonun farklı bölgelerinden en yoğun konuşma pencerelerini seçer.
    Altyazılar ÇEVRİLMEZ; orijinal dilde gömülür.
    """
    segments = transcript_data["segments"]
    if not segments:
        raise LLMError("Yedek seçim için transkript boş.")

    duration = float(transcript_data.get("duration") or segments[-1]["end"])
    target = config.TARGET_CLIP_DURATION

    # Video süresine göre hedef kesit sayısı
    if duration <= 600:
        target_count = 2
    elif duration <= 1800:
        target_count = 3
    elif duration <= 3600:
        target_count = 5
    else:
        target_count = 7

    # Tüm olası pencereleri puanla
    windows: list[tuple[float, float, int]] = []
    for segment in segments:
        window_start = segment["start"]
        window_end = window_start + target
        chars = sum(
            len(s["text"])
            for s in segments
            if s["start"] >= window_start and s["end"] <= window_end
        )
        # Videonun ilk %10'u ve son %10'u genelde giriş/çıkış olduğu için cezalandır
        if duration > 0 and (window_start < duration * 0.1 or window_end > duration * 0.95):
            chars = int(chars * 0.6)
        windows.append((window_start, min(window_end, duration), chars))

    # En yüksek puanlıları al ama örtüşmeyenleri seç
    windows.sort(key=lambda w: w[2], reverse=True)
    selected: list[tuple[float, float]] = []
    for w_start, w_end, _ in windows:
        if len(selected) >= target_count:
            break
        # Örtüşme kontrolü
        overlaps = any(
            not (w_end <= s[0] or w_start >= s[1]) for s in selected
        )
        if not overlaps:
            selected.append((w_start, w_end))

    if not selected:
        # Hiç seçilemediyse en iyisini al
        best = windows[0] if windows else (segments[0]["start"], segments[0]["start"] + target, 0)
        selected = [(best[0], best[1])]

    # Kesitleri zamana göre sırala
    selected.sort(key=lambda s: s[0])

    clips: list[dict[str, Any]] = []
    for start, end in selected:
        if end - start < config.MIN_CLIP_DURATION:
            start = max(0.0, end - config.TARGET_CLIP_DURATION)

        captions = LLMAnalyzer.build_caption_blocks(transcript_data, start, end)
        for caption in captions:
            caption["original"] = caption["text"]

        clips.append({
            "start_time": round(start, 2),
            "end_time": round(end, 2),
            "duration": round(end - start, 2),
            "hook": "",
            "reason": "LLM kullanılamadı; en yoğun konuşma penceresi otomatik seçildi.",
            "niche": "genel",
            "title_tr": str(transcript_data.get("title", ""))[:60],
            "hashtags": ["#shorts"],
            "score": None,
            "source": "heuristic",
            "translated": False,
            "captions": LLMAnalyzer._to_relative(captions, start, end - start),
        })

    LOGGER.warning(
        "SEZGİSEL YEDEK: %d kesit seçildi (altyazılar ÇEVRİLMEDİ).", len(clips)
    )
    return clips


# Geriye dönük uyumluluk: eski isim hâlâ çalışsın
def heuristic_clip(transcript_data: dict[str, Any]) -> dict[str, Any]:
    """Tek kesit döndüren eski fonksiyon (geriye dönük uyumluluk)."""
    clips = heuristic_clips(transcript_data)
    return clips[0]


if __name__ == "__main__":  # Basit manuel test
    import sys

    if len(sys.argv) < 2:
        print("Kullanım: python llm_analyzer.py <youtube_url>")
        raise SystemExit(1)

    config.ensure_directories()
    data = tf.fetch_transcript(sys.argv[1])
    analyzer = LLMAnalyzer()
    plans = analyzer.analyze(data)
    print(f"\n{len(plans)} kesit bulundu:\n")
    for i, plan in enumerate(plans, 1):
        print(f"--- Kesit {i} ---")
        print(json.dumps(plan, ensure_ascii=False, indent=2)[:1500])
        print()
