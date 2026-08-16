# markdown

This is a package for the [Milo language](https://milo-language.github.io/milo/).

## Overview

Parse markdown and render it to HTML. Implements CommonMark 0.31.2 plus GFM.

The parsed tree is public too, so you can walk it yourself: pull out headings,
check links, find raw HTML.

One thing to know: raw HTML in the source reaches the output verbatim, because
CommonMark says it does. This is a parser, not a sanitizer.

Full API, conformance numbers and the not-implemented list: [docs/api.md](docs/api.md).

## Installation

```bash
milo add github.com/milo-language/milo-markdown          # latest release
milo add github.com/milo-language/milo-markdown@v0.1.0   # or pin a tag
```

```milo
from "markdown" import { mdToHtml, Markdown, NodeKind }
```

## Examples

### Rendering to HTML

```milo
from "markdown" import { mdToHtml }

fn main(): i32 {
    print(mdToHtml("# Release notes\n\nSome *emphasis* and a [link](/x).\n"))
    return 0
}
```

```html
<h1>Release notes</h1>
<p>Some <em>emphasis</em> and a <a href="/x">link</a>.</p>
```

### Walking the tree

The same parse answers questions the renderer never asked. Pulling an outline
out of a document is a walk over its top-level children:

```milo
from "markdown" import { Markdown, NodeKind }

fn main(): i32 {
    let doc = Markdown.parse("# One\n\ntext\n\n## Two\n\n### Three\n")
    let root = doc.root()
    for i in 0..doc.len(root) {
        let n = doc.at(root, i)
        if doc.kind(n) as i32 == NodeKind.Heading as i32 {
            print($"h{doc.level(n)}: {doc.innerText(n)}")
        }
    }
    return 0
}
```

```
h1: One
h2: Two
h3: Three
```

### Refusing raw HTML from untrusted authors

Since raw HTML reaches the output untouched, rendering markdown you did not
write means checking the tree for it first:

```milo
from "markdown" import { Markdown, NodeKind }

// True if any node in the subtree is raw HTML the author wrote themselves.
fn hasRawHtml(doc: &Markdown, node: i64): bool {
    let k = doc.kind(node) as i32
    if k == NodeKind.HtmlBlock as i32 || k == NodeKind.HtmlInline as i32 {
        return true
    }
    for i in 0..doc.len(node) {
        if hasRawHtml(doc, doc.at(node, i)) {
            return true
        }
    }
    return false
}

fn main(): i32 {
    let clean = Markdown.parse("Just *markdown* here.\n")
    let dirty = Markdown.parse("Hello <script>alert(1)</script>\n")

    print($"clean: {hasRawHtml(clean, clean.root())}")
    print($"dirty: {hasRawHtml(dirty, dirty.root())}")
    print(dirty.toHtml())
    return 0
}
```

```
clean: false
dirty: true
<p>Hello <script>alert(1)</script></p>
```

Reject the document, or run the output through a real sanitizer. Do not rely on
this package to do it.

A renderer you can point at any file, in HTML or outline form:

```bash
milo run examples/render.milo README.md
milo run examples/render.milo README.md --outline
```
