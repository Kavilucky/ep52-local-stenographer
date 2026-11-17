#!/usr/bin/env python3
"""Оркестратор локального стенографиста (лид-магнит L02).

Конвейер: аудиофайл -> faster-whisper (локальный инференс, int8, CPU) -> transcript.txt
-> выгрузка модели Whisper из VRAM -> локальная модель Ollama по промпту system_prompt.txt
-> protocol.md.

Зависимости: стандартная библиотека + requests (HTTP к Ollama) + faster_whisper
(импортируется лениво, только когда нужен локальный режим расшифровки).

Важно про VRAM 4 ГБ: модель Whisper и модель Ollama не умеют жить в одной карте
одновременно. Поэтому перед вызовом Ollama воркер принудительно выгружает модель
Whisper из видеопамяти (освобождает объект модели, вызывает gc.collect() и, если
доступен torch, torch.cuda.empty_cache()), а затем дополнительно проверяет, что
память действительно освободилась (nvidia-smi), и просит Ollama выгрузить свои
модели через keep_alive=0. Только после этого запускается генерация протокола.

Никаких секретов в коде нет: адреса и имена моделей приходят из аргументов или
переменных окружения.
"""

from __future__ import annotations

import argparse
import gc
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover - зависимость ставится через requirements
    requests = None  # type: ignore[assignment]


def force_utf8_console() -> None:
    """Переводит консоль в UTF-8, иначе русский текст в --help и логах — кракозябры."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


# ----------------------------------------------------------------------------
# Утилиты вывода
# ----------------------------------------------------------------------------

def log(message: str) -> None:
    print(f"[worker] {message}", flush=True)


def die(message: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    log(f"ОШИБКА: {message}")
    raise SystemExit(code)


# ----------------------------------------------------------------------------
# Расшифровка: локальный режим (импорт faster_whisper)
# ----------------------------------------------------------------------------

def load_faster_whisper():
    """Ленивый импорт faster_whisper с понятным сообщением об ошибке."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        die(
            "Пакет faster_whisper не найден. Установите его: "
            "pip install faster-whisper\n"
            "Либо используйте --asr-backend service (расшифровка через "
            "HTTP-сервис faster-whisper из docker-compose.yml).",
            code=2,
        )
        raise exc  # pragma: no cover
    return WhisperModel


def transcribe_local(audio_path: Path, model_name: str, device: str,
                     compute_type: str, language: Optional[str]) -> Tuple[List[Dict[str, Any]], str]:
    """Расшифровка локально. Возвращает (сегменты, путь/имя модели в памяти)."""
    WhisperModel = load_faster_whisper()
    log(f"Загружаю модель faster-whisper '{model_name}' (device={device}, compute_type={compute_type})")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    log("Начинаю распознавание")
    try:
        segments_iter, info = model.transcribe(str(audio_path), language=language, vad_filter=True)
    except Exception as exc:  # noqa: BLE001
        die(
            f"Не удалось прочитать аудиофайл {audio_path}: {exc}\n"
            "Проверьте, что файл не повреждён и поддерживается (wav/mp3/m4a/flac/ogg).",
            code=3,
        )
        raise

    segments: List[Dict[str, Any]] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append({
            "start": float(seg.start),
            "end": float(seg.end),
            "text": text,
        })

    detected = getattr(info, "language", None) or language or "unknown"
    log(f"Распознано сегментов: {len(segments)}, язык: {detected}")

    # Модель Whisper держит веса в VRAM — отдаём наружу ссылку, чтобы выгрузить
    # память перед вызовом Ollama (см. release_whisper_from_vram).
    return segments, model


# ----------------------------------------------------------------------------
# Расшифровка: сервисный режим (HTTP к контейнеру faster-whisper)
# ----------------------------------------------------------------------------

def transcribe_service(audio_path: Path, asr_url: str, timeout: int = 3600) -> List[Dict[str, Any]]:
    if requests is None:
        die("Нужен пакет requests для сервисного режима: pip install requests", code=2)

    url = asr_url.rstrip("/") + "/asr"
    log(f"Отправляю аудио в faster-whisper: {url}")

    with open(audio_path, "rb") as fh:
        files = {"audio_file": (audio_path.name, fh)}
        data = {"response_format": "json", "task": "transcribe"}
        resp = requests.post(url, files=files, data=data, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()

    raw_segments = payload.get("segments") or []
    segments: List[Dict[str, Any]] = []
    for seg in raw_segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append({
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": text,
        })
    log(f"Распознано сегментов: {len(segments)}")
    return segments


# ----------------------------------------------------------------------------
# Освобождение VRAM
# ----------------------------------------------------------------------------

def release_whisper_from_vram(model: Any) -> None:
    """Принудительно выгружает модель Whisper из видеопамяти.

    Шаги: снять ссылку на объект модели -> gc.collect() -> при наличии torch
    вызвать torch.cuda.empty_cache(). После этого CTranslate2 освобождает CUDA
    allocator, и карта (даже 4 ГБ) остаётся для Ollama.
    """
    if model is None:
        return

    log("Выгружаю модель Whisper из VRAM")
    try:
        # У faster-whisper есть finalize/close у разных версий — зовём, если есть.
        for attr in ("finalize", "close", "release"):
            candidate = getattr(model, attr, None)
            if callable(candidate):
                candidate()
                break
    except Exception as exc:  # noqa: BLE001 - освобождение памяти не должно ронять пайплайн
        log(f"Мягкое закрытие модели не сработало ({exc}), продолжаю")

    try:
        del model
    except Exception:  # noqa: BLE001
        pass
    gc.collect()

    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            log("torch.cuda.empty_cache() выполнен")
        else:
            log("torch CUDA недоступен — очистка кэша не требуется")
    except ImportError:
        log("torch не установлен — считаем, что вычисления шли на CPU")
    except Exception as exc:  # noqa: BLE001
        log(f"Не удалось очистить CUDA-кэш: {exc}")


def nvidia_free_memory_mb() -> Optional[int]:
    """Свободная VRAM в МБ по nvidia-smi; None, если утилиты нет."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return int(out.stdout.strip().splitlines()[0].strip())
    except Exception:  # noqa: BLE001
        return None


def wait_for_vram_release(min_free_mb: int, timeout: int = 60, interval: int = 2) -> None:
    """Ждёт, пока освободится хотя бы min_free_mb видеопамяти."""
    free = nvidia_free_memory_mb()
    if free is None:
        log("nvidia-smi недоступна — проверка VRAM пропущена")
        return
    log(f"Свободно VRAM после выгрузки: {free} МБ (нужно минимум {min_free_mb} МБ)")
    waited = 0
    while free < min_free_mb and waited < timeout:
        time.sleep(interval)
        waited += interval
        free = nvidia_free_memory_mb()
        if free is None:
            return
        log(f"Ожидание освобождения VRAM... свободно {free} МБ (ждали {waited} с)")
    if free < min_free_mb:
        log(f"ВНИМАНИЕ: свободно только {free} МБ VRAM, генерация может упасть по памяти")


def ollama_unload_models(ollama_url: str, timeout: int = 60) -> None:
    """Просит Ollama выгрузить все загруженные модели (keep_alive=0).

    Это освобождает VRAM перед тем, как воркер начнёт генерацию.
    """
    if requests is None:
        return
    try:
        resp = requests.get(ollama_url.rstrip("/") + "/api/ps", timeout=timeout)
        resp.raise_for_status()
        loaded = resp.json().get("models") or []
    except Exception as exc:  # noqa: BLE001
        log(f"Не удалось получить список моделей Ollama: {exc}")
        loaded = []

    names = []
    for item in loaded:
        name = item.get("name") or item.get("model")
        if name:
            names.append(name)

    if not names:
        log("В Ollama сейчас нет загруженных моделей")
        return

    log(f"Выгружаю модели Ollama из VRAM: {', '.join(names)}")
    for name in names:
        try:
            requests.post(
                ollama_url.rstrip("/") + "/api/generate",
                json={"model": name, "keep_alive": 0},
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось выгрузить модель {name}: {exc}")


# ----------------------------------------------------------------------------
# Протокол через Ollama
# ----------------------------------------------------------------------------

def read_system_prompt(path: Path) -> str:
    if not path.is_file():
        die(f"Файл промпта не найден: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        die(f"Файл промпта пуст: {path}")
    return text


def build_transcript_text(segments: List[Dict[str, Any]]) -> str:
    """Текст расшифровки с таймкодами — и для transcript.txt, и для промпта."""
    lines: List[str] = []
    for seg in segments:
        lines.append(f"[{format_ts(seg['start'])} --> {format_ts(seg['end'])}] {seg['text']}")
    return "\n".join(lines)


def format_ts(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def generate_protocol(ollama_url: str, ollama_model: str, system_prompt: str,
                       transcript_text: str, timeout: int = 1800,
                       keep_alive: int = 0) -> str:
    if requests is None:
        die("Нужен пакет requests для обращения к Ollama: pip install requests", code=2)

    url = ollama_url.rstrip("/") + "/api/generate"
    log(f"Генерирую протокол: модель {ollama_model}")
    resp = requests.post(
        url,
        json={
            "model": ollama_model,
            "system": system_prompt,
            "prompt": f"РАСШИФРОВКА ВСТРЕЧИ:\n\n{transcript_text}",
            "stream": False,
            # keep_alive=0 (по умолчанию): модель выгружается из VRAM сразу после ответа,
            # иначе следующий прогон упрётся в нехватку памяти на 4 ГБ.
            "keep_alive": keep_alive,
            "options": {"temperature": 0.2},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    text = (payload.get("response") or "").strip()
    if not text:
        die("Ollama вернула пустой ответ")
    return text


# ----------------------------------------------------------------------------
# Запись результатов
# ----------------------------------------------------------------------------

def write_outputs(output_dir: Path, transcript_text: str, protocol_text: str,
                  audio_path: Path, ollama_model: str) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = output_dir / "transcript.txt"
    header = (
        f"# Расшифровка: {audio_path.name}\n"
        f"# Сгенерировано локально: faster-whisper (int8, CPU) + Ollama ({ollama_model})\n"
        f"# Формат строки: [таймкод начала --> таймкод конца] текст\n\n"
    )
    transcript_path.write_text(header + transcript_text + "\n", encoding="utf-8")

    protocol_path = output_dir / "protocol.md"
    proto_header = (
        f"# Протокол встречи\n\n"
        f"- Источник: {audio_path.name}\n"
        f"- Локальная модель: {ollama_model}\n\n"
        f"---\n\n"
    )
    protocol_path.write_text(proto_header + protocol_text + "\n", encoding="utf-8")

    return transcript_path, protocol_path


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="worker.py",
        description="Локальный стенографист: расшифровка встречи (faster-whisper) "
                    "и протокол по ней (Ollama). Всё считается локально.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--audio", required=True, help="Путь к аудиофайлу записи")
    parser.add_argument("--output-dir", default="./out", help="Куда писать transcript.txt и protocol.md")
    parser.add_argument("--system-prompt", default="system_prompt.txt", help="Файл промпта секретаря-референта")
    parser.add_argument("--asr-backend", choices=("service", "local"), default="service",
                        help="service — HTTP-сервис faster-whisper; local — импорт faster_whisper в этом процессе")
    parser.add_argument("--asr-url", default=os.environ.get("ASR_URL", "http://faster-whisper:9000"),
                        help="Базовый URL сервиса faster-whisper (режим service)")
    parser.add_argument("--whisper-model", default=os.environ.get("WHISPER_MODEL", "small"),
                        help="Модель faster-whisper: small или medium")
    parser.add_argument("--whisper-device", default=os.environ.get("WHISPER_DEVICE", "cpu"),
                        help="Устройство вычислений: cpu или cuda")
    parser.add_argument("--compute-type", default=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
                        help="Квантизация faster-whisper: int8, int8_float16, float16 и т.п.")
    parser.add_argument("--language", default="ru", help="Язык распознавания (пусто — автоопределение)")
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
                        help="Базовый URL Ollama")
    parser.add_argument("--ollama-model", default=os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"),
                        help="Локальная модель Ollama для составления протокола")
    parser.add_argument("--min-free-vram-mb", type=int, default=1500,
                        help="Сколько свободной VRAM ждать перед генерацией")
    parser.add_argument("--keep-alive", type=int, default=0,
                        help="keep_alive для Ollama: 0 — выгрузить модель сразу после ответа")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    force_utf8_console()
    args = parse_args(argv)

    audio_path = Path(args.audio).expanduser()
    if not audio_path.is_file():
        die(f"Аудиофайл не найден: {audio_path}")

    output_dir = Path(args.output_dir).expanduser()
    prompt_path = Path(args.system_prompt).expanduser()
    system_prompt = read_system_prompt(prompt_path)

    whisper_model_obj: Any = None
    if args.asr_backend == "local":
        segments, whisper_model_obj = transcribe_local(
            audio_path, args.whisper_model, args.whisper_device, args.compute_type,
            args.language or None,
        )
    else:
        segments = transcribe_service(audio_path, args.asr_url)

    if not segments:
        die("Расшифровка пуста — нечего превращать в протокол")

    transcript_text = build_transcript_text(segments)

    # --- Освобождаем VRAM до вызова Ollama (критично для карт 4 ГБ) ---
    if whisper_model_obj is not None:
        release_whisper_from_vram(whisper_model_obj)
    else:
        log("Режим service: модель Whisper в этом процессе не загружалась")
    ollama_unload_models(args.ollama_url)
    wait_for_vram_release(args.min_free_vram_mb)

    protocol_text = generate_protocol(
        args.ollama_url, args.ollama_model, system_prompt, transcript_text,
        keep_alive=args.keep_alive,
    )

    transcript_path, protocol_path = write_outputs(
        output_dir, transcript_text, protocol_text, audio_path, args.ollama_model
    )
    log(f"Готово: {transcript_path}")
    log(f"Готово: {protocol_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
