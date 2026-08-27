#!/usr/bin/env python3
"""
Merge product directories into canonical kebab-case names.

Two-phase safety pattern:
  1. --execute: Merge files and move originals to _to_delete_/
  2. --cleanup: Delete _to_delete_/ after manual verification

Handles:
- CamelCase (WowClassic) -> kebab-case (wow-classic)
- snake_case (wow_classic) -> kebab-case (wow-classic)
- CDN codes (fenris, hsb, zeus) -> friendly names (diablo4, hearthstone, ...)
"""

import re
import shutil
from collections import defaultdict
from pathlib import Path

TO_DELETE_DIR = "_to_delete_"

# Map CDN codes to canonical kebab-case names
CDN_CODE_MAP = {
    "fenris": "diablo4",
    "fenrisb": "diablo4-beta",
    "hsb": "hearthstone",
    "hsc": "hearthstone-tournament",
    "pro": "overwatch",
    "prot": "overwatch-test",
    "d3": "diablo3",
    "d3t": "diablo3-ptr",
    "w3": "warcraft3",
    "zeus": "call-of-duty-black-ops-cold-war",
    "codbocw": "call-of-duty-black-ops-cold-war",
}


def to_kebab_case(name: str) -> str:
    """Convert a directory name to canonical kebab-case."""
    if name.lower() in CDN_CODE_MAP:
        return CDN_CODE_MAP[name.lower()]

    name = name.replace("_", "-")
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    name = name.lower()
    name = re.sub(r"-+", "-", name)
    name = name.strip("-")

    return name


def has_real_content(path: Path) -> bool:
    """Check if directory has non-hidden files."""
    for item in path.iterdir():
        if not item.name.startswith("."):
            return True
    return False


def is_version_directory(name: str) -> bool:
    """Check if directory name looks like a version (e.g., 11.2.5.64270)."""
    return bool(re.match(r"^\d+\.\d+", name))


def same_path_case_insensitive(path1: Path, path2: Path) -> bool:
    """Check if two paths refer to the same location (case-insensitive filesystem)."""
    try:
        return path1.resolve() == path2.resolve()
    except OSError:
        return path1.as_posix().lower() == path2.as_posix().lower()


def get_directory_groups(root: Path) -> dict[str, list[Path]]:
    """Group directories by their canonical kebab-case name."""
    groups = defaultdict(list)

    for path in sorted(root.iterdir()):
        if path.is_dir() and not path.name.startswith(".") and path.name != TO_DELETE_DIR:
            canonical = to_kebab_case(path.name)
            groups[canonical].append(path)

    return dict(groups)


def move_to_trash(root: Path, source: Path, dry_run: bool) -> None:
    """Move a path to the _to_delete_ directory instead of deleting."""
    trash_dir = root / TO_DELETE_DIR

    if dry_run:
        return

    trash_dir.mkdir(exist_ok=True)

    # Create unique destination path in trash
    dest = trash_dir / source.name
    counter = 1
    while dest.exists():
        dest = trash_dir / f"{source.name}_{counter}"
        counter += 1

    shutil.move(str(source), str(dest))


def merge_directories(root: Path, dry_run: bool = True):
    """Merge variant directories into canonical kebab-case directories."""
    groups = get_directory_groups(root)
    trash_dir = root / TO_DELETE_DIR

    print("=== Directory Groups ===\n")
    for canonical, dirs in sorted(groups.items()):
        if len(dirs) > 1 or dirs[0].name != canonical:
            print(f"{canonical}:")
            for d in dirs:
                print(f"  - {d.name}")
    print()

    if dry_run:
        print("=== Dry Run (no changes made) ===\n")

    for canonical, source_dirs in sorted(groups.items()):
        # Skip if only one dir and it's already canonical
        if len(source_dirs) == 1 and source_dirs[0].name == canonical:
            continue

        canonical_path = root / canonical

        print(f"Processing: {canonical}")

        # Create canonical directory if it doesn't exist
        if not canonical_path.exists():
            print(f"  Creating: {canonical}/")
            if not dry_run:
                canonical_path.mkdir()

        # Merge each source directory
        for source_dir in source_dirs:
            if source_dir == canonical_path:
                print(f"  Skipping (is canonical): {source_dir.name}/")
                continue

            # Check if this is actually the same directory (case-insensitive filesystem)
            if same_path_case_insensitive(source_dir, canonical_path):
                if source_dir.name != canonical:
                    print(f"  Renaming: {source_dir.name}/ -> {canonical}/ (same dir, different case)")
                    if not dry_run:
                        # Use a temp name to handle case-only renames
                        temp_path = root / f".{canonical}_temp_{source_dir.name}"
                        source_dir.rename(temp_path)
                        temp_path.rename(canonical_path)
                else:
                    print(f"  Skipping (already canonical): {source_dir.name}/")
                continue

            print(f"  Merging: {source_dir.name}/ -> {canonical}/")

            # Iterate version directories inside each source
            for version_dir in sorted(source_dir.iterdir()):
                if not version_dir.is_dir():
                    continue

                # Skip hidden directories
                if version_dir.name.startswith("."):
                    continue

                # Skip directories that don't look like versions and have no real content
                if not is_version_directory(version_dir.name):
                    if not has_real_content(version_dir):
                        print(f"    {version_dir.name}/ (moving to trash - empty/no content)")
                        move_to_trash(root, version_dir, dry_run)
                        continue
                    else:
                        print(f"    {version_dir.name}/ (warning - not a version dir)")

                dest_version = canonical_path / version_dir.name

                if dest_version.exists():
                    # Merge files into existing version directory
                    print(f"    {version_dir.name}/ (merging into existing)")
                    if not dry_run:
                        for item in version_dir.rglob("*"):
                            if item.is_file() and not item.name.startswith("."):
                                rel = item.relative_to(version_dir)
                                dest_file = dest_version / rel
                                if not dest_file.exists():
                                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(item, dest_file)
                        # Move source to trash after merging
                        move_to_trash(root, version_dir, dry_run)
                else:
                    # Move entire version directory
                    print(f"    {version_dir.name}/ (moving)")
                    if not dry_run:
                        shutil.move(str(version_dir), str(dest_version))

            # Move source directory to trash (may have .DS_Store or other hidden files left)
            if not dry_run:
                if source_dir.exists():
                    move_to_trash(root, source_dir, dry_run)
                    print(f"  Moved to trash: {source_dir.name}/")

        print()

    if dry_run:
        print("Run with --execute to apply changes.")
    else:
        if trash_dir.exists():
            print(f"\n=== Originals moved to {TO_DELETE_DIR}/ ===")
            print("Verify the merge is correct, then run with --cleanup to delete.")


def cleanup_trash(root: Path, dry_run: bool = True):
    """Delete the _to_delete_ directory after verification."""
    trash_dir = root / TO_DELETE_DIR

    if not trash_dir.exists():
        print(f"No {TO_DELETE_DIR}/ directory found. Nothing to clean up.")
        return

    # Show what will be deleted
    print(f"=== Contents of {TO_DELETE_DIR}/ ===\n")
    for item in sorted(trash_dir.iterdir()):
        if item.is_dir():
            count = sum(1 for _ in item.rglob("*") if _.is_file())
            print(f"  {item.name}/ ({count} files)")
        else:
            print(f"  {item.name}")
    print()

    if dry_run:
        print(f"Run with --cleanup --execute to permanently delete {TO_DELETE_DIR}/")
    else:
        shutil.rmtree(trash_dir)
        print(f"Deleted {TO_DELETE_DIR}/")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Merge product directories to kebab-case (two-phase safety)",
        epilog="""
Examples:
  %(prog)s ./wow                    # Dry run - show what would happen
  %(prog)s ./wow --execute          # Merge and move originals to _to_delete_/
  %(prog)s ./wow --cleanup          # Show what's in _to_delete_/
  %(prog)s ./wow --cleanup --execute  # Actually delete _to_delete_/
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("directory", type=Path, help="Root directory containing product folders")
    parser.add_argument("--execute", action="store_true", help="Actually perform the operation (default is dry-run)")
    parser.add_argument("--cleanup", action="store_true", help="Clean up _to_delete_/ directory")
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"Error: {args.directory} is not a directory")
        exit(1)

    if args.cleanup:
        cleanup_trash(args.directory, dry_run=not args.execute)
    else:
        merge_directories(args.directory, dry_run=not args.execute)
