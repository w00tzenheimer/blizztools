"""
Install-manifest tag decoding, and tag-derived filenames for name collisions.

An install manifest can list the same path more than once. Those are not
duplicates: they are different builds selected by tag. Wow ships two
`Wow.exe` entries, one tagged CN and one tagged EU/KR/TW/US, differing by
~25 MB. Naming them apart by CKey preserves the bytes but discards the only
fact that explains them, so this module names them by tag instead:

    Wow.exe          (EU, KR, TW, US - the broadest variant keeps the plain name)
    Wow-CN_Windows_x86_64.exe

Each tag carries a bitmask with one bit per manifest entry, most significant
bit first, so entry *i* is covered when `mask[i // 8] >> (7 - i % 8) & 1`.
"""

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

TAG_TYPE_PLATFORM = 1
TAG_TYPE_ARCHITECTURE = 2
TAG_TYPE_LOCALE = 3
TAG_TYPE_REGION = 4

# Categories used to build a disambiguating suffix, in the order they appear
# in the filename. Locale is deliberately excluded: every variant carries all
# locales, so it never distinguishes anything.
NAME_TAG_TYPES: Tuple[int, ...] = (
    TAG_TYPE_REGION,
    TAG_TYPE_PLATFORM,
    TAG_TYPE_ARCHITECTURE,
)


def mask_covers(mask: bytes, index: int) -> bool:
    """True when a tag's bitmask covers manifest entry `index`."""
    byte = index // 8
    if byte >= len(mask):
        return False
    return bool(mask[byte] >> (7 - index % 8) & 1)


def decode_tags(tags, num_entries: int) -> Dict[int, Dict[str, Set[int]]]:
    """
    Decode tag bitmasks into {tag_type: {tag_name: {entry indices}}}.
    """
    decoded: Dict[int, Dict[str, Set[int]]] = {}
    for tag in tags:
        by_name = decoded.setdefault(int(tag.tag_type), {})
        by_name[tag.name] = {
            i for i in range(num_entries) if mask_covers(tag.mask, i)
        }
    return decoded


def _split_name(name: str) -> Tuple[str, str, str]:
    """Split a manifest entry name into (directory, stem, extension)."""
    normalized = name.replace("\\", "/")
    directory, _, base = normalized.rpartition("/")
    dot = base.rfind(".")
    if dot <= 0:
        return directory, base, ""
    return directory, base[:dot], base[dot:]


# Directory suffixes that make a subtree a single addressable artifact. A file
# inside one of these cannot be renamed on its own: the bundle's Info.plist,
# code signature, and dylib install names all refer to the interior paths.
BUNDLE_SUFFIXES: Tuple[str, ...] = (
    ".app",
    ".framework",
    ".bundle",
    ".xpc",
    ".plugin",
    ".kext",
)


def _label_name(name: str, label: str) -> str:
    """
    Apply `label` to a manifest entry name.

    For a file inside a bundle the label goes on the OUTERMOST bundle
    directory, so the whole variant lands as an intact parallel bundle:

        World of Warcraft.app/Contents/MacOS/World of Warcraft
        -> World of Warcraft-CN_OSX.app/Contents/MacOS/World of Warcraft

    Labelling the leaf instead would produce `MacOS/World of Warcraft-CN_OSX`,
    breaking CFBundleExecutable, and would interleave both builds' files in
    one directory. Everything else is labelled on the filename.
    """
    parts = name.replace("\\", "/").split("/")
    for i, part in enumerate(parts[:-1]):
        lowered = part.lower()
        for suffix in BUNDLE_SUFFIXES:
            if lowered.endswith(suffix):
                stem = part[: -len(suffix)]
                parts[i] = f"{stem}-{label}{part[len(stem):]}"
                return "/".join(parts)

    directory, stem, ext = _split_name(name)
    prefix = f"{directory}/" if directory else ""
    return f"{prefix}{stem}-{label}{ext}"


def tag_label(decoded: Dict[int, Dict[str, Set[int]]], index: int) -> str:
    """
    Build a label like 'CN_Windows_x86_64' for one entry.

    A category is skipped when it does not apply at all, or when the entry
    carries *every* tag in that category -- a universal binary tagged with all
    three architectures learns nothing from listing them.
    """
    parts: List[str] = []
    for tag_type in NAME_TAG_TYPES:
        by_name = decoded.get(tag_type)
        if not by_name:
            continue
        applied = sorted(name for name, covered in by_name.items() if index in covered)
        if not applied or len(applied) == len(by_name):
            continue
        parts.append("+".join(applied))
    return "_".join(parts)


def build_variant_names(
    entries: Sequence, tags: Optional[Iterable] = None, num_entries: Optional[int] = None
) -> Dict[int, str]:
    """
    Map entry index -> filename, disambiguating repeated names by tag.

    Returns an entry for every named index. Names that occur once map to
    themselves. For a repeated name the variant covering the most regions
    keeps the plain name (it is the general build) and the others gain a tag
    suffix. Indices whose tags cannot distinguish them keep the plain name, so
    the caller's CKey-suffix fallback still applies.
    """
    named = [(i, e.name) for i, e in enumerate(entries) if e.name]
    result = {i: name for i, name in named}

    duplicates: Dict[str, List[int]] = {}
    for i, name in named:
        duplicates.setdefault(name, []).append(i)
    duplicates = {n: idx for n, idx in duplicates.items() if len(idx) > 1}
    if not duplicates:
        return result

    if tags is None or num_entries is None:
        return result
    decoded = decode_tags(tags, num_entries)
    if not decoded:
        return result

    regions = decoded.get(TAG_TYPE_REGION, {})

    for name, indices in duplicates.items():
        labels = {i: tag_label(decoded, i) for i in indices}
        if not any(labels.values()):
            continue

        def region_count(i: int) -> int:
            return sum(1 for covered in regions.values() if i in covered)

        # The strictly-broadest variant is the general build and keeps the
        # plain name. If nothing is strictly broadest (say CN vs US, one
        # region each), no variant has a claim to the unqualified name and
        # every one of them gets a label.
        counts = sorted((region_count(i) for i in indices), reverse=True)
        primary = None
        if len(counts) > 1 and counts[0] > counts[1]:
            primary = max(indices, key=region_count)

        assigned = {name}
        for i in indices:
            if i == primary or not labels[i]:
                continue
            candidate = _label_name(name, labels[i])
            if candidate in assigned:
                continue  # fall through to the CKey suffix
            assigned.add(candidate)
            result[i] = candidate

    _route_bundle_exclusives(entries, result, decoded)
    return result


def _bundle_root(name: str) -> Optional[str]:
    """Outermost bundle-suffixed path component of `name`, if any."""
    parts = name.replace("\\", "/").split("/")
    for part in parts[:-1]:
        lowered = part.lower()
        if any(lowered.endswith(s) for s in BUNDLE_SUFFIXES):
            return part
    return None


def _route_bundle_exclusives(
    entries: Sequence,
    result: Dict[int, str],
    decoded: Dict[int, Dict[str, Set[int]]],
) -> None:
    """
    Move variant-exclusive files into the matching labelled bundle, in place.

    A file can belong to exactly one variant without its name being repeated:
    Wow ships a single `World of Warcraft.app/Contents/MacOS/libenvsdk.dylib`
    tagged CN+OSX. The duplicate-name pass never sees it, so it would stay in
    the plain bundle and be filed as the global build. Here any unlabelled
    bundle file whose own tags match a label already minted for that bundle
    gets routed to it.
    """
    variants: Dict[str, Set[str]] = {}
    for i in list(result):
        original = entries[i].name
        if not original or result[i] == original:
            continue
        root = _bundle_root(original)
        new_root = _bundle_root(result[i])
        if root is None or new_root is None or root == new_root:
            continue
        suffix = _suffix_of(root)
        stem = root[: -len(suffix)]
        label = new_root[len(stem) + 1 : -len(suffix)]
        if label:
            variants.setdefault(root, set()).add(label)

    if not variants:
        return

    for i, entry in enumerate(entries):
        if not entry.name or result.get(i) != entry.name:
            continue
        root = _bundle_root(entry.name)
        if root is None or root not in variants:
            continue
        label = tag_label(decoded, i)
        if label in variants[root]:
            result[i] = _label_name(entry.name, label)


def _suffix_of(component: str) -> str:
    lowered = component.lower()
    for suffix in BUNDLE_SUFFIXES:
        if lowered.endswith(suffix):
            return component[-len(suffix) :]
    return ""
