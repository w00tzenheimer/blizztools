# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build and Test Commands

```bash
pip install -e .            # install for development
pip install -e ".[test]"    # install with test dependencies

pytest tests/               # run all tests (135 pass, 4 skipped)
pytest tests/ -v
pytest tests/test_main.py::test_should_download -v   # single test
```

Tests live in `tests/` at the repo root. README.md says `pytest blizztools/tests/` — that path does not exist; ignore it.

Four tests in `tests/test_main.py` are marked `@pytest.mark.skip` (CDN/version/install-manifest/download command tests) due to unresolved mocking and BLTE fixture issues. Network tests use `pytest-httpx`'s `httpx_mock` fixture; everything else is pure `tmp_path` filesystem logic.

## CLI

```bash
blizztools version WowClassic          # note: PascalCase, see "Two product namespaces"
blizztools cdn Wow
blizztools install-manifest Wow
blizztools download Wow <ckey> --output ./target
blizztools grab --product wow-classic -d ./target    # note: kebab-case
blizztools grab --all-products -p '\.pdb$'
blizztools grab --no-pdb-companions ...                # disable .pdb -> binary pairing
blizztools index ./target --dest ./target
```

`python -m blizztools.main` is equivalent to the `blizztools` entry point.

## Architecture

Python CLI over Blizzard's CDN (TACT/CASC). Click for CLI, httpx for async HTTP/2, `construct` for binary parsing, rich for output. Every Click command is a thin sync wrapper calling an async `*_command()` in the same module via `asyncio.run()`.

### Modules

- **products.py** — 603 CDN product codes (`ALL_PRODUCT_CODES`), sourced from Blizzard's Ribbit service plus legacy codes. Builds the `Product` enum dynamically at import. See "Two product namespaces" below.
- **main.py** — all Click commands, the download pipeline, and the CKey-map bookkeeping.
- **parsers.py** — pipe-delimited text tables from the patch server (`http://us.patch.battle.net:1119/{code}/versions` and `/cdns`) → `VersionDefinition` / `CdnDefinition`. Also the `parse_named_attribute{,_pair}` helpers used for key-value config files.
- **cdn.py** — build config (key-value text) → `BuildConfig` with root/install/download/encoding refs. Parsing is **positional**: `parse_named_attribute*` pops lines off the front of the list and raises `ParserError` if the key doesn't match, so attributes must appear in the expected order (including the `size`/`size-size` pair that's parsed and discarded).
- **encoding.py** — binary encoding manifest → `(header, [CeKeyPageEntry])` mapping CKey→EKeys. Pages are read in `ce_page_size_kb * 1024` chunks; trailing page padding raises, which is caught and treated as end-of-page.
- **tags.py** — install-manifest tag decoding and tag-derived filenames. See "Manifest tags and name variants".
- **cache.py** — local disk cache for immutable CDN blobs (archive indices).
- **archives.py** — TACT archive `.index` parsing plus a lenient key-value reader for the CDN config. See "Archived vs loose objects".
- **blte.py** — BLTE container decompression. Only `PlainData` (`N`) and `Zlib` (`Z`) are implemented; `Recursive` (`F`) and `Encrypted` (`E`) raise `NotImplementedError`.
- **models.py** — `construct` structs (`InstallManifest` "IN", `EncodingManifest` "EN", `DownloadManifest` "DL", `IndexFile`) plus `Md5Hash`, which accepts a 32-char hex string or 16 raw bytes and is hashable/comparable.

### Key concepts

- **CKey** — MD5 of decoded file content. **EKey** — MD5 of the encoded (BLTE) blob on the CDN.
- CDN fetches are **https**, in two shapes: `https://{host}/{cdn_path}/config/{h[:2]}/{h[2:4]}/{h}` for configs and `https://{host}/{cdn_path}/data/{ekey[:2]}/{ekey[2:4]}/{ekey}` for data. Only the patch server (`BASE_URL`) is plain http.
- `.ckey_map.json` in the dest dir maps CKey hex → `{filename, product, version}`, making `grab` resumable.

### Two product namespaces (common source of confusion)

`products.py` derives two different names from each CDN code:

| | Produced by | Example (code `wowclassic`) | Used by |
|---|---|---|---|
| Enum name (PascalCase) | `_code_to_enum_name` + `_ENUM_NAME_OVERRIDES` | `WowClassic` | `version`, `cdn`, `install-manifest`, `download` |
| CLI name (kebab-case) | `_code_to_cli_name` + `_CLI_NAME_OVERRIDES` | `wow-classic` | `grab --product`, `grab -f`, `DEFAULT_PRODUCTS` |

The four non-`grab` commands use `click.Choice([p.name for p in Product])`, so they accept **only** the PascalCase enum name — `blizztools version wow-classic` is rejected. `grab` goes through `product_name_to_enum()` / `PRODUCT_NAME_MAP` (640 entries), which accepts kebab-case *and* the raw CDN code. README examples showing `blizztools version wow-classic` are wrong.

Side effect: because the enum has 603 members, any `click.Choice` validation error or `--help` for those commands dumps all 603 names.

`products.py` raises `ValueError` **at import time** if two codes map to the same enum name. Adding a code to `ALL_PRODUCT_CODES` can therefore break the whole package; add an `_ENUM_NAME_OVERRIDES` entry to disambiguate.

`DEFAULT_PRODUCTS` (122 entries) is what `grab` uses with no `--product`/`-f`/`--all-products`: every code starting with `wow`, `w1`, `w2`, `w3`, `war`, `pro`, `hs`, `fenris`, or `drtl`.

### Download pipeline (`grab`)

```
product name → product_name_to_enum → /versions + /cdns → build config hash
→ fetch build config (try every cdn_def host, then every cdn_def server, on 404)
→ install manifest (install EKey → BLTE) → filter entry names by regex
→ per file: skip if CKey in map / file present → encoding manifest CKey→EKey
→ download EKey → BLTE decompress → write → rename into place → update CKey map
```

`install_manifest_command(..., return_data=True)` doubles as the internal fetch used by `grab`; it returns `(manifest, version_name, selected_cdn_url)` so the CDN chosen during the fallback walk is reused for subsequent downloads instead of re-resolved.

### The rename dance (and why merge_dirs.py exists)

`grab` calls `download_command(product_enum.name, ...)`, which writes to `{dest}/{EnumName}/{version}/{ckey}` — note the **PascalCase** directory. `grab` then renames that file into `{dest}/{cli-name}/{version}/{filename}`, i.e. the **kebab-case** directory. On a case-insensitive filesystem (default macOS) `Wow/` and `wow/` are the same directory; on a case-sensitive one you get both, and the PascalCase one is left behind empty. `merge_dirs.py` normalizes these leftovers — that's its purpose, not just cosmetic renaming.

`merge_dirs.py` is two-phase for safety: `--execute` moves originals into `_to_delete_/`, and only `--cleanup --execute` deletes them. Default is dry-run. Its `CDN_CODE_MAP` duplicates a subset of `_CLI_NAME_OVERRIDES` from `products.py` — keep the two in sync when adding well-known products.

### CDN host pool

`CdnPool` holds every `host/path` from the `/cdns` table (hosts first, then servers), tries them in order, retries transient errors, and remembers which host worked. **A single edge can be healthy for small objects and broken for large ones**: `level3.blizzard.com` has served configs fine while dropping a 187 MB body after exactly 6 MiB, when `us.cdn.blizzard.com` served the same object whole in 3.3s. `install_manifest_command` used to pin the first host that answered for the entire run, so one sick edge failed everything downstream.

A 403/404 means "not on this host" and moves to the next without consuming retries; missing from every host means the object is archived, not absent (see below).

**WoW's encoding manifest is 187 MB.** `download_command` used to refetch and reparse it per file, so a 105-file bundle grab meant 105 downloads of it — that is what an `InvalidBodyLengthError: Expected 187471460` on some unrelated file actually is. `install_manifest_command(return_data=True)` now resolves CKey→EKey once (2.87M mappings for Wow) and threads the dict through.

### Scaling the archive scan

A cold WoW run needs 1,347 archive indices before it can resolve one archived file, and retaining every entry costs hundreds of MB (WoW's archives hold ~4.7M objects). Three mitigations, all in `ArchiveResolver`:

- **Wanted set.** `grab` calls `resolver.set_wanted(...)` with the EKeys it actually intends to fetch, so only those are retained.
- **Incremental scan.** `_locate` reads archive indices in batches and stops at the batch that yields the requested key, keeping its position so later lookups resume rather than restart. Crucially it stops on *the key it was asked for*, not on the wanted set filling up: the wanted set contains loose objects that are in no archive at all, so waiting for it would always read every index. `_load` remains the exhaustive path for callers that genuinely need everything.
- **Disk cache.** `IndexCache` stores index blobs under `~/.cache/blizztools` (`BLIZZTOOLS_CACHE_DIR` or `XDG_CACHE_HOME` override, `--no-index-cache` to disable). Indices are named by the hash of their own contents, so they are immutable and **never need invalidating**; the cache is shared across products and versions. It lives on local disk deliberately, not under `--dest`, which is often a network share.

Measured on WoW's 1,347 archives (4.7M objects): a full cold scan is 8.7s and 1.21 GB peak RSS; warm from cache 2.9s; restricted retention drops peak to 63 MB (19.5x), and a real lookup on Hearthstone reads 16 of 89 indices instead of all 89. The scan is a real cost but was never "most of" a cold run — downloading the payload dominates.

`set_wanted` keeps a completed unrestricted index (it already holds everything) but discards a restricted one, which was built for a different question. If a lookup misses while `_full_scan_done` is false, `_locate` rebuilds unrestricted rather than reporting the object absent — without that, an early-exited scan would produce false "not archived" results.

### Destination probing

`grab` asks "does this exist?" once per manifest entry, and the collision check needs the directory listing. Done naively that is a listing plus a stat per entry, per file — O(dir size) syscalls per file against a destination that is frequently a slow network share (`/Volumes/public`). `DirIndex` lists each directory once via `os.scandir` and answers from memory; `find_existing_file_by_path`, `is_file_already_downloaded`, and `make_unique_filename` all accept one. Files created during the run are registered with `add`/`discard` so the cache stays truthful — a rename must do both.

### Streaming and memory

Downloads never materialize a file in memory. `CdnPool.stream_to` writes the response body straight into a sink (rewinding and truncating before a failover retry, so a partial body cannot contaminate the next host), and `blte.decode_blte_stream` walks the chunk table decoding one chunk at a time into an output stream. `download_by_ekey_to_path` chains them via a `.blte.part` staging file.

The dominant cost was never the file bodies, though — it was resolving CKey→EKey. `parse_encoding_manifest` builds one `construct` Container per entry, and WoW has ~2.87M of them. `build_ckey_lookup` instead takes the set of CKeys actually being fetched and walks `iter_ce_entries` once, stopping as soon as that set is satisfied.

Measured on `grab --product wow -p '^Wow\.exe$'` (two files, 175 MB total):

| | peak RSS | wall |
|---|---|---|
| buffered + full encoding parse | 2.45 GB | 44.7s |
| streamed + bounded lookup | 106 MB | 12.1s |

Peak RSS is now 1.12x the largest single file, down from 26.4x. Keep it that way: do not reintroduce a full `parse_encoding_manifest` on the grab path, and do not `.read()` a response body.

### Archived vs loose objects

Only a minority of CDN objects exist as loose `data/xx/yy/{ekey}` files. The rest are packed into archives listed in the **CDN config** (`archives = ...`, reached via `version.cdn_config` — distinct from the *build* config). Each archive has a `{archive}.index` mapping EKey → (offset, size); fetching such a file is a ranged GET into the archive.

`download_by_ekey` tries the loose path first and falls back to `ArchiveResolver` on 403/404. Both statuses matter: `level3.blizzard.com` answers 403 for absent objects while `*.cdn.blizzard.com` answers 404.

`ArchiveResolver` is lazy — it costs one GET per archive index (89 for Hearthstone, ~76k objects) and is only built when a loose fetch actually misses, then cached. It is created in `install_manifest_command` and threaded through `download_command` so one product's indices are fetched at most once.

Index layout is documented in `archives.py`; note `num_elements` is little-endian in the footer while each entry's size/offset are big-endian, and entries are zero-padded to `block_size_kb * 1024` boundaries.

### .pdb companions

With `--pdb-companions` (default on), a matched `.pdb` also pulls the binary sharing its name. Matching is on **basename stem across the whole manifest**, not directory — Hearthstone ships `Hearthstone_Data/Plugins/x86_64/NgWebview.pdb` while `NgWebview.dll` sits at the manifest root, so a same-directory lookup finds nothing. A fallback strips a trailing build stamp (`Wow_11.1.0.60257.pdb` → `Wow.exe`); an exact stem match always wins.

This is a filename heuristic. The authoritative pairing is the CodeView GUID/age in the binary's debug directory, which manifest names cannot express — but the heuristic was verified correct for the one live case (`NgWebview.dll`'s RSDS record names `NgWebview.pdb`, GUID `4F856EB3-2914-4D22-94A7-57A7A347C898`, age 1, matching the fetched PDB).

Note pdbs are rare: of 1017 files in the sample dest tree only 2 were pdbs, and the live Wow/Overwatch/Warcraft3/WowClassic manifests currently ship none.

### Manifest tags and name variants

An install manifest lists the same path more than once when a build ships per-region or per-architecture variants. Wow has 81 such duplicated names; `Wow.exe` appears as a **CN** build (99.7 MB) and an **EU/KR/TW/US** build (74.8 MB) — a 33.3% size difference, not a duplicate.

Each tag carries a bitmask with one bit per entry, MSB first: entry *i* is covered when `mask[i // 8] >> (7 - i % 8) & 1`. Tag types: 1 platform, 2 architecture, 3 locale, 4 region.

`build_variant_names` resolves collisions in three steps, falling back as each fails:
1. Unique name → used as-is.
2. Tags distinguish the variants → `Wow-CN_Windows_x86_64.exe`. For a file inside a bundle (`.app`, `.framework`, `.bundle`, `.xpc`, `.plugin`, `.kext`) the label goes on the **outermost bundle directory**, not the leaf: `World of Warcraft-CN_OSX.app/Contents/MacOS/World of Warcraft`. Labelling the leaf would break `CFBundleExecutable`, mangle `PkgInfo` / `_CodeSignature/CodeResources`, and interleave both builds in one directory. The *strictly* broadest variant by region count keeps the plain name; when nothing is strictly broadest (e.g. `Utils/icudtl.dat`, both all-region but differing by arch) every variant gets a label. A tag category is omitted when the entry carries all of its values, so a universal binary does not list all three architectures.
3. Tags absent or non-distinguishing → CKey suffix via `make_unique_filename`, the original behavior.

A file can belong to one variant without its name being repeated — Wow ships a single `libenvsdk.dylib` tagged CN+OSX. The duplicate-name pass never sees it, so `_route_bundle_exclusives` makes a second pass moving such files into the matching labelled bundle. Without it a CN-only file is filed as global.

47 of Wow's 52 paired bundle files share a CKey (identical bytes under two region tags); only 5 genuinely differ. Since the CKey map is content-addressed and holds one path per CKey, the second path would be skipped and the variant bundle left incomplete — `link_duplicate` hard-links (falling back to copy) instead.

**`models.py` mask width**: the mask is `(num_entries + 7) // 8` bytes. Using `num_entries // 8` truncates whenever the entry count is not a multiple of 8 — Wow's 266 entries need 34 bytes, not 33 — which shifts every tag after the first and corrupts its name (`?OSX`, `ÿenUS`). Tag names parsing with leading junk means this regressed.

`find_existing_file_by_path` matches a CKey-suffixed sibling by exact shape, not `startswith`. Tag-derived names share a prefix with the plain name by design, so a prefix test reports `Wow-CN_Windows_x86_64.exe` as an existing copy of `Wow.exe`.

### Collision handling

Same filename, different CKey → `make_unique_filename` inserts the first 8 chars of the CKey before the extension (`Wow.a1b2c3d4.exe`), falling back to the full 32-char CKey if that also collides. `update_ckey_map` drops any stale entry pointing at the same relative path so an overwritten file doesn't leave two CKeys mapped to it.

### index

`index` walks a directory, computes plain file MD5, and writes it as the CKey. This is correct because a CKey *is* the MD5 of decoded content — but it means indexing still-BLTE-encoded or otherwise unprocessed files produces entries that will never match a manifest. Paths must be `{base}/{product}/{version}/...` or the file is skipped.

## Known rough edges

- `blte.py` line 24 references `Bytes` without importing it, so the `header_size == 0` (headerless BLTE) branch raises `NameError`. That path appears to be unexercised.
- Errors in `grab` are caught per-file and per-product and printed; the command exits 0 regardless. Check output, not exit code.
- `USE_HTTP2 = False`. Blizzard's edges reset or truncate large HTTP/2 bodies: a 268 MB object reset immediately and repeatably with `StreamReset(INTERNAL_ERROR)`, and a 6 MiB range returned short as `InvalidBodyLengthError`, while HTTP/1.1 streamed the same bytes reliably. Do not re-enable HTTP/2 for data transfers without re-testing against a >100 MB object.
- `fetch()` (still used for the small patch-server tables) buffers and has no failover; only the `CdnPool` paths retry.
- The `/cdns` table's `ConfigPath` column (e.g. `tpr/configs/data`) is parsed into `CdnDefinition.config_path` and never used — it addresses *product* configs, which blizztools does not fetch. Build and CDN configs live under `{path}/config/...`.
- `grabpdb.sh` writes to `../wow` (outside the repo). The repo also has a `wow` symlink to `/Volumes/public/wow/binaries`, so a `-d ./wow` run targets an external volume.
- `_gitless/ribbit/` holds the Ribbit product/version/cdn dump that `ALL_PRODUCT_CODES` was derived from; it is not part of the package.
- There is no `.gitignore`, no linter, and no formatter configured.
