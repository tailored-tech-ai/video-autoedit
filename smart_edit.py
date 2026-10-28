#!/usr/bin/env python3
"""
Умный монтаж с голосовыми метками дублей.

Поверх обычного VAD-монтажа находит голосовую метку («кат-кат») и выбрасывает
запоротый дубль целиком — назад до предыдущей длинной паузы.

Протокол записи, под который это заточено:
  1. Перед каждым смысловым блоком — пауза 2 секунды. Она и есть точка возврата.
  2. Сбился — молчишь секунду, говоришь «кат-кат», молчишь секунду.
  3. Пауза 2 секунды и переписываешь блок с самого начала.

Почему так: пауза с обеих сторон гарантирует, что метка попадёт в отдельный
сегмент VAD. Тогда её ищут не расшифровкой всего файла, а проверкой только
коротких изолированных сегментов — секунды вместо минут. Границы реза берутся
из VAD (края реальных пауз), а не из таймингов Whisper, которые на словах
вроде растянутого «и» врут на секунды.

По умолчанию НИЧЕГО НЕ РЕНДЕРИТ — показывает, что будет вырезано. Рендер по --apply.

Примеры:
    # посмотреть, что найдётся
    python3 execution/smart_edit.py in.mov out.mp4 --enhance-audio --scale 2560:1440

    # отрендерить
    python3 execution/smart_edit.py in.mov out.mp4 --enhance-audio --scale 2560:1440 --apply

    # добить ручным резом, если метку не сказал
    python3 execution/smart_edit.py in.mov out.mp4 --cut 20.7-39.6 --cut 40.0-54.7 --apply
"""

import argparse
import difflib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jump_cut_vad_patched as jc

SR = 16000


# ---------------------------------------------------------------- утилиты

def fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:05.2f}"


def norm(s: str) -> str:
    """Оставить только буквы и цифры, в нижнем регистре."""
    return "".join(c for c in s.lower() if c.isalnum())


def gaps_around(segments, idx, duration):
    """Пауза перед сегментом и после него."""
    before = segments[idx][0] - segments[idx - 1][1] if idx > 0 else segments[idx][0]
    after = (segments[idx + 1][0] - segments[idx][1]
             if idx + 1 < len(segments) else duration - segments[idx][1])
    return before, after


def subtract(segments, cuts):
    """Вычесть из списка сегментов список интервалов."""
    out = []
    for a, b in segments:
        pieces = [(a, b)]
        for ca, cb in cuts:
            nxt = []
            for pa, pb in pieces:
                if cb <= pa or ca >= pb:
                    nxt.append((pa, pb))
                    continue
                if ca > pa:
                    nxt.append((pa, min(ca, pb)))
                if cb < pb:
                    nxt.append((max(cb, pa), pb))
            pieces = nxt
        out.extend(p for p in pieces if p[1] - p[0] > 0.05)
    return out


# ---------------------------------------------------------------- звук и распознавание

def load_audio(input_path):
    """Вытащить моно 16 кГц в numpy — один раз на весь запуск."""
    import numpy as np
    import soundfile as sf

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = tmp.name
    try:
        jc.extract_audio(input_path, wav, sample_rate=SR)
        y, _ = sf.read(wav, dtype="float32")
    finally:
        if os.path.exists(wav):
            os.remove(wav)
    if y.ndim > 1:
        y = y.mean(axis=1)
    return np.ascontiguousarray(y), wav


class Recognizer:
    """Ленивая обёртка: модель грузится только если действительно понадобилась."""

    def __init__(self, model_name, language):
        self.model_name = model_name
        self.language = language
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            print(f"   загружаю Whisper ({self.model_name})...", flush=True)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def text(self, audio, a, b):
        clip = audio[int(a * SR):int(b * SR)]
        if len(clip) < SR * 0.1:
            return ""
        segs, _ = self.model.transcribe(clip, language=self.language,
                                        vad_filter=False, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segs).strip()


# ---------------------------------------------------------------- поиск меток

def find_markers(segments, audio, rec, phrase, duration,
                 max_dur, min_isolation, threshold):
    """
    Кандидат в метки — короткий сегмент, окружённый паузами с обеих сторон.
    Распознаётся только он, а не весь файл.
    """
    target = norm(phrase)
    candidates = []
    for i, (a, b) in enumerate(segments):
        if b - a > max_dur:
            continue
        before, after = gaps_around(segments, i, duration)
        if before >= min_isolation and after >= min_isolation:
            candidates.append(i)

    print(f"🔎 Коротких изолированных сегментов: {len(candidates)} "
          f"(из {len(segments)}) — распознаю только их")
    if not candidates:
        return []

    markers = []
    for i in candidates:
        a, b = segments[i]
        said = rec.text(audio, a, b)
        score = difflib.SequenceMatcher(None, target, norm(said)).ratio()
        mark = "✅ МЕТКА" if score >= threshold else "  —"
        print(f'   {fmt(a)}-{fmt(b)}  «{said}»  похоже на «{phrase}»: {score:.2f}  {mark}')
        if score >= threshold:
            markers.append(i)
    return markers


def rewind(segments, marker_idx, checkpoint_gap, max_rewind, duration):
    """
    От метки назад до начала блока: удаляем сегменты, пока не удалим тот,
    перед которым стоит пауза длиннее checkpoint_gap. Он и есть начало блока.
    """
    doomed = {marker_idx}
    j = marker_idx - 1
    stopped_cleanly = False
    while j >= 0:
        if segments[marker_idx][1] - segments[j][0] > max_rewind:
            break
        doomed.add(j)
        before, _ = gaps_around(segments, j, duration)
        if before >= checkpoint_gap:
            stopped_cleanly = True
            break
        j -= 1
    else:
        stopped_cleanly = True  # дошли до начала файла

    lo = min(doomed)
    return (segments[lo][0], segments[marker_idx][1]), stopped_cleanly


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(
        description="VAD-монтаж + вырезание запоротых дублей по голосовой метке")
    p.add_argument("input")
    p.add_argument("output")

    p.add_argument("--marker", default="кат кат", help="фраза-метка (по умолчанию: «кат кат»)")
    p.add_argument("--checkpoint-gap", type=float, default=2.0,
                   help="пауза, до которой откатываемся назад (сек, по умолчанию 2.0)")
    p.add_argument("--max-rewind", type=float, default=90.0,
                   help="предохранитель: дальше этого назад не откатываемся (сек)")
    p.add_argument("--marker-max-dur", type=float, default=2.0,
                   help="метка не длиннее этого (сек)")
    p.add_argument("--marker-isolation", type=float, default=0.4,
                   help="минимальная пауза с каждой стороны от метки (сек)")
    p.add_argument("--marker-threshold", type=float, default=0.6,
                   help="порог похожести на метку, 0..1")
    p.add_argument("--whisper-model", default="large-v3",
                   help="модель для распознавания метки (small быстрее)")
    p.add_argument("--language", default="ru")
    p.add_argument("--cut", action="append", default=[], metavar="A-B",
                   help="ручной рез в секундах, можно несколько раз")
    p.add_argument("--no-markers", action="store_true", help="не искать метки, только --cut")
    p.add_argument("--apply", action="store_true",
                   help="рендерить (по умолчанию только предпросмотр)")

    # параметры базового конвейера
    p.add_argument("--min-silence", type=float, default=0.5)
    p.add_argument("--min-speech", type=float, default=0.25)
    p.add_argument("--padding", type=int, default=50)
    p.add_argument("--merge-gap", type=float, default=0.3)
    p.add_argument("--no-keep-start", action="store_false", dest="keep_start", default=True)
    p.add_argument("--enhance-audio", action="store_true")
    p.add_argument("--scale", default=None, help="например 2560:1440")
    p.add_argument("--apply-lut", default=None)
    p.add_argument("--workers", type=int, default=4)

    args = p.parse_args()

    if args.scale:
        jc.SCALE_FILTER = f"scale={args.scale}:flags=lanczos"

    duration = jc.get_duration(args.input)
    print(f"🎬 {Path(args.input).name}  —  {fmt(duration)}")

    # --- базовый конвейер: речь, склейка, паддинг
    print("🎵 Извлекаю звук...")
    audio, _ = load_audio(args.input)

    print("🎯 Silero VAD...")
    segments = jc.get_speech_timestamps_silero(
        _wav_from(audio), min_speech_duration=args.min_speech,
        min_silence_duration=args.min_silence)
    segments = jc.merge_close_segments(segments, args.merge_gap)
    segments = jc.add_padding(segments, args.padding / 1000, duration)
    print(f"   сегментов речи: {len(segments)}")

    cuts = []

    # --- ручные резы
    for spec in args.cut:
        try:
            a, b = (float(x) for x in spec.split("-", 1))
        except ValueError:
            print(f"❌ Не понял --cut {spec!r}, нужно вида 20.7-39.6")
            return 1
        cuts.append((a, b))
        print(f"✂️  Ручной рез: {fmt(a)} - {fmt(b)}  ({b-a:.2f}s)")

    # --- метки дублей
    rec = Recognizer(args.whisper_model, args.language)
    if not args.no_markers:
        markers = find_markers(segments, audio, rec, args.marker, duration,
                               args.marker_max_dur, args.marker_isolation,
                               args.marker_threshold)
        if not markers:
            print("   меток не найдено")
        for idx in markers:
            (a, b), clean = rewind(segments, idx, args.checkpoint_gap,
                                   args.max_rewind, duration)
            cuts.append((a, b))
            warn = "" if clean else "  ⚠️ упёрлось в предохранитель --max-rewind"
            print(f"\n✂️  Дубль: {fmt(a)} - {fmt(b)}  ({b-a:.1f}s){warn}")
            said = rec.text(audio, a, min(b, a + 60))
            print(f'   выбрасывается: «{said[:400]}»')

    if not cuts:
        print("\nРезать нечего — обычный монтаж по тишине.")

    # схлопнуть пересекающиеся резы, чтобы отчёт не двоился
    if cuts:
        cuts.sort()
        merged = [list(cuts[0])]
        for ca, cb in cuts[1:]:
            if ca <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], cb)
            else:
                merged.append([ca, cb])
        cuts = [tuple(c) for c in merged]

    # --- что останется
    kept = subtract(segments, cuts) if cuts else segments

    # keep-start применим только если до первого уцелевшего сегмента ничего не резали.
    # Иначе растягивание к 0:00 затащит вырезанное обратно — это баг оригинального скрипта.
    first_cut = min((ca for ca, _ in cuts), default=float("inf"))
    if args.keep_start and kept and kept[0][0] > 0:
        if kept[0][0] < first_cut:
            kept[0] = (0.0, kept[0][1])
            print("\n📌 Интро сохранено: первый сегмент растянут к 0:00")
        else:
            print("\n📌 Начало попало под рез — keep-start не применяю "
                  "(иначе вернёт вырезанное)")

    total = sum(b - a for a, b in kept)
    print(f"\n{'─'*58}")
    print(f"Исходник:        {fmt(duration)}")
    print(f"Только тишина:   {fmt(sum(b-a for a,b in segments))}")
    print(f"С резом дублей:  {fmt(total)}   ({len(kept)} сегментов)")
    print(f"{'─'*58}")

    # --- склейки: как прочитается шов
    if cuts:
        print("\n🔗 Швы:")
        for ca, cb in sorted(cuts):
            before = rec.text(audio, max(0, ca - 6), ca)
            after = rec.text(audio, cb, min(duration, cb + 6))
            print(f"   ...{before[-70:]}  ⟩⟨  {after[:70]}...")

    if not args.apply:
        print("\nЭто предпросмотр. Добавь --apply, чтобы отрендерить.")
        return 0

    print()
    jc.concatenate_segments_parallel(
        args.input, kept, args.output,
        enhance_audio=args.enhance_audio, lut_path=args.apply_lut,
        workers=args.workers, smart_render=False)
    return 0


def _wav_from(audio):
    """Silero принимает путь к файлу — кладём numpy обратно во временный wav."""
    import soundfile as sf
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    sf.write(tmp.name, audio, SR)
    return tmp.name


if __name__ == "__main__":
    sys.exit(main())
