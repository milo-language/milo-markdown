#!/usr/bin/env python3
"""Run every CommonMark spec example through this parser and score the result.

    python3 scripts/spec-oracle.py                 # score + per-section breakdown
    python3 scripts/spec-oracle.py --check         # fail if a passing example regressed
    python3 scripts/spec-oracle.py --update        # rewrite the baseline
    python3 scripts/spec-oracle.py --failures 20   # show the first 20 diffs
    python3 scripts/spec-oracle.py --section Lists # only one section

tests/spec.txt is the CommonMark 0.31.2 spec itself; its 655 examples are fenced
with 32 backticks, hold the markdown and the expected HTML separated by a line
containing only ".", and spell tabs as U+2192.

All examples run in a single process: the runner takes them as one batch file
separated by a marker line, which turns 655 compiler invocations into one.
"""

import argparse
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
from collections import OrderedDict
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "tests" / "spec.txt"
BASELINE = ROOT / "tests" / "spec-baseline.json"
RUNNER = ROOT / "tests" / "spec_runner.milo"
SEPARATOR = "@@@MILO-MD-SPLIT@@@"


# ── HTML normalisation ───────────────────────────────────────────────────────
#
# cmark's own test driver (test/normalize.py) does not compare bytes, and
# neither do we: the spec fixes the *document*, not the whitespace between block
# tags, so "<ul>\n<li>a</li>\n</ul>" and "<ul><li>a</li></ul>" are the same
# answer. This is a port of that normaliser, and it is deliberately the only
# leniency in the gate. Exactly four things happen:
#
#   1. runs of whitespace inside text collapse to a single space,
#   2. text touching a block-level tag is stripped (both sides for an end tag,
#      the left for a start tag), and output is right-stripped before a block
#      tag opens or closes,
#   3. attributes are sorted and re-quoted with double quotes,
#   4. everything inside <pre> is left exactly as it is.
#
# Nothing else is forgiven. Tag names, nesting, attribute values, entity
# references and all text inside <pre>/<code> must match the spec byte for byte,
# so a wrong escape or a missing tag is still a failure.

BLOCK_TAGS = {
    "article", "header", "aside", "hgroup", "blockquote", "hr", "iframe",
    "body", "li", "map", "button", "object", "canvas", "ol", "caption",
    "output", "col", "p", "colgroup", "pre", "dd", "progress", "div",
    "section", "dl", "table", "td", "dt", "tbody", "embed", "textarea",
    "fieldset", "tfoot", "figcaption", "th", "figure", "thead", "footer",
    "tr", "form", "ul", "h1", "h2", "h3", "h4", "h5", "h6", "video",
    "script", "style",
}

WHITESPACE_RE = re.compile(r"\s+")


class Normalizer(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=False)
        self.last = "starttag"
        self.in_pre = False
        self.output = ""
        self.last_tag = ""

    def is_block_tag(self, tag):
        return tag in BLOCK_TAGS

    def handle_data(self, data):
        after_tag = self.last in ("endtag", "starttag")
        after_block_tag = after_tag and self.is_block_tag(self.last_tag)
        if self.in_pre:
            self.output += data
            self.last = "data"
            return
        if after_tag and self.last_tag == "br":
            data = data.lstrip("\n")
        data = WHITESPACE_RE.sub(" ", data)
        if after_block_tag:
            if self.last == "starttag":
                data = data.lstrip()
            elif self.last == "endtag":
                data = data.strip()
        self.output += data
        self.last = "data"

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            self.in_pre = True
        if self.is_block_tag(tag):
            self.output = self.output.rstrip()
        self.output += "<" + tag
        for (k, v) in sorted(attrs):
            if v is None:
                self.output += " " + k
            else:
                v = v.replace("&", "&amp;").replace('"', "&quot;")
                self.output += ' %s="%s"' % (k, v)
        self.output += ">"
        self.last_tag = tag
        self.last = "starttag"

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_pre = False
        elif self.is_block_tag(tag):
            self.output = self.output.rstrip()
        self.output += "</" + tag + ">"
        self.last_tag = tag
        self.last = "endtag"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.last = "endtag"

    def handle_comment(self, data):
        self.output += "<!--" + data + "-->"
        self.last = "comment"

    def handle_decl(self, data):
        self.output += "<!" + data + ">"
        self.last = "decl"

    def unknown_decl(self, data):
        self.output += "<![" + data + "]>"
        self.last = "decl"

    def handle_pi(self, data):
        self.output += "<?" + data + ">"
        self.last = "pi"

    def handle_entityref(self, data):
        self.output += "&" + data + ";"
        self.last = "data"

    def handle_charref(self, data):
        self.output += "&#" + data + ";"
        self.last = "data"


def normalize(html):
    p = Normalizer()
    try:
        p.feed(html)
        p.close()
        return p.output
    except Exception:
        # Unparseable output can only be wrong; compare it raw so it still fails
        # loudly instead of erroring out of the run.
        return html


# ── Spec parsing ─────────────────────────────────────────────────────────────

def load_examples():
    examples = []
    section = ""
    state = 0  # 0 prose, 1 markdown, 2 html
    markdown = []
    html = []
    number = 0
    fence = re.compile(r"^`{32} example")
    end_fence = re.compile(r"^`{32}$")
    with SPEC.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if state == 0:
                if fence.match(line):
                    state = 1
                    markdown, html = [], []
                elif line.startswith("#"):
                    section = line.lstrip("#").strip()
            elif state == 1:
                if line == ".":
                    state = 2
                else:
                    markdown.append(line)
            elif state == 2:
                if end_fence.match(line):
                    number += 1
                    examples.append({
                        "number": number,
                        "section": section,
                        "markdown": "\n".join(markdown + [""]).replace("→", "\t"),
                        "html": "\n".join(html + [""]).replace("→", "\t"),
                    })
                    state = 0
                else:
                    html.append(line)
    return examples


# ── Running ──────────────────────────────────────────────────────────────────

def render_all(milo, examples):
    """Render every example in one process; returns the list of HTML outputs."""
    batch = ("\n" + SEPARATOR + "\n").join(e["markdown"] for e in examples)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(batch)
        path = f.name
    try:
        proc = subprocess.run(
            milo + ["run", str(RUNNER), "--batch", path],
            capture_output=True, text=True, cwd=str(ROOT),
        )
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        sys.stderr.write("\nrunner failed (exit %d)\n" % proc.returncode)
        sys.exit(2)
    out = proc.stdout.split("\n" + SEPARATOR + "\n")
    if len(out) != len(examples):
        sys.stderr.write("runner returned %d documents, expected %d\n" % (len(out), len(examples)))
        sys.exit(2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--milo", default="milo", help="milo command (may include arguments)")
    ap.add_argument("--check", action="store_true", help="fail if a previously passing example fails")
    ap.add_argument("--update", action="store_true", help="rewrite the baseline")
    ap.add_argument("--failures", type=int, default=0, help="print the first N failing diffs")
    ap.add_argument("--section", default=None, help="only examples whose section contains this")
    args = ap.parse_args()

    milo = shlex.split(args.milo)
    examples = load_examples()
    if args.section:
        examples = [e for e in examples if args.section.lower() in e["section"].lower()]
    if not examples:
        sys.stderr.write("no examples selected\n")
        return 2

    outputs = render_all(milo, examples)

    passing = []
    failing = []
    sections = OrderedDict()
    for e, got in zip(examples, outputs):
        sec = sections.setdefault(e["section"], [0, 0])
        sec[1] += 1
        if normalize(got) == normalize(e["html"]):
            passing.append(e["number"])
            sec[0] += 1
        else:
            failing.append((e, got))

    total = len(examples)
    npass = len(passing)
    print("%d/%d (%.1f%%)\n" % (npass, total, 100.0 * npass / total))
    width = max(len(s) for s in sections)
    for name, (ok, n) in sections.items():
        bar = "" if ok == n else "  <-- %d failing" % (n - ok)
        print("  %-*s %3d/%-3d%s" % (width, name, ok, n, bar))

    shown = 0
    for e, got in failing:
        if shown >= args.failures:
            break
        shown += 1
        print("\n--- example %d (%s)" % (e["number"], e["section"]))
        print("markdown: %r" % e["markdown"])
        print("expected: %r" % e["html"])
        print("got     : %r" % got)

    if args.update:
        rows = [", ".join(str(n) for n in passing[i:i + 20]) for i in range(0, len(passing), 20)]
        body = ",\n    ".join(rows)
        BASELINE.write_text('{\n  "total": 655,\n  "passing": [\n    %s\n  ]\n}\n' % body)
        print("\nbaseline updated: %d passing" % npass)
        return 0

    if not BASELINE.exists():
        sys.stderr.write("\nno baseline: run with --update\n")
        return 0 if not args.check else 1

    baseline = set(json.loads(BASELINE.read_text())["passing"])
    now = set(passing)
    regressed = sorted(baseline - now)
    gained = sorted(now - baseline)
    if gained:
        print("\nnew passes (%d): %s" % (len(gained), ", ".join(str(n) for n in gained[:40])))
    if regressed:
        print("\nREGRESSED (%d): %s" % (len(regressed), ", ".join(str(n) for n in regressed[:40])))
        print("examples that used to pass now fail; fix them or re-run with --update if the change is deliberate")
        return 1
    if args.check:
        print("\nratchet ok: all %d baseline examples still pass" % len(baseline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
