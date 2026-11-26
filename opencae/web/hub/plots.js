/* Canvas 2-D plots for the Results Hub. window.HubPlots.mount(box, plot) draws one contract plot item:
   {id, title, x:{label,unit}, y:{label,unit,log}, series:[{name,x:[],y:[]}], note}. Only API values are drawn. */
(function () {
  'use strict';
  var COLORS = ['#4c9be8', '#f3864a', '#4cbb7f', '#d36ad0', '#e7b544', '#6fd3d0', '#f0807a', '#a6b0ff'];
  var M = { l: 66, r: 16, t: 12, b: 46 };

  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function fmt(v) {
    if (v === 0) return '0';
    var a = Math.abs(v);
    if (a >= 1e5 || a < 1e-3) return v.toExponential(2).replace('e+', 'e');
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    return v.toPrecision(3);
  }
  function niceTicks(lo, hi, n) {
    var span = hi - lo; if (!(span > 0)) return [lo];
    var raw = span / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    var step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p, out = [];
    for (var v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    return out;
  }
  function label(ax) { return ax && ax.label ? ax.label + (ax.unit ? ' (' + ax.unit + ')' : '') : (ax && ax.unit) || ''; }

  function csv(plot) {
    var rows = ['series,' + JSON.stringify(label(plot.x) || 'x') + ',' + JSON.stringify(label(plot.y) || 'y')];
    (plot.series || []).forEach(function (s) {
      for (var i = 0; i < s.x.length; i++) rows.push(JSON.stringify(s.name) + ',' + s.x[i] + ',' + s.y[i]);
    });
    return rows.join('\n') + '\n';
  }
  function download(name, text) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
    a.download = name; document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  function mount(box, plot) {
    var series = (plot.series || []).filter(function (s) { return s.x && s.x.length; });
    var cv = document.createElement('canvas'), tip = document.createElement('div');
    tip.className = 'ptip'; tip.hidden = true;
    box.appendChild(cv); box.appendChild(tip);
    var bars = series.length > 0 && series.every(function (s) { return s.x.length <= 1; });
    var allPos = series.length > 0 && series.every(function (s) { return s.y.every(function (v) { return v > 0; }); });
    var log = !!(plot.y && plot.y.log) && allPos;
    var view = null, ext = null, drag = null, hover = null;

    function ty(v) { return log ? Math.log10(v) : v; }
    function extent() {
      var x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      series.forEach(function (s) {
        s.x.forEach(function (v) { if (v < x0) x0 = v; if (v > x1) x1 = v; });
        s.y.forEach(function (v) { v = ty(v); if (v < y0) y0 = v; if (v > y1) y1 = v; });
      });
      if (!(x1 > x0)) { x0 -= 0.5; x1 += 0.5; }
      if (!(y1 > y0)) { var d = Math.abs(y0) * 0.1 || 0.5; y0 -= d; y1 += d; }
      var py = (y1 - y0) * 0.06;
      if (bars) { y0 = log ? y0 - 0.5 : Math.min(0, y0); y1 += py; }
      else { y0 -= py; y1 += py; }
      return { x0: x0, x1: x1, y0: y0, y1: y1 };
    }
    function reset() { ext = extent(); view = { x0: ext.x0, x1: ext.x1, y0: ext.y0, y1: ext.y1 }; draw(); }

    function geom() {
      var r = cv.getBoundingClientRect();
      return { w: r.width, h: r.height, pw: r.width - M.l - M.r, ph: r.height - M.t - M.b };
    }
    function px(v, g) { return M.l + (v - view.x0) / (view.x1 - view.x0) * g.pw; }
    function py(v, g) { return M.t + (1 - (ty(v) - view.y0) / (view.y1 - view.y0)) * g.ph; }

    function draw() {
      var dpr = window.devicePixelRatio || 1, g = geom();
      if (g.w < 10 || g.h < 10) return;
      cv.width = Math.round(g.w * dpr); cv.height = Math.round(g.h * dpr);
      var c = cv.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0);
      var ink = css('--ink', '#ddd'), ink3 = css('--ink3', '#889'), rule = css('--rule', '#334'), panel = css('--panel', '#111');
      c.clearRect(0, 0, g.w, g.h);
      c.font = '11.5px ' + css('--fm', 'monospace');
      if (!series.length) {
        c.fillStyle = ink3; c.font = '13px ' + css('--f', 'sans-serif'); c.textAlign = 'center';
        c.fillText('No data in this plot.', g.w / 2, g.h / 2); return;
      }
      // grid + y ticks
      c.strokeStyle = rule; c.fillStyle = ink3; c.lineWidth = 1; c.textAlign = 'right'; c.textBaseline = 'middle';
      var yt = [];
      if (log) { for (var k = Math.ceil(view.y0 - 1e-9); k <= Math.floor(view.y1 + 1e-9); k++) yt.push(k); if (yt.length > 8) yt = yt.filter(function (_, i) { return i % Math.ceil(yt.length / 7) === 0; }); }
      else yt = niceTicks(view.y0, view.y1, Math.max(3, Math.floor(g.ph / 42)));
      yt.forEach(function (v) {
        var y = M.t + (1 - (v - view.y0) / (view.y1 - view.y0)) * g.ph;
        c.beginPath(); c.moveTo(M.l, y + .5); c.lineTo(M.l + g.pw, y + .5); c.stroke();
        c.fillText(log ? '1e' + v : fmt(v), M.l - 6, y);
      });
      c.textAlign = 'center'; c.textBaseline = 'top';
      var xt = bars ? [] : niceTicks(view.x0, view.x1, Math.max(3, Math.floor(g.pw / 90)));
      xt.forEach(function (v) {
        var x = px(v, g);
        c.beginPath(); c.moveTo(x + .5, M.t); c.lineTo(x + .5, M.t + g.ph); c.stroke();
        c.fillText(fmt(v), x, M.t + g.ph + 5);
      });
      c.strokeStyle = ink3; c.strokeRect(M.l + .5, M.t + .5, g.pw, g.ph);
      // axis labels
      c.fillStyle = ink; c.font = '12px ' + css('--f', 'sans-serif');
      c.textAlign = 'center'; c.textBaseline = 'bottom';
      if (!bars) c.fillText(label(plot.x), M.l + g.pw / 2, g.h - 3);
      c.save(); c.translate(13, M.t + g.ph / 2); c.rotate(-Math.PI / 2); c.textBaseline = 'top';
      c.fillText(label(plot.y) + (log ? '  [log]' : ''), 0, -8); c.restore();
      // data
      c.save(); c.beginPath(); c.rect(M.l, M.t, g.pw, g.ph); c.clip();
      if (bars) {
        var n = series.length, slot = g.pw / n, bw = Math.min(70, slot * 0.55), base = M.t + g.ph;
        series.forEach(function (s, i) {
          var cx = M.l + slot * (i + .5), y = py(s.y[0], g);
          c.fillStyle = COLORS[i % COLORS.length]; c.fillRect(cx - bw / 2, y, bw, base - y);
        });
        c.restore();
        c.fillStyle = ink; c.font = '12px ' + css('--f', 'sans-serif'); c.textBaseline = 'top'; c.textAlign = 'center';
        series.forEach(function (s, i) {
          var cx = M.l + slot * (i + .5);
          c.fillText(s.name, cx, M.t + g.ph + 6);
          c.textBaseline = 'bottom'; c.font = '11.5px ' + css('--fm', 'monospace');
          c.fillText(fmt(s.y[0]), cx, Math.max(M.t + 12, py(s.y[0], g) - 2)); c.textBaseline = 'top'; c.font = '12px ' + css('--f', 'sans-serif');
        });
        return;
      }
      series.forEach(function (s, i) {
        c.strokeStyle = c.fillStyle = COLORS[i % COLORS.length]; c.lineWidth = 1.8; c.beginPath();
        var started = false;
        for (var j = 0; j < s.x.length; j++) {
          if (log && !(s.y[j] > 0)) { started = false; continue; }
          var x = px(s.x[j], g), y = py(s.y[j], g);
          if (!started) { c.moveTo(x, y); started = true; } else c.lineTo(x, y);
        }
        c.stroke();
        if (s.x.length <= 60) s.x.forEach(function (xv, j) { if (log && !(s.y[j] > 0)) return; c.beginPath(); c.arc(px(xv, g), py(s.y[j], g), 2.6, 0, 6.3); c.fill(); });
      });
      if (hover != null) {
        var hx = px(hover, g); c.strokeStyle = ink3; c.lineWidth = 1; c.setLineDash([4, 3]);
        c.beginPath(); c.moveTo(hx + .5, M.t); c.lineTo(hx + .5, M.t + g.ph); c.stroke(); c.setLineDash([]);
        series.forEach(function (s, i) {
          var j = nearest(s, hover); if (j < 0) return;
          if (log && !(s.y[j] > 0)) return;
          c.fillStyle = COLORS[i % COLORS.length]; c.strokeStyle = panel; c.lineWidth = 2;
          c.beginPath(); c.arc(px(s.x[j], g), py(s.y[j], g), 4.2, 0, 6.3); c.fill(); c.stroke();
        });
      }
      c.restore();
      // legend
      if (series.length > 1) {
        c.font = '12px ' + css('--f', 'sans-serif'); c.textAlign = 'left'; c.textBaseline = 'middle';
        var lx = M.l + 8, ly = M.t + 12;
        series.forEach(function (s, i) {
          c.fillStyle = COLORS[i % COLORS.length]; c.fillRect(lx, ly - 4, 12, 8);
          c.fillStyle = ink; c.fillText(s.name, lx + 16, ly); lx += 16 + c.measureText(s.name).width + 14;
          if (lx > g.w - 120 && i < series.length - 1) { lx = M.l + 8; ly += 16; }
        });
      }
    }
    function nearest(s, xv) {
      var best = -1, bd = Infinity;
      for (var i = 0; i < s.x.length; i++) { var d = Math.abs(s.x[i] - xv); if (d < bd) { bd = d; best = i; } }
      return best;
    }
    function dataX(e, g) {
      var r = cv.getBoundingClientRect();
      return view.x0 + ((e.clientX - r.left) - M.l) / g.pw * (view.x1 - view.x0);
    }
    function inside(e, g) {
      var r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      return x >= M.l && x <= M.l + g.pw && y >= M.t && y <= M.t + g.ph;
    }
    function showTip(e, g) {
      if (bars) {
        var r0 = cv.getBoundingClientRect(), slot = g.pw / series.length, i0 = Math.floor((e.clientX - r0.left - M.l) / slot);
        if (i0 < 0 || i0 >= series.length) { tip.hidden = true; return; }
        var s0 = series[i0];
        tip.innerHTML = '<i style="background:' + COLORS[i0 % COLORS.length] + '"></i>' + esc(s0.name) + ': <b>' + fmt(s0.y[0]) + '</b> ' + esc(plot.y && plot.y.unit || '');
        place(e); return;
      }
      var xv = dataX(e, g); hover = xv;
      var s0 = series[0], j0 = nearest(s0, xv); if (j0 >= 0) hover = s0.x[j0];
      var html = '<div class="muted" style="color:inherit;opacity:.75">' + esc(plot.x && plot.x.label || 'x') + ' = ' + fmt(hover) + ' ' + esc(plot.x && plot.x.unit || '') + '</div>';
      series.forEach(function (s, i) {
        var j = nearest(s, hover); if (j < 0) return;
        html += '<div><i style="background:' + COLORS[i % COLORS.length] + '"></i>' + esc(s.name) + ': <b>' + fmt(s.y[j]) + '</b> ' + esc(plot.y && plot.y.unit || '') + '</div>';
      });
      tip.innerHTML = html; place(e); draw();
    }
    function place(e) {
      tip.hidden = false;
      var r = box.getBoundingClientRect(), x = e.clientX - r.left + 14, y = e.clientY - r.top + 10;
      if (x + tip.offsetWidth > r.width - 4) x = e.clientX - r.left - tip.offsetWidth - 14;
      if (y + tip.offsetHeight > r.height - 4) y = r.height - tip.offsetHeight - 4;
      tip.style.left = Math.max(2, x) + 'px'; tip.style.top = Math.max(2, y) + 'px';
    }
    function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

    cv.addEventListener('pointermove', function (e) {
      var g = geom();
      if (drag) {
        var dx = (e.clientX - drag.x) / g.pw * (view.x1 - view.x0), dy = (e.clientY - drag.y) / g.ph * (view.y1 - view.y0);
        view.x0 = drag.v.x0 - dx; view.x1 = drag.v.x1 - dx; view.y0 = drag.v.y0 + dy; view.y1 = drag.v.y1 + dy;
        tip.hidden = true; draw(); return;
      }
      if (!inside(e, g)) { hover = null; tip.hidden = true; draw(); return; }
      showTip(e, g);
    });
    cv.addEventListener('pointerleave', function () { hover = null; tip.hidden = true; if (!drag) draw(); });
    cv.addEventListener('pointerdown', function (e) {
      if (bars) return;
      cv.setPointerCapture(e.pointerId); drag = { x: e.clientX, y: e.clientY, v: { x0: view.x0, x1: view.x1, y0: view.y0, y1: view.y1 } };
    });
    cv.addEventListener('pointerup', function () { drag = null; });
    cv.addEventListener('wheel', function (e) {
      if (bars) return; e.preventDefault();
      var g = geom(), f = e.deltaY < 0 ? 0.8 : 1.25, r = cv.getBoundingClientRect();
      if (e.shiftKey) {
        var fy = 1 - ((e.clientY - r.top) - M.t) / g.ph, yc = view.y0 + fy * (view.y1 - view.y0);
        view.y0 = yc - (yc - view.y0) * f; view.y1 = yc + (view.y1 - yc) * f;
      } else {
        var xc = dataX(e, g); view.x0 = xc - (xc - view.x0) * f; view.x1 = xc + (view.x1 - xc) * f;
      }
      draw();
    }, { passive: false });
    cv.addEventListener('dblclick', reset);
    var ro = new ResizeObserver(function () { if (view) draw(); });
    ro.observe(box);
    reset();
    return {
      reset: reset, csv: function () { return csv(plot); }, redraw: draw,
      setLog: function (on) { log = !!on && allPos; reset(); }, canLog: allPos && !bars, isLog: function () { return log; }, isBars: bars,
      destroy: function () { ro.disconnect(); }
    };
  }
  window.HubPlots = { mount: mount, csv: csv, download: download };
})();
