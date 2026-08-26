"""Render docs/API.md into a navigable single-page HTML reference."""
import html
import os
import re
import tempfile
from pathlib import Path

REPO = Path(r"C:\Users\Nicholas\Documents\gc-py")
SRC = REPO / "docs/API.md"
OUT = Path(__file__).resolve().parent / "lab_api_reference.html"

MD = SRC.read_text(encoding="utf-8")

# --------------------------------------------------------------------------
# markdown -> html. Focused: this converter only has to handle the subset the
# source actually uses, which is why it is 90 lines instead of a dependency.
# --------------------------------------------------------------------------

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def slug(text: str) -> str:
    s = re.sub(r"`", "", text)
    s = re.sub(r"[^\w\s.-]", "", s).strip().lower()
    return re.sub(r"[\s.]+", "-", s)


def inline(text: str) -> str:
    """Escape, then apply inline markup. Code spans are protected first."""
    spans = []

    def stash(m):
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = INLINE_CODE.sub(stash, text)
    text = html.escape(text, quote=False)
    text = LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">'
                              f'{m.group(1)}</a>', text)
    text = BOLD.sub(r"<strong>\1</strong>", text)
    text = ITALIC.sub(r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{html.escape(spans[int(m.group(1))], quote=False)}</code>",
                  text)
    return text


def convert(md: str):
    out, toc = [], []
    lines = md.split("\n")
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]

        # fenced code
        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            body = []
            while i < n and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(f'<div class="code-wrap"><pre{cls}><code>'
                       f'{html.escape(chr(10).join(body), quote=False)}</code></pre></div>')
            continue

        # headings
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            lvl, txt = len(m.group(1)), m.group(2).strip()
            sid = slug(txt)
            if lvl == 1:
                out.append(f'<h1 id="{sid}">{inline(txt)}</h1>')
            else:
                out.append(f'<h{lvl} id="{sid}">'
                           f'<a class="anchor" href="#{sid}" aria-label="link">#</a>'
                           f'{inline(txt)}</h{lvl}>')
            if lvl in (2, 3):
                toc.append((lvl, sid, txt))
            i += 1
            continue

        # horizontal rule
        if re.match(r"^-{3,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # table
        if line.strip().startswith("|") and i + 1 < n and re.match(
                r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            tb = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r)
                         + "</tr>" for r in rows)
            out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr>'
                       f'</thead><tbody>{tb}</tbody></table></div>')
            continue

        # lists
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            ordered = bool(re.match(r"\d+\.", m.group(2)))
            tag = "ol" if ordered else "ul"
            items = []
            while i < n:
                mm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if not mm:
                    # a wrapped continuation line belongs to the previous item
                    if items and lines[i].startswith(("  ", "\t")) and lines[i].strip():
                        items[-1] += " " + lines[i].strip()
                        i += 1
                        continue
                    break
                items.append(mm.group(3))
                i += 1
            body = "".join(f"<li>{inline(it)}</li>" for it in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        # blank
        if not line.strip():
            i += 1
            continue

        # paragraph
        para = []
        while i < n and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|```|\||\s*([-*]|\d+\.)\s|-{3,}\s*$)", lines[i]):
            para.append(lines[i])
            i += 1
        text = " ".join(para)
        # In this document a Note always flags something the code gets wrong or
        # cannot yet check, so it earns its own treatment.
        if text.lstrip().startswith("**Note:**"):
            body = inline(text.lstrip()[len("**Note:**"):].strip())
            out.append(f'<aside class="note"><span class="note-tag">Note</span>'
                       f'<p>{body}</p></aside>')
        else:
            out.append(f"<p>{inline(text)}</p>")

    return "\n".join(out), toc


BODY, TOC = convert(MD)

# --------------------------------------------------------------------------
# nav: modules in the order data actually flows through them
# --------------------------------------------------------------------------

FLOW = ["lab/config.py", "lab/labware.py", "lab/effector.py", "lab/deck.py",
        "lab/machine.py", "lab/simulate.py", "lab/logbook.py"]

modules, current = [], None
for lvl, sid, txt in TOC:
    bare = txt.strip("`")
    if lvl == 2:
        current = {"id": sid, "label": bare, "fns": [],
                   "is_module": bare in FLOW}
        modules.append(current)
    elif current is not None:
        current["fns"].append((sid, bare))

nav = []
step = 0
for mod in modules:
    cls = "nav-mod" + (" is-module" if mod["is_module"] else " is-front")
    marker = ""
    if mod["is_module"]:
        step += 1
        marker = f'<span class="flow-step">{step}</span>'
    count = (f'<span class="fn-count">{len(mod["fns"])}</span>'
             if mod["fns"] else "")
    nav.append(f'<li class="{cls}">{marker}'
               f'<a href="#{mod["id"]}"><span>{html.escape(mod["label"])}</span>'
               f'{count}</a>')
    if mod["fns"]:
        nav.append('<ul class="nav-fns">' + "".join(
            f'<li><a href="#{sid}"><code>{html.escape(label)}</code></a></li>'
            for sid, label in mod["fns"]) + "</ul>")
    nav.append("</li>")
NAV = "\n".join(nav)

n_fns = sum(len(m["fns"]) for m in modules)

CSS = """
:root {
  --ground:#F6F8FA; --surface:#FFFFFF; --surface-2:#EEF1F6;
  --ink:#1A2029; --ink-soft:#5A6675; --ink-faint:#8592A3;
  --rule:#DCE1E9; --rule-soft:#E9EDF3;
  --accent:#0D6E78; --accent-soft:#E2F0F1; --accent-ink:#0A575F;
  --signal:#A66207; --signal-soft:#FBF1E0;
  --danger:#9C3227;
  --code-bg:#EFF2F6; --code-ink:#20323B;
  --shadow:0 1px 2px rgba(20,30,45,.05), 0 8px 24px -12px rgba(20,30,45,.10);
  --rail:288px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#131720; --surface:#1A1F2A; --surface-2:#212836;
    --ink:#E4E9F1; --ink-soft:#98A4B5; --ink-faint:#6C7A8C;
    --rule:#2A3140; --rule-soft:#222937;
    --accent:#3FB3BE; --accent-soft:#123034; --accent-ink:#6FCDD6;
    --signal:#D99A3B; --signal-soft:#2C2313;
    --danger:#E0776A;
    --code-bg:#111620; --code-ink:#C7D3DE;
    --shadow:0 1px 2px rgba(0,0,0,.30), 0 10px 30px -14px rgba(0,0,0,.55);
  }
}
:root[data-theme="dark"] {
  --ground:#131720; --surface:#1A1F2A; --surface-2:#212836;
  --ink:#E4E9F1; --ink-soft:#98A4B5; --ink-faint:#6C7A8C;
  --rule:#2A3140; --rule-soft:#222937;
  --accent:#3FB3BE; --accent-soft:#123034; --accent-ink:#6FCDD6;
  --signal:#D99A3B; --signal-soft:#2C2313;
  --danger:#E0776A;
  --code-bg:#111620; --code-ink:#C7D3DE;
  --shadow:0 1px 2px rgba(0,0,0,.30), 0 10px 30px -14px rgba(0,0,0,.55);
}

* { box-sizing:border-box; }
body {
  margin:0; background:var(--ground); color:var(--ink);
  font-family:"Source Serif 4","Iowan Old Style",Georgia,serif;
  font-size:17px; line-height:1.62;
  -webkit-font-smoothing:antialiased;
}

/* ---- header ---- */
.masthead {
  border-bottom:1px solid var(--rule); background:var(--surface);
  padding:34px 32px 26px;
}
.masthead-in { max-width:1180px; margin:0 auto;
  display:flex; gap:32px; align-items:flex-start; flex-wrap:wrap; }
.brand { flex:1 1 340px; min-width:0; }
.eyebrow {
  font-family:Archivo,"Helvetica Neue",Arial,sans-serif;
  font-size:11px; font-weight:600; letter-spacing:.15em; text-transform:uppercase;
  color:var(--accent); margin:0 0 8px;
}
h1 {
  font-family:Archivo,"Helvetica Neue",Arial,sans-serif;
  font-size:clamp(30px,4.2vw,44px); font-weight:700; letter-spacing:-.02em;
  line-height:1.05; margin:0 0 12px; text-wrap:balance;
}
h1 code { font-size:.86em; background:none; padding:0; color:var(--accent); }
.lede { margin:0; color:var(--ink-soft); max-width:60ch; font-size:17px; }
.stats {
  display:flex; gap:26px; margin:18px 0 0; padding:0; list-style:none;
  font-family:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
  font-size:12px; color:var(--ink-faint);
}
.stats b { display:block; font-size:20px; font-weight:600; color:var(--ink);
  font-variant-numeric:tabular-nums; }

/* the Z indicator: +Z down is the fact most likely to cause a crash */
.zcard {
  flex:0 0 auto; display:flex; gap:16px; align-items:center;
  border:1px solid var(--rule); border-radius:3px;
  background:var(--ground); padding:14px 18px;
}
.zcard svg { display:block; flex:0 0 auto; }
.zcard-txt { font-family:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
  font-size:11.5px; line-height:1.75; color:var(--ink-soft); }
.zcard-txt b { color:var(--ink); font-weight:600; }

/* ---- conventions strip ---- */
.rules { border-bottom:1px solid var(--rule); background:var(--surface-2); }
.rules-in {
  max-width:1180px; margin:0 auto; padding:20px 32px;
  display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:20px;
}
.rule-item { display:flex; gap:12px; }
.rule-num {
  font-family:"JetBrains Mono",ui-monospace,monospace; font-size:11px;
  color:var(--accent); padding-top:3px; flex:0 0 auto;
}
.rule-item p { margin:0; font-size:14.5px; line-height:1.5; color:var(--ink-soft); }
.rule-item strong { color:var(--ink); }

/* ---- shell ---- */
.shell { max-width:1180px; margin:0 auto; padding:0 32px;
  display:grid; grid-template-columns:var(--rail) minmax(0,1fr); gap:48px; }

/* ---- nav ---- */
.rail { padding:34px 0 80px; }
.rail-sticky { position:sticky; top:24px; max-height:calc(100vh - 48px);
  overflow-y:auto; padding-right:8px; }
.rail h2 {
  font-family:Archivo,sans-serif; font-size:10.5px; font-weight:600;
  letter-spacing:.15em; text-transform:uppercase; color:var(--ink-faint);
  margin:0 0 4px; padding:0; border:0;
}
.rail .flow-note {
  font-size:12px; color:var(--ink-faint); margin:0 0 16px; line-height:1.45;
  font-family:Archivo,sans-serif;
}
.rail ul { list-style:none; margin:0; padding:0; }
.nav-mod { margin:0 0 2px; }
.nav-mod > a {
  display:flex; align-items:baseline; gap:8px; text-decoration:none;
  font-family:Archivo,sans-serif; font-size:13.5px; font-weight:600;
  color:var(--ink); padding:6px 8px 6px 0; border-radius:2px;
}
.nav-mod.is-module { padding-left:26px; position:relative; }
.flow-step {
  position:absolute; left:0; top:7px;
  font-family:"JetBrains Mono",monospace; font-size:10px; font-weight:500;
  color:var(--accent); width:18px; text-align:center;
  border:1px solid var(--rule); border-radius:2px; line-height:16px;
}
.nav-mod.is-module::after {
  content:""; position:absolute; left:9px; top:25px; bottom:-2px; width:1px;
  background:var(--rule);
}
.nav-mod.is-module:last-of-type::after { display:none; }
.nav-mod.is-front > a { font-weight:500; color:var(--ink-soft); font-size:13px; }
.nav-mod > a:hover { color:var(--accent); }
.fn-count {
  margin-left:auto; font-family:"JetBrains Mono",monospace; font-size:10px;
  color:var(--ink-faint); font-variant-numeric:tabular-nums;
}
.nav-fns { margin:0 0 10px; padding:0 0 0 26px; }
.nav-fns li { margin:0; }
.nav-fns a {
  display:block; text-decoration:none; padding:2.5px 0 2.5px 10px;
  border-left:1px solid var(--rule-soft);
}
.nav-fns code {
  font-family:"JetBrains Mono",ui-monospace,monospace; font-size:11.5px;
  color:var(--ink-soft); background:none; padding:0;
  overflow-wrap:anywhere;
}
.nav-fns a:hover { border-left-color:var(--accent); }
.nav-fns a:hover code { color:var(--accent); }
.nav-fns a.active { border-left:2px solid var(--accent); background:var(--accent-soft); }
.nav-fns a.active code { color:var(--accent-ink); font-weight:500; }

/* ---- article ---- */
article { padding:34px 0 140px; max-width:74ch; }
article > h1 { display:none; }
article h2 {
  font-family:Archivo,sans-serif; font-size:26px; font-weight:700;
  letter-spacing:-.01em; margin:64px 0 16px; padding-bottom:10px;
  border-bottom:2px solid var(--ink); text-wrap:balance; scroll-margin-top:24px;
}
article h2:first-of-type { margin-top:0; }
article h2 code { font-size:.92em; background:none; padding:0; }
article h3 {
  font-family:Archivo,sans-serif; font-size:17.5px; font-weight:600;
  margin:38px 0 10px; letter-spacing:-.005em; scroll-margin-top:24px;
  text-wrap:balance;
}
article h3 code { font-size:.95em; background:var(--accent-soft);
  color:var(--accent-ink); padding:1px 6px; border-radius:2px; }
article h4 {
  font-family:Archivo,sans-serif; font-size:14.5px; font-weight:600;
  margin:28px 0 8px; color:var(--ink-soft); scroll-margin-top:24px;
}
article h4 code { font-size:1em; background:none; padding:0; color:var(--ink); }
.anchor {
  float:left; margin-left:-1.05em; width:1.05em; text-decoration:none;
  color:var(--rule); font-weight:400; opacity:0;
  font-family:"JetBrains Mono",monospace;
}
h2:hover .anchor, h3:hover .anchor, h4:hover .anchor { opacity:1; }
.anchor:hover { color:var(--accent); }
article p { margin:0 0 15px; }
article ul, article ol { margin:0 0 16px; padding-left:22px; }
article li { margin:0 0 7px; }
article hr { border:0; border-top:1px solid var(--rule); margin:46px 0; }
article a { color:var(--accent); text-decoration:underline;
  text-decoration-thickness:1px; text-underline-offset:2px; }

code {
  font-family:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
  font-size:.855em; background:var(--code-bg); color:var(--code-ink);
  padding:1.5px 5px; border-radius:2px; overflow-wrap:anywhere;
}
.code-wrap { overflow-x:auto; margin:0 0 18px; border-radius:3px;
  border:1px solid var(--rule); background:var(--code-bg); }
pre { margin:0; padding:15px 18px; }
pre code {
  background:none; padding:0; font-size:12.8px; line-height:1.62;
  white-space:pre; color:var(--code-ink);
}

.table-wrap { overflow-x:auto; margin:0 0 20px; border:1px solid var(--rule);
  border-radius:3px; }
table { border-collapse:collapse; width:100%; font-size:14px;
  font-family:Archivo,sans-serif; }
th, td { text-align:left; padding:9px 14px; border-bottom:1px solid var(--rule-soft);
  vertical-align:top; }
th { background:var(--surface-2); font-weight:600; font-size:12px;
  letter-spacing:.03em; text-transform:uppercase; color:var(--ink-soft);
  white-space:nowrap; }
tbody tr:last-child td { border-bottom:0; }
td code { font-size:12.5px; }
td { font-variant-numeric:tabular-nums; }

/* every Note in this document flags something the code gets wrong or
   cannot yet check, so it is information, not decoration */
.note {
  display:flex; gap:14px; margin:0 0 20px; padding:14px 16px;
  background:var(--signal-soft); border-left:3px solid var(--signal);
  border-radius:0 2px 2px 0;
}
.note-tag {
  flex:0 0 auto; font-family:Archivo,sans-serif; font-size:10px; font-weight:700;
  letter-spacing:.11em; text-transform:uppercase; color:var(--signal);
  padding-top:4px;
}
.note p { margin:0; font-size:15px; line-height:1.55; }

::selection { background:var(--accent-soft); }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:2px; }

@media (max-width:940px) {
  .shell { grid-template-columns:1fr; gap:0; }
  .rail { padding:24px 0 0; border-bottom:1px solid var(--rule); }
  .rail-sticky { position:static; max-height:none; }
  .nav-fns { display:none; }
  .nav-mod.is-module::after { display:none; }
  article { padding-top:28px; }
}
@media (max-width:640px) {
  body { font-size:16px; }
  .masthead, .rules-in, .shell { padding-left:20px; padding-right:20px; }
  .zcard { display:none; }
}
@media (prefers-reduced-motion:reduce) { * { scroll-behavior:auto !important; } }
html { scroll-behavior:smooth; }
"""

ZSVG = """<svg width="58" height="72" viewBox="0 0 58 72" fill="none" aria-hidden="true">
  <line x1="15" y1="8" x2="15" y2="60" stroke="currentColor" stroke-width="1.25" opacity=".35"/>
  <path d="M15 60 l-4 -7 h8 z" fill="var(--accent)"/>
  <circle cx="15" cy="8" r="2.6" fill="none" stroke="var(--accent)" stroke-width="1.4"/>
  <text x="24" y="11" font-family="JetBrains Mono, monospace" font-size="8.5"
        fill="currentColor" opacity=".75">Z 0</text>
  <text x="24" y="40" font-family="JetBrains Mono, monospace" font-size="8.5"
        fill="var(--accent)">+Z</text>
  <text x="24" y="63" font-family="JetBrains Mono, monospace" font-size="8.5"
        fill="currentColor" opacity=".75">Z 270</text>
</svg>"""

HTML = f"""<title>lab API Reference</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=JetBrains+Mono:wght@400;500&display=swap">
<style>{CSS}</style>

<header class="masthead">
  <div class="masthead-in">
    <div class="brand">
      <p class="eyebrow">Self-driving lab &middot; Klipper pipetting robot</p>
      <h1>The <code>lab</code> package, function by function</h1>
      <p class="lede">Every public function in the library: what it does, what it
      raises, and the part worth reading &mdash; what mistake it exists to
      prevent.</p>
      <ul class="stats">
        <li><b>{n_fns}</b>functions</li>
        <li><b>7</b>modules</li>
        <li><b>66</b>tests</li>
      </ul>
    </div>
    <div class="zcard">
      {ZSVG}
      <div class="zcard-txt">
        <b>+Z is DOWN</b><br>
        Z 0 is home, at the top.<br>
        Larger Z is <b>lower</b>.<br>
        "above Z 60" means <b>z &lt; 60</b>.
      </div>
    </div>
  </div>
</header>

<section class="rules">
  <div class="rules-in">
    <div class="rule-item"><span class="rule-num">01</span>
      <p><strong>+Z is down.</strong> Every Z comparison is written in this frame.
      If one reads the natural way round, it is a bug.</p></div>
    <div class="rule-item"><span class="rule-num">02</span>
      <p><strong>Volumes are microlitres.</strong> The conversion to plunger
      millimetres happens in one function and nowhere else.</p></div>
    <div class="rule-item"><span class="rule-num">03</span>
      <p><strong>A check that cannot be evaluated is not a pass.</strong> Missing
      measurements produce an explicit warning. Silence means checked.</p></div>
  </div>
</section>

<div class="shell">
  <nav class="rail" aria-label="Contents">
    <div class="rail-sticky">
      <h2>Contents</h2>
      <p class="flow-note">Modules numbered in the order data flows through
      them.</p>
      <ul>{NAV}</ul>
    </div>
  </nav>
  <article>{BODY}</article>
</div>

<script>
(function () {{
  // Position scan rather than IntersectionObserver: a thin observer band never
  // fires when the reader jumps (anchor click, Ctrl+F, restored scroll), which
  // leaves nothing highlighted exactly when the rail is most needed.
  var rail = document.querySelector('.rail-sticky');
  var links = Array.prototype.slice.call(document.querySelectorAll('.nav-fns a'));
  if (!links.length) return;

  var entries = [];
  function measure() {{
    entries = links.map(function (a) {{
      var el = document.getElementById(a.getAttribute('href').slice(1));
      return el ? {{ a: a, top: el.getBoundingClientRect().top + window.scrollY }} : null;
    }}).filter(Boolean).sort(function (p, q) {{ return p.top - q.top; }});
  }}

  var active = null;
  function update() {{
    var mark = window.scrollY + 140;
    var lo = 0, hi = entries.length - 1, found = -1;
    while (lo <= hi) {{
      var mid = (lo + hi) >> 1;
      if (entries[mid].top <= mark) {{ found = mid; lo = mid + 1; }}
      else {{ hi = mid - 1; }}
    }}
    var next = found >= 0 ? entries[found].a : null;
    if (next === active) return;
    if (active) active.classList.remove('active');
    active = next;
    if (!active) return;
    active.classList.add('active');
    // Keep the highlight in view without yanking the whole page.
    var r = active.getBoundingClientRect(), rr = rail.getBoundingClientRect();
    if (r.top < rr.top + 8 || r.bottom > rr.bottom - 8) {{
      rail.scrollTop += r.top - rr.top - rr.height / 2;
    }}
  }}

  // Timestamp throttle, not requestAnimationFrame: rAF never fires while the
  // page is not compositing (a hidden tab or preview pane), which would leave
  // the rail dead exactly where it is hardest to notice.
  var last = 0, timer = null;
  function onScroll() {{
    var now = Date.now();
    if (now - last >= 80) {{ last = now; update(); return; }}
    if (timer) return;
    timer = setTimeout(function () {{
      timer = null; last = Date.now(); update();
    }}, 80 - (now - last));
  }}

  measure();
  update();
  window.addEventListener('scroll', onScroll, {{ passive: true }});
  window.addEventListener('resize', function () {{ measure(); update(); }});
  window.addEventListener('load', function () {{ measure(); update(); }});
}})();
</script>
"""

fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(HTML)
os.replace(tmp, OUT)
print("wrote %s  (%d KB)" % (OUT, len(HTML) // 1024))
print("  nav: %d modules, %d function entries" % (len(modules), n_fns))
print("  unconverted markdown left in body: %d '**' pairs, %d backtick runs"
      % (BODY.count("**") // 2, BODY.count("```")))
