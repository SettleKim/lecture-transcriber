# 강의 자막 (lecture-transcriber)

자막이 없는 온라인 강의를 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)로 받아써서 `[mm:ss]` 시간 표시가 붙은 텍스트와 `.srt` 자막으로 저장한다.
창(진행 막대 · 남은 시간 · 받아쓰기 미리보기)으로도, 터미널로도 쓸 수 있다.

> **본인이 수강 중인 강의를 개인 학습용으로 받아쓰는 도구입니다.**
> 강의 영상과 받아쓴 결과물은 강의자(와 학교)의 저작물이므로 공유·배포하지 마세요.
> 학교 이용약관이나 강의자가 녹음·복제를 금지했다면 쓰지 마세요.

## 준비 (처음 한 번)

Windows · Python 3.10 이상 기준.

1. [ffmpeg](https://ffmpeg.org/download.html)를 설치하고 `bin` 폴더를 PATH에 추가한다 (`ffmpeg`, `ffprobe`가 터미널에서 실행되면 됨).
2. 패키지 설치

   ```bash
   pip install -r requirements.txt
   ```

   첫 받아쓰기 때 모델(`large-v3-turbo`, 약 1.6GB)을 내려받는다.

3. 북마클릿 등록: 브라우저 북마크바에서 우클릭 → 페이지 추가 → 이름 `강의 자막`, URL 칸에 아래 한 줄을 그대로 붙여 넣는다.

   ```
   javascript:(function () { var src = null; var hrefs = []; function scan(win) { try { var d = win.document; hrefs.push(win.location.href); d.querySelectorAll('iframe, frame').forEach(function (f) { hrefs.push(f.src); }); d.querySelectorAll('video, video source').forEach(function (v) { var s = v.currentSrc || v.src; if (!src && s && s.indexOf('.m3u8') >= 0) { src = s; } }); for (var i = 0; i < win.frames.length; i++) { scan(win.frames[i]); } } catch (e) {} } scan(window); if (!src) { alert('영상 주소를 찾지 못했습니다. 강의 영상 화면에서 눌러 주세요.'); return; } var name = ''; hrefs.forEach(function (h) { var m = !name && h && h.match(/[?&]path=([^&]*)/); if (m) { name = decodeURIComponent(m[1].replace(/\+/g, ' ')).split('/').pop().replace(/\.mp4$/i, ''); } }); name = name.replace(/[^0-9A-Za-z가-힣-]+/g, '_').replace(/^_+|_+$/g, '') || 'lecture'; var cmd = '"' + src + '" -o ' + name; var ta = document.createElement('textarea'); ta.value = cmd; document.body.appendChild(ta); ta.select(); var ok = false; try { ok = document.execCommand('copy'); } catch (e) {} document.body.removeChild(ta); prompt(ok ? '복사했습니다. 강의 자막 창으로 돌아가세요.' : 'Ctrl+C 로 복사하세요.', cmd); })();
   ```

## 쓰는 법

### 창으로

1. `app.pyw`를 더블클릭해 창을 띄워 둔다.
2. 강의 영상 화면에서 북마크 `강의 자막`을 누른다. 영상 주소와 강의 제목이 클립보드에 복사된다.
3. 창으로 돌아오면 주소·이름이 자동으로 채워진다. `＋ 대기열에 추가`(또는 Enter)를 누른다.
4. 받아쓸 강의마다 2~3을 되풀이한 뒤 `▶ 대기열 시작`을 누른다. 돌고 있는 중에 더 추가해도 된다.

- **대기열**: 위에서부터 차례로 받아쓴다. 끌어서 놓거나 `▲ 위로`·`▼ 아래로`·`맨 위로`(Alt+↑/↓)로 순서를 바꾼다.
  받아쓰기는 CPU를 다 쓰므로 한 번에 하나씩 하고, CPU를 거의 안 쓰는 음성 받기는 그동안 다음 강의들 것을 미리 해 둔다.
- 한 강의가 오류(예: 403)로 실패해도 다음 강의로 넘어간다. 두 번 클릭하면 오류 내용이, 완료된 줄은 받아쓴 파일이 열린다.
- **정확도**: `정확(large-v3-turbo)` · `보통(medium)` · `빠름(small)`. medium·small은 처음 고를 때 모델을 새로 내려받는다.
- **CPU 사용**: 코어를 적게 쓰면 느리지만 덜 뜨겁고 다른 작업이 덜 버벅인다. 돌리는 중에 바꾸면 다음 강의부터 적용된다.
- 중지해도 그때까지 받아쓴 부분은 남고, `대기열 시작`을 다시 누르면 멈춘 강의부터 이어서 한다(그 강의는 처음부터 다시 받아씀).
- 로컬 영상·음성 파일은 `파일…` 버튼으로 고른다 (여러 개 고르면 한꺼번에 대기열에 들어감).
- 대기열은 `out/queue.json`에 저장돼서 창을 닫았다 열어도 남는다. 받아쓰는 도중에 닫았던 강의는 `중지됨`으로 돌아와 다음 시작 때 다시 받아쓴다.

### 터미널로

```bash
python transcribe.py "<m3u8 주소 또는 파일 경로>" -o 이름 --model large-v3-turbo
```

북마클릿이 복사한 내용을 `python transcribe.py ` 뒤에 그대로 붙여도 된다.

### 결과

`out/` 폴더에 저장된다 (git에는 올라가지 않음).

| 파일 | 내용 |
|---|---|
| `<이름>.wav` | 추출한 음성 (16kHz 모노). 다시 돌리면 이걸 재사용한다 |
| `<이름>.txt` | `[mm:ss]` 시간 표시가 붙은 받아쓰기 |
| `<이름>.srt` | 영상 플레이어에 붙일 수 있는 자막 |
| `queue.json` | 창의 대기열 (순서·상태). 지우면 대기열이 비워진다 |

## 파일

| 파일 | 하는 일 |
|---|---|
| `app.pyw` | 창 버전 (tkinter, 추가 설치 없음) |
| `transcribe.py` | ffmpeg로 음성 추출 → faster-whisper로 받아쓰기. 터미널에서 직접 실행도 가능 |
| `bookmarklet.js` | 북마클릿 원본. 고치면 위 한 줄도 다시 만들 것 |

## 알아 둔 것

- 영상 주소가 403을 내면 서버가 로그인을 요구하는 것이라 이 방식으로는 받을 수 없다.
- faster-whisper 1.2.1의 파일 디코딩은 PyAV 19와 맞지 않아(`metadata_errors` 인자 오류) wav를 직접 읽어 넘긴다.
- 받아쓰기는 전문용어를 가끔 틀린다 (예: 딥러닝 → 팀러닝). 시험 공부에 쓸 때는 강의 자료와 같이 볼 것.
- 47분 강의 기준 음성 추출 1~2분, 받아쓰기는 CPU 4코어로 대략 20분 안팎.

## 라이선스

MIT. 받아쓰기 모델과 라이브러리는 각자의 라이선스를 따른다 (faster-whisper · Whisper: MIT, ffmpeg: LGPL/GPL — 이 저장소에 포함하지 않음).
