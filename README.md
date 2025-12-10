# blizztools

blizztools - interact with Blizzard CDN through CLI.

Useful for CI/CD workflows dependent on client binaries.

## Requirements

- Python 3.8 or higher
- Dependencies: `construct`, `click`, `httpx[http2]`, `rich`

## Installation

```bash
pip install -e .
```

For development with tests:

```bash
pip install -e ".[test]"
```

## Running Tests

```bash
# Run all tests
pytest blizztools/tests/

# Run with verbose output
pytest blizztools/tests/ -v

# Run specific test file
pytest blizztools/tests/test_main.py

```

## Supported Products

All Blizzard products are supported:

- `diablo3`, `diablo3-ptr`
- `diablo4`, `diablo4-beta`
- `hearthstone`, `hearthstone-tournament`
- `overwatch`, `overwatch-test`
- `warcraft3`
- `wow`, `wow-beta`, `wow-classic`, `wow-classic-beta`, `wow-classic-ptr`
- `wow-classic-era`, `wow-classic-era-beta`, `wow-classic-era-ptr`
- `wow-demo`, `wow-dev`, `wow-dev2`, `wow-dev3`
- `wow-e1`, `wow-e3`, `wow-live-test`, `wow-live-test2`
- `wow-t`, `wow-v`, `wow-v2`, `wow-v3`, `wow-v4`, `wow-x-ptr`, `wow-z`
- `call-of-duty-black-ops-cold-war`

## Keywords

TACT, CDN, BLTE, Install Manifest, Download Manifest, CE Table, EncodingKey, ContentKey

## Commands

```console
$ blizztools --help
Blizztools in Python

Usage: blizztools [OPTIONS] COMMAND [ARGS]...

Commands:
  cdn              Get available CDNs for a product.
  download         Download a file by its content key.
  grab             Grab PDBs / loader DLLs from Blizzard CDNs.
  index            Index files in a directory and create/update...
  install-manifest Download and parse the install manifest.
  version          Get available versions for a product.
```

### version

Get available versions for a product.

```bash
blizztools version Wow
blizztools version Wow --version-file versions.txt
```

### cdn

Get available CDNs for a product.

```bash
blizztools cdn Wow
```

### install-manifest

Download and parse the install manifest for a product.

```bash
blizztools install-manifest Wow
blizztools install-manifest Wow --version-file versions.txt --config-file config.txt
```

### download

Download a file by its content key.

```bash
blizztools download Wow 3bdf94e861f99559347cc9c576f0e236 --output ./target
blizztools download Wow 3bdf94e861f99559347cc9c576f0e236 --output ./target --version-file versions.txt
```

### grab

Grab files matching patterns from Blizzard CDNs. This is the main command for bulk downloading files like PDBs and loader DLLs.

```bash
# Download all .pdb and _loader.dll files (default patterns)
blizztools grab

# Download with custom patterns
blizztools grab -p '\.pdb$' -p '_loader\.dll$' -d ./target

# Download for specific product
blizztools grab --product wow-beta

# Download from product list file
blizztools grab -f product_list.txt

# Overwrite existing files
blizztools grab --overwrite
```

I usually run it like this: `python -m blizztools.main grab -p '^(?!WowVoiceProxy(?:-China|T|\.exe$))Wow.*\.exe$' -p 'World.of.Warcraft$' -d ./wow`

**Options:**

- `-p, --pattern`: Regex pattern to match (can be specified multiple times). Defaults to `\.pdb$` and `_loader\.dll$`
- `-d, --dest`: Download directory (default: `./target`)
- `-f, --file`: Text file with one product name per line (overrides built-in list)
- `--product`: Single product name to process (overrides built-in list and file)
- `--overwrite`: Overwrite existing files even if they have the same hash (default: preserve existing files)

**Features:**

- Automatically skips files that are already downloaded (checks CKey map)
- Detects existing files even if not in map
- Handles filename collisions by appending CKey
- Creates proper directory structure for nested paths
- Saves progress to `.ckey_map.json` for resumable downloads

### index

Index files in a directory and create/update `.ckey_map.json`. Useful for indexing existing downloaded files.

```bash
# Index files in directory (map saved to same directory)
blizztools index ./target

# Index files but save map to different location
blizztools index ./target --dest ./target

# Index with custom base directory for relative paths
blizztools index ./files --dest ./target --base-dir ./target
```

**Options:**

- `directory`: Directory to index (required)
- `--dest`: Directory where `.ckey_map.json` will be saved (should match `grab --dest`). Defaults to directory if not specified
- `--base-dir`: Base directory for relative paths in map (defaults to dest-dir if not specified)

**Note:** The `.ckey_map.json` file should be in the same directory that `grab --dest` uses, so that `grab` can find and use it to skip already-downloaded files.

## Quick Start

### Download Individual Files

#### Step 1: Check the version

```bash
blizztools version wow-classic
```

#### Step 2: Check the install manifest

```bash
blizztools install-manifest wow-classic
```

This will list all files with their CKeys (content keys/hashes).

#### Step 3: Download specific files

```bash
blizztools download wow-classic 3bdf94e861f99559347cc9c576f0e236 --output ./target
```

Files are downloaded to `{output}/{product}/{version}/{ckey}`.

### Bulk Download with `grab`

The `grab` command is the easiest way to download multiple files matching patterns:

```bash
# Download all .pdb and _loader.dll files for all products
blizztools grab

# Download for specific product
blizztools grab --product wow-beta

# Custom patterns and destination
blizztools grab -p '\.pdb$' -p '\.exe$' -d ./my_downloads
```

Files are organized as `{dest}/{product}/{version}/{filename}` with proper directory structure for nested paths.

### Index Existing Files

If you already have downloaded files and want to create a CKey map:

```bash
# Index files in directory
blizztools index ./target

# Make sure map is saved where grab will find it
blizztools index ./target --dest ./target
```

This creates `.ckey_map.json` that `grab` uses to skip already-downloaded files.

## CKey Map (.ckey_map.json)

The `.ckey_map.json` file tracks downloaded files to avoid re-downloading:

- **Location**: Stored in the download directory (same as `grab --dest`)
- **Purpose**: Maps CKey (MD5 hash) to file location, product, and version
- **Auto-update**: Updated automatically by `grab` and `index` commands
- **Collision handling**: Automatically removes old entries when files are overwritten

### Example map entry

```json
{
  "3bdf94e861f99559347cc9c576f0e236": {
    "filename": "wow/11.2.5.64270/Wow.exe",
    "product": "wow",
    "version": "11.2.5.64270"
  }
}
```

## File Organization

Files are organized in the following structure:

```text
./target/
├── wow/
│   ├── 11.2.5.64270/
│   │   ├── Wow.exe
│   │   ├── wow_loader.dll
│   │   └── World of Warcraft.app/
│   │       └── Contents/
│   │           └── MacOS/
│   │               └── World of Warcraft
│   └── 11.2.5.63906/
│       └── ...
├── wow-beta/
│   └── 12.0.0.63967/
│       └── ...
└── .ckey_map.json
```

## Features

- **Resumable downloads**: CKey map prevents re-downloading existing files
- **Collision handling**: Files with same name but different hash get unique names
- **Path normalization**: Handles both Windows (`\`) and Unix (`/`) path separators
- **Overwrite protection**: By default, existing files are preserved (use `--overwrite` to override)
- **Progress tracking**: Shows download progress and skips already-downloaded files
