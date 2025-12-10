import asyncio
import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import click
import httpx
from rich.console import Console

from blizztools.blte import parse_blte
from blizztools.cdn import parse_build_config
from blizztools.encoding import parse_encoding_manifest
from blizztools.models import InstallManifest, Md5Hash
from blizztools.parsers import parse_cdn_table, parse_version_table

console = Console()

CKEY_MAP_FILENAME = ".ckey_map.json"


class Product(Enum):
    Diablo3 = "d3"
    Diablo3Ptr = "d3t"
    Diablo4 = "fenris"
    Diablo4Beta = "fenrisb"
    Hearthstone = "hsb"
    HearthstoneTournament = "hsc"
    Overwatch = "pro"
    OverwatchTest = "prot"
    Warcraft3 = "w3"
    Wow = "wow"
    WowAlpha = "wow_alpha"
    WowBeta = "wow_beta"
    WowClassic = "wow_classic"
    WowClassicBeta = "wow_classic_beta"
    WowClassicPtr = "wow_classic_ptr"
    WowClassicEra = "wow_classic_era"
    WowClassicEraBeta = "wow_classic_era_beta"
    WowClassicEraPtr = "wow_classic_era_ptr"
    WowDemo = "wowdemo"
    WowDev = "wowdev"
    WowDev2 = "wowdev2"
    WowDev3 = "wowdev3"
    WowE1 = "wowe1"
    WowE3 = "wowe3"
    WowLiveTest = "wowlivetest"
    WowLiveTest2 = "wowlivetest2"
    WowT = "wowt"
    WowV = "wowv"
    WowV2 = "wowv2"
    WowV3 = "wowv3"
    WowV4 = "wowv4"
    WowXPtr = "wowxptr"
    WowZ = "wowz"
    CallOfDutyBlackOpsColdWar = "zeus"


BASE_URL = "http://us.patch.battle.net:1119"

# Product name mapping from grab.py format to Product enum names
PRODUCT_NAME_MAP = {
    "diablo3": "Diablo3",
    "diablo3-ptr": "Diablo3Ptr",
    "diablo4": "Diablo4",
    "diablo4-beta": "Diablo4Beta",
    "hearthstone": "Hearthstone",
    "hearthstone-tournament": "HearthstoneTournament",
    "overwatch": "Overwatch",
    "overwatch-test": "OverwatchTest",
    "warcraft3": "Warcraft3",
    "wow": "Wow",
    "wow-beta": "WowBeta",
    "wow-classic": "WowClassic",
    "wow-classic-beta": "WowClassicBeta",
    "wow-classic-ptr": "WowClassicPtr",
    "wow-classic-era": "WowClassicEra",
    "wow-classic-era-beta": "WowClassicEraBeta",
    "wow-classic-era-ptr": "WowClassicEraPtr",
    "wow-demo": "WowDemo",
    "wow-dev": "WowDev",
    "wow-dev2": "WowDev2",
    "wow-dev3": "WowDev3",
    "wow-e1": "WowE1",
    "wow-e3": "WowE3",
    "wow-live-test": "WowLiveTest",
    "wow-live-test2": "WowLiveTest2",
    "wow-t": "WowT",
    "wow-v": "WowV",
    "wow-v2": "WowV2",
    "wow-v3": "WowV3",
    "wow-v4": "WowV4",
    "wow-x-ptr": "WowXPtr",
    "wow-z": "WowZ",
    "call-of-duty-black-ops-cold-war": "CallOfDutyBlackOpsColdWar",
}

DEFAULT_PRODUCTS: List[str] = list(PRODUCT_NAME_MAP.keys())


def product_name_to_enum(product_name: str) -> Optional[Product]:
    """Convert product name from grab.py format to Product enum."""
    enum_name = PRODUCT_NAME_MAP.get(product_name.lower())
    if enum_name:
        try:
            return Product[enum_name]
        except KeyError:
            return None
    return None


def should_download(filename: str, patterns: Iterable[re.Pattern]) -> bool:
    """Check if filename matches any of the patterns."""
    return any(p.search(filename) for p in patterns)


def make_unique_filename(base_path: Path, ckey: str) -> Path:
    """
    Make a filename unique by appending CKey if the file already exists.
    Inserts CKey before the extension if there is one, otherwise appends it.
    If the resulting filename also exists, appends the full CKey.
    """
    if not base_path.exists():
        return base_path

    # File exists, need to make it unique
    stem = base_path.stem
    suffix = base_path.suffix
    parent = base_path.parent

    # Use first 8 characters of CKey for uniqueness (shorter, cleaner)
    ckey_short = ckey[:8]

    if suffix:
        # Has extension: insert CKey before extension
        new_name = f"{stem}.{ckey_short}{suffix}"
    else:
        # No extension: append CKey
        new_name = f"{stem}.{ckey_short}"

    unique_path = parent / new_name

    # If the unique filename also exists (very unlikely), use full CKey
    if unique_path.exists():
        if suffix:
            new_name = f"{stem}.{ckey}{suffix}"
        else:
            new_name = f"{stem}.{ckey}"
        unique_path = parent / new_name

    return unique_path


def load_ckey_map(dest_dir: Path) -> Dict[str, Dict[str, str]]:
    """
    Load the CKey mapping from the destination directory.
    Returns a dictionary mapping CKey to file info (filename, product, version).
    """
    map_file = dest_dir / CKEY_MAP_FILENAME
    if not map_file.exists():
        return {}

    try:
        with open(map_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        # If file is corrupted, return empty dict
        return {}


def save_ckey_map(dest_dir: Path, ckey_map: Dict[str, Dict[str, str]]) -> None:
    """Save the CKey mapping to the destination directory."""
    map_file = dest_dir / CKEY_MAP_FILENAME
    try:
        with open(map_file, "w", encoding="utf-8") as f:
            json.dump(ckey_map, f, indent=2, sort_keys=True)
    except IOError as e:
        console.print(f"[yellow]⚠  Warning: Could not save CKey map: {e}[/yellow]")


def find_existing_file_by_path(
    dest_dir: Path, product: str, version: str, filename: str
) -> Optional[Path]:
    """
    Check if a file exists at the expected path based on product/version/filename.
    Returns the Path to the existing file if found, None otherwise.
    """
    # Normalize path separators
    normalized_name = filename.replace("\\", "/")
    path_parts = normalized_name.split("/")

    # Build expected path
    target_dir = dest_dir / product / version
    if len(path_parts) > 1:
        file_dir = target_dir
        for part in path_parts[:-1]:
            file_dir = file_dir / part
        expected_path = file_dir / path_parts[-1]
    else:
        expected_path = target_dir / path_parts[0]

    # Check if file exists (may have CKey suffix if collision occurred)
    if expected_path.exists() and expected_path.is_file():
        return expected_path

    # Also check for files with CKey suffix (collision resolution)
    # Look for files matching the base name with CKey suffix
    if len(path_parts) > 1:
        file_dir = target_dir
        for part in path_parts[:-1]:
            file_dir = file_dir / part
        base_stem = path_parts[-1]
    else:
        file_dir = target_dir
        base_stem = path_parts[0]

    # Check for files starting with the base name (handles collision cases)
    if file_dir.exists():
        for existing_file in file_dir.iterdir():
            if existing_file.is_file() and existing_file.stem.startswith(
                Path(base_stem).stem
            ):
                # Could be the same file with collision suffix
                return existing_file

    return None


def is_file_already_downloaded(
    dest_dir: Path, ckey: str, ckey_map: Dict[str, Dict[str, str]]
) -> Optional[Path]:
    """
    Check if a file with the given CKey has already been downloaded.
    Returns the Path to the existing file if found, None otherwise.
    """
    if ckey not in ckey_map:
        return None

    file_info = ckey_map[ckey]
    file_path = dest_dir / file_info["filename"]

    # Verify the file actually exists
    if file_path.exists() and file_path.is_file():
        return file_path

    # File doesn't exist, remove from map
    del ckey_map[ckey]
    return None


def get_ckey_for_file_path(
    dest_dir: Path, file_path: Path, ckey_map: Dict[str, Dict[str, str]]
) -> Optional[str]:
    """
    Check if a file path is already mapped to a CKey.
    Returns the CKey if found, None otherwise.
    """
    rel_path = file_path.relative_to(dest_dir)
    rel_path_str = str(rel_path)

    for existing_ckey, file_info in ckey_map.items():
        if file_info.get("filename") == rel_path_str:
            return existing_ckey

    return None


def update_ckey_map(
    dest_dir: Path,
    ckey: str,
    file_path: Path,
    product: str,
    version: str,
    ckey_map: Dict[str, Dict[str, str]],
) -> None:
    """Update the CKey mapping with a new file entry."""
    rel_path = file_path.relative_to(dest_dir)
    rel_path_str = str(rel_path)

    # Remove any old entries that point to the same file (different CKey)
    # This handles the case where a file is overwritten with new content
    keys_to_remove = []
    for existing_ckey, file_info in ckey_map.items():
        if existing_ckey != ckey and file_info.get("filename") == rel_path_str:
            keys_to_remove.append(existing_ckey)

    for key in keys_to_remove:
        del ckey_map[key]

    # Add/update entry with new CKey
    ckey_map[ckey] = {
        "filename": rel_path_str,
        "product": product,
        "version": version,
    }


async def fetch(url: str, client: httpx.AsyncClient, is_text=True):
    response = await client.get(url)
    response.raise_for_status()
    if is_text:
        return response.text
    return response.content


@click.group()
def main():
    """Blizztools in Python"""
    pass


@main.command()
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.option("--version-file", type=click.Path(exists=True))
@click.pass_context
def version(ctx, product, version_file):
    """Get available versions for a product."""
    asyncio.run(versions_command(product, version_file))


async def versions_command(product_name, version_file):
    product = Product[product_name]
    if version_file:
        with open(version_file, "r") as f:
            version_text = f.read()
    else:
        url = f"{BASE_URL}/{product.value}/versions"
        async with httpx.AsyncClient() as client:
            version_text = await fetch(url, client)

    version_table = parse_version_table(version_text)
    console.print(version_table)


@main.command()
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.pass_context
def cdn(ctx, product):
    """Get available CDNs for a product."""
    asyncio.run(cdn_command(product))


async def cdn_command(product_name):
    product = Product[product_name]
    url = f"{BASE_URL}/{product.value}/cdns"
    async with httpx.AsyncClient() as client:
        cdn_text = await fetch(url, client)

    cdn_table = parse_cdn_table(cdn_text)
    console.print(cdn_table)


async def download_by_ekey(
    selected_cdn: str, e_key: Md5Hash, client: httpx.AsyncClient
) -> bytes:
    e_key_str = str(e_key)
    file_url = (
        f"https://{selected_cdn}/data/{e_key_str[:2]}/{e_key_str[2:4]}/{e_key_str}"
    )
    blte_bytes = await fetch(file_url, client, is_text=False)
    return parse_blte(blte_bytes)


@main.command(name="install-manifest")
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.option("--version-file", type=click.Path(exists=True))
@click.option("--config-file", type=click.Path(exists=True))
@click.pass_context
def install_manifest_cmd(ctx, product, version_file, config_file):
    """Download and parse the install manifest."""
    asyncio.run(install_manifest_command(product, version_file, config_file))


async def install_manifest_command(
    product_name, version_file, config_file, return_data=False
):
    product = Product[product_name]
    url_base = f"{BASE_URL}/{product.value}"

    async with httpx.AsyncClient(http2=True) as client:
        cdn_text = await fetch(f"{url_base}/cdns", client)
        cdn_table = parse_cdn_table(cdn_text)
        if not return_data:
            print(cdn_table)

        # Initialize default CDN selection
        selected_cdn_def = cdn_table[0]
        selected_host = selected_cdn_def.hosts[0]
        cdn_path = selected_cdn_def.path
        selected_cdn_url = f"{selected_host}/{cdn_path}"

        if version_file:
            with open(version_file, "r") as f:
                version_text = f.read()
        else:
            version_text = await fetch(f"{url_base}/versions", client)
        version_table = parse_version_table(version_text)
        latest_version = version_table[0]

        if config_file:
            with open(config_file, "rb") as f:
                build_config_bytes = f.read()
        else:
            build_config_hash_str = str(latest_version.build_config)
            build_config_bytes = None
            last_error = None

            # Try each CDN definition
            for cdn_def in cdn_table:
                # Try each host in this CDN definition
                for host in cdn_def.hosts:
                    cdn_path = cdn_def.path
                    cdn_url = f"{host}/{cdn_path}"
                    config_url = f"https://{cdn_url}/config/{build_config_hash_str[:2]}/{build_config_hash_str[2:4]}/{build_config_hash_str}"
                    try:
                        build_config_bytes = await fetch(
                            config_url, client, is_text=False
                        )
                        selected_cdn_def = cdn_def
                        selected_cdn_url = cdn_url
                        break
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            last_error = e
                            continue
                        raise
                if build_config_bytes:
                    break

            # If hosts didn't work, try servers
            if not build_config_bytes:
                for cdn_def in cdn_table:
                    for server in cdn_def.servers:
                        # Extract host from server URL (remove http:// or https:// and query params)
                        server_url = server.split("?")[0]
                        if server_url.startswith("http://"):
                            host = server_url[7:]
                        elif server_url.startswith("https://"):
                            host = server_url[8:]
                        else:
                            host = server_url

                        cdn_path = cdn_def.path
                        cdn_url = f"{host}/{cdn_path}"
                        config_url = f"https://{cdn_url}/config/{build_config_hash_str[:2]}/{build_config_hash_str[2:4]}/{build_config_hash_str}"
                        try:
                            build_config_bytes = await fetch(
                                config_url, client, is_text=False
                            )
                            selected_cdn_def = cdn_def
                            selected_cdn_url = cdn_url
                            break
                        except httpx.HTTPStatusError as e:
                            if e.response.status_code == 404:
                                last_error = e
                                continue
                            raise
                    if build_config_bytes:
                        break

            if not build_config_bytes:
                if last_error:
                    raise last_error
                raise Exception(
                    "Could not fetch build config from any CDN host or server"
                )

        build_config = parse_build_config(build_config_bytes)

        install_hash = build_config.install[1]
        table_data = await download_by_ekey(selected_cdn_url, install_hash, client)

        install_manifest_data = InstallManifest.parse(table_data)

        if return_data:
            return install_manifest_data, latest_version.version_name, selected_cdn_url

        for entry in install_manifest_data.entries:
            if entry.name:
                console.print(f"Name: {entry.name}, CKey: {entry.hash}")


@main.command()
@click.argument("product", type=click.Choice([p.name for p in Product]))
@click.argument("content_key", type=str)
@click.option("--output", type=click.Path(), default=".")
@click.option("--version-file", type=click.Path(exists=True))
@click.option("--config-file", type=click.Path(exists=True))
@click.pass_context
def download(ctx, product, content_key, output, version_file, config_file):
    """Download a file by its content key."""
    asyncio.run(
        download_command(product, content_key, output, version_file, config_file)
    )


async def download_command(
    product_name,
    content_key_str,
    output_dir,
    version_file,
    config_file,
    version_name=None,
    selected_cdn_url=None,
    return_path=False,
):
    product = Product[product_name]
    content_key = Md5Hash(content_key_str)
    url_base = f"{BASE_URL}/{product.value}"

    async with httpx.AsyncClient(http2=True) as client:
        if selected_cdn_url is None:
            cdn_text = await fetch(f"{url_base}/cdns", client)
            cdn_table = parse_cdn_table(cdn_text)
            selected_cdn_def = cdn_table[0]
            selected_host = selected_cdn_def.hosts[0]
            cdn_path = selected_cdn_def.path
            selected_cdn_url = f"{selected_host}/{cdn_path}"

        if version_file:
            with open(version_file, "r") as f:
                version_text = f.read()
        else:
            version_text = await fetch(f"{url_base}/versions", client)
        version_table = parse_version_table(version_text)
        latest_version = version_table[0]

        if version_name is None:
            version_name = latest_version.version_name

        if config_file:
            with open(config_file, "rb") as f:
                build_config_bytes = f.read()
        else:
            build_config_hash_str = str(latest_version.build_config)
            config_url = f"https://{selected_cdn_url}/config/{build_config_hash_str[:2]}/{build_config_hash_str[2:4]}/{build_config_hash_str}"
            build_config_bytes = await fetch(config_url, client, is_text=False)
        build_config = parse_build_config(build_config_bytes)

        encoding_hash = build_config.encoding[1]
        encoding_data = await download_by_ekey(selected_cdn_url, encoding_hash, client)

        encoding_header, encoding_entries = parse_encoding_manifest(encoding_data)

        ekey = None
        for entry in encoding_entries:
            if entry.c_key == content_key:
                if entry.e_keys:
                    ekey = entry.e_keys[0]
                    break

        if not ekey:
            if not return_path:
                console.print(f"Could not find EKey for CKey {content_key}")
            return None

        if not return_path:
            console.print(f"Found EKey: {ekey}")
        file_data = await download_by_ekey(selected_cdn_url, ekey, client)

        # Create directory structure: $output_dir/$product/$version/
        output_path_obj = Path(output_dir).expanduser().resolve()
        if version_name:
            output_path_obj = output_path_obj / product_name / version_name
        output_path_obj.mkdir(parents=True, exist_ok=True)

        output_path = output_path_obj / content_key_str
        with open(output_path, "wb") as f:
            f.write(file_data)

        if not return_path:
            console.print(f"Successfully downloaded and wrote to {output_path}")

        return str(output_path)


@main.command()
@click.option(
    "-p",
    "--pattern",
    "patterns",
    multiple=True,
    help="Regex pattern to watch for (may be given multiple times). "
    r"Defaults to '\.pdb$' and '_loader\.dll$'.",
)
@click.option(
    "-d",
    "--dest",
    "dest_dir",
    default="./target",
    type=click.Path(),
    help="Download directory (default: ./target)",
)
@click.option(
    "-f",
    "--file",
    "product_file",
    type=click.Path(exists=True),
    help="Text file with one product name per line (overrides built-in list).",
)
@click.option(
    "--product",
    "single_product",
    help="Single product name to process (overrides built-in list and file).",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite existing files even if they have the same hash. "
    "By default, existing files are preserved.",
)
@click.pass_context
def grab(ctx, patterns, dest_dir, product_file, single_product, overwrite):
    """Grab PDBs / loader DLLs from Blizzard CDNs."""
    asyncio.run(
        grab_command(patterns, dest_dir, product_file, single_product, overwrite)
    )


async def grab_command(patterns, dest_dir, product_file, single_product, overwrite):
    """Grab files matching patterns from Blizzard CDNs."""
    # Compile patterns
    raw_patterns = list(patterns) if patterns else [r"\.pdb$", r"_loader\.dll$"]
    compiled_patterns = [re.compile(p, re.IGNORECASE) for p in raw_patterns]

    # Resolve download directory
    dest_path = Path(dest_dir).expanduser().resolve()
    dest_path.mkdir(parents=True, exist_ok=True)

    # Load CKey mapping to avoid re-downloading existing files
    ckey_map = load_ckey_map(dest_path)

    # Build product list
    if single_product:
        products = [single_product]
    elif product_file:
        with open(product_file, "r", encoding="utf-8") as fp:
            products = [ln.strip() for ln in fp if ln.strip()]
    else:
        products = DEFAULT_PRODUCTS

    # Iterate products
    for prod_name in products:
        console.print(f"▶  {prod_name}")
        product_enum = product_name_to_enum(prod_name)
        if not product_enum:
            console.print(f"[red]❌  Unknown product: {prod_name}[/red]")
            continue

        try:
            install_manifest, version_name, selected_cdn_url = (
                await install_manifest_command(
                    product_enum.name, None, None, return_data=True
                )
            )
        except Exception as e:
            console.print(
                f"[red]❌  Failed to get install manifest for {prod_name}: {e}[/red]"
            )
            continue

        for entry in install_manifest.entries:
            if not entry.name:
                continue

            if not should_download(entry.name, compiled_patterns):
                continue

            ckey_str = str(entry.hash)

            # Check if file is already downloaded (via CKey map)
            existing_file = is_file_already_downloaded(dest_path, ckey_str, ckey_map)
            if existing_file:
                if not overwrite:
                    console.print(
                        f"[cyan]⊘  Skipped {entry.name:<45} "
                        f"(CKey {ckey_str}) - already exists[/cyan]"
                    )
                    continue
                else:
                    # Overwrite flag is set, continue to download
                    console.print(
                        f"[yellow]⚠  Will overwrite {entry.name:<45} "
                        f"(CKey {ckey_str}) - file exists in map[/yellow]"
                    )

            # Check if file exists at expected path (even if map doesn't exist)
            existing_file = find_existing_file_by_path(
                dest_path, prod_name, version_name, entry.name
            )
            if existing_file:
                # Check if the existing file is already mapped to a different CKey
                existing_ckey = get_ckey_for_file_path(
                    dest_path, existing_file, ckey_map
                )

                if existing_ckey:
                    if existing_ckey == ckey_str:
                        # Same CKey, skip (shouldn't happen due to earlier check, but safe)
                        if not overwrite:
                            console.print(
                                f"[cyan]⊘  Skipped {entry.name:<45} "
                                f"(CKey {ckey_str}) - already exists[/cyan]"
                            )
                            continue
                    else:
                        # Different CKey - collision detected, proceed with download
                        # The download will handle renaming with CKey suffix
                        if not overwrite:
                            console.print(
                                f"[yellow]⚠  Collision detected: {entry.name} exists with different CKey. "
                                f"Will download as {entry.name} with CKey suffix[/yellow]"
                            )
                else:
                    # File exists but not in map - could be same or different
                    # Proceed with download, which will handle collision if needed
                    if not overwrite:
                        console.print(
                            f"[yellow]⚠  File {entry.name} exists but not in map. "
                            f"Will download (will rename if collision detected)[/yellow]"
                        )
                if overwrite:
                    # Overwrite flag is set, continue to download
                    console.print(
                        f"[yellow]⚠  Will overwrite {entry.name:<45} "
                        f"(CKey {ckey_str}) at {existing_file.relative_to(dest_path)}[/yellow]"
                    )

            try:
                downloaded_path = await download_command(
                    product_enum.name,
                    ckey_str,
                    str(dest_path),
                    None,
                    None,
                    version_name=version_name,
                    selected_cdn_url=selected_cdn_url,
                    return_path=True,
                )

                if downloaded_path:
                    # Move to proper location with correct filename
                    downloaded_path_obj = Path(downloaded_path)
                    if downloaded_path_obj.exists():
                        # Create organized structure: $dest/$product/$version/$filename
                        target_dir = dest_path / prod_name / version_name
                        target_dir.mkdir(parents=True, exist_ok=True)

                        # Normalize path separators (handle both \ and /)
                        # Convert backslashes to forward slashes, then split and join with Path
                        normalized_name = entry.name.replace("\\", "/")
                        # Build the path component by component to ensure proper directory structure
                        path_parts = normalized_name.split("/")

                        # If there are multiple parts, create directory structure
                        if len(path_parts) > 1:
                            # All parts except the last are directories
                            file_dir = target_dir
                            for part in path_parts[:-1]:
                                file_dir = file_dir / part
                            file_dir.mkdir(parents=True, exist_ok=True)
                            # Last part is the filename
                            base_filename = file_dir / path_parts[-1]
                        else:
                            # Single filename, no directory structure needed
                            base_filename = target_dir / path_parts[0]
                            base_filename.parent.mkdir(parents=True, exist_ok=True)

                        # Make filename unique if collision detected
                        proper_filename = make_unique_filename(base_filename, ckey_str)
                        is_collision = proper_filename != base_filename

                        downloaded_path_obj.rename(proper_filename)

                        # Update CKey mapping
                        update_ckey_map(
                            dest_path,
                            ckey_str,
                            proper_filename,
                            prod_name,
                            version_name,
                            ckey_map,
                        )

                        console.print(
                            f"[green]✔  Downloaded {entry.name:<45} "
                            f"(CKey {ckey_str}) for {prod_name}[/green]"
                        )
                        if is_collision:
                            rel_path = proper_filename.relative_to(dest_path)
                            console.print(
                                f"   → Renamed to {rel_path} (collision resolved with CKey)"
                            )
                        else:
                            rel_path = proper_filename.relative_to(dest_path)
                            console.print(f"   → Renamed to {rel_path}")
                    else:
                        console.print(
                            f"[red]   ⚠ Warning: Could not find downloaded file with CKey {ckey_str}[/red]"
                        )
                else:
                    console.print(
                        f"[red]   ⚠ Warning: Failed to download file {entry.name} (CKey {ckey_str})[/red]"
                    )
            except Exception as e:
                console.print(
                    f"[red]   ⚠ Warning: Error downloading {entry.name} (CKey {ckey_str}): {e}[/red]"
                )

        # Save CKey map after processing each product
        save_ckey_map(dest_path, ckey_map)


def calculate_file_md5(file_path: Path) -> str:
    """Calculate MD5 hash of a file."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def extract_product_version_from_path(
    base_dir: Path, file_path: Path
) -> Optional[Tuple[str, str]]:
    """
    Extract product and version from file path.
    Expected structure: $base_dir/$product/$version/...
    Returns (product, version) tuple or None if structure doesn't match.
    """
    try:
        rel_path = file_path.relative_to(base_dir)
        parts = rel_path.parts
        if len(parts) >= 2:
            return (parts[0], parts[1])
    except ValueError:
        # File is not under base_dir
        pass
    return None


@main.command()
@click.argument(
    "directory", type=click.Path(exists=True, file_okay=False, dir_okay=True)
)
@click.option(
    "--dest",
    "dest_dir",
    type=click.Path(file_okay=False, dir_okay=True),
    help="Directory where .ckey_map.json will be saved (should match grab --dest). "
    "Defaults to directory if not specified.",
)
@click.option(
    "--base-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Base directory for relative paths in map (defaults to dest-dir if not specified).",
)
@click.pass_context
def index(ctx, directory, dest_dir, base_dir):
    """Index files in a directory and create/update .ckey_map.json.

    The .ckey_map.json file will be saved to the --dest directory (or the directory
    being indexed if --dest is not specified). This should match the directory
    used with 'grab --dest' so that grab can find the map.
    """
    asyncio.run(index_command(directory, dest_dir, base_dir))


async def index_command(directory, dest_dir, base_dir):
    """Index files in directory and create CKey map."""
    dir_path = Path(directory).expanduser().resolve()
    # dest_path is where the map file will be saved (should match grab --dest)
    dest_path = Path(dest_dir).expanduser().resolve() if dest_dir else dir_path
    # base_path is used for extracting product/version and relative paths
    base_path = Path(base_dir).expanduser().resolve() if base_dir else dest_path

    if not dir_path.exists() or not dir_path.is_dir():
        console.print(f"[red]❌  Directory does not exist: {directory}[/red]")
        return

    dest_path.mkdir(parents=True, exist_ok=True)

    # Load existing map if it exists (from dest_path where grab would look for it)
    ckey_map = load_ckey_map(dest_path)

    console.print(f"[cyan]📁  Indexing directory: {dir_path}[/cyan]")
    console.print(f"[cyan]📁  Map will be saved to: {dest_path}[/cyan]")
    console.print(f"[cyan]📁  Base directory for paths: {base_path}[/cyan]")

    indexed_count = 0
    skipped_count = 0

    # Traverse directory recursively
    for file_path in dir_path.rglob("*"):
        if not file_path.is_file():
            continue

        # Extract product and version from path
        # First try base_path, then dir_path
        product_version = extract_product_version_from_path(base_path, file_path)
        map_base = base_path

        if not product_version:
            # Try extracting from dir_path instead if base_path doesn't match
            product_version = extract_product_version_from_path(dir_path, file_path)
            if product_version:
                map_base = dir_path
            else:
                console.print(
                    f"[yellow]⚠  Skipping {file_path.relative_to(dir_path)} - "
                    f"path structure doesn't match $base/$product/$version/...[/yellow]"
                )
                skipped_count += 1
                continue

        product, version = product_version
        rel_path = file_path.relative_to(map_base)

        # Calculate MD5 hash
        try:
            md5_hash = calculate_file_md5(file_path)
        except Exception as e:
            console.print(f"[red]⚠  Error calculating hash for {rel_path}: {e}[/red]")
            skipped_count += 1
            continue

        # Update CKey map (use map_base for relative paths)
        update_ckey_map(map_base, md5_hash, file_path, product, version, ckey_map)
        indexed_count += 1

        if indexed_count % 100 == 0:
            console.print(f"[cyan]   Indexed {indexed_count} files...[/cyan]")

    # Save the map to dest_path (where grab will look for it)
    save_ckey_map(dest_path, ckey_map)

    console.print(
        f"[green]✔  Indexed {indexed_count} files, skipped {skipped_count} files[/green]"
    )
    console.print(
        f"[green]✔  CKey map saved to {dest_path / CKEY_MAP_FILENAME}[/green]"
    )
    console.print(
        f"[yellow]💡  Note: Use the same directory with 'grab --dest {dest_path}' "
        f"so grab can find this map[/yellow]"
    )


if __name__ == "__main__":
    main()
