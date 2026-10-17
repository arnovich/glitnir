# Third-party notices

Glitnir-owned code is MIT licensed; see [LICENSE](LICENSE). Third-party components
retain their own licences and copyright notices.

The universal thin VSIX includes:

| Component | Licence | Notice in the installed extension |
| --- | --- | --- |
| KaTeX 0.16.47 renderer and styles | MIT | `dist/media/katex/LICENSE` |
| KaTeX fonts | SIL OFL 1.1 | `dist/media/katex/OFL.txt` |
| yauzl | MIT | `node_modules/yauzl/LICENSE` |
| pend | MIT | `node_modules/pend/LICENSE` |
| buffer-crc32 code adapted in yauzl | MIT | Included below |

KaTeX comes from https://github.com/KaTeX/KaTeX. The separate backend wheel also
contains the canonical KaTeX assets and licences. Its own THIRD_PARTY_NOTICES.md
is included in the wheel’s `.dist-info/licenses/` directory.

The default VSIX and backend wheel do not include Python, Lean, Mathlib, or
the optional `glitnir-forseti` package and its dependencies. Separately installed
dependencies retain their own licences. The Glitnir licence does not relicense
Asgard or other separately installed packages.

## buffer-crc32

`yauzl/crc32.js` adapts buffer-crc32 v0.2.13 from
https://github.com/brianloveswords/buffer-crc32. Its notice follows.

The MIT License

Copyright (c) 2013 Brian J. Brennan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
