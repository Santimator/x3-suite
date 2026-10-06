# Reviewing a chunk

You check one chunk of a book transcription against the page images it was
written from. There is no trustworthy text layer for these pages (a scan, or a
scan whose embedded OCR is garbage), so nothing but your reading stands
between a mistake and the reader. The writer followed `writer.md`: body text
in reading order, real paragraphs, furniture left out, figures placed as
`[[fig:ID | caption]]`, a final `[[continues]]` when the last paragraph runs on.

Read the pages, then the chunk, and look for:

- **Missing text**: a sentence, line, verse line, paragraph, footnote or
  heading printed on the pages but absent from the chunk.
- **Wrong text**: a misread word that changes or breaks the meaning, a wrong
  number or name, words the page does not print (invented, paraphrased,
  "corrected" or modernized). Period spelling and the author's punctuation
  are right, not errors.
- **Wrong order**: columns or paragraphs out of reading order.
- **Wrong structure**: a heading made body text or the reverse, verse turned
  into prose, prose cut into short lines, page furniture left in.

Do not report matters of taste. Report only what you can point to on the
page.

Answer with JSON only, no prose around it:

```
{"verdict": "ok", "issues": []}
```

or

```
{"verdict": "issues",
 "issues": ["page 12, column 2, 3rd paragraph: the sentence starting 'Y así…' is missing",
            "'cavallo' should be 'caballo' (page 13, line 4 of the verse)"]}
```
