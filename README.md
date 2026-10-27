# QuinePack

Build self-containing archives: extracting the archive yields the original payload plus a byte-for-byte copy of the archive itself.

QuinePack 把本地文件或目录树转换为「自含压缩包」。解压后会得到原始载荷，以及归档自身的一份逐字节副本。

## Status

Stage 2 is implemented:

- `7z`: compressed edition, double-LZMA2 coder chain.
- `7z-store`: store-only compatibility edition, single LZMA2 coder.
- `txz`: store-only tar.xz compatibility edition, readable by `xz`/`tar` and verified with Python stdlib `lzma`/`tarfile`.
- `pack`, `verify`, `version` commands.
- `--format all` and deterministic output for the same payload bytes and mtimes.

Not implemented yet:

- Streaming/mmap implementation for the 1 GiB memory target in ADR-0007.
- PAX tar headers for names beyond ustar's 100-byte name / 155-byte prefix limit.
- Type/zip variants; ZIP is intentionally unsupported (see ADR-0006).

## Requirements

- Python 3.11 or newer.
- Official 7-Zip for external verification (`7z`, `7zr`, or `--7zr PATH`).
- No third-party runtime dependencies.

## Usage

Build a compressed archive next to the input:

```bash
python -m quinepack pack site/ --format 7z
```

Build all three variants into a directory and verify them:

```bash
python -m quinepack pack site/ \
  --format all \
  --outdir dist/ \
  --7zr "/path/to/7za" \
  --verify
```

Verify an existing archive:

```bash
python -m quinepack verify dist/site.7z --against site/ --7zr "/path/to/7za"
```

Output layout:

```text
site/
├── index.html
├── assets/...
├── site.7z                     # self copy
└── .quinepack/
    └── site.7z.seed            # seed / bootstrap copy
```

The seed is scaffolding used to rebuild the self copy. It is not payload and can be deleted after extraction.

## Development

```bash
python -m pytest -q
python -m compileall -q quinepack tests
```

Stage 2 keeps the whole payload and archive in memory. The 1 GiB streaming design is recorded in `docs/adr/0007-1gib-prototype-baseline.md`.

## License

MIT.