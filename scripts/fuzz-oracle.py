#!/usr/bin/env python3
"""Differential fuzzing against cmark: same input, two parsers, one answer.

    python3 scripts/fuzz-oracle.py                     # 400 cases, fixed seed
    python3 scripts/fuzz-oracle.py --n 5000 --seed 7
    python3 scripts/fuzz-oracle.py --show 5            # print the first 5 diffs

The spec examples pin the constructs the spec chose to write down. This pins
what happens between them: inputs are built by mutating spec examples and by
splicing markdown fragments together, so nesting and adjacency get exercised in
combinations nobody wrote a test for. Both parsers render each input and the
HTML is compared with the same normalisation the spec oracle uses.

cmark 0.31.2 does not implement GFM, so tables, strikethrough, task lists and
bare-URL autolinks make the two disagree *by design*. Those diffs are detected
and counted separately; anything else is a finding and fails the run.
"""

import argparse
import pathlib
import random
import re
import shlex
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Reuse the spec oracle's normaliser and example loader.
import importlib.util
_spec = importlib.util.spec_from_file_location("spec_oracle", ROOT / "scripts" / "spec-oracle.py")
spec_oracle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spec_oracle)

SEPARATOR = spec_oracle.SEPARATOR
RUNNER = spec_oracle.RUNNER

FRAGMENTS = [
    "# heading\n", "## h2 ##\n", "setext\n===\n", "para text\n", "\n", "---\n",
    "> quote\n", ">> deep\n", "- item\n", "1. item\n", "2) item\n",
    "    indented code\n", "```info\ncode\n```\n", "~~~\ncode\n~~~\n",
    "<div>\nhtml\n</div>\n", "<!-- comment -->\n", "<span>inline</span>\n",
    "*em* **strong** ***both***\n", "_a_ __b__ ___c___\n", "`code` ``t`ck``\n",
    "[link](/url 'title')\n", "[ref][a]\n", "[a]: /u \"t\"\n", "![img](/i)\n",
    "<https://a.b>\n", "<a@b.cd>\n", "a\\*b\\\\c\n", "&amp; &#65; &nope;\n",
    "line  \nbreak\n", "line\\\nbreak\n", "  lazy\ncontinuation\n",
    "* a\n  * b\n\n    c\n", "1. a\n\n2. b\n", "- [ ] task\n",
    "| a | b |\n| - | - |\n| 1 | 2 |\n", "~~struck~~\n", "www.example.com\n",
    "\ttab code\n", "  * \t item\n", "[]()\n", "[a](<b c>)\n", "![](i \"t\")\n",
    "text with <b>tag</b> and & < > \" chars\n", "*a **b* c**\n", "[[[[a]]]]\n",
]

MUTATION_CHARS = list("*_`[]()<>#-+.!\\&|~ \t\n\"'0123456789abc")


def mutate(rng, text):
    for _ in range(rng.randint(1, 4)):
        if not text:
            break
        op = rng.randint(0, 3)
        i = rng.randrange(len(text))
        if op == 0:                      # delete
            text = text[:i] + text[i + 1:]
        elif op == 1:                    # insert
            text = text[:i] + rng.choice(MUTATION_CHARS) + text[i:]
        elif op == 2:                    # duplicate a slice
            j = min(len(text), i + rng.randint(1, 8))
            text = text[:j] + text[i:j] + text[j:]
        else:                            # replace
            text = text[:i] + rng.choice(MUTATION_CHARS) + text[i + 1:]
    return text


def generate(rng, seeds, n):
    cases = []
    while len(cases) < n:
        if rng.random() < 0.5:
            body = mutate(rng, rng.choice(seeds))
        else:
            body = "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 5)))
            if rng.random() < 0.4:
                body = mutate(rng, body)
        if "@@@" in body or "\0" in body:
            continue
        if not body.endswith("\n"):
            body += "\n"
        cases.append(body)
    return cases


GFM_MARKERS = ("<table", "<del>", 'type="checkbox"')

URL_ATTR_RE = re.compile(r'(href="|src=")([^"]*)"')


def _trim_url_edge_ws(html):
    def repl(m):
        v = re.sub(r"^(?:%20|%09)+", "", m.group(2))
        v = re.sub(r"(?:%20|%09)+$", "", v)
        return m.group(1) + v + '"'
    return URL_ATTR_RE.sub(repl, html)


ALT_ATTR_RE = re.compile(r'(alt=")([^"]*)"')
REFDEF_P_RE = re.compile(r"<p>\[[^\]<]*\]:[^<]*</p>")


def known_divergence(ours, theirs):
    """Diffs investigated by hand, where commonmark.js — the spec's own
    reference implementation — produces exactly our output and cmark does not.
    Each was checked by running commonmark.js on the same input.

    1. url-edge-whitespace   cmark trims whitespace off a `<...>` link
       destination. The spec says the destination is the characters between the
       angle brackets, so `< b >` is " b " and encodes as %20b%20.
    2. alt-text-newline      cmark turns a soft break inside an image
       description into a space; commonmark.js keeps the newline.
    3. unterminated-title    `[a]: /u "t\"` is not a definition: what follows
       the destination is not whitespace to end of line. cmark accepts it
       anyway and swallows the paragraph.
    4. comment-tail          a stray `-->` after a comment is text and gets
       escaped; cmark emits it raw.
    """
    a, b = ours, theirs
    if _trim_url_edge_ws(a) == _trim_url_edge_ws(b):
        return "url-edge-whitespace"
    collapse = lambda h: ALT_ATTR_RE.sub(lambda m: m.group(1) + re.sub(r"\s+", " ", m.group(2)) + '"', h)
    if collapse(a) == collapse(b):
        return "alt-text-newline"
    if REFDEF_P_RE.sub("", a) == REFDEF_P_RE.sub("", b):
        return "title-rewind"
    strip_title = lambda h: re.sub(r' title="[^"]*"', "", h)
    if strip_title(a) == strip_title(b) and a.count(" title=") < b.count(" title="):
        return "title-rewind"
    if a.replace("--&gt;", "-->") == b.replace("--&gt;", "-->"):
        return "comment-tail"
    return None


def gfm_explains(ours, theirs):
    """Is this diff wholly explained by a GFM extension cmark does not have?"""
    if any(m in ours for m in GFM_MARKERS):
        return True
    # extended autolink: we linked a bare www./http(s) URL that cmark left as text
    extra_links = re.findall(r'<a href="(https?://[^"]*)"', ours)
    if extra_links and len(re.findall(r"<a href=", ours)) > len(re.findall(r"<a href=", theirs)):
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--milo", default="milo")
    ap.add_argument("--cmark", default="cmark")
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--show", type=int, default=3)
    args = ap.parse_args()

    milo = shlex.split(args.milo)
    try:
        version = subprocess.run([args.cmark, "--version"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        sys.stderr.write("cmark not found; install it or pass --cmark\n")
        return 2

    rng = random.Random(args.seed)
    seeds = [e["markdown"] for e in spec_oracle.load_examples()]
    cases = generate(rng, seeds, args.n)

    batch = ("\n" + SEPARATOR + "\n").join(cases)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(batch)
        path = f.name
    try:
        proc = subprocess.run(milo + ["run", str(RUNNER), "--batch", path],
                              capture_output=True, text=True, cwd=str(ROOT))
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        sys.stderr.write("\nrunner failed (exit %d) — a crash on generated input is itself a finding\n"
                         % proc.returncode)
        return 2
    ours_all = proc.stdout.split("\n" + SEPARATOR + "\n")
    if len(ours_all) != len(cases):
        sys.stderr.write("runner returned %d documents, expected %d\n" % (len(ours_all), len(cases)))
        return 2

    findings = []
    gfm_diffs = 0
    known = 0
    known_classes = {}
    for case, ours in zip(cases, ours_all):
        # --unsafe: cmark 0.30+ omits raw HTML by default, but CommonMark says
        # to pass it through, and that is what this package does.
        theirs = subprocess.run([args.cmark, "--unsafe"], input=case,
                                capture_output=True, text=True).stdout
        if spec_oracle.normalize(ours) == spec_oracle.normalize(theirs):
            continue
        if gfm_explains(ours, theirs):
            gfm_diffs += 1
            continue
        cls = known_divergence(spec_oracle.normalize(ours), spec_oracle.normalize(theirs))
        if cls:
            known += 1
            known_classes[cls] = known_classes.get(cls, 0) + 1
            continue
        findings.append((case, ours, theirs))

    print("%s vs milo-markdown: %d cases (seed %d)" % (version, len(cases), args.seed))
    print("  agree                 %d" % (len(cases) - len(findings) - gfm_diffs - known))
    print("  differ (GFM by design) %d" % gfm_diffs)
    detail = " (%s)" % ", ".join("%s x%d" % kv for kv in sorted(known_classes.items())) if known_classes else ""
    print("  differ (known, we follow commonmark.js) %d%s" % (known, detail))
    print("  differ (findings)      %d" % len(findings))
    for case, ours, theirs in findings[:args.show]:
        print("\n--- input   : %r" % case)
        print("    cmark   : %r" % theirs)
        print("    ours    : %r" % ours)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
