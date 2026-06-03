// Minimal canvas signature pad — no dependencies. Captures mouse + touch,
// exports a PNG data URL into the hidden #signature_png field on submit.
(function () {
  var canvas = document.getElementById('sigpad');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var drawing = false;
  var hasInk = false;
  var last = null;

  // Scale the canvas backing store to the device pixel ratio so the line is
  // crisp and the drawn coordinates line up with the displayed size.
  function fitCanvas() {
    var ratio = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * ratio;
    canvas.height = rect.height * ratio;
    ctx.scale(ratio, ratio);
    ctx.lineWidth = 2.2;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#111';
  }
  fitCanvas();

  function pos(e) {
    var rect = canvas.getBoundingClientRect();
    var src = e.touches ? e.touches[0] : e;
    return { x: src.clientX - rect.left, y: src.clientY - rect.top };
  }

  function start(e) {
    drawing = true;
    last = pos(e);
    e.preventDefault();
  }
  function move(e) {
    if (!drawing) return;
    var p = pos(e);
    ctx.beginPath();
    ctx.moveTo(last.x, last.y);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
    last = p;
    hasInk = true;
    e.preventDefault();
  }
  function end() { drawing = false; }

  canvas.addEventListener('mousedown', start);
  canvas.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);
  canvas.addEventListener('touchstart', start, { passive: false });
  canvas.addEventListener('touchmove', move, { passive: false });
  canvas.addEventListener('touchend', end);

  var clearBtn = document.getElementById('clearSig');
  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      hasInk = false;
    });
  }

  var form = document.getElementById('signForm');
  form.addEventListener('submit', function (e) {
    var intent = document.getElementById('intent');
    var name = document.getElementById('full_name');
    if (!name.value.trim()) { alert('Please type your full legal name.'); e.preventDefault(); return; }
    if (!hasInk) { alert('Please draw your signature in the box.'); e.preventDefault(); return; }
    if (!intent.checked) { alert('Please tick the box to confirm you intend to sign.'); e.preventDefault(); return; }
    document.getElementById('signature_png').value = canvas.toDataURL('image/png');
  });
})();
