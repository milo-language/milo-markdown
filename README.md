# markdown

A CommonMark 0.31.2 + GFM parser and HTML renderer for
[Milo](https://github.com/milo-language/milo). No dependencies beyond the
standard library.

```bash
milo add github.com/milo-language/milo-markdown          # latest release
milo add github.com/milo-language/milo-markdown@v0.1.0   # or pin a specific tag
```

The ref-less form resolves to the highest tag and writes it into `milo.json`;
`milo.lock` then records the exact commit and tree hash either way.

```milo
from "markdown" import { mdToHtml, Markdown, NodeKind }

fn main(): i32 {
    print(mdToHtml("# Hi\n\nSome *emphasis* and a [link](/x).\n"))

    // The tree is public, so the same parse answers other questions.
    let doc = Markdown.parse("# One\n\n## Two\n")
    for i in 0..doc.len(doc.root()) {
        let n = doc.at(doc.root(), i)
        if doc.kind(n) as i32 == NodeKind.Heading as i32 {
            print($"h{doc.level(n)}: {doc.innerText(n)}")
        }
    }
    return 0
}
```

## Handles, not trees

A document parses into a flat pool of nodes. Every node is an `i64` handle into
that pool, and **-1 means "not there"** — so a walk that misses is not an error,
it is a handle that reads as absent:

```milo
doc.text(doc.at(doc.root(), 99))   // "" — no bounds check needed at the call site
```

The pool (rather than a recursive enum) is deliberate: Milo has no pointers in
safe code, so a self-referential value type would need an arena anyway — and
indices make the whole document one contiguous, cheaply-cloned value.

## The tree is the product

`toHtml` is one consumer of the tree, not the only one. A docs generator pulls
`Heading` nodes out of it, a link checker walks `Link`/`Image` nodes for their
`dest`, and a linter can refuse raw HTML by looking for `HtmlBlock`. Anything
the renderer can see, a caller can see.

```bash
milo run examples/render.milo README.md             # HTML
milo run examples/render.milo README.md --outline   # headings, links, code blocks
```

## API

| Function | Returns |
|---|---|
| `mdToHtml(src: &string)` | the rendered HTML |
| `Markdown.parse(src: string)` | a parsed document — there is no error case, markdown always parses |
| `escapeHtml(s: &string)` | text escaped for element content or a quoted attribute |

Walking, all on `Markdown`:

| Method | Meaning |
|---|---|
| `root()` | handle of the document node |
| `len(node)` | number of children (0 for a leaf or a missing handle) |
| `at(node, i)` | i-th child, or -1 |
| `parent(node)` | enclosing node, or -1 |
| `kind(node)` | `NodeKind.Document / Heading / Paragraph / CodeBlock / …` |
| `exists(node)` | whether the handle is real |

Reading:

| Method | Meaning |
|---|---|
| `text(node)` | literal payload: text, code span/block body, raw HTML |
| `innerText(node)` | every descendant's text, concatenated — a heading's title, an image's alt |
| `dest(node)` | `Link`/`Image` destination |
| `title(node)` | `Link`/`Image` title |
| `info(node)` | a fenced code block's info string: `rust` for a rust-tagged fence |
| `level(node)` | heading level, or a list's start number |
| `align(node)` | a table cell's `Align.None / Left / Center / Right` |
| `isOrdered(node)` / `isTight(node)` | list shape |
| `isTask(node)` / `isChecked(node)` | GFM task list items |
| `toHtml()` | render the whole document |

`NodeKind` is `Document, BlockQuote, List, Item, CodeBlock, HtmlBlock,
Paragraph, Heading, ThematicBreak, Table, TableRow, TableCell, Text, SoftBreak,
LineBreak, Code, HtmlInline, Emph, Strong, Strike, Link, Image, Missing`.

## Conformance

**653 of the 655 CommonMark 0.31.2 spec examples pass (99.7%).** The suite is in
`tests/spec.txt` and `scripts/spec-oracle.py` scores it, prints a per-section
breakdown, and ratchets the result against `tests/spec-baseline.json` so an
example that passes today cannot quietly stop passing tomorrow.

The two failures are the same deliberate deviation: examples 610 and 613 assert
that a bare `https://example.com` in running text stays text, and the GFM
extended-autolink extension — which this package implements — says it becomes a
link. Turning it off would score 655/655 and be less useful.

Comparison is HTML-normalised, exactly as cmark's own test driver does it:
whitespace between block tags does not count, attributes are sorted, everything
inside `<pre>` is compared verbatim. Tag names, nesting, attribute values,
entity references and code content must match byte for byte.

As an independent check, rendering the 206 KB spec document itself produces
**output byte-identical to `cmark 0.31.2`**, and `scripts/fuzz-oracle.py`
differentially fuzzes the two parsers (mutated spec examples plus spliced
fragments); the last 22,500 generated inputs produced no unexplained
disagreement. Speed is within 3× of cmark: 2 MB of markdown in 0.03 s.

### What is not implemented

- **GFM email autolinks.** `<a@b.com>` works (that is CommonMark); a bare
  `a@b.com` is not turned into a link.
- **A table header must be its paragraph's only line.** GFM splits a multi-line
  paragraph at its last line and starts the table there; this package requires
  the header row to stand alone, which is how tables are actually written.
- **Footnotes, definition lists, and GFM's `tagfilter`** — none of these are
  CommonMark, and the last one is a sanitizer's job (see below).
- **Smart punctuation** (cmark's `--smart`): quotes and dashes are left alone.
- **Source positions.** Nodes do not carry line/column numbers.
- **Full Unicode tables.** Case folding for reference labels covers ASCII,
  Latin-1, Latin Extended-A, Greek, Cyrillic and both sharp-s forms; the
  punctuation and whitespace classes the emphasis rules need cover the common
  ranges plus every currency symbol. A codepoint outside those ranges is treated
  as an ordinary letter, which is the right answer for a script without case or
  punctuation and the wrong one for a rare mark.

## Escaping, and what this package does not do for you

Escaping is a security boundary, so it is worth being precise about where it is.
Text, code and attribute values are escaped on the way out: `&`, `<`, `>` and
`"` become character references, and a URL in an `href`/`src` is percent-encoded
(with `&` and `'` as references) so a destination cannot close the attribute.

**Raw HTML in the source is emitted verbatim, because CommonMark says it is.**
That means `<script>alert(1)</script>` in the input reaches the output. If you
render markdown from untrusted authors, either walk the tree and drop
`HtmlBlock`/`HtmlInline` nodes, or run the output through a real sanitizer. This
package is a parser, not a sanitizer, and no option here makes attacker-supplied
HTML safe.

## Tests

```bash
milo test tests/markdown_test.milo        # unit tests: GFM, escaping, tree API
python3 scripts/spec-oracle.py            # 655 spec examples + per-section breakdown
python3 scripts/spec-oracle.py --check    # the ratchet: fails on any regression
python3 scripts/fuzz-oracle.py --n 5000   # differential fuzzing against cmark
milo run examples/render.milo README.md
```

The fuzz oracle needs `cmark` installed (`brew install cmark`); everything else
needs only Milo and Python 3.

## License

MIT
