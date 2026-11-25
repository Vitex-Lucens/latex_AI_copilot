# Final pass: clean, readable LaTeX

The document has been rebuilt from a PDF, and it compiles. Before it goes to the person who'll edit it, go
over the whole source once more, as an experienced LaTeX author would: make it clean and easy to read and
edit, **without changing what the PDF says or how it looks**.

## What to clean up

1. **One paragraph = one block of text.** The conversion sometimes copies the PDF line by line. Join those
   lines back into paragraphs: remove `\\`, `\newline`, `\par`, blank lines, or separate boxes and nodes that
   only mark where the original's lines happened to end, and rejoin words hyphenated across them. Write each
   paragraph as continuous text (one line per paragraph is fine; don't wrap it by hand), with a blank line
   between paragraphs. Keep deliberate breaks: addresses, signatures, poems, table cells, list items.
2. **Positioned text** (TikZ nodes, for example on pages built from `layers/page-NN.tex`): one node per
   paragraph or text block, not one per line. Give it the block's position, a `text width` equal to the
   block's width, the alignment it has (`align=justify`, `left`, `center` or `right`), and its font, size,
   line spacing (`\fontsize{size}{line pitch}\selectfont`) and colour. Drop `\makebox[…][s]`: it only
   stretches a line to the original's width (the font is a stand-in, so it spreads the words apart). Keep
   the point the text hangs from (its left edge, centre or right edge, via the node's `anchor`) where it was.
3. **Repeated formatting becomes named commands.** Formatting that repeats (a coloured bold term, a box
   style, a spacing pattern) gets a command or environment in the preamble, used everywhere. Define colours
   once with `\definecolor` and a meaningful name, instead of repeating hex values.
4. **An orderly preamble**, grouped with a short comment per group: class and page layout, fonts, colours,
   headings, headers and footers, lists and boxes, your own commands.
5. **Remove what has no effect**: duplicate or unused packages, unused commands, empty groups, `\vspace{0pt}`,
   spacing commands that cancel out, leftover comments about the conversion.
6. **A consistent source layout**: two-space indentation inside environments, one blank line between
   paragraphs, a `% --- Name ---` comment before each major part. In a project split into
   `content/part-N.tex` files you may give the files names that say what they contain (and update the
   `\input` lines).

## What must not change

- **The wording.** Every word, number and punctuation mark stays exactly as it is, in the same order.
  The app compares the text of the PDF before and after, and undoes the whole pass if anything differs.
- **The look.** Fonts, sizes, colours, spacing between blocks, headings, lists, tables, figures and their
  positions, page size and margins. Lines inside a paragraph may break in different places than before;
  nothing else should change. The app compares every page before and after.
- **Figure snippets** (`figures/fig-NN-K.tex`): leave their coordinates as they are.

## How

Read `import/conversion-notes.md` for the decisions made during the conversion, then go through every `.tex`
file of the document (main.tex and the files it inputs, apart from the figure snippets), editing as you go.
Don't compile or run anything: the app compiles after your turn. Finish with a short summary of what you
cleaned up.
