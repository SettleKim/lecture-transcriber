"""강의 자막 받아쓰기 창. 더블클릭으로 연다.

북마클릿이 복사해 준 명령(또는 m3u8 주소)이 클립보드에 있으면 창을 열거나 창으로 돌아올 때 알아서 채운다.
강의를 대기열에 여러 개 넣어 두면 위에서부터 차례로 받아쓴다. 받아쓰기는 CPU 를 다 쓰므로 한 번에 하나씩 하고,
CPU 를 거의 안 쓰는 음성 받기는 그동안 다음 강의들 것을 미리 해 둔다.
대기열은 out/queue.json 에 저장돼서 창을 닫았다 열어도 남는다.
실제 일은 transcribe.py 가 하고, 이 파일은 대기열과 진행 표시만 맡는다.
"""

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from itertools import count
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

# 대기열 한 줄의 상태
WAITING, FETCHING, READY, WORKING = "대기", "음성 받는 중", "음성 준비됨", "받아쓰는 중"
DONE, FAILED, STOPPED = "완료", "오류", "중지됨"
PENDING = (WAITING, FETCHING, READY)  # 아직 받아쓸 차례가 남은 상태
ACTIVE = (FETCHING, WORKING)          # 스레드가 붙잡고 있는 상태 (뺄 수 없음)


def parse_clipboard(text: str) -> tuple[str, str | None] | None:
    """북마클릿이 복사한 `"<주소>" -o <이름>` 이나 주소만 있는 글에서 주소와 이름을 꺼낸다."""
    m = re.search(r'https?://[^\s"]+?\.m3u8[^\s"]*', text)
    if not m:
        return None
    n = re.search(r'\s-o\s+"?([^"\s]+)', text)
    return m.group(0), (n.group(1) if n else None)


def queue_file() -> Path:
    return tr.OUT_DIR / "queue.json"


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" ._")


_ids = count(1)


@dataclass(eq=False)
class Job:
    source: str
    name: str
    state: str = WAITING
    detail: str = ""  # 상태 옆에 붙는 진행률이나 짧은 오류
    error: str = ""   # 오류 전문 (두 번 클릭하면 보여 줌)
    iid: str = field(default_factory=lambda: f"job{next(_ids)}")

    def path(self, ext: str) -> Path:
        return tr.OUT_DIR / f"{self.name}{ext}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        # jobs 의 순서와 각 Job.state 는 cond 를 잡고 바꾼다 (창과 두 스레드가 같이 본다)
        self.jobs: list[Job] = []
        self.cond = threading.Condition()
        self.active = False            # 대기열이 돌고 있는가 (cond 를 잡고 바꾼다)
        self.stop = threading.Event()  # 중지 버튼: 하던 일까지 끊는다
        self.threads: list[threading.Thread] = []
        self.closing = False
        self.last_clip = ""
        self.current: Job | None = None  # 지금 받아쓰는(또는 마지막으로 받아쓴) 강의
        self.t0 = 0.0

        root.title("강의 자막")
        root.geometry("780x760")
        root.minsize(600, 600)
        self._build()
        self.load_queue()
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
        src_entry = ttk.Entry(top, textvariable=self.source)
        src_entry.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="붙여넣기", command=self.fill_from_clipboard).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(top, text="파일…", command=self.pick_files).grid(row=0, column=3, padx=(6, 12))

        ttk.Label(top, text="저장 이름").grid(row=1, column=0, sticky="w", **pad)
        self.name = tk.StringVar()
        name_entry = ttk.Entry(top, textvariable=self.name)
        name_entry.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="＋ 대기열에 추가", command=self.add_from_fields).grid(
            row=1, column=2, columnspan=2, sticky="ew", padx=(6, 12))
        for entry in (src_entry, name_entry):
            entry.bind("<Return>", lambda e: self.add_from_fields())

        opts = ttk.Frame(top)
        opts.grid(row=2, column=0, columnspan=4, sticky="w", padx=12, pady=4)
        ttk.Label(opts, text="정확도").pack(side="left")
        self.model = ttk.Combobox(opts, values=list(MODELS), state="readonly", width=20)
        self.model.current(0)
        self.model.pack(side="left", padx=(8, 20))
        ttk.Label(opts, text="CPU 사용").pack(side="left")
        self.threads_box = ttk.Combobox(opts, values=list(THREADS), state="readonly", width=16)
        self.threads_box.current(1)
        self.threads_box.pack(side="left", padx=8)
        ttk.Label(opts, text="(돌리는 중에 바꾸면 다음 강의부터)", foreground="#888888").pack(side="left")
        # 받아쓰기 스레드가 tk 를 건드리지 않게, 고른 값을 평범한 속성에 옮겨 둔다
        self.model_name = MODELS[self.model.get()]
        self.cpu_threads = THREADS[self.threads_box.get()]
        self.model.bind("<<ComboboxSelected>>", self.on_options)
        self.threads_box.bind("<<ComboboxSelected>>", self.on_options)

        # ----- 대기열 -----
        qframe = ttk.LabelFrame(self.root, text="대기열  (끌어서 순서 바꾸기 · 두 번 클릭하면 결과/오류 보기)")
        qframe.pack(fill="x", padx=12, pady=(8, 4))
        self.tree = ttk.Treeview(qframe, columns=("no", "name", "state"), show="headings",
                                 height=6, selectmode="browse")
        self.tree.heading("no", text="#")
        self.tree.heading("name", text="저장 이름")
        self.tree.heading("state", text="상태")
        self.tree.column("no", width=36, anchor="center", stretch=False)
        self.tree.column("name", width=300)
        self.tree.column("state", width=260)
        self.tree.tag_configure(DONE, foreground="#1a7f37")
        self.tree.tag_configure(FAILED, foreground="#cf222e")
        self.tree.tag_configure(STOPPED, foreground="#9a6700")
        self.tree.tag_configure(WORKING, foreground="#0550ae")
        qscroll = ttk.Scrollbar(qframe, command=self.tree.yview)
        self.tree.configure(yscrollcommand=qscroll.set)

        side = ttk.Frame(qframe)
        side.pack(side="right", fill="y", padx=(6, 6), pady=4)
        ttk.Button(side, text="▲ 위로", width=9, command=lambda: self.move_selected(-1)).pack(pady=1)
        ttk.Button(side, text="▼ 아래로", width=9, command=lambda: self.move_selected(+1)).pack(pady=1)
        ttk.Button(side, text="맨 위로", width=9, command=lambda: self.move_selected(-10**6)).pack(pady=1)
        ttk.Button(side, text="빼기", width=9, command=self.remove_selected).pack(pady=(10, 1))
        ttk.Button(side, text="완료 지우기", width=9, command=self.clear_finished).pack(pady=1)
        qscroll.pack(side="right", fill="y", pady=4)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)

        self.tree.bind("<ButtonPress-1>", self.on_press)
        self.tree.bind("<B1-Motion>", self.on_drag)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Alt-Up>", lambda e: (self.move_selected(-1), "break")[1])
        self.tree.bind("<Alt-Down>", lambda e: (self.move_selected(+1), "break")[1])
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_buttons())

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=12, pady=(6, 4))
        self.start_btn = ttk.Button(btns, text="▶  대기열 시작", command=self.start)
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
        self.text = tk.Text(box, wrap="word", font=("Malgun Gothic", 10), state="disabled", height=6,
                            relief="solid", borderwidth=1, padx=8, pady=6)
        scroll = ttk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("time", foreground="#888888")

    def on_options(self, _e=None):
        self.model_name = MODELS[self.model.get()]
        self.cpu_threads = THREADS[self.threads_box.get()]

    # ---------- 입력 ----------
    def fill_from_clipboard(self, auto: bool = False):
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
        if auto and (url == self.source.get() or any(j.source == url for j in self.jobs)):
            return
        self.source.set(url)
        self.name.set(name or tr.guess_name(url))
        if not self.running():
            self.stage_label.configure(foreground="")
            self.stage.set("주소를 채웠습니다. '대기열에 추가'를 누르세요.")

    def pick_files(self):
        paths = filedialog.askopenfilenames(
            title="강의 영상 또는 음성 파일 (여러 개 고를 수 있음)",
            filetypes=[("영상/음성", "*.mp4 *.mkv *.webm *.mov *.m4a *.mp3 *.wav"), ("모든 파일", "*.*")])
        for path in paths:
            self.add_job(path, Path(path).stem)

    def add_from_fields(self):
        source = self.source.get().strip().strip('"')
        name = self.name.get()
        # 북마클릿 글(`"<주소>" -o <이름>`)을 칸에 직접 붙여 넣은 경우 주소와 이름을 나눈다
        parsed = parse_clipboard(source)
        if parsed:
            source = parsed[0]
            if not name.strip():
                name = parsed[1] or ""
        if not source:
            messagebox.showwarning("강의 자막", "강의 영상 주소나 파일을 넣어 주세요.")
            return
        if self.add_job(source, name):
            self.source.set("")
            self.name.set("")

    def add_job(self, source: str, name: str) -> bool:
        name = safe_name(name) or safe_name(tr.guess_name(source)) or "lecture"
        if any(j.name == name for j in self.jobs):
            messagebox.showwarning("강의 자막", f"'{name}' 이(가) 이미 대기열에 있습니다. 저장 이름을 바꿔 주세요.")
            return False
        if (tr.OUT_DIR / f"{name}.txt").exists() and not messagebox.askyesno(
                "강의 자막", f"{name}.txt 이(가) 이미 있습니다. 다시 받아써서 덮어쓸까요?"):
            return False
        job = Job(source, name)
        with self.cond:
            self.jobs.append(job)
            self.cond.notify_all()  # 돌고 있으면 바로 집어 가게
            self.save_queue()
        self.tree.insert("", "end", iid=job.iid)
        self.refresh_rows()
        if self.running():
            self.stage_label.configure(foreground="")
        else:
            self.stage.set(f"대기열 {len(self.pending_jobs())}개.  '대기열 시작'을 누르면 위에서부터 차례로 받아씁니다.")
            self.stage_label.configure(foreground="")
        self.update_buttons()
        return True

    # ---------- 대기열 저장 ----------
    def save_queue(self):
        """jobs 를 out/queue.json 에 쓴다. 창 스레드와 작업 스레드 둘 다 부르므로 cond 를 잡고 쓴다."""
        with self.cond:
            data = [{"source": j.source, "name": j.name, "state": j.state, "detail": j.detail, "error": j.error}
                    for j in self.jobs]
        try:
            tr.OUT_DIR.mkdir(exist_ok=True)
            tmp = queue_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(queue_file())  # 쓰다가 꺼져도 반쪽짜리 파일이 남지 않게
        except OSError:
            pass  # 저장을 못 해도 지금 도는 받아쓰기는 계속한다

    def load_queue(self):
        try:
            data = json.loads(queue_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for d in data:
            job = Job(d["source"], d["name"], d.get("state", WAITING), d.get("detail", ""), d.get("error", ""))
            # 지난번에 하던 도중 창이 닫혔으면 처음 상태로 돌린다
            if job.state == FETCHING or (job.state == READY and not job.path(".wav").exists()):
                job.state, job.detail = WAITING, ""
            elif job.state == WORKING:
                job.state, job.detail = STOPPED, "(받아쓴 데까지 저장됨)"
            elif job.state not in (WAITING, READY, DONE, FAILED, STOPPED):
                job.state, job.detail = WAITING, ""
            self.jobs.append(job)
            self.tree.insert("", "end", iid=job.iid)
        self.refresh_rows()
        todo = sum(j.state in PENDING + (FAILED, STOPPED) for j in self.jobs)
        if todo:
            self.stage.set(f"지난번 대기열을 불러왔습니다 (남은 강의 {todo}개). '대기열 시작'을 누르면 이어서 합니다.")

    # ---------- 대기열 편집 ----------
    def job_by_iid(self, iid) -> Job | None:
        return next((j for j in self.jobs if j.iid == iid), None)

    def selected(self) -> Job | None:
        sel = self.tree.selection()
        return self.job_by_iid(sel[0]) if sel else None

    def pending_jobs(self) -> list[Job]:
        return [j for j in self.jobs if j.state in PENDING]

    def move_to(self, job: Job, index: int):
        with self.cond:
            self.jobs.remove(job)
            index = max(0, min(index, len(self.jobs)))
            self.jobs.insert(index, job)
            self.save_queue()
        self.tree.move(job.iid, "", index)
        self.tree.see(job.iid)
        self.refresh_rows()

    def move_selected(self, delta: int):
        job = self.selected()
        if job:
            self.move_to(job, self.jobs.index(job) + delta)

    def on_press(self, e):
        self.drag_from = self.tree.identify_row(e.y)

    def on_drag(self, e):
        target = self.tree.identify_row(e.y)
        job = self.job_by_iid(getattr(self, "drag_from", ""))
        if job and target and target != job.iid:
            self.move_to(job, self.tree.index(target))

    def remove_selected(self):
        job = self.selected()
        if not job:
            return
        with self.cond:
            if job.state in ACTIVE:
                busy = True
            else:
                busy = False
                self.jobs.remove(job)
                self.save_queue()
        if busy:
            messagebox.showinfo("강의 자막", "지금 처리 중인 강의는 뺄 수 없습니다. 중지한 뒤 빼 주세요.")
            return
        self.tree.delete(job.iid)
        self.refresh_rows()
        self.update_buttons()

    def clear_finished(self):
        with self.cond:
            gone = [j for j in self.jobs if j.state == DONE]
            self.jobs = [j for j in self.jobs if j.state != DONE]
            self.save_queue()
        for j in gone:
            self.tree.delete(j.iid)
        self.refresh_rows()
        self.update_buttons()

    def refresh_rows(self, *jobs: Job):
        """번호와 상태 칸을 다시 그린다. jobs 를 주면 그 줄만."""
        for i, job in enumerate(self.jobs, 1):
            if jobs and job not in jobs:
                continue
            state = job.state + (f"  {job.detail}" if job.detail else "")
            self.tree.item(job.iid, values=(i, job.name, state), tags=(job.state,))

    def on_double(self, e):
        job = self.job_by_iid(self.tree.identify_row(e.y))
        if not job:
            return
        if job.state == FAILED:
            messagebox.showerror("강의 자막", f"{job.name}\n\n{job.error}")
        elif job.path(".txt").exists():
            os.startfile(job.path(".txt"))

    # ---------- 실행 ----------
    def running(self) -> bool:
        return any(t.is_alive() for t in self.threads)

    def start(self):
        if self.running():
            return
        with self.cond:
            # 지난번에 멈췄거나 실패한 강의도 다시 시도한다
            for j in self.jobs:
                if j.state in (FAILED, STOPPED):
                    j.state, j.detail = WAITING, ""
            if not self.pending_jobs():
                messagebox.showinfo("강의 자막", "대기열에 받아쓸 강의가 없습니다. 먼저 '대기열에 추가'를 눌러 주세요.")
                return
            self.active = True
            self.save_queue()
        tr.OUT_DIR.mkdir(exist_ok=True)
        self.stop.clear()
        self.refresh_rows()
        self.threads = [threading.Thread(target=self.fetch_loop, daemon=True),
                        threading.Thread(target=self.work_loop, daemon=True)]
        for t in self.threads:
            t.start()
        self.stage_label.configure(foreground="")
        self.stage.set("시작합니다…")
        self.set_running(True)

    def next_job(self, want: str) -> Job | None:
        """cond 를 잡은 채로 부른다. 대기열 순서상 첫 번째 '받아쓸 차례가 남은' 강의 가운데 want 상태인 것.

        받아쓰기(want=READY)는 맨 앞 강의의 음성이 아직이면 뒤 강의로 건너뛰지 않고 기다린다 (순서를 지키려고).
        음성 받기(want=WAITING)는 앞에서부터 아직 안 받은 것을 고른다."""
        for j in self.jobs:
            if j.state == want:
                return j
            if want == READY and j.state in PENDING:
                return None
        return None

    def set_state(self, job: Job, state: str, detail: str = ""):
        with self.cond:
            job.state, job.detail = state, detail
            self.cond.notify_all()
            self.save_queue()
        self.events.put(("row", job))

    def fetch_loop(self):
        put = self.events.put
        while True:
            with self.cond:
                job = None
                while self.active and job is None:
                    job = self.next_job(WAITING)
                    if job is None:
                        self.cond.wait(1)
                if not self.active:
                    return
                job.state, job.detail = FETCHING, ""
            put(("row", job))

            def progress(done, total, job=job):
                job.detail = f"{min(done / total * 100, 100):.0f}%" if total else tr.fmt_txt(done)
                put(("row", job))

            try:
                tr.extract_audio(job.source, job.path(".wav"), should_stop=self.stop.is_set, on_progress=progress)
                self.set_state(job, READY)
            except tr.Cancelled:
                self.set_state(job, WAITING)
            except Exception as e:  # 이 강의만 오류로 표시하고 다음 강의로 넘어간다
                job.error = str(e)
                self.set_state(job, FAILED, str(e).splitlines()[-1][:60])

    def work_loop(self):
        put = self.events.put
        while True:
            with self.cond:
                job = None
                while self.active and job is None:
                    job = self.next_job(READY)
                    if job is None:
                        self.cond.wait(1)
                if not self.active:
                    return
                job.state, job.detail = WORKING, ""
            model, threads = self.model_name, self.cpu_threads
            put(("row", job))
            put(("loading", job, model))
            try:
                tr.transcribe(job.path(".wav"), job.path(".txt"), job.path(".srt"), model,
                              cpu_threads=threads, should_stop=self.stop.is_set,
                              on_start=lambda total, job=job: put(("begin", job, total)),
                              on_segment=lambda s, e, total, text, job=job: put(("seg", job, s, e, total, text)))
                self.set_state(job, DONE)
            except tr.Cancelled:
                self.set_state(job, STOPPED, "(받아쓴 데까지 저장됨)")
            except Exception as e:
                job.error = str(e)
                self.set_state(job, FAILED, str(e).splitlines()[-1][:60] if str(e) else "")

    def request_stop(self):
        self.stop.set()
        with self.cond:
            self.active = False
            self.cond.notify_all()
        self.stop_btn.configure(state="disabled")
        self.stage.set("멈추는 중… (지금 하던 문장까지만 하고 멈춥니다)")

    # ---------- 진행 표시 ----------
    def poll(self):
        try:
            while True:
                self.handle(*self.events.get_nowait())
        except queue.Empty:
            pass
        if self.threads and not self.running():
            self.threads = []
            self.on_stopped()
        elif self.active:
            self.check_all_done()
        if self.closing and not self.running():
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def check_all_done(self):
        with self.cond:
            if any(j.state in PENDING + ACTIVE for j in self.jobs):
                return
            self.active = False  # 두 스레드가 기다리다 깨어나 끝난다
            self.cond.notify_all()

    def on_stopped(self):
        """두 스레드가 다 끝났을 때. 중지 버튼으로 끝났는지, 대기열을 다 비웠는지에 따라 마무리한다."""
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.set_running(False)
        done = sum(j.state == DONE for j in self.jobs)
        failed = sum(j.state == FAILED for j in self.jobs)
        if self.stop.is_set():
            self.stage.set("중지했습니다. 받아쓰던 강의는 그때까지 저장돼 있고, '대기열 시작'으로 이어서 합니다.")
            self.stage_label.configure(foreground="#9a6700")
            self.root.title("중지 - 강의 자막")
            return
        msg = f"대기열을 다 돌았습니다.  완료 {done}개"
        if failed:
            msg += f" · 오류 {failed}개 (두 번 클릭하면 이유가 보입니다)"
        self.stage.set(msg)
        self.stage_label.configure(foreground="#cf222e" if failed else "#1a7f37")
        self.bar.configure(value=100)
        self.root.title("완료 - 강의 자막")
        self.root.bell()

    def queue_position(self, job: Job) -> str:
        todo = [j for j in self.jobs if j.state in PENDING + ACTIVE or j is job]
        return f"({todo.index(job) + 1}/{len(todo)})" if job in todo else ""

    def handle(self, kind, *data):
        if kind == "row":
            self.refresh_rows(data[0])
            self.update_buttons()
        elif kind == "loading":
            job, model = data
            self.current = job
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
            self.stage.set(f"받아쓰기 준비 중: {job.name}  ({model} 불러오는 중)")
            self.pos.set("")
            self.eta.set("")
            self.bar.configure(mode="indeterminate")
            self.bar.start(15)
        elif kind == "begin":
            job, total = data
            self.bar.stop()
            self.bar.configure(mode="determinate", value=0)
            self.stage.set(f"받아쓰는 중: {job.name}")
            self.pos.set(f"00:00 / {tr.fmt_txt(total)}  ·  0%")
            self.t0 = time.monotonic()
        elif kind == "seg":
            job, start, end, total, text = data
            pct = min(end / total * 100, 100) if total else 0
            self.bar.configure(value=pct)
            self.pos.set(f"{tr.fmt_txt(end)} / {tr.fmt_txt(total)}  ·  {pct:.0f}%")
            self.root.title(f"{pct:.0f}% {self.queue_position(job)} - 강의 자막")
            elapsed = time.monotonic() - self.t0
            eta = f"경과 {tr.fmt_txt(elapsed)}"
            if end > 30:  # 초반 몇 문장으로는 남은 시간이 크게 흔들린다
                eta += f"  ·  이 강의 남은 시간 약 {tr.fmt_txt(elapsed / end * (total - end))}"
            self.eta.set(eta)
            if job.state == WORKING:  # 이미 끝났으면 늦게 도착한 진행률로 덮어쓰지 않는다
                job.detail = f"{pct:.0f}%"
                self.refresh_rows(job)
            self.append(start, text)
        # 받아쓰기 스레드가 다음 강의 음성을 기다리는 동안에는 무엇을 기다리는지 보여 준다
        if self.active and not any(j.state == WORKING for j in self.jobs):
            first = next((j for j in self.jobs if j.state in PENDING), None)
            if first and first.state != READY:
                self.bar.stop()
                self.bar.configure(mode="determinate", value=0)
                self.stage.set(f"음성 받는 중: {first.name}  (받는 대로 받아쓰기 시작)")
                self.pos.set(first.detail)
                self.eta.set("")

    def append(self, start, text):
        at_bottom = self.text.yview()[1] > 0.99
        self.text.configure(state="normal")
        self.text.insert("end", f"[{tr.fmt_txt(start)}] ", "time")
        self.text.insert("end", text + "\n")
        self.text.configure(state="disabled")
        if at_bottom:  # 위로 올려 읽는 중이면 끌어내리지 않는다
            self.text.see("end")

    def set_running(self, on: bool):
        self.start_btn.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")
        if on:
            self.root.title("강의 자막")
        self.update_buttons()

    def txt_to_open(self) -> Path | None:
        job = self.selected() or self.current
        if job and job.path(".txt").exists():
            return job.path(".txt")
        return None

    def update_buttons(self):
        self.open_txt_btn.configure(state="normal" if self.txt_to_open() else "disabled")

    # ---------- 기타 ----------
    def open_folder(self):
        tr.OUT_DIR.mkdir(exist_ok=True)
        os.startfile(tr.OUT_DIR)

    def open_txt(self):
        path = self.txt_to_open()
        if path:
            os.startfile(path)

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
