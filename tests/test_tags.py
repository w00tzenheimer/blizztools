import pytest

from blizztools.tags import (
    TAG_TYPE_ARCHITECTURE,
    TAG_TYPE_PLATFORM,
    TAG_TYPE_REGION,
    _label_name,
    build_variant_names,
    decode_tags,
    mask_covers,
    tag_label,
)


class _Tag:
    def __init__(self, name, tag_type, covered, num_entries):
        self.name = name
        self.tag_type = tag_type
        mask = bytearray((num_entries + 7) // 8)
        for i in covered:
            mask[i // 8] |= 1 << (7 - i % 8)
        self.mask = bytes(mask)


class _Entry:
    def __init__(self, name):
        self.name = name


def test_mask_covers_msb_first():
    tag = _Tag("t", 1, {0, 7, 8}, 16)
    assert tag.mask[0] == 0b10000001
    assert mask_covers(tag.mask, 0)
    assert mask_covers(tag.mask, 7)
    assert mask_covers(tag.mask, 8)
    assert not mask_covers(tag.mask, 1)


def test_mask_covers_out_of_range_is_false():
    assert not mask_covers(b"\xff", 99)


def test_decode_tags_groups_by_type():
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0}, 8),
        _Tag("US", TAG_TYPE_REGION, {1}, 8),
        _Tag("Windows", TAG_TYPE_PLATFORM, {0, 1}, 8),
    ]
    decoded = decode_tags(tags, 8)
    assert decoded[TAG_TYPE_REGION]["CN"] == {0}
    assert decoded[TAG_TYPE_PLATFORM]["Windows"] == {0, 1}


def test_tag_label_matches_requested_format():
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0}, 8),
        _Tag("US", TAG_TYPE_REGION, {1}, 8),
        _Tag("Windows", TAG_TYPE_PLATFORM, {0, 1}, 8),
        _Tag("OSX", TAG_TYPE_PLATFORM, set(), 8),
        _Tag("x86_64", TAG_TYPE_ARCHITECTURE, {0, 1}, 8),
        _Tag("arm64", TAG_TYPE_ARCHITECTURE, set(), 8),
    ]
    assert tag_label(decode_tags(tags, 8), 0) == "CN_Windows_x86_64"


def test_tag_label_omits_category_covering_everything():
    # A universal binary carrying every architecture learns nothing from them.
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0}, 8),
        _Tag("US", TAG_TYPE_REGION, {1}, 8),
        _Tag("OSX", TAG_TYPE_PLATFORM, {0, 1}, 8),
        _Tag("Windows", TAG_TYPE_PLATFORM, set(), 8),
        _Tag("arm64", TAG_TYPE_ARCHITECTURE, {0, 1}, 8),
        _Tag("x86_64", TAG_TYPE_ARCHITECTURE, {0, 1}, 8),
    ]
    # arch covers every value -> dropped; platform is 1 of 2 -> kept
    assert tag_label(decode_tags(tags, 8), 0) == "CN_OSX"


def test_build_variant_names_renames_only_narrow_variant():
    entries = [_Entry("Wow.exe"), _Entry("Wow.exe")]
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0}, 2),
        _Tag("EU", TAG_TYPE_REGION, {1}, 2),
        _Tag("US", TAG_TYPE_REGION, {1}, 2),
        _Tag("Windows", TAG_TYPE_PLATFORM, {0, 1}, 2),
        _Tag("OSX", TAG_TYPE_PLATFORM, set(), 2),
        _Tag("x86_64", TAG_TYPE_ARCHITECTURE, {0, 1}, 2),
        _Tag("arm64", TAG_TYPE_ARCHITECTURE, set(), 2),
    ]
    names = build_variant_names(entries, tags, 2)
    assert names[0] == "Wow-CN_Windows_x86_64.exe"
    assert names[1] == "Wow.exe"  # broadest variant keeps the plain name


def test_build_variant_names_preserves_directory():
    entries = [
        _Entry("World of Warcraft.app\\Contents\\MacOS\\World of Warcraft"),
        _Entry("World of Warcraft.app\\Contents\\MacOS\\World of Warcraft"),
    ]
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0}, 2),
        _Tag("EU", TAG_TYPE_REGION, {1}, 2),
        _Tag("KR", TAG_TYPE_REGION, {1}, 2),
        _Tag("TW", TAG_TYPE_REGION, {1}, 2),
        _Tag("US", TAG_TYPE_REGION, {1}, 2),
        _Tag("OSX", TAG_TYPE_PLATFORM, {0, 1}, 2),
        _Tag("Windows", TAG_TYPE_PLATFORM, set(), 2),
    ]
    names = build_variant_names(entries, tags, 2)
    # Inside a bundle the label lands on the .app directory, leaving the
    # executable name (CFBundleExecutable) untouched.
    assert names[0] == (
        "World of Warcraft-CN_OSX.app/Contents/MacOS/World of Warcraft"
    )
    # The untouched variant is returned verbatim, separators and all.
    assert names[1] == "World of Warcraft.app\\Contents\\MacOS\\World of Warcraft"


def test_build_variant_names_unique_names_untouched():
    entries = [_Entry("a.exe"), _Entry("b.exe")]
    names = build_variant_names(entries, [], 2)
    assert names == {0: "a.exe", 1: "b.exe"}


def test_build_variant_names_without_tags_falls_back_to_plain():
    # No tags at all -> caller's CKey-suffix path must still handle it.
    entries = [_Entry("Wow.exe"), _Entry("Wow.exe")]
    assert build_variant_names(entries, None, None) == {0: "Wow.exe", 1: "Wow.exe"}
    assert build_variant_names(entries, [], 2) == {0: "Wow.exe", 1: "Wow.exe"}


def test_build_variant_names_indistinguishable_tags_fall_back_to_plain():
    # Both variants carry identical tags -> no label distinguishes them.
    entries = [_Entry("Wow.exe"), _Entry("Wow.exe")]
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0, 1}, 2),
        _Tag("US", TAG_TYPE_REGION, {0, 1}, 2),
    ]
    assert build_variant_names(entries, tags, 2) == {0: "Wow.exe", 1: "Wow.exe"}


def test_build_variant_names_skips_unnamed_entries():
    entries = [_Entry(""), _Entry("a.exe")]
    names = build_variant_names(entries, [], 2)
    assert 0 not in names and names[1] == "a.exe"


def test_build_variant_names_labels_all_when_none_is_broadest():
    # CN vs US, one region each: neither owns the unqualified name.
    entries = [_Entry("Wow.exe"), _Entry("Wow.exe")]
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0}, 2),
        _Tag("US", TAG_TYPE_REGION, {1}, 2),
        _Tag("Windows", TAG_TYPE_PLATFORM, {0, 1}, 2),
        _Tag("OSX", TAG_TYPE_PLATFORM, set(), 2),
    ]
    names = build_variant_names(entries, tags, 2)
    assert names[0] == "Wow-CN_Windows.exe"
    assert names[1] == "Wow-US_Windows.exe"


def test_label_name_uses_outermost_bundle():
    got = _label_name(
        "World of Warcraft.app/Contents/Helpers/WowVoiceProxy.app/Contents/MacOS/WowVoiceProxy",
        "CN_OSX",
    )
    # Outermost bundle only; the nested helper bundle stays intact inside it.
    assert got == (
        "World of Warcraft-CN_OSX.app/Contents/Helpers/"
        "WowVoiceProxy.app/Contents/MacOS/WowVoiceProxy"
    )


def test_label_name_leaves_bundle_interior_names_alone():
    for interior in ("Contents/PkgInfo", "Contents/_CodeSignature/CodeResources",
                     "Contents/Resources/wow.icns"):
        got = _label_name(f"World of Warcraft.app/{interior}", "CN_OSX")
        assert got == f"World of Warcraft-CN_OSX.app/{interior}"


def test_label_name_handles_backslashes():
    got = _label_name(r"World of Warcraft.app\Contents\PkgInfo", "CN_OSX")
    assert got == "World of Warcraft-CN_OSX.app/Contents/PkgInfo"


def test_label_name_plain_file_labels_the_filename():
    assert _label_name("Wow.exe", "CN_Windows_x86_64") == "Wow-CN_Windows_x86_64.exe"
    assert _label_name("Utils/icudtl.dat", "Windows_arm64") == "Utils/icudtl-Windows_arm64.dat"


def test_label_name_extensionless_plain_file():
    assert _label_name("dir/World of Warcraft", "CN") == "dir/World of Warcraft-CN"


def test_label_name_other_bundle_kinds():
    assert _label_name("Foo.framework/Versions/A/Foo", "CN") == (
        "Foo-CN.framework/Versions/A/Foo"
    )


def test_label_name_ignores_bundle_suffix_on_the_leaf_itself():
    # A file literally named '*.app' is not a directory; label the filename.
    assert _label_name("dir/thing.app", "CN") == "dir/thing-CN.app"


def test_build_variant_names_routes_bundle_exclusive_file():
    # A CN-only file whose name is NOT duplicated must still land in the CN
    # bundle, not the plain (global) one. Wow ships exactly this:
    # World of Warcraft.app/Contents/MacOS/libenvsdk.dylib, tagged CN+OSX.
    entries = [
        _Entry("World of Warcraft.app/Contents/MacOS/World of Warcraft"),
        _Entry("World of Warcraft.app/Contents/MacOS/World of Warcraft"),
        _Entry("World of Warcraft.app/Contents/MacOS/libenvsdk.dylib"),
    ]
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0, 2}, 3),
        _Tag("EU", TAG_TYPE_REGION, {1}, 3),
        _Tag("KR", TAG_TYPE_REGION, {1}, 3),
        _Tag("TW", TAG_TYPE_REGION, {1}, 3),
        _Tag("US", TAG_TYPE_REGION, {1}, 3),
        _Tag("OSX", TAG_TYPE_PLATFORM, {0, 1, 2}, 3),
        _Tag("Windows", TAG_TYPE_PLATFORM, set(), 3),
    ]
    names = build_variant_names(entries, tags, 3)
    assert names[0] == "World of Warcraft-CN_OSX.app/Contents/MacOS/World of Warcraft"
    assert names[1] == "World of Warcraft.app/Contents/MacOS/World of Warcraft"
    assert names[2] == "World of Warcraft-CN_OSX.app/Contents/MacOS/libenvsdk.dylib"


def test_build_variant_names_shared_bundle_file_stays_put():
    # An all-region file inside a variant bundle is not exclusive to CN and
    # must not be routed into the CN bundle.
    entries = [
        _Entry("App.app/Contents/MacOS/App"),
        _Entry("App.app/Contents/MacOS/App"),
        _Entry("App.app/Contents/Resources/shared.dat"),
    ]
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0, 2}, 3),
        _Tag("US", TAG_TYPE_REGION, {1, 2}, 3),
        _Tag("EU", TAG_TYPE_REGION, {1, 2}, 3),
        _Tag("OSX", TAG_TYPE_PLATFORM, {0, 1, 2}, 3),
        _Tag("Windows", TAG_TYPE_PLATFORM, set(), 3),
    ]
    names = build_variant_names(entries, tags, 3)
    assert names[2] == "App.app/Contents/Resources/shared.dat"


def test_build_variant_names_exclusive_routing_needs_a_bundle():
    # Outside a bundle there is no variant directory to route into, so a
    # non-duplicated file keeps its name.
    entries = [_Entry("a.exe"), _Entry("a.exe"), _Entry("cn-only.dll")]
    tags = [
        _Tag("CN", TAG_TYPE_REGION, {0, 2}, 3),
        _Tag("US", TAG_TYPE_REGION, {1}, 3),
        _Tag("Windows", TAG_TYPE_PLATFORM, {0, 1, 2}, 3),
        _Tag("OSX", TAG_TYPE_PLATFORM, set(), 3),
    ]
    names = build_variant_names(entries, tags, 3)
    assert names[2] == "cn-only.dll"
