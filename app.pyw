"""강의 자막 받아쓰기 창. 더블클릭으로 연다.

북마클릿이 복사해 준 명령(또는 m3u8 주소)이 클립보드에 있으면 창을 열거나 창으로 돌아올 때 알아서 채운다.
실제 일은 transcribe.py 가 하고, 이 파일은 진행 막대와 받아쓰기 미리보기만 맡는다.
"""

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# pythonw 로 열면 콘솔이 없어 stdout/stderr 가 None 이라, 라이브러리가 진행 표시를 쓰다 죽지 않게 막는다
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream) is None:
        setattr(sys, _stream, open(os.devnull, "w", encoding="utf-8"))

sys.path.insert(0, str(Path(__file__).parent))
import transcribe as tr  # noqa: E402

MODELS = {
    "정확 (large-v3-turbo)": "large-v3-turbo",
    "보통 (medium)": "medium",
    "빠름 (small)": "small",
}
_cores = os.cpu_count() or 4
THREADS = {
    "조용히 (2코어)": 2,
    "보통 (4코어)": 4,
    f"빠르게 ({max(4, _cores // 2)}코어)": max(4, _cores // 2),
}


def parse_clipboard(text: str) -> tuple[str, str | None] | None:
    """북마클릿이 복사한 `"<주소>" -o <이름>` 이나 주소만 있는 글에서 주소와 이름을 꺼낸다."""
    m = re.search(r'https?://[^\s"]+?\.m3u8[^\s"]*', text)
    if not m:
        return None
    n = re.search(r'\s-o\s+"?([^"\s]+)', text)
    return m.group(0), (n.group(1) if n else None)


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" ._")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing = False
        self.last_clip = ""
        self.txt_path: Path | None = None
        self.t0 = 0.0

        root.title("강의 자막")
        root.geometry("760x640")
        root.minsize(560, 480)
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<FocusIn>", lambda e: self.fill_from_clipboard(auto=True))
        root.after(200, lambda: self.fill_from_clipboard(auto=True))
        root.after(100, self.poll)

    # ---------- 화면 ----------
    def _build(self):
        pad = {"padx": 12, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", pady=(10, 0))
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="강의 영상").grid(row=0, column=0, sticky="w", **pad)
        self.source = tk.StringVar()
        ttk.Entry(top, textvariable=self.source).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="붙여넣기", command=self.fill_from_clipboard).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(top, text="파일…", command=self.pick_file).grid(row=0, column=3, padx=(6, 12))

        ttk.Label(top, text="저장 이름").grid(row=1, column=0, sticky="w", **pad)
        self.name = tk.StringVar()
        ttk.Entry(top, textvariable=self.name).grid(row=1, column=1, sticky="ew", pady=4)

        opts = ttk.Frame(top)
        opts.grid(row=2, column=0, columnspan=4, sticky="w", padx=12, pady=4)
        ttk.Label(opts, text="정확도").pack(side="left")
        self.model = ttk.Combobox(opts, values=list(MODELS), state="readonly", width=20)
        self.model.current(0)
        self.model.pack(side="left", padx=(8, 20))
        ttk.Label(opts, text="CPU 사용").pack(side="left")
        self.threads = ttk.Combobox(opts, values=list(THREADS), state="readonly", width=16)
        self.threads.current(1)
        self.threads.pack(side="left", padx=8)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=12, pady=(6, 4))
        self.start_btn = ttk.Button(btns, text="▶  받아쓰기 시작", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="■  중지", command=self.request_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        ttk.Separator(self.root).pack(fill="x", padx=12, pady=8)

        self.stage = tk.StringVar(value="강의 영상 화면에서 북마크 '강의 자막'을 누른 뒤 이 창으로 돌아오세요.")
        self.stage_label = ttk.Label(self.root, textvariable=self.stage, font=("Malgun Gothic", 12, "bold"))
        self.stage_label.pack(anchor="w", padx=12)

        self.bar = ttk.Progressbar(self.root, maximum=100)
        self.bar.pack(fill="x", padx=12, pady=6)

        info = ttk.Frame(self.root)
        info.pack(fill="x", padx=12)
        self.pos = tk.StringVar()
        self.eta = tk.StringVar()
        ttk.Label(info, textvariable=self.pos).pack(side="left")
        ttk.Label(info, textvariable=self.eta).pack(side="right")

        # 아래 버튼 줄을 먼저 붙여야 창이 작아져도 받아쓰기 칸에 밀려 사라지지 않는다
        bottom = ttk.Frame(self.root)
        bottom.pack(side="bottom", fill="x", padx=12, pady=(0, 12))
        ttk.Button(bottom, text="결과 폴더 열기", command=self.open_folder).pack(side="left")
        self.open_txt_btn = ttk.Button(bottom, text="받아쓰기 파일 열기", command=self.open_txt, state="disabled")
        self.open_txt_btn.pack(side="left", padx=6)

        box = ttk.Frame(self.root)
        box.pack(fill="both", expand=True, padx=12, pady=8)
        self.text = tk.Text(box, wrap="word", font=("Malgun Gothic", 10), state="disabled", height=8,
                            relief="solid", borderwidth=1, padx=8, pady=6)
        scroll = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("time", foreground="#888888")

    # ---------- 입력 ----------
    def fill_from_clipboard(self, auto: bool = False):
        if self.running():
            return
        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            clip = ""
        if auto and clip == self.last_clip:
            return
        self.last_clip = clip
        parsed = parse_clipboard(clip)
        if not parsed:
            if not auto:
                messagebox.showinfo("강의 자막", "클립보드에 강의 영상 주소(m3u8)가 없습니다.\n"
                                                 "강의 영상 화면에서 북마크 '강의 자막'을 먼저 눌러 주세요.")
            return
        url, name = parsed
        if auto and url == self.source.get():
            return
        self.source.set(url)
        self.name.set(name or tr.guess_name(url))
        self.stage.set("주소를 채웠습니다. '받아쓰기 시작'을 누르세요.")

    def pick_file(self):
        path = filedialog.askopenfilename(
            title="강의 영상 또는 음성 파일",
            filetypes=[("영상/음성", "*.mp4 *.mkv *.webm *.mov *.m4a *.mp3 *.wav"), ("모든 파일", "*.*")])
        if path:
            self.source.set(path)
            self.name.set(Path(path).stem)

    # ---------- 실행 ----------
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def start(self):
        source = self.source.get().strip().strip('"')
        if not source:
            messagebox.showwarning("강의 자막", "강의 영상 주소나 파일을 넣어 주세요.")
            return
        name = safe_name(self.name.get()) or safe_name(tr.guess_name(source)) or "lecture"
        self.name.set(name)
        tr.OUT_DIR.mkdir(exist_ok=True)
        wav, txt, srt = (tr.OUT_DIR / f"{name}{ext}" for ext in (".wav", ".txt", ".srt"))
        if txt.exists() and not messagebox.askyesno("강의 자막", f"{txt.name} 이(가) 이미 있습니다. 덮어쓸까요?"):
            return

        self.txt_path = txt
        self.stop.clear()
        self.set_running(True)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.bar.configure(value=0, mode="determinate")
        self.pos.set("")
        self.eta.set("")

        args = (source, wav, txt, srt, MODELS[self.model.get()], THREADS[self.threads.get()])
        self.worker = threading.Thread(target=self.work, args=args, daemon=True)
        self.worker.start()

    def work(self, source, wav, txt, srt, model, threads):
        put = self.events.put
        try:
            if wav.exists():
                put(("stage", "1/2  이미 뽑아 둔 음성을 씁니다"))
            else:
                put(("stage", "1/2  음성 뽑는 중"))
                tr.extract_audio(source, wav, should_stop=self.stop.is_set,
                                 on_progress=lambda done, total: put(("extract", done, total)))
            put(("loading", f"2/2  받아쓰기 준비 중 ({model} 불러오는 중)"))
            tr.transcribe(wav, txt, srt, model, cpu_threads=threads, should_stop=self.stop.is_set,
                          on_start=lambda total: put(("begin", total)),
                          on_segment=lambda s, e, total, text: put(("seg", s, e, total, text)))
            put(("done",))
        except tr.Cancelled:
            put(("cancelled",))
        except Exception as e:  # 창에 보여 주고 끝낸다
            put(("error", str(e)))

    def request_stop(self):
        self.stop.set()
        self.stop_btn.configure(state="disabled")
        self.stage.set("멈추는 중… (지금 하던 문장까지만 하고 멈춥니다)")

    # ---------- 진행 표시 ----------
    def poll(self):
        try:
            while True:
                self.handle(*self.events.get_nowait())
        except queue.Empty:
            pass
        if self.closing and not self.running():
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def handle(self, kind, *data):
        if kind == "stage":
            self.stage.set(data[0])
            self.bar.configure(value=0)
        elif kind == "extract":
            done, total = data
            if total:
                pct = min(done / total * 100, 100)
                self.bar.configure(value=pct)
                self.pos.set(f"{tr.fmt_txt(done)} / {tr.fmt_txt(total)}  ·  {pct:.0f}%")
            else:
                self.pos.set(f"{tr.fmt_txt(done)} 뽑음")
        elif kind == "loading":
            self.stage.set(data[0])
            self.pos.set("")
            self.bar.configure(mode="indeterminate")
            self.bar.start(15)
        elif kind == "begin":
            self.bar.stop()
            self.bar.configure(mode="determinate", value=0)
            self.stage.set("2/2  받아쓰는 중")
            self.pos.set(f"00:00 / {tr.fmt_txt(data[0])}  ·  0%")
            self.t0 = time.monotonic()
        elif kind == "seg":
            start, end, total, text = data
            pct = min(end / total * 100, 100) if total else 0
            self.bar.configure(value=pct)
            self.pos.set(f"{tr.fmt_txt(end)} / {tr.fmt_txt(total)}  ·  {pct:.0f}%")
            self.root.title(f"{pct:.0f}% - 강의 자막")
            elapsed = time.monotonic() - self.t0
            eta = f"경과 {tr.fmt_txt(elapsed)}"
            if end > 30:  # 초반 몇 문장으로는 남은 시간이 크게 흔들린다
                eta += f"  ·  남은 시간 약 {tr.fmt_txt(elapsed / end * (total - end))}"
            self.eta.set(eta)
            self.append(start, text)
        elif kind == "done":
            self.finish("완료!  결과는 out 폴더에 txt·srt 로 저장됐습니다.", "#1a7f37")
            self.bar.configure(value=100)
            self.root.title("완료 - 강의 자막")
            self.root.bell()
        elif kind == "cancelled":
            self.finish("중지했습니다. 지금까지 받아쓴 부분은 저장돼 있습니다.", "#9a6700")
        elif kind == "error":
            self.finish("오류로 멈췄습니다.", "#cf222e")
            messagebox.showerror("강의 자막", data[0])

    def append(self, start, text):
        at_bottom = self.text.yview()[1] > 0.99
        self.text.configure(state="normal")
        self.text.insert("end", f"[{tr.fmt_txt(start)}] ", "time")
        self.text.insert("end", text + "\n")
        self.text.configure(state="disabled")
        if at_bottom:  # 위로 올려 읽는 중이면 끌어내리지 않는다
            self.text.see("end")

    def finish(self, message, color):
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.stage.set(message)
        self.stage_label.configure(foreground=color)
        self.set_running(False)

    def set_running(self, on: bool):
        self.start_btn.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        self.open_txt_btn.configure(state="disabled" if on else
                                    ("normal" if self.txt_path and self.txt_path.exists() else "disabled"))
        if on:
            self.stage_label.configure(foreground="")
            self.root.title("강의 자막")

    # ---------- 기타 ----------
    def open_folder(self):
        tr.OUT_DIR.mkdir(exist_ok=True)
        os.startfile(tr.OUT_DIR)

    def open_txt(self):
        if self.txt_path and self.txt_path.exists():
            os.startfile(self.txt_path)

    def on_close(self):
        if not self.running():
            self.root.destroy()
            return
        if messagebox.askyesno("강의 자막", "받아쓰는 중입니다. 멈추고 닫을까요?\n(지금까지 받아쓴 부분은 남습니다)"):
            self.request_stop()
            self.stage.set("멈추는 중… 끝나면 창이 닫힙니다")
            self.closing = True  # ffmpeg 가 남아 돌지 않게, 작업이 정리된 뒤 poll 에서 닫는다


def main():
    try:  # 고해상도 화면에서 글씨가 흐리지 않게
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.option_add("*Font", ("Malgun Gothic", 10))
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
