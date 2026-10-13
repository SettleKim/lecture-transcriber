/*
 * 강의 영상 화면에서 누르면 영상 주소(m3u8)와 강의 제목을 `"<주소>" -o <이름>` 꼴로 클립보드에 복사한다.
 * 창(app.pyw)은 이걸 알아서 읽어 채우고, 터미널에서는 `python transcribe.py ` 뒤에 그대로 붙이면 된다.
 * 북마크 주소로 넣을 한 줄짜리는 README.md 에 있다 (이 파일을 고치면 README 의 한 줄도 다시 만들 것).
 * http 페이지에서는 navigator.clipboard 를 못 써서 execCommand('copy') 로 복사한다.
 */
(function () {
  var src = null;
  var hrefs = [];
  function scan(win) {
    try {
      var d = win.document;
      hrefs.push(win.location.href);
      d.querySelectorAll('iframe, frame').forEach(function (f) { hrefs.push(f.src); });
      d.querySelectorAll('video, video source').forEach(function (v) {
        var s = v.currentSrc || v.src;
        if (!src && s && s.indexOf('.m3u8') >= 0) { src = s; }
      });
      for (var i = 0; i < win.frames.length; i++) { scan(win.frames[i]); }
    } catch (e) {}
  }
  scan(window);
  if (!src) { alert('영상 주소를 찾지 못했습니다. 강의 영상 화면에서 눌러 주세요.'); return; }
  var name = '';
  hrefs.forEach(function (h) {
    var m = !name && h && h.match(/[?&]path=([^&]*)/);
    if (m) { name = decodeURIComponent(m[1].replace(/\+/g, ' ')).split('/').pop().replace(/\.mp4$/i, ''); }
  });
  name = name.replace(/[^0-9A-Za-z가-힣-]+/g, '_').replace(/^_+|_+$/g, '') || 'lecture';
  var cmd = '"' + src + '" -o ' + name;
  var ta = document.createElement('textarea');
  ta.value = cmd;
  document.body.appendChild(ta);
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
  prompt(ok ? '복사했습니다. 강의 자막 창으로 돌아가세요.' : 'Ctrl+C 로 복사하세요.', cmd);
})();
