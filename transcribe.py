"""온라인 강의 음성을 받아써서 자막 텍스트로 저장한다.

사용법:
    python transcribe.py <m3u8 주소 또는 로컬 영상/음성 파일> [-o 이름] [--model 모델]

창으로 쓰려면 app.pyw 를 더블클릭.

결과 (out/ 폴더):
    <이름>.wav  - 추출한 음성 (16kHz 모노)
    <이름>.txt  - [mm:ss] 시간 표시가 붙은 받아쓰기
    <이름>.srt  - 영상 플레이어에 붙일 수 있는 자막
"""

import argparse
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

OUT_DIR = Path(__file__).parent / "out"

# 창(pythonw)에서 부를 때 ffmpeg 콘솔 창이 깜빡 뜨지 않게
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Cancelled(Exception):
    """사용자가 중지를 눌렀다."""


def guess_name(source: str) -> str:
    # 주소가 .../<번호>_720p.mp4/playlist.m3u8 꼴이면 <번호>를 이름으로 쓴다
    m = re.search(r"/(\d+)_\d+p\.mp4", source)
    if m:
        return m.group(1)
    return Path(source).stem


def probe_duration(source: str) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", source],
        capture_output=True, text=True, creationflags=_NO_WINDOW,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def extract_audio(source: str, wav: Path, on_progress=None, should_stop=None) -> None:
    """on_progress(지난 초, 전체 초 또는 None) 으로 진행을 알린다."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg 를 찾을 수 없습니다. 설치 후 PATH 에 추가하세요.")
    if wav.exists():
        print(f"이미 있는 음성 사용: {wav}")
        return
    print("음성 추출 중...")
    total = probe_duration(source) if on_progress else None
    # 중간에 멈추거나 실패해도 반쪽짜리 wav 가 '이미 있는 음성'으로 재사용되지 않게 임시 이름에 쓴다
    part = wav.with_suffix(".part.wav")
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
         "-y", "-i", source, "-vn", "-ac", "1", "-ar", "16000", str(part)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NO_WINDOW,
    )
    stopped = False
    for line in proc.stdout:
        if should_stop and should_stop():
            proc.terminate()
            stopped = True
            break
        key, _, val = line.strip().partition("=")
        if key == "out_time_us" and on_progress and val.isdigit():
            on_progress(int(val) / 1e6, total)
    err = proc.stderr.read().strip()
    proc.wait()
    if stopped:
        part.unlink(missing_ok=True)
        raise Cancelled
    if proc.returncode != 0:
        part.unlink(missing_ok=True)
        last = err.splitlines()[-1] if err else ""
        raise RuntimeError("음성 추출 실패. 403 이면 서버가 로그인을 요구해서 이 방식으로는 받을 수 없습니다."
                           + (f"\n{last}" if last else ""))
    part.replace(wav)


def load_wav(wav: Path) -> np.ndarray:
    # faster-whisper 의 파일 디코딩은 PyAV 19 와 맞지 않아(metadata_errors 인자 제거됨),
    # ffmpeg 가 만든 16kHz 모노 wav 를 직접 읽어 배열로 넘긴다
    with wave.open(str(wav), "rb") as w:
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def fmt_txt(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02}:{s:02}" if m < 60 else f"{m // 60}:{m % 60:02}:{s:02}"


_model_cache: dict = {}


def load_model(model_name: str, cpu_threads: int = 0):
    """대기열로 여러 강의를 이어 받아쓸 때 매번 다시 불러오지 않게, 마지막에 쓴 모델 하나를 들고 있는다."""
    # faster_whisper 는 불러오는 데 몇 초 걸려서, 창이 늦게 뜨지 않게 쓸 때 불러온다
    from faster_whisper import WhisperModel

    key = (model_name, cpu_threads)
    if key not in _model_cache:
        _model_cache.clear()  # 다른 모델로 바꾸면 앞의 것은 놓아 메모리를 아낀다
        _model_cache[key] = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=cpu_threads)
    return _model_cache[key]


def transcribe(wav: Path, txt: Path, srt: Path, model_name: str, cpu_threads: int = 0,
               on_start=None, on_segment=None, should_stop=None) -> None:
    """on_start(전체 초) 는 받아쓰기 시작 때, on_segment(시작, 끝, 전체, 문장) 은 한 문장마다 불린다.
    cpu_threads 0 은 faster-whisper 기본값(4)."""
    model = load_model(model_name, cpu_threads)
    segments, info = model.transcribe(load_wav(wav), language="ko", vad_filter=True)

    total = info.duration
    if on_start:
        on_start(total)
    with txt.open("w", encoding="utf-8") as ft, srt.open("w", encoding="utf-8") as fs:
        for i, seg in enumerate(segments, 1):
            if should_stop and should_stop():
                raise Cancelled
            text = seg.text.strip()
            ft.write(f"[{fmt_txt(seg.start)}] {text}\n")
            fs.write(f"{i}\n{fmt_srt(seg.start)} --> {fmt_srt(seg.end)}\n{text}\n\n")
            ft.flush()
            fs.flush()
            if on_segment:
                on_segment(seg.start, seg.end, total, text)


def print_progress(start: float, end: float, total: float, text: str) -> None:
    print(f"\r진행 {end / total * 100:5.1f}%  ({fmt_txt(end)} / {fmt_txt(total)})",
          end="", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="강의 음성 받아쓰기")
    p.add_argument("source", help="m3u8 주소 또는 로컬 파일 경로")
    p.add_argument("-o", "--name", help="결과 파일 이름 (기본: 주소에서 추정)")
    p.add_argument("--model", default="large-v3-turbo",
                   help="faster-whisper 모델 (느리면 small, medium)")
    args = p.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    name = args.name or guess_name(args.source)
    wav = OUT_DIR / f"{name}.wav"
    txt = OUT_DIR / f"{name}.txt"
    srt = OUT_DIR / f"{name}.srt"

    try:
        extract_audio(args.source, wav)
        print(f"모델 불러오는 중: {args.model} (처음엔 내려받느라 오래 걸림)")
        transcribe(wav, txt, srt, args.model, on_segment=print_progress)
    except RuntimeError as e:
        sys.exit(str(e))
    print()
    print(f"완료: {txt}\n      {srt}")


if __name__ == "__main__":
    main()
