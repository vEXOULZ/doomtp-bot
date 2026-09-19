# Emoji art

The SVG files in this folder are **Twemoji**, from <https://github.com/jdecked/twemoji> (the maintained
fork of Twitter's original project), vendored at tag `v15.1.0`.

Copyright 2019 Twitter, Inc and other contributors.
Graphics licensed under **CC-BY 4.0**: <https://creativecommons.org/licenses/by/4.0/>

They are served from `/static/emoji/<codepoints>.svg` and used by `webui/emoji.py`, which swaps these
characters for their image wherever a page shows one. Adding another emoji is one file, named after its
codepoints, in this folder — no code change.
