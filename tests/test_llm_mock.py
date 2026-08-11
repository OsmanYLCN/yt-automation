#!/usr/bin/env python3
"""
tests/test_llm_mock.py
======================
llm_analyzer modülünü GERÇEK API'ye bağlanmadan doğrular.

Yerelde OpenAI uyumlu küçük bir sahte (mock) sunucu ayağa kaldırır ve
LLMAnalyzer'ın istekleri doğru gönderdiğini, yanıtları doğru ayrıştırdığını,
süre sınırlarını uyguladığını ve çevirileri altyazı bloklarına
eşlediğini test eder.

Çalıştırma:
    python tests/test_llm_mock.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import llm_analyzer  # noqa: E402
import subtitle_burner  # noqa: E402

PORT = 8899

# Test için örnek transkript (gerçek yt-dlp çıktısıyla aynı yapıda)
FAKE_TRANSCRIPT = {
    "video_id": "test123",
    "title": "How To Build Discipline",
    "duration": 300.0,
    "url": "https://youtu.be/test123",
    "language": "en",
    "is_automatic": False,
    "segments": [
        {"start": float(i * 5), "end": float(i * 5 + 4.6), "text": f"This is sentence number {i} about discipline and focus."}
        for i in range(60)
    ],
}
FAKE_TRANSCRIPT["raw_segments"] = FAKE_TRANSCRIPT["segments"]


class MockHandler(BaseHTTPRequestHandler):
    """Basit /chat/completions taklidi."""

    def log_message(self, *args):  # Test çıktısını kirletmemek için sessiz
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        prompt = body["messages"][-1]["content"]

        if "BLOKLAR:" in prompt:
            # Çeviri isteği: kaç blok geldiyse o kadar Türkçe çeviri döndür
            block_lines = [
                line for line in prompt.split("BLOKLAR:")[1].splitlines() if "|" in line
            ]
            translations = [
                {"index": index, "tr": f"Türkçe çeviri {index} - disiplin ve odak"}
                for index in range(len(block_lines))
            ]
            content = json.dumps({"translations": translations}, ensure_ascii=False)
        else:
            # Klip seçimi isteği
            content = json.dumps(
                {
                    "start_time": 100.0,
                    "end_time": 142.0,
                    "hook": "Discipline beats motivation every time",
                    "reason": "Bölüm net bir mesaj veriyor ve kendi içinde tamamlanıyor.",
                    "niche": "motivasyon",
                    "title_tr": "Disiplin motivasyonu her zaman yener",
                    "hashtags": ["#motivasyon", "#disiplin", "#shorts"],
                    "score": 88,
                },
                ensure_ascii=False,
            )

        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_tests() -> int:
    # Windows'ta CP1252 kodlamasından kaynaklanan UnicodeEncodeError'u önle
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config.ensure_directories()
    server = HTTPServer(("127.0.0.1", PORT), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    failures = 0

    try:
        analyzer = llm_analyzer.LLMAnalyzer(
            api_key="test-key",
            base_url=f"http://127.0.0.1:{PORT}/v1",
            models=["mock-model"],
        )
        # analyze() artık list[dict] döndürüyor; ilk (en yüksek puanlı) kesiti al
        plans = analyzer.analyze(FAKE_TRANSCRIPT)
        plan = plans[0]

        checks = [
            ("Klip başlangıcı doğru", plan["start_time"] == 100.0),
            # Bitiş, cümlenin ortasında kesilmemesi için en yakın cümle sonuna yuvarlanır
            ("Klip bitişi cümle sınırına yuvarlandı", 142.0 <= plan["end_time"] <= 148.0),
            (
                "Süre 30-60 sn arasında",
                config.MIN_CLIP_DURATION <= plan["duration"] <= config.MAX_CLIP_DURATION,
            ),
            ("Türkçe başlık geldi", bool(plan["title_tr"])),
            ("Hashtagler geldi", len(plan["hashtags"]) == 3),
            ("Çeviri yapıldı bayrağı", plan["translated"] is True),
            ("Altyazı blokları üretildi", len(plan["captions"]) > 0),
            (
                "Altyazılar Türkçe",
                all("Türkçe çeviri" in caption["text"] for caption in plan["captions"]),
            ),
            (
                "Zamanlar klip başına göreli",
                plan["captions"][0]["start"] < 5.0
                and plan["captions"][-1]["end"] <= plan["duration"] + 0.1,
            ),
        ]

        # ASS üretimi de aynı planla test edilir
        ass_path = config.TEMP_DIR / "mock_test.ass"
        subtitle_burner.generate_ass(
            plan["captions"], ass_path, clip_duration=plan["duration"], mode="highlight"
        )
        ass_text = ass_path.read_text(encoding="utf-8")
        checks += [
            ("ASS dosyası oluştu", ass_path.exists()),
            ("ASS stil satırı var", "Style: Shorts," in ass_text),
            ("ASS Dialogue satırı var", ass_text.count("Dialogue:") > 5),
            ("Sarı vurgu rengi kullanıldı", "&H00FFFF&" in ass_text),
            ("PlayRes 1080x1920", "PlayResX: 1080" in ass_text and "PlayResY: 1920" in ass_text),
        ]

        # Süre sınırı testi: 200 saniyelik saçma bir aralık kısaltılmalı
        long_clip = analyzer._normalize_clip(
            {"start_time": 10, "end_time": 210}, FAKE_TRANSCRIPT["segments"], 300.0
        )
        checks.append(
            ("Uzun klip max süreye kısaltıldı", long_clip["duration"] <= config.MAX_CLIP_DURATION)
        )

        # Çok kısa aralık uzatılmalı
        short_clip = analyzer._normalize_clip(
            {"start_time": 100, "end_time": 108}, FAKE_TRANSCRIPT["segments"], 300.0
        )
        checks.append(
            ("Kısa klip min süreye uzatıldı", short_clip["duration"] >= config.MIN_CLIP_DURATION)
        )

        # Sezgisel yedek de çalışmalı
        fallback = llm_analyzer.heuristic_clip(FAKE_TRANSCRIPT)
        checks.append(("Sezgisel yedek klip üretti", fallback["duration"] > 0))
        checks.append(("Sezgisel yedekte çeviri yok", fallback["translated"] is False))

        print("\n" + "=" * 58)
        print("  LLM ANALYZER MOCK TESTLERİ")
        print("=" * 58)
        for name, passed in checks:
            print(f"  [{'OK  ' if passed else 'HATA'}] {name}")
            if not passed:
                failures += 1
        print("=" * 58)
        print(f"  Sonuç: {len(checks) - failures}/{len(checks)} test geçti")
        print("=" * 58 + "\n")

        print("Örnek plan (ilk 3 altyazı):")
        preview = dict(plan)
        preview["captions"] = plan["captions"][:3]
        print(json.dumps(preview, ensure_ascii=False, indent=2))

    except Exception as exc:  # Test altyapısı hatası
        import traceback

        print(f"TEST ÇALIŞTIRILAMADI: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        server.shutdown()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_tests())
